import os
import threading
import time
import random

import qdx
import fskmodem
import ale


#TODO
# - how to recognize an ongoing call?
# - check fskmodem delay on transmit buffer, ensure no timing issues (ex. send call ack in 2 sec window)
# - add confidence (float) to rx callback in fskmodem
# - add fskmodem.receiving(), return bool (based on carrier sense)
# - add fskmodem.transmitting(), return bool
# - Reticulum ALE interface
# - selective calling via Reticulum destination
# - transmit timing may not be accurate due to fskmodem carrier sense



class ALE:

    # default channels
    # Note: these are non-standard ALE channels
    #TODO should we use HF packet freqs (and no modem sync byte) to sense normal traffic?
    DEFAULT_CHANNELS = {
        '80A'   : {'freq': 3555000, 'mode': 'USB'},
        '80B'   : {'freq': 3655000, 'mode': 'USB'},
        '40A'   : {'freq': 7055000, 'mode': 'USB'},
        '40B'   : {'freq': 7105000, 'mode': 'USB'},
        '20A'   : {'freq': 14055000, 'mode': 'USB'},
        '20B'   : {'freq': 14155000, 'mode': 'USB'}
    }
    NUM_CHANNELS = len(DEFAULT_CHANNELS.keys())

    # operating states
    STATE_SCANNING      = 0xA # scanning channels
    STATE_CALLING       = 0xB # calling out
    STATE_CONNECTING    = 0xC # responding to call
    STATE_CONNECTED     = 0xD # active call
    STATE_SOUNDING      = 0xE # sounding or listening for acks
    STATES = [STATE_SCANNING, STATE_CALLING, STATE_CONNECTING, STATE_CONNECTED, STATE_SOUNDING]
    #TODO ensure every state is handled, and every state can return to scanning

    # special addresses
    ADDRESS_ANY = b'ANY'
    ADDRESS_ALL = b'ALL'
    SPECIAL_ADDRESSES = [ADDRESS_ANY, ADDRESS_ALL]

    # commands
    CMD_SOUND   = b'CS'
    CMD_ACK     = b'CA'
    CMD_CALL    = b'CC'
    CMD_END     = b'CE'
    COMMANDS = [CMD_SOUND, CMD_ACK, CMD_CALL, CMD_END]

    # timing variables
    SCAN_WINDOW     = 3 # seconds
    SOUND_TIMEOUT   = SCAN_WINDOW * NUM_CHANNELS # seconds
    CALL_TIMEOUT    = SCAN_WINDOW * NUM_CHANNELS * 3 # seconds


    def __init__(self, address, radio_serial_port=None, alsa_device_string='QDX')

        ale_dir_path = os.abspath(os.join(os.expanduser('~'), '/.ale'))
        channel_file_path = os.join(ale_dir_path, '/channels')

        if not os.path.exists(channel_file_path):
            if not os.path.exists(ale_dir_path):
                os.mkdir(ale_dir_path)

            self.channels = ALE.DEFAULT_CHANNELS

            with open(channel_file_path, 'w'):
                #TODO write default channels to file
                pass
        else:
            #TODO read channels from file
            # self.channels = ...
            self.channels = ALE.DEFAULT_CHANNELS

        if address not type(address, bytes):
            address = address.encode('utf-8')

        self.address = address
        self.addresses = [self.address]
        self.call_address = b''
        self.callback = {
            'rx' : None,
            'call' : None,
            'connected' : None,
            'disconnected' : None
        }

        self.enable_whitelist = False
        self.whitelist_addresses = []
        self.enable_blacklist = False
        self.blacklist_addresses = []

        self.lqa = ale.LQA(self)

        self.last_carrier_sense_timestamp = 0 # any activity
        self.last_activity_timestamp = 0 # activity addressed to us
        self.call_started_timestamp = 0
        self.call_timeout_timestamp = 0
        self.sound_started_timestamp = 0
        self.sound_timeout_timestamp = 0
        self.last_sound_packet_timestamp = 0
        self.last_call_packet_timestamp = 0
        self.last_ack_packet_timestamp = 0

        self.radio = qdx.QDX(port=radio_serial_port)
        self.set_channel(self.channels.keys()[0])
        self.last_channel_change_timestamp = time.time()

        #TODO allow baudrate and other setting changes
        alsa_device = fskmodem.get_alsa_device(alsa_device_string)
        self.modem = fskmodem(alsa_dev_in=alsa_device, start=False)
        self.modem.set_rx_callback(self._receive)

        self.state = ALE.STATE_SCANNING

    def start(self):
        self.modem.start()
        self.online = True

        thread = threading.Thread(target=self._jobs)
        thread.setDaemon = True
        thread.start()

    def stop(self):
        self.online = False
        self.modem.stop()

    def add_address(self, address):
        if address not in self.addresses:
            self.addresses.append(address)

    def remove_address(self, address):
        if address in self.addresses:
            self.addresses.remove(address)

    def enable_whitelist(self):
        self.enable_whitelist = True

    def disable_whitelist(self):
        self.enable_whitelist = False

    def add_whitelist(self, address):
        if address not in self.whitelist:
            self.whitelist_addresses.append(address)

    def remove_whitelist(self, address):
        if address in self.whitelist:
            self.whitelist_addresses.remove(address)

    def enable_blacklist(self):
        self.enable_blacklist = True

    def disable_whitelist(self):
        self.enable_blacklist = False

    def add_blacklist(self, address):
        if address not in self.blacklist:
            self.blacklist_addresses.append(address)

    def remove_blacklist(self, address):
        if address in self.blacklist:
            self.blacklist_addresses.remove(address)

    def set_rx_callback(self, func):
        self.callback['receive'] = func
        #self.modem.set_rx_callback(func)

    def set_incoming_call_callback(self, func):
        self.callback['call'] = func

    def set_connected_callback(self, func):
        self.callback['connected'] = func

    def set_channel(self, channel):
        if channel not in self.channels.keys():
            return None

        # if error with radio, go offline
        try:
            self.radio.set_vfo_a(self.channels[self.channel]['freq'])

            self.last_channel = self.channel
            self.channel = channel
            self.last_channel_change_timestamp = time.time()
            self.last_carrier_sense_timestamp = 0
    
            #TODO change qdx to accept 'USB' and 'LSB' as sideband settings 
            if self.channels[self.channel]['mode'] == 'USB':
                self.radio.set_sideband(0)
            if self.channels[self.channel]['mode'] == 'LSB':
                self.radio.set_sideband(1)
        except:
            #TODO handle intelligently
            self.online == False

    def next_channel(self):
        channels = self.channels.keys()
        channel_index = channels.index(self.channel)

        if channel_index < (len(channels) - 1):
            next_channel = channels[channel_index + 1]
        else:
            next_channel = channels[0]

        self.set_channel(next_channel)

    #TODO if call fails try calling on other channels
    def call(self, address):
        if self.state != ALE.STATE_CALLING:
            self.state = ALE.STATE_CALLING
            self.call_address = address
            best_channel = self.lqa.best_channel(address)
            self.set_channel(best_channel)
            self.call_timeout_timestamp = time.time()
            self.call_started_timestamp = time.time()

        self.last_call_packet_timestamp = time.time()
        self._send_ale(ALE.CMD_CALL, address)

    def send(self, data):
        self.modem.send(data)
        
    # make call and sound packets have a 1 second transmit time based on baudrate
    def _send_ale(self, command, address=b'', data=b''):
        if command not in ALE.COMMANDS:
            return None

        packet = ale.Packet(self.address, address, command, data)

        if command == ALE.CMD_CALL or command == ALE.CMD_SOUND:
            len_packet = len(packet.pack())
            # baud rate (bits per second) / 8 bits per character
            len_one_second_tx = int(self.modem.baudrate / 8)
            if len_packet < len_one_second_tx:
                # pad packet data to create 1 second transmission
                packet.data = b'#' * (len_one_second - len_packet)

        self.modem.send(packet.pack())

    def _receive(self, raw, confidence):
        preamble = packet[:len(ale.Packet.PREAMBLE)]
        # handle non-ale packets
        if preamble != ale.Packet.PREAMBLE:
            if self.state == ALE.CONNECTED:
                self.call_timeout_timestamp = time.time() + ALE.CALL_TIMEOUT
                
                # pass data to data handling application when connected
                if self.callback['receive'] != None:
                    self.callback['receive'](raw)

            return None

        packet = ale.Packet()
        # discard packet if it fails to unpack, which likely means it is corrupted
        try:
            packet.unpack(raw)
        except:
            return None

        packet.timestamp = time.time()
        packet.channel = self.channel
        packet.confidence = confidence
        self.lqa.store(packet)

        if self.enable_whitelist and packet.origin not in self.whitelist_addresses:
            return None

        if self.enable_blacklist and packet.origin in self.blacklist_addresses:
            return None

        if packet.command in ALE.COMMANDS:
            if packet.destination in self.addresses:
                self.last_activity_timestamp = time.time()

            if self.state == ALE.STATE_SCANNING:
                if packet.command == ALE.CMD_SOUND:
                    #TODO add random delay avoid all stations ack-ing at the same time
                    if self.lqa.should_ack_sound(self.channel, packet.origin):
                        # send ack
                        self._send_ale(ALE.CMD_ACK, packet.origin)

                elif packet.command == ALE.CMD_ACK:
                    pass

                elif packet.command == ALE.CMD_CALL:
                    if packet.destination in self.addresses or packet.destination == ALE.ADDRESS_ANY:
                        if self.callback['call'] != None:
                            self.callback['call'](packet.origin)
                        
                        self.call_address = packet.origin
                        self.state = ALE.STATE_CONNECTING

                        # send ack
                        self.last_ack_packet_timestamp = time.time()
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                elif packet.command == ALE.CMD_END:
                    pass
                
            if self.state == ALE.STATE_CALLING:
                if packet.command == ALE.CMD_SOUND:
                    pass

                # call handshake step 2
                elif packet.command == ALE.CMD_ACK:
                    if packet.destination in self.addresses and packet.origin == self.calling_address:
                        self.state = ALE.STATE_CONNECTED

                        # send ack
                        self.last_ack_packet_timestamp = time.time()
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                        self.call_timeout_timestamp = time.time() + ALE.CALL_TIMEOUT
                        self.call_started_timestamp = time.time()
                        
                        if self.callback['connected'] != None:
                            self.callback['connected'](packet.origin)

                # call handshake step 1
                elif packet.command == ALE.CMD_CALL:
                    if packet.destination in self.addresses and packet.origin == self.calling_address:
                        self.state = ALE.STATE_CONNECTING

                        # send ack
                        self.last_ack_packet_timestamp = time.time()
                        self._send_ale(ALE.CMD_ACK, self.call_address)
                        
                        self.call_timeout_timestamp = time.time() + ALE.CALL_TIMEOUT

                elif packet.command == ALE.CMD_END:
                    call_duration = time.time() - self.call_started_timestamp
    
                    if self.callback['disconnected'] != None:
                        self.callback['disconnected'](self.call_address, call_duration)

                    self.call_address = None
                    self.call_started_timestamp = 0
                    self.call_timeout_timestamp = 0
                    self.state = ALE.STATE_SCANNING

            # only the station receiving the call is in a connecting state, since the station making
            # the call goes from calling state to connected state after ack
            elif self.state == ALE.STATE_CONNECTING:
                if packet.command == ALE.CMD_SOUND:
                    pass

                # call handshake step 3
                if packet.command == ALE.CMD_ACK:
                    if packet.destination in self.addresses and packet.origin == self.calling_address:
                        self.state = ALE.STATE_CONNECTED

                        # send ack
                        self.last_ack_packet_timestamp = time.time()
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                        self.call_timeout_timestamp = time.time() + ALE.CALL_TIMEOUT
                        self.call_started_timestamp = time.time()
                        
                        if self.callback['connected'] != None:
                            self.callback['connected'](packet.origin)

                if packet.command == ALE.CMD_CALL:
                    if packet.destination in self.addresses and packet.origin == self.calling_address:
                        self.state = ALE.STATE_CONNECTING

                        # send ack
                        self.last_ack_packet_timestamp = time.time()
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                        self.call_timeout_timestamp = time.time() + ALE.CALL_TIMEOUT

                if packet.command == ALE.CMD_END:
                    call_duration = time.time() - self.call_started_timestamp
    
                    if self.callback['disconnected'] != None:
                        self.callback['disconnected'](self.call_address, call_duration)

                    self.call_address = None
                    self.call_started_timestamp = 0
                    self.call_timeout_timestamp = 0
                    self.state = ALE.STATE_SCANNING


            elif self.state == ALE.STATE_CONNECTED:
                if packet.command == ALE.CMD_SOUND:
                    pass

                if packet.command == ALE.CMD_ACK:
                    pass

                if packet.command == ALE.CMD_CALL:
                    pass

                if packet.command == ALE.CMD_END:
                    call_duration = time.time() - self.call_started_timestamp
    
                    if self.callback['disconnected'] != None:
                        self.callback['disconnected'](self.call_address, call_duration)

                    self.call_address = None
                    self.call_started_timestamp = 0
                    self.call_timeout_timestamp = 0
                    self.state = ALE.STATE_SCANNING


            elif self.state == ALE.STATE_SOUNDING:
                if packet.command == ALE.CMD_SOUND:
                    pass

                if packet.command == ALE.CMD_ACK:
                    # LQA logged above
                    pass

                if packet.command == ALE.CMD_CALL:
                    if packet.destination in self.addresses or packet.destination == ALE.ADDRESS_ANY:
                        if self.callback['call'] != None:
                            self.callback['call'](from_address)
                        
                        self.call_address = packet.origin
                        self.state = ALE.STATE_CONNECTING

                        # send ack
                        self.last_ack_packet_timestamp = time.time()
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                if packet.command == ALE.CMD_END:
                    pass


    def _jobs(self):
        while self.online:
            current_time = time.time()

            if self.modem.carrier_sense:
                self.last_carrier_sense_timestamp = current_time

            if self.state == ALE.STATE_SCANNING:
                change_channel = current_time > (self.last_channel_change_timestamp + ALE.SCAN_WINDOW)
                channel_active = current_time > (self.last_activity_timestamp + ALE.SCAN_WINDOW)

                if change_channel and not channel_active:
                    if self.lqa.channel_stale(self.channel):
                        self.state = ALE.STATE_SOUNDING
                        self.sound_timeout_timestamp = time.time() + ALE.SOUND_TIMEOUT
                        self.last_sound_packet_timestamp = time.time()
                        self._send_ale(ALE.CMD_SOUND, ALE.ADDRESS_ALL)
                    else:
                        self.next_channel()
                        self.last_channel_change_timestamp = current_time

            if self.state in [ALE.STATE_CALLING, ALE.STATE_CONNECTING, ALE.STATE_CONNECTED]:
                if current_time > self.call_timeout_timestamp:
                    call_duration = time.time() - self.call_started_timestamp
    
                    if self.callback['disconnected'] != None:
                        self.callback['disconnected'](self.call_address, call_duration)

                    self.call_address = None
                    self.call_started_timestamp = 0
                    self.call_timeout_timestamp = 0
                    self.state = ALE.STATE_SCANNING

            if self.state == ALE.STATE_CALLING:
                if current_time > (self.last_call_packet_timestamp + ALE.SCAN_WINDOW):
                    self.last_call_packet_timestamp = time.time()
                    self._send_ale(ALE.CMD_CALL, self.call_address)

            if self.state == ALE.STATE_CONNECTING:
                if current_time > (self.last_ack_packet_timestamp + ALE.SCAN_WINDOW):
                    self.last_ack_packet_timestamp = time.time()
                    self._send_ale(ALE.CMD_ACK, self.call_address)

            if self.state == ALE.STATE_SOUNDING:
                if current_time > self.sound_timeout_timestamp:
                    self.state = ALE.STATE_SCANNING

                elif current_time > (self.last_sound_packet_timestamp + ALE.SCAN_WINDOW):
                    self.last_sound_packet_timestamp = time.time()
                    self._send_ale(ALE.CMD_SOUND, ALE.ADDRESS_ALL)


            time.sleep(0.1)

            



            
    










