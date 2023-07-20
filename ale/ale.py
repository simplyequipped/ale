# Non-standard Automatic Link Establishment (ALE) module
#
# Written by Howard at Simply Equipped
# June 2022
#
# simplyequipped.com
# github.com/simplyequipped


import os
import threading
import time
import random
import json

#import qdx
import fskmodem
import ale


#TODO
# - recognize activity on channel (lqa?), look for next best channel to place call
# - add fskmodem.receiving(), return bool (based on carrier sense)
# - add fskmodem.transmitting(), return bool
# - Reticulum ALE interface using Reticulum destinations as addresses
# - transmit timing may not be accurate due to fskmodem carrier sense collision avoidance
# - support other tranceivers via hamlib/flrig?
# - support other modems via fldigi?
# - channel data could include more extensive modem and radio config

#TODO handle group calls, specifically multiple stations sending acks. how does this work
#       once in a connected state where acks will be ignored? Remove group support?



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

    def __init__(self, config_path=None, text_mode=False):
        self._text_mode = text_mode

        self.radio_serial_port = None
        self.modem_alsa_device = 'QDX'
        self.modem_baudrate = 300
        self.modem_sync_byte = 0x23
        self.modem_confidence = 1.5

        self.scanlists = ale.default_scanlists
        self.address = None
        self.addresses = []
        self.enable_whitelist = False
        self.whitelist_addresses = []
        self.enable_blacklist = False
        self.blacklist_addresses = []

        self.callback = {
            'rx' : None,
            'call' : None,
            'connected' : None,
            'disconnected' : None
        }

        self.log_queue = []
        self.last_log_timestamp = 0

        self.config_dir = os.path.expanduser('~/.ale')
        self.config_path = os.path.join(self.config_dir, 'config')
        self.scanlist_path = os.path.join(self.config_dir, 'scanlists')
        self.log_path = os.path.join(self.config_dir, 'log')

        # use given alternate config file path if it exsits
        if os.path.exists(config_path):
            self.config_path = config_path

        # ensure config directory exists
        if not os.path.exists(self.config_dir):
            os.mkdir(self.config_dir)

        #TODO cull log instead?
        # clear log file
        if os.path.exists(self.log_path):
            os.remove(self.log_path)

        # if scanlist file exists, load it
        if os.path.exists(self.scanlist_path):
            self.load_scanlists()
        # if scanlist file does not exist, create it using the default scanlist
        else:
            self.save_scanlists()

        # if config file exists, load it
        if os.path.exists(self.config_path):
            self.load_config()
        # if config file does not exist, create it using default settings
        else:
            self.save_config()

        if self.address in [None, '', b'']:
            raise ValueError('ALE address cannot be empty. Update config file or pass address to ale.ALE() object on creation.')

        if not isinstance(self.address, bytes):
            self.address = address.encode('utf-8')

        if self.address not in self.addresses:
            self.addresses.append(self.address)

        for i in range(len(self.addresses)):
            if not isinstance(self.addresses[i], bytes):
                self.addresses[i] = self.addresses[i].encode('utf-8')

        # configure radio and modem
        #TODO
        if self._text_mode:
            self.radio = None
            self.modem = None
            self.log('Text-only mode')
        else:
            self.radio = qdx.QDX(port=radio_serial_port)
            self.log('Radio started')

            #TODO move modem config to config file
            alsa_device = fskmodem.get_alsa_device(alsa_device_string)
            self.modem = fskmodem.Modem(
                alsa_dev_in = alsa_device, 
                baudrate = baudrate,
                sync_byte = sync_byte,
                confidence = confidence
            )
            self.log('Modem started')
            self.modem.set_rx_callback(self._receive)

        self.online = True
        self.set_scanlist(self.get_scanlists()[0])
        self.set_channel(list(self.channels.keys())[0])
        self.lqa = ale.LQA(self)
        self.state_machine = ale.ALEStateMachine(self)
        self.log(str(self) + ' online')

        thread = threading.Thread(target=self._jobs)
        thread.setDaemon(True)
        thread.start()

    def __repr__(self):
        return '<ALE {}>'.format(self.address.decode('utf-8'))

    def stop(self):
        if not self._text_mode:
            self.modem.stop()
            self.log('Modem stopped')

        if self.online:
            self.online = False
            self.log(str(self) + ' offline')
        
        self.lqa.save_history()
        self._process_log_queue()

    # useful for displaying antenna requirements
    def get_channel_freq_list(self):
        freqs = [channel['freq'] for channel in self.channels]
        freqs.sort()

        return freqs

    def load_config(self):
        try:
            with open(self.config_path, 'r') as fd:
                config = json.load(fd)

            self.address = config['address']
            if 'group_addresses' in config.keys():
                self.addresses = config['addresses']
            if 'whitelist' in config.keys():
                self.enable_whitelist = True
                self.whitelist_addresses = config['whitelist']
            if 'blacklist' in config.keys():
                self.enable_blacklist = True
                self.blacklist_addresses = config['blacklist']
            if 'scanlist' in config.keys():
                self.set_scanlist(config['scanlist'])
            if 'radio' in config.keys():
                if 'serial_port' in config['radio'].keys() 
                    self.radio_serial_port = config['radio']['serial_port']
            if 'modem' in config.keys():
                if 'alsa_device' in config['modem']:
                    self.modem_alsa_device = config['modem']['alsa_device']
                if 'baudrate' in config['modem']:
                    self.modem_baudrate = config['modem']['baudrate']
                if 'sync_byte' in config['modem']:
                    self.modem_sync_byte = config['modem']['sync_byte']
                if 'confidence' in config['modem']:
                    self.modem_confidence = config['modem']['confidence']
 
            self.log('Loaded configuration from ' + self.config_path)
        except:
            #TODO handle
            pass

    def save_config(self):
        config = {
            'address': self.address,
            'group_addresses': self.addressses,
            'whitelist': self.whitelist_adddresses,
            'blacklist': self.whitelist_addresses,
            'scanlist': self.scanlist,
            'radio': {
                'serial_port': self.radio_serial_port
                },
            'modem': {
                'alsa_device': self.modem_alsa_device,
                'baudrate': self.modem_baudrate,
                'sync_byte': self.modem_sync_byte,
                'confidence': self.modem_confidence
                }
        }

        try:
            with open(self.config_path, 'w') as fd:
                json.dump(config, fd, indent='\t')

            self.log('Saved configuration to ' + self.config_path)
        except:
            #TODO handle
            pass

    def load_scanlists(self):
        if os.path.exists(self.scanlist_path):
            try:
                with open(self.scanlist_path, 'r') as fd:
                    self.scanlists = json.load(fd)
                self.log('Loaded scanlists from ' + self.scanlist_path)
            except:
                #TODO handle
                pass

    def save_scanlists(self):
        try:
            with open(self.scanlist_path, 'w') as fd:
                json.dump(self.scanlists, fd, indent='\t')
            self.log('Saved scanlists to ' + self.scanlist_path)
        except:
            #TODO handle
            pass

    def set_scanlist(self, scanlist):
        if scanlist not in self.scanlists.keys():
            raise ValueError('Scanlist not found')

        self.scanlist = scanlist
        self.channels = self.scanlists[scanlist]
        num_channels = len(self.channels)

        self.log('Scanlist set to {} ({} channels, {} seconds total scan time)'.format(self.scanlist, len(self.channels), len(self.channels) * ALE.SCAN_WINDOW))

    def get_scanlists(self):
        return self.scanlists.keys()

    def add_scanlist(self, scanlist):
        if scanlist not in self.scanlists:
            self.scanlists[scanlists] = []

    def remove_scanlist(self, scanlist):
        if scanlist in self.scanlists:
            del self.scanlists[scanlist]

    def add_channel(self, scanlist, channel_name, freq, mode):
        if scanlist in self.scanlists and channel_name not in self.scanlists[scanlist]:
            self.scanlists[scanlist][channel_name] = {'freq': freq, 'mode': mode}

    def remove_channel(self, scanlist, channel_name):
        if scanlist in self.scanlists and channel_name in self.scanlists[scanlist]:
            del self.scanlists[scanlist][channel_name]

    def update_channel(self, scanlist, channel_name, freq=None, mode=None):
        if scanlist in self.scanlists and channel_name in self.scanlists[scanlist]:
            if freq != None:
                self.scanlists[scanlist][channel_name]['freq'] = freq

            if mode != None:
                self.scanlists[scanlist][channel_name]['mode'] = mode

    def set_channel(self, channel):
        if channel not in self.channels:
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

    def add_address(self, address):
        if address not in self.addresses:
            self.addresses.append(address)
            self.log('Added self address ' + address.decode('utf-8'))

    def remove_address(self, address):
        if address != self.address and address in self.addresses:
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

    def call(self, address):
        self.state_machine.call(address)

    def send(self, data, keep_alive=False):
        #TODO
        if self._text_mode:
            print(data)
        else:
            self.state_machine.send(data, keep_alive)    
        
    def _send_ale(self, command, address=b'', data=b''):
        if command not in ALE.COMMANDS:
            raise ValueError('Invalid command \'{}\''.format(command))

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
        preamble = raw[:len(ale.Packet.PREAMBLE)]
        # handle non-ale packets
        if preamble != ale.Packet.PREAMBLE:
            if self.state_machine.state == ale.ALE.CONNECTED:
                self.state.keep_alive()
                
                # pass to data handling application when connected
                if self.callback['receive'] != None:
                    self.callback['receive'](raw)

            # drop the packet, no further processing
            return None

        # load received data into packet
        packet = ale.Packet()
        # discard packet if it fails to unpack, which likely means it is corrupted
        try:
            packet.unpack(raw)
        except:
            return None

        packet.timestamp = time.time()
        packet.channel = self.channel
        packet.confidence = confidence
        # store packet in lqa history
        self.lqa.store(packet)

        if self.enable_whitelist and packet.origin not in self.whitelist_addresses:
            return None

        if self.enable_blacklist and packet.origin in self.blacklist_addresses:
            return None

        # pass packet to the current state for handling
        self.state_machine.receive_packet(packet)

    def _jobs(self):
        while self.online:
            # process log queue
            if time.time() > (self.last_log_timestamp + 1) and len(self.log_queue) > 0:
                thread = threading.Thread(target=self._process_log_queue)
                thread.setDaemon(True)
                thread.start()

            # tick state machine
            self.state_machine.tick()

            # remove packets in the modem tx buffer if they are for a channel other than the current channel
            if self.modem != None and (len(self.modem._tx_buffer) > 0):
                for i in range(len(self.modem._tx_buffer)):
                    if self.modem._tx_buffer[i].channel != self.channel:
                        self.modem._tx_buffer.remove(i)

            # simmer down
            time.sleep(0.001)


