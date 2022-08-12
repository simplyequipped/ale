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
# - Reticulum ALE interface using Reticulum destinations as addresses
# - transmit timing may not be accurate due to fskmodem carrier sense
# - support other tranceivers via hamlib/flrig?
# - support other modems via fldigi?
# - channel data could include more extensive modem and radio config

#TODO in jobs loop, monitor modem tx buffer for packets with channels other than the current channel
#TODO save and load config data: address, whitelist, blacklist


class ALE:

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

    SCAN_WINDOW = 3 # seconds

    def __init__(self, address, radio_serial_port=None, alsa_device_string='QDX', baudrate=300, sync_byte='0x23', confidence=1.5, text_mode=False):
        self._text_mode = text_mode
        self.baudrate = baudrate
        self.scanlists = ale.default_scanlists

        self.enable_whitelist = False
        self.whitelist_addresses = []
        self.enable_blacklist = False
        self.blacklist_addresses = []

        if not isinstance(address, bytes):
            address = address.encode('utf-8')

        self.address = address
        self.addresses = [self.address]
        self.group_addresses = []

        self.callback = {
            'rx' : None,
            'call' : None,
            'connected' : None,
            'disconnected' : None
        }

        self.log_queue = []
        self.last_log_timestamp = 0

        self.config_dir = os.path.expanduser('~/.ale')
        self.scanlist_path = os.path.join(self.config_dir, 'scanlists')
        self.log_path = os.path.join(self.config_dir, 'log')

        #TODO cull log instead
        # clear log when starting
        if os.path.exists(self.log_path):
            os.remove(self.log_path)

        # if scanlist file doesn't exist, create it using the default scanlist
        if not os.path.exists(self.scanlist_path):
            if not os.path.exists(self.config_dir):
                os.mkdir(self.config_dir)

            try:
                with open(self.scanlist_path, 'w') as fd:
                    json.dump(self.scanlists, fd, indent='\t')
                self.log('Saved scanlists to ' + self.scanlist_path)
            except:
                #TODO handle
                pass

        # if the scanlist file exists, load it
        else:
            try:
                with open(self.scanlist_path, 'r') as fd:
                    self.scanlists = json.load(fd)
                self.log('Loaded scanlists from ' + self.scanlist_path)
            except:
                #TODO handle
                pass
        
        # configure radio and modem
        #TODO
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

        self.set_scanlist(self.get_scanlists()[0])
        self.set_channel(list(self.channels.keys())[0])
        self.lqa = ale.LQA(self)
        self.state_machine = ale.ALEStateMachine(self)
        self.online = True
        self.log(str(self) + ' online')

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

    # useful for displaying antenna requirements
    def get_channel_freq_list(self):
        freqs = []

        for channel in self.channels:
            freqs.append(channel['freq'])

        return freqs.sort()

    def set_scanlist(self, scanlist):
        if scanlist not in self.scanlists.keys():
            return None

        self.scanlist = scanlist
        self.channels = self.scanlists[self.scanlist]
        num_channels = len(self.channels.keys())

        self.log('Scanlist set to ' + self.scanlist + ' (' + str(num_channels) + ' channels, ' + str(num_channels * ALE.SCAN_WINDOW) + ' seconds total scan time)')

    def get_scanlists(self):
        return list(self.scanlists.keys())

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

    def log(self, message):
        log_message = time.strftime('%x %X') + '  ' + message + '\n'
        self.log_queue.append(log_message)

    def _process_log_queue(self):
        with open(self.log_path, 'a') as fd:
            for message in self.log_queue:
                fd.write(message)

        self.log_queue.clear()
        self.last_log_timestamp = time.time()

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
                #TODO handle
                # if error communicating with radio, go offline
                self.online == False
                self.log('Going offline, failed to communicate with radio')

        if self.online:
            self.channel = channel
            #TODO remove, or add logging levels?
            #self.log('Channel set to ' + channel) 

    def call(self, address):
        self.state_machine.call(address)

    def send(self, data):
        #TODO
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
                #TODO pad with a different character since b'#' is the default fskmodem sync byte?
                # pad packet data to equal minimum transmit time
                packet.data = b'#' * (len_min_tx - len_packet)

        self.send(packet.pack())

    def _receive(self, raw, confidence):
        current_time = time.time()

        preamble = raw[:len(ale.Packet.PREAMBLE)]
        # handle non-ale packets
        if preamble != ale.Packet.PREAMBLE:
            if self.state_machine.state == ale.ALE.CONNECTED:
                self.state.keep_alive()
                
                # pass packets to data handling application when connected
                if self.callback['receive'] != None:
                    self.callback['receive'](raw)

            # drop the packet, no further processing
            return None

        # load received date into packet
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

        self.state_machine.receive_packet(packet)

    def _jobs(self):
        while self.online:
            current_time = time.time()

            # process log queue
            if current_time > (self.last_log_timestamp + 1) and len(self.log_queue) > 0:
                thread = threading.Thread(target=self._process_log_queue)
                thread.setDaemon(True)
                thread.start()

            self.state_machine.tick()

            # simmer down
            time.sleep(0.1)


