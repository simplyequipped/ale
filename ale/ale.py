import os
import threading
import time
import random
import json
import atexit

#import qdx
import fskmodem
import ale


#TODO
# - recognize activity on channel (lqa?), look for next best channel to place call
# - add fskmodem.receiving(), return bool (based on carrier sense)
# - add fskmodem.transmitting(), return bool
# - Reticulum ALE interface
# - selective calling via Reticulum destination
# - transmit timing may not be accurate due to fskmodem carrier sense
# - support other tranceivers via hamlib or flrig?
# - support other modems via fldigi?


class ALE:

    # default scanlists and channels
    #TODO channel data could include more extensive modem and radio config
    DEFAULT_SCANLIST = {
        'General' : {
            '40A'   : {'freq': 7057000, 'mode': 'USB'},
            '40B'   : {'freq': 7157000, 'mode': 'USB'},
            '20A'   : {'freq': 14057000, 'mode': 'USB'},
            '20B'   : {'freq': 14157000, 'mode': 'USB'},
            '10A'   : {'freq': 28557000, 'mode': 'USB'},
            '10B'   : {'freq': 29257000, 'mode': 'USB'}
        },
        'NVIS' : {
            '80A'   : {'freq': 3557000, 'mode': 'USB'},
            '80B'   : {'freq': 3657000, 'mode': 'USB'},
            '40A'   : {'freq': 7057000, 'mode': 'USB'},
            '40B'   : {'freq': 7157000, 'mode': 'USB'}
        },
        'HF Packet' : {
            '80PKT'   : {'freq': 3598000, 'mode': 'LSB'},
            '40PKT'   : {'freq': 7086500, 'mode': 'USB'},
            '20PKT'   : {'freq': 14105000, 'mode': 'LSB'}
        }
    }

    # operating states
    STATE_SCANNING      = 0xA # scanning channels
    STATE_CALLING       = 0xB # calling out
    STATE_CONNECTING    = 0xC # responding to call
    STATE_CONNECTED     = 0xD # active call
    STATE_SOUNDING      = 0xE # sounding or listening for acks
    STATES = [STATE_SCANNING, STATE_CALLING, STATE_CONNECTING, STATE_CONNECTED, STATE_SOUNDING]

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

    SCAN_WINDOW     = 3 # seconds

    def __init__(self, address, radio_serial_port=None, alsa_device_string='QDX', baudrate=300, sync_byte='0x23', confidence=1.5, text_mode=False):
        self._text_mode = text_mode
        self.baudrate = baudrate

        self.loaded_scanlists = ALE.DEFAULT_SCANLIST

        self.config_dir = os.path.expanduser('~/.ale')
        self.scanlist_path = os.path.join(self.config_dir, 'scanlists')
        self.log_path = os.path.join(self.config_dir, 'log.txt')

        self.log_queue = []
        self.last_log_timestamp = 0

        #TODO cull log instead?
        # clear log when starting
        if os.path.exists(self.log_path):
            os.remove(self.log_path)

        # if scanlist file doesn't exist, create it using the default scanlist
        # if the scanlist file exists, load it
        if not os.path.exists(self.scanlist_path):
            if not os.path.exists(self.config_dir):
                os.mkdir(self.config_dir)

            try:
                with open(self.scanlist_path, 'w') as fd:
                    json.dump(self.loaded_scanlists, fd, indent='\t')
                self.log('Saved scanlists to ' + self.scanlist_path)
            except:
                #TODO handle
                pass

        else:
            try:
                with open(self.scanlist_path, 'r') as fd:
                    self.loaded_scanlists = json.load(fd)
                self.log('Loaded scanlists from ' + self.scanlist_path)
            except:
                #TODO handle
                pass
        
        if not isinstance(address, bytes):
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

        self.last_channel_change_timestamp = 0
        self.last_carrier_sense_timestamp = 0 # any activity
        self.last_activity_timestamp = 0 # activity addressed to us

        self.call_started_timestamp = 0
        self.call_timeout_timestamp = 0
        self.last_call_packet_timestamp = 0
        # only used for call ack packets, not all ack packets
        self.last_ack_packet_timestamp = 0
        self.call_channel_attempts = []

        self.sound_started_timestamp = 0
        self.sound_timeout_timestamp = 0
        self.last_sound_packet_timestamp = 0
        self.sound_rx_ack_count = 0
        self.sound_packet = None
        self.sound_ack_delay = 0

        self.call_timeout = 0
        self.sound_timeout = 0
        self.connected_timeout = 5 * 60 # seconds

        if not self._text_mode:
            self.radio = qdx.QDX(port=radio_serial_port)

            alsa_device = fskmodem.get_alsa_device(alsa_device_string)
            self.modem = fskmodem(
                alsa_dev_in = alsa_device, 
                baudrate = baudrate,
                sync_byte = sync_byte,
                confidence = confidence
            )
            self.log('Modem started')
            self.modem.set_rx_callback(self._receive)
        else:
            self.modem = None
            self.radio = None
            self.log('Text-only mode')

        # configure exit handler
        atexit.register(self.stop)

        self.online = True
        self.state = ALE.STATE_SCANNING
        self.log(str(self) + ' online')
        self.set_scanlist(self.get_scanlists()[0])
        self.set_channel(list(self.channels.keys())[0])
        self.lqa = ale.LQA(self)

        thread = threading.Thread(target=self._jobs)
        thread.setDaemon(True)
        thread.start()

    def __repr__(self):
        return 'ALE[' + self.address.decode('utf-8') + ']'

    def stop(self):
        if not self._text_mode:
            self.modem.stop()
            self.log('Modem stopped')

        if self.online:
            self.online = False
            self.log(str(self) + ' offline')
        
        self.lqa.save_history()
        self._manage_log()

    def log(self, message):
        log_message = time.strftime('%x %X') + '  ' + message + '\n'
        self.log_queue.append(log_message)

    def _manage_log(self):
        if len(self.log_queue) == 0:
            return None

        log_messages = ''

        with open(self.log_path, 'a') as fd:
            for message in self.log_queue:
                fd.write(message)

        self.log_queue.clear()
        self.last_log_timestamp = time.time()

    def get_state(self):
        if self.state == ALE.STATE_SCANNING:
            return 'scanning'
        elif self.state == ALE.STATE_CALLING:
            return 'calling'
        elif self.state == ALE.STATE_CONNECTING:
            return 'connecting'
        elif self.state == ALE.STATE_CONNECTED:
            return 'connected'
        elif self.state == ALE.STATE_SOUNDING:
            return 'sounding'

    # useful for displaying antenna requirements
    def get_channel_freq_list(self):
        freqs = []

        for channel in self.channels:
            freqs.append(channel['freq'])

        return freqs.sort()

    def set_scanlist(self, scanlist):
        if scanlist not in self.loaded_scanlists.keys():
            return None

        self.scanlist = scanlist
        self.channels = self.loaded_scanlists[self.scanlist]
        self.call_timeout = ALE.SCAN_WINDOW * len(self.channels.keys()) * 2 # seconds
        self.sound_timeout = ALE.SCAN_WINDOW * len(self.channels.keys()) # seconds
        self.max_call_channel_attempts = min(3, len(self.channels.keys()))
        num_channels = len(self.channels.keys())

        self.log('Scanlist set to ' + self.scanlist + ' (' + str(num_channels) + ' channels, ' + str(num_channels * ALE.SCAN_WINDOW) + ' seconds total scan time)')

    def get_scanlists(self):
        return list(self.loaded_scanlists.keys())

    def add_address(self, address):
        if address not in self.addresses:
            self.addresses.append(address)
            self.log('Added self address ' + address.decode('utf-8'))

    def remove_address(self, address):
        if address in self.addresses:
            self.addresses.remove(address)
            self.log('Removed self address ' + address.decode('utf-8'))

    def enable_whitelist(self):
        self.enable_whitelist = True
        self.log('Whitelist enabled')

    def disable_whitelist(self):
        self.enable_whitelist = False
        self.log('Whitelist disabled')

    def add_whitelist(self, address):
        if address not in self.whitelist:
            self.whitelist_addresses.append(address)
            self.log('Added whitelist address ' + address.decode('utf-8'))

    def remove_whitelist(self, address):
        if address in self.whitelist:
            self.whitelist_addresses.remove(address)
            self.log('Removed whitelist address ' + address.decode('utf-8'))

    def enable_blacklist(self):
        self.enable_blacklist = True
        self.log('Blacklist enabled')

    def disable_whitelist(self):
        self.enable_blacklist = False
        self.log('Blacklist disabled')

    def add_blacklist(self, address):
        if address not in self.blacklist:
            self.blacklist_addresses.append(address)
            self.log('Added blacklist address ' + address.decode('utf-8'))

    def remove_blacklist(self, address):
        if address in self.blacklist:
            self.blacklist_addresses.remove(address)
            self.log('Removed blacklist address ' + address.decode('utf-8'))

    def set_rx_callback(self, func):
        self.callback['receive'] = func

    def set_incoming_call_callback(self, func):
        self.callback['call'] = func

    def set_connected_callback(self, func):
        self.callback['connected'] = func

    def set_channel(self, channel):
        if channel not in self.channels.keys():
            return None

        if not self._text_mode:
            try:
                self.radio.set_vfo_a(self.channels[self.channel]['freq'])
    
                #TODO change qdx to accept 'USB' and 'LSB' as sideband settings 
                if self.channels[self.channel]['mode'] == 'USB':
                    self.radio.set_sideband(0)
                if self.channels[self.channel]['mode'] == 'LSB':
                    self.radio.set_sideband(1)
            except:
                #TODO handle intelligently
                # if error communicating with radio, go offline
                self.online == False
                self.log('Going offline, failed to communicate with radio')

        if self.online:
            self.channel = channel
            self.last_channel_change_timestamp = time.time()
            self.last_carrier_sense_timestamp = 0
            #self.log('Channel set to ' + channel) 

    def next_channel(self):
        channels = list(self.channels.keys())
        channel_index = channels.index(self.channel)

        if channel_index < (len(channels) - 1):
            next_channel = channels[channel_index + 1]
        else:
            next_channel = channels[0]

        self.set_channel(next_channel)

    #TODO if call fails try calling on other channels
    def call(self, address, next_channel=False):
        if not isinstance(address, bytes):
            address = address.encode('utf-8')

        if self.state != ALE.STATE_CALLING or next_channel:
            self.state = ALE.STATE_CALLING
            self.call_address = address

            if next_channel:
                best_channel = self.lqa.best_channel(address, exclude = self.call_channel_attempts)
            else:
                best_channel = self.lqa.best_channel(address)
                self.call_started_timestamp = time.time()
                
            self.call_channel_attempts.append(best_channel)
            self.set_channel(best_channel)
            self.call_timeout_timestamp = time.time() + self.call_timeout

        self.log('Calling ' + self.call_address.decode('utf-8') + ' on channel ' + best_channel)
        self.last_call_packet_timestamp = time.time()
        self._send_ale(ALE.CMD_CALL, address)

    def send(self, data):
        if self._text_mode:
            print(data)
        else:
            self.modem.send(data)
        
    def _send_ale(self, command, address=b'', data=b''):
        if command not in ALE.COMMANDS:
            return None

        packet = ale.Packet(self.address, address, command, data)

        # pad call and sound packets to have a minimum transmit time of 1/3 of the scan window
        # example:  scan window: 3 seconds
        #           baudrate:    300 bps
        #           min length:  (300 / 8) * (3 * 1/3)  ~= 37 characters for 1 second tx
        if command == ALE.CMD_CALL or command == ALE.CMD_SOUND:
            # length of packet, including modem packet delimiters (6 characters)
            len_packet = len(packet.pack()) + 6
            # (baudrate (bps) / 8 bits per character) * (scan window / 3)
            len_min_tx = int( (self.baudrate / 8) * (ALE.SCAN_WINDOW / 3) )
            if len_packet < len_min_tx:
                # pad packet data to equal minimum transmit time
                packet.data = b'#' * (len_min_tx - len_packet)

        self.send(packet.pack())

    def _receive(self, raw, confidence):
        current_time = time.time()

        preamble = raw[:len(ale.Packet.PREAMBLE)]
        # handle non-ale packets
        if preamble != ale.Packet.PREAMBLE:
            if self.state == ALE.CONNECTED:
                self.call_timeout_timestamp = current_time + self.connected_timeout
                
                # pass packets to data handling application when connected
                if self.callback['receive'] != None:
                    self.callback['receive'](raw)

            return None

        packet = ale.Packet()
        # discard packet if it fails to unpack, which likely means it is corrupted
        try:
            packet.unpack(raw)
        except:
            return None

        packet.timestamp = current_time
        packet.channel = self.channel
        packet.confidence = confidence
        self.lqa.store(packet)

        if self.enable_whitelist and packet.origin not in self.whitelist_addresses:
            return None

        if self.enable_blacklist and packet.origin in self.blacklist_addresses:
            return None

        if packet.command in ALE.COMMANDS:
            if packet.destination in self.addresses:
                self.last_activity_timestamp = current_time

            if self.state == ALE.STATE_SCANNING:
                if packet.command == ALE.CMD_SOUND:
                    self.sound_packet = packet
                    # random delay to avoid multiple stations ack-ing a sounding at the same time
                    self.sound_ack_delay = random.uniform(0.25, 1)

                # if command == ack, do nothing

                elif packet.command == ALE.CMD_CALL:
                    if packet.destination in self.addresses or packet.destination == ALE.ADDRESS_ANY:
                        self.call_address = packet.origin
                        self.call_timeout_timestamp = current_time + self.call_timeout
                        self.state = ALE.STATE_CONNECTING
                        self.log('Incoming call from address ' + packet.origin.decode('utf-8'))

                        if self.callback['call'] != None:
                            self.callback['call'](packet.origin)
                        
                        # send ack
                        self.last_ack_packet_timestamp = current_time
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                # if command == end, do nothing
                
            elif self.state == ALE.STATE_CALLING:
                # if command == sound, do nothing

                # call handshake step 2
                if packet.command == ALE.CMD_ACK:
                    if packet.destination in self.addresses and packet.origin == self.call_address:
                        self.call_timeout_timestamp = current_time + self.connected_timeout
                        self.call_started_timestamp = current_time 
                        self.state = ALE.STATE_CONNECTED
                        self.log('Connected to address ' + self.call_address.decode('utf-8'))

                        if self.callback['connected'] != None:
                            self.callback['connected'](self.call_address)

                        # send ack
                        self.last_ack_packet_timestamp = current_time
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                # call handshake step 1
                elif packet.command == ALE.CMD_CALL:
                    if packet.destination in self.addresses and packet.origin == self.call_address:
                        self.call_timeout_timestamp = current_time + self.call_timeout
                        self.state = ALE.STATE_CONNECTING
                        self.log('Connecting to address ' + self.call_address.decode('utf-8'))

                        # send ack
                        self.last_ack_packet_timestamp = current_time
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                elif packet.command == ALE.CMD_END:
                    call_duration = current_time - self.call_started_timestamp
                    self.log('Disconnected from address ' + self.call_address.decode('utf-8') + ', call duration: ' + str(int(call_duration)) + ' seconds')
    
                    if self.callback['disconnected'] != None:
                        self.callback['disconnected'](self.call_address, call_duration)

                    self.call_address = None
                    self.call_started_timestamp = 0
                    self.call_timeout_timestamp = 0
                    self.state = ALE.STATE_SCANNING

            # only the called station can be in a connecting state, since the calling station goes from
            # the calling state directly to to the connected state after ack
            elif self.state == ALE.STATE_CONNECTING:
                # if command == sound, do nothing

                # call handshake step 3
                if packet.command == ALE.CMD_ACK:
                    if packet.destination in self.addresses and packet.origin == self.call_address:
                        self.call_timeout_timestamp = current_time + self.connected_timeout
                        self.call_started_timestamp = current_time
                        self.state = ALE.STATE_CONNECTED
                        self.log('Connected to address ' + self.call_address.decode('utf-8'))

                        # send ack
                        self.last_ack_packet_timestamp = current_time
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                        if self.callback['connected'] != None:
                            self.callback['connected'](packet.origin)

                elif packet.command == ALE.CMD_CALL:
                    if packet.destination in self.addresses and packet.origin == self.call_address:
                        self.call_timeout_timestamp = current_time + self.call_timeout
                        self.state = ALE.STATE_CONNECTING
                        self.log('Connecting to address ' + self.call_address.decode('utf-8'))

                        # send ack
                        self.last_ack_packet_timestamp = current_time
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                elif packet.command == ALE.CMD_END:
                    call_duration = current_time - self.call_started_timestamp
                    self.log('Disconnected from address ' + self.call_address.decode('utf-8') + ', call duration: ' + str(int(call_duration)) + ' seconds')
    
                    if self.callback['disconnected'] != None:
                        self.callback['disconnected'](self.call_address, call_duration)

                    self.call_address = None
                    self.call_started_timestamp = 0
                    self.call_timeout_timestamp = 0
                    self.state = ALE.STATE_SCANNING


            elif self.state == ALE.STATE_CONNECTED:
                # if command == sound, do nothing

                # if command == ack, do nothing

                # if command == call, do nothing

                if packet.command == ALE.CMD_END:
                    call_duration = current_time - self.call_started_timestamp
                    self.log('Disconnected from address ' + self.call_address.decode('utf-8') + ', call duration: ' + str(int(call_duration)) + ' seconds')
    
                    if self.callback['disconnected'] != None:
                        self.callback['disconnected'](self.call_address, call_duration)

                    self.call_address = None
                    self.call_started_timestamp = 0
                    self.call_timeout_timestamp = 0
                    self.state = ALE.STATE_SCANNING

            elif self.state == ALE.STATE_SOUNDING:
                # if command == sound, do nothing

                if packet.command == ALE.CMD_ACK:
                    # LQA logged above
                    self.sound_rx_ack_count += 1

                elif packet.command == ALE.CMD_CALL:
                    if packet.destination in self.addresses or packet.destination == ALE.ADDRESS_ANY:
                        self.log('End sounding on channel ' + self.channel + ', ' + str(self.sound_rx_ack_count) + ' responses')
                        self.sound_rx_ack_count = 0

                        self.state = ALE.STATE_CONNECTING
                        self.log('Incoming call from address ' + self.call_address.decode('utf-8'))
                        self.call_address = packet.origin

                        if self.callback['call'] != None:
                            self.callback['call'](packet.origin)

                        # send ack
                        self.last_ack_packet_timestamp = current_time
                        self._send_ale(ALE.CMD_ACK, self.call_address)

                # if command == end, do nothing

    def _jobs(self):
        while self.online:
            current_time = time.time()

            # process log queue
            if current_time > (self.last_log_timestamp + 1) and len(self.log_queue) > 0:
                thread = threading.Thread(target=self._manage_log)
                thread.setDaemon(True)
                thread.start()

            if not self._text_mode:
                if self.modem.carrier_sense:
                    self.last_carrier_sense_timestamp = current_time

            if self.sound_packet != None:
                # if the channel changed before sending the sound ack
                if self.channel != self.sound_packet.channel:
                    self.sound_packet = None

            if self.state == ALE.STATE_SCANNING:
                # ack sounding after delay
                if (
                    # sound packet has been received
                    self.sound_packet != None and
                    # and channel has not changed since the sounding was received
                    self.channel == self.sound_packet.channel and
                    # and no carrier currently detected (i.e. other stations ack-ing)
                    self.last_carrier_sense_timestamp != current_time and 
                    # and sounding ack delay has passed
                    current_time > (self.sound_packet.timestamp + self.sound_ack_delay) and
                    # and other strong stations have not ack-ed already
                    self.lqa.should_ack_sound(self.channel, self.sound_packet.origin)
                ):
                    #send ack
                    self._send_ale(ALE.CMD_ACK, self.sound_packet.origin)
                    self.sound_packet = None
                        
                # go to the next channel
                if (
                    # time to change channel
                    current_time > (self.last_channel_change_timestamp + ALE.SCAN_WINDOW) and
                    # no recent activity on the channel
                    current_time > (self.last_activity_timestamp + ALE.SCAN_WINDOW)
                ):
                    # perform a sounding first if the channel quality data is stale
                    if self.lqa.channel_stale(self.channel):
                        self.sound_timeout_timestamp = current_time + self.sound_timeout
                        self.state = ALE.STATE_SOUNDING
                        self.log('Begin sounding on channel ' + self.channel)

                        self.last_sound_packet_timestamp = current_time
                        self._send_ale(ALE.CMD_SOUND, ALE.ADDRESS_ALL)
                        self.lqa.set_next_sounding(self.channel)

                    # if there are no pending packets in the modem transmit buffer
                    elif not self._text_mode and len(self.modem._tx_buffer) == 0:
                        self.next_channel()
                        self.last_channel_change_timestamp = current_time

                    elif self._text_mode:
                        self.next_channel()
                        self.last_channel_change_timestamp = current_time
                        

            # handle timeout in call states
            if (
                # in any call state
                (self.state == ALE.STATE_CALLING or
                self.state == ALE.STATE_CONNECTING or
                self.state == ALE.STATE_CONNECTED) and
                # and the call times out
                current_time > self.call_timeout_timestamp
            ):
                # if calling, try the next best channel
                if self.state == ALE.STATE_CALLING:
                    if len(self.call_channel_attempts) < self.max_call_channel_attempts:
                        self.call(self.call_address, next_channel = True)
                # otherwise, end the call
                else:
                    call_duration = current_time - self.call_started_timestamp
                    self.log('Call timed out, disconnected from address ' + self.call_address.decode('utf-8') + ', call duration: ' + str(int(call_duration)) + ' seconds')
        
                    if self.callback['disconnected'] != None:
                        self.callback['disconnected'](self.call_address, call_duration)
    
                    self.call_address = None
                    self.call_started_timestamp = 0
                    self.call_timeout_timestamp = 0
                    self.state = ALE.STATE_SCANNING

            # send call packets once per scan window
            if self.state == ALE.STATE_CALLING:
                if current_time > (self.last_call_packet_timestamp + ALE.SCAN_WINDOW):
                    self.last_call_packet_timestamp = current_time
                    self._send_ale(ALE.CMD_CALL, self.call_address)

            # send call ack packets once per scan window
            if self.state == ALE.STATE_CONNECTING:
                if current_time > (self.last_ack_packet_timestamp + ALE.SCAN_WINDOW):
                    self.last_ack_packet_timestamp = current_time
                    self._send_ale(ALE.CMD_ACK, self.call_address)

            if self.state == ALE.STATE_SOUNDING:
                # stop sounding if sounding times out without ack
                if current_time > self.sound_timeout_timestamp:
                    self.log('End sounding on channel ' +  self.channel + ', ' + str(self.sound_rx_ack_count) + ' responses')
                    self.sound_rx_ack_count = 0
                    self.state = ALE.STATE_SCANNING
                # send sounding packets once per scan window
                elif current_time > (self.last_sound_packet_timestamp + ALE.SCAN_WINDOW):
                    self.last_sound_packet_timestamp = current_time
                    self._send_ale(ALE.CMD_SOUND, ALE.ADDRESS_ALL)

            # simmer down
            time.sleep(0.1)

            



            
    










