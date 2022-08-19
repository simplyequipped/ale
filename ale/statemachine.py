import time
import random

import ale


class StateScanning:
    def __init__(self, machine):
        self.name = 'scanning'
        self.state = ale.ALE.STATE_SCANNING
        self.active = False
        self.machine = machine

        self.call_address = b''
        self.last_channel_change_timestamp = 0
        self.last_carrier_sense_timestamp = 0
        self.last_activity_timestamp = 0
        self.received_sound_packet = None
        self.sound_ack_delay = 0

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<ALE State ' + self.name + '>'

    def __eq__(self, ale_state):
        return self.state == ale_state

    def enter_state(self):
        self.call_address = b''
        self.received_sound_packet = None
        self.sound_ack_delay = 0

        if self.machine.last_state != None:
            self.last_carrier_sense_timestamp = self.machine.last_state.last_carrier_sense_timestamp
            self.last_activity_timestamp = self.machine.last_state.last_activity_timestamp

        self.active = True

    def leave_state(self):
        self.active = True
        
    def next_channel(self):
        channels = list(self.machine.owner.channels.keys())
        channel_index = channels.index(self.channel)

        if channel_index < (len(channels) - 1):
            next_channel = channels[channel_index + 1]
        else:
            next_channel = channels[0]

        self.machine.owner.set_channel(next_channel)
        self.last_channel_change_timestamp = current_time
        self.last_carrier_sense_timestamp = 0

    def receive_packet(self, packet):
        if not self.active:
            return None

        if packet.command == ale.ALE.CMD_SOUND:
            # ack once per sounding event, other sounding packets stored for lqa
            if self.received_sound_packet == None:
                self.last_activity_timestamp = time.time()

                self.received_sound_packet = packet
                # random delay to avoid multiple stations ack-ing a sounding at the same time
                self.sound_ack_delay = random.uniform(0.25, 1)

        # if packet.command == ack, do nothing

        elif packet.command == ale.ALE.CMD_CALL:
            if packet.destination in self.machine.owner.addresses or packet.destination == ale.ALE.ADDRESS_ANY:
                self.last_activity_timestamp = time.time()
                self.call_address = packet.origin
                self.machine.change_state(ale.ALE.STATE_CONNECTING)

        # if packet.command == end, do nothing
                
    def tick(self):
        if not self.active:
            return None

        # store current time to avoid multiple calls to time.time()
        current_time = time.time()
        should_ack_sounding = False
        
        if self.machine.owner.modem != None and self.machine.owner.modem.carrier_sense:
            self.last_carrier_sense_timestamp = current_time

        # if we should ack sounding
        if (
            # sound packet has been received
            self.received_sound_packet != None and
            # and other strong stations have not ack-ed already
            self.machine.owner.lqa.should_ack_sound(self.machine.owner.channel, self.received_sound_packet.origin)
        ):
            should_ack_sounding = True

        # otherwise drop the sounding packet so we can go to the next channel
        else:
            self.received_sound_packet = None

        # if we should ack the sounding, and we are able to ack sounding
        if (
            should_ack_sounding and
            # no carrier detected within the last millisecond (i.e. other stations responding)
            self.last_carrier_sense_timestamp < (current_time - 0.001) and 
            # and sounding ack delay has passed
            current_time > (self.received_sound_packet.timestamp + self.sound_ack_delay)
        ):
            #send ack
            self.machine.owner._send_ale(ale.ALE.CMD_ACK, self.received_sound_packet.origin)
            self.received.sound_packet = None

        # if it is time to change the channel
        if (
            # time to go to the next channel
            current_time > (self.last_channel_change_timestamp + ale.ALE.SCAN_WINDOW) and
            # no recent activity on the current channel
            current_time > (self.last_activity_timestamp + ale.ALE.SCAN_WINDOW)
        ):
            # perform a sounding first if the channel quality data is stale
            if self.machine.owner.lqa.channel_stale(self.machine.owner.channel):
                self.machine.change_state(ale.ALE.STATE_SOUNDING)
            
            # if there are no pending sounding acks or pending packets in the modem transmit buffer
            elif self.received_sound_packet == None and self.machine.owner.modem != None and len(self.machine.owner.modem._tx_buffer) == 0:
                # go to the next channel
                self.next_channel()


class StateCalling:
    def __init__(self, machine):
        self.name = 'calling'
        self.state = ale.ALE.STATE_CALLING
        self.active = False
        self.machine = machine
        self.call_timeout = 30 # seconds

        self.call_address = b''
        self.last_channel_change_timestamp = 0
        self.last_carrier_sense_timestamp = 0
        self.last_activity_timestamp = 0
        self.last_call_packet_timestamp = 0
        self.max_call_channel_attempts = 0
        self.call_started_timestamp = 0
        self.call_timeout_timestamp = 0
        self.best_channel = None
        self.call_channel_attempts = []

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<ALE State ' + self.name + '>'

    def __eq__(self, ale_state):
        return self.state == ale_state

    def enter_state(self):
        if not isinstance(self.call_address, bytes):
            self.call_address = self.call_address.encode('utf-8')

        # set calling timeout based on number of channels in current scanlist
        self.call_timeout = ale.ALE.SCAN_WINDOW * (len(self.machine.owner.channels.keys()) + 1) # seconds
        self.call_started_timestamp = time.time()
        self.call_timeout_timestamp = 0
        self.last_call_packet_timestamp = 0
        self.call_channel_attempts.clear()
        #TODO should max attempts equal number of channels in scanlist?
        self.max_call_channel_attempts = min(3, len(self.machine.owner.channels.keys()))

        if self.machine.last_state != None:
            self.last_carrier_sense_timestamp = self.machine.last_state.last_carrier_sense_timestamp
            self.last_activity_timestamp = self.machine.last_state.last_activity_timestamp

        self.active = True

    def leave_state(self):
        self.active = False

    def receive_packet(self, packet):
        if not self.active:
            return None

        # store current time to avoid multiple calls to time.time()
        current_time = time.time()
        addresses = self.machine.ownder.addresses

        # if packet.command == sound, do nothing
        
        # call acknowledged
        if packet.command == ale.ALE.CMD_ACK:
            if packet.destination in addresses and packet.origin == self.call_address:
                self.last_activity_timestamp = time.time()
                self.machine.change_state(ale.ALE.STATE_CONNECTED)

        # calling each other at the same time
        if packet.command == ale.ALE.CMD_CALL:
            if packet.destination in addresses and packet.origin == self.call_address:
                self.last_activity_timestamp = time.time()
                self.machine.change_state(ale.ALE.STATE_CONNECTING)
            
        # ignored
        if packet.command == ale.ALE.CMD_END:
            if packet.destination in addresses and packet.origin == self.call_address:
                self.last_activity_timestamp = time.time()

                address = self.call_address.decode('utf-8')
                call_duration = int(current_time - self.call_started_timestamp)
                self.machine.owner.log('Call ended by address ' + address + ' (call duration: ' + str(call_duration) + ' seconds)')
    
                if self.machine.owner.callback['disconnected'] != None:
                    self.machine.owner.callback['disconnected'](self.call_address, call_duration)

                self.machine.change_state(ale.ALE.STATE_SCANNING)

    def next_channel(self):
        self.best_channel = self.machine.owner.lqa.best_channel(self.call_address, exclude = self.call_channel_attempts)
        self.call_channel_attempts.append(self.best_channel)
        self.machine.owner.set_channel(self.best_channel)
        self.last_channel_change_timestamp = time.time()
        self.last_carrier_sense_timestamp = 0

        address = self.call_address.decode('utf-8')
        scanlist = self.machine.owner.scanlist
        channel = self.best_channel
        self.machine.owner.log('Calling ' + address + ' on channel ' + scanlist + ':' + channel)

    def tick(self):
        if not self.active:
            return None

        # store current time to avoid multiple calls to time.time()
        current_time = time.time()

        if self.machine.owner.modem != None and self.machine.owner.modem.carrier_sense:
            self.last_carrier_sense_timestamp = current_time

        # if call timed out
        if current_time > self.call_timeout_timestamp:
            # try the next best channel
            if len(self.call_channel_attempts) < self.max_call_channel_attempts:
                self.next_channel()
                self.call_timeout_timestamp = current_time + self.call_timeout
            else:
                # end the call
                address = self.call_address.decode('utf-8')
                call_duration = int(current_time - self.call_started_timestamp)
                self.machine.owner.log('Call timed out, no answer from ' + address + ' (call duration: ' + str(call_duration) + ' seconds)')
                
                if self.machine.owner.callback['disconnected'] != None:
                    self.machine.owner.callback['disconnected'](self.call_address, call_duration)

                self.machine.change_state(ale.ALE.STATE_SCANNING)

        # while calling send call packets once per scan window
        elif current_time > (self.last_call_packet_timestamp + ale.ALE.SCAN_WINDOW):
            self.last_call_packet_timestamp = current_time
            self.machine.owner._send_ale(ale.ALE.CMD_CALL, self.call_address)


# only the called station can be in a connecting state, since the calling station goes from
# the calling state directly to the connected state after ack
class StateConnecting:
    def __init__(self, machine):
        self.name = 'connecting'
        self.state = ale.ALE.STATE_CONNECTING
        self.active = False
        self.machine = machine
        self.call_timeout = 5 * 60 # seconds

        self.call_address = b''
        self.last_ack_packet_timestamp = 0
        self.last_carrier_sense_timestamp = 0
        self.last_activity_timestamp = 0
        self.call_started_timestamp = 0
        self.call_timeout_timestamp = 0

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<ALE State ' + self.name + '>'

    def __eq__(self, ale_state):
        return self.state == ale_state

    def enter_state(self):
        #TODO set in scanning and sounding state where packet is received
        self.call_address = self.machine.last_state.call_address
        self.last_ack_packet_timestamp = 0
        self.call_started_timestamp = time.time()
        self.call_timeout_timestamp = time.time() + self.call_timeout
        
        address = self.call_address.decode('utf-8')
        scanlist = self.machine.owner.scanlist
        channel = self.machine.owner.channel
        self.machine.owner.log('Incoming call from address ' + address + ' on channel ' + scanlist + ':' + channel)

        if self.machine.owner.callback['call'] != None:
            self.machine.owner.callback['call'](self.call_address)

        if self.machine.last_state != None:
            self.last_carrier_sense_timestamp = self.machine.last_state.last_carrier_sense_timestamp
            self.last_activity_timestamp = self.machine.last_state.last_activity_timestamp

        self.active = True
                        
    def leave_state(self):
        self.active = False

    def receive_packet(self, packet):
        if not self.active:
            return None

        # store current time to avoid multiple calls to time.time()
        current_time = time.time()
        addresses = self.machine.ownder.addresses

        # if packet.command == sound, do nothing
        
        # call handshake complete
        if packet.command == ale.ALE.CMD_ACK:
            if packet.destination in addresses and packet.origin == self.call_address:
                self.last_activity_timestamp = time.time()
                self.machine.change_state(ale.ALE.STATE_CONNECTED)

        # called again by the address we are already in the process of connecting
        if packet.command == ale.ALE.CMD_CALL:
            if packet.destination in addresses and packet.origin == self.call_address:
                self.last_activity_timestamp = time.time()
                # restart the connecting process
                self.last_ack_packet_timestamp = 0
                self.call_started_timestamp = time.time()
                self.call_timeout_timestamp = time.time() + self.call_timeout
            
        # call ended before connection was established
        if packet.command == ale.ALE.CMD_END:
            if packet.destination in addresses and packet.origin == self.call_address:
                self.last_activity_timestamp = time.time()

                address = self.call_address.decode('utf-8')
                call_duration = int(current_time - self.call_started_timestamp)
                self.machine.owner.log('Call ended by address ' + address + ' (call duration: ' + str(call_duration) + ' seconds)')
    
                if self.machine.owner.callback['disconnected'] != None:
                    self.machine.owner.callback['disconnected'](self.call_address, call_duration)

                self.machine.change_state(ale.ALE.STATE_SCANNING)

    def tick(self):
        if not self.active:
            return None

        # store current time to avoid multiple calls to time.time()
        current_time = time.time()

        if self.machine.owner.modem != None and self.machine.owner.modem.carrier_sense:
            self.last_carrier_sense_timestamp = current_time

        # if call timed out
        if current_time > (self.call_timeout_timestamp):
            # end the call
            address = self.call_address.decode('utf-8')
            call_duration = int(current_time - self.call_started_timestamp)
            self.machine.owner.log('Call timed out, no acknowledgement from ' + address + ' (call duration: ' + str(call_duration) + ' seconds)')
            
            if self.machine.owner.callback['disconnected'] != None:
                self.machine.owner.callback['disconnected'](self.call_address, call_duration)
                
            self.machine.change_state(ale.ALE.STATE_SCANNING)

        # while connecting send ack packets once per scan window
        elif current_time > (self.last_ack_packet_timestamp + ale.ALE.SCAN_WINDOW):
            self.last_ack_packet_timestamp = current_time
            self.machine.owner._send_ale(ale.ALE.CMD_ACK, self.call_address)


class StateConnected:
    def __init__(self, machine):
        self.name = 'connected'
        self.state = ale.ALE.STATE_CONNECTED
        self.active = True
        self.machine = machine
        self.call_timeout = 5 * 60 # seconds

        self.call_address = b''
        self.last_carrier_sense_timestamp = 0
        self.last_activity_timestamp = 0
        self.call_started_timestamp = 0
        self.call_timeout_timestamp = 0

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<ALE State ' + self.name + '>'

    def __eq__(self, ale_state):
        return self.state == ale_state

    # enter state from:
    #   calling
    #   connecting
    #
    def enter_state(self):
        self.call_address = self.machine.last_state.call_address
        self.call_started_timestamp = self.machine.last_state.call_started_timestamp
        self.call_timeout_timestamp = time.time() + self.call_timeout
        
        address = self.call_address.decode('utf-8')
        scanlist = self.machine.owner.scanlist
        channel = self.machine.owner.channel
        self.machine.owner.log('Incoming call from address ' + address + ' on channel ' + scanlist + ':' + channel)

        if self.machine.owner.callback['call'] != None:
            self.machine.owner.callback['call'](self.call_address)

        if self.machine.last_state != None:
            self.last_carrier_sense_timestamp = self.machine.last_state.last_carrier_sense_timestamp
            self.last_activity_timestamp = self.machine.last_state.last_activity_timestamp

        self.active = True

    def leave_state(self):
        self.active = False

    def receive_packet(self, packet):
        if not self.active:
            return None

        # if packet.command == sound, do nothing

        # if packet.command == ack, do nothing

        # if packet.command == call, do nothing

        # call ended
        if packet.command == ale.ALE.CMD_END:
            self.last_activity_timestamp = time.time()

            address = self.call_address.decode('utf-8')
            call_duration = int(current_time - self.call_started_timestamp)
            self.machine.owner.log('Call ended by address ' + address + ' (call duration: ' + str(call_duration) + ' seconds)')
    
            if self.machine.owner.callback['disconnected'] != None:
                self.machine.owner.callback['disconnected'](self.call_address, call_duration)

            self.machine.change_state(ale.ALE.STATE_SCANNING)

    def keep_alive(self):
        self.call_timeout_timestamp = time.time() + self.call_timeout

    def tick(self):
        if not self.active:
            return None

        # store current time to avoid multiple calls to time.time()
        current_time = time.time()

        if self.machine.owner.modem != None and self.machine.owner.modem.carrier_sense:
            self.last_carrier_sense_timestamp = current_time

        # if call timed out
        if current_time > (self.call_timeout_timestamp):
            # end the call
            address = self.call_address.decode('utf-8')
            call_duration = int(current_time - self.call_started_timestamp)
            self.machine.owner.log('Call timed out, disconnected from ' + address + ' (call duration: ' + str(call_duration) + ' seconds)')
            
            if self.machine.owner.callback['disconnected'] != None:
                self.machine.owner.callback['disconnected'](self.call_address, call_duration)
                
            self.machine.change_state(ale.ALE.STATE_SCANNING)


class StateSounding:
    def __init__(self, machine):
        self.name = 'sounding'
        self.state = ale.ALE.STATE_SOUNDING
        self.active = False
        self.machine = machine

        self.call_address = b''
        self.sound_timeout = 0
        self.sound_started_timestamp = 0
        self.sound_timeout_timestamp = 0
        self.last_carrier_sense_timestamp = 0
        self.last_activity_timestamp = 0
        self.last_sound_packet_timestamp = 0
        self.sound_rx_ack_count = 0

    def __str__(self):
        return self.name

    def __repr__(self):
        return '<ALE State ' + self.name + '>'

    def __eq__(self, ale_state):
        return self.state == ale_state

    def enter_state(self):
        # set sounding timeout based on number of channels in current scanlist
        self.sound_timeout = ale.ALE.SCAN_WINDOW * (len(self.machine.owner.channels.keys()) + 1) # seconds
        self.sound_started_timestamp = time.time()
        self.sound_timeout_timestamp = time.time() + self.sound_timeout
        self.sound_rx_ack_count = 0

        scanlist = self.machine.owner.scanlist
        channel = self.machine.owner.channel
        self.machine.owner.log('Begin sounding on channel ' + scanlist + ':' + channel)

        if self.machine.last_state != None:
            self.last_carrier_sense_timestamp = self.machine.last_state.last_carrier_sense_timestamp
            self.last_activity_timestamp = self.machine.last_state.last_activity_timestamp

        self.active = True

        #TODO check if another sounding is in progress via lqa?

    def leave_state(self):
        self.active = False
        self.machine.owner.lqa.set_next_sounding(self.machine.owner.channel)

    def receive_packet(self, packet):
        if not self.active:
            return None

        # if packet.command == sound, do nothing
        
        # count sounding acks
        if packet.command == ale.ALE.CMD_ACK:
            if packet.destination in self.machine.owner.addresses:
                self.last_activity_timestamp = time.time()
                self.sound_rx_ack_count += 1

        # incoming call
        if packet.command == ale.ALE.CMD_CALL:
            self.last_activity_timestamp = time.time()
            self.call_address = packet.origin
            self.machine.change_state(ale.ALE.STATE_CONNECTING)
            
        # if packet.command == end, do nothing

    def tick(self):
        if not self.active:
            return None

        # store current time to avoid multiple calls to time.time()
        current_time = time.time()

        if self.machine.owner.modem != None and self.machine.owner.modem.carrier_sense:
            self.last_carrier_sense_timestamp = current_time

        # if sounding timed out
        if current_time > (self.sound_timeout_timestamp):
            # end sounding
            scanlist = self.machine.owner.scanlist
            channel = self.machine.owner.channel
            self.machine.owner.log('End sounding on channel ' + scanlist + ':' + channel + ', ' + str(self.sound_rx_ack_count) + ' responses')
            
            self.machine.change_state(ale.ALE.STATE_SCANNING)

        # while sounding send sound packets once per scan window
        elif current_time > (self.last_sound_packet_timestamp + ale.ALE.SCAN_WINDOW):
            self.last_sound_packet_timestamp = current_time
            self.machine.owner._send_ale(ale.ALE.CMD_SOUND, ale.ALE.ADDRESS_ALL)


class ALEStateMachine:
    def __init__(self, owner):
        self.owner = owner
        self.states = []
        self.state = None
        self.last_state = None

        self.states.append(StateScanning(self))
        self.states.append(StateCalling(self))
        self.states.append(StateConnecting(self))
        self.states.append(StateConnected(self))
        self.states.append(StateSounding(self))

        self.online = True

        # set initial state
        init_state_index = self.states.index(ale.ALE.STATE_SCANNING)
        self.state = self.states[init_state_index]
        self.state.enter_state()

    def change_state(self, ale_state):
        # leave the current state
        self.state.leave_state()
        # save the last state
        self.last_state = self.state
        # get the object for the next state
        next_state_index = self.states.index(ale_state)
        self.state = self.states[next_state_index]
        # enter the next state
        self.state.enter_state()

    def get_state(self):
        return self.state

    #TODO remove if not used
    def get_state_object(self, ale_state):
        state_index = self.states.index(ale_state)
        return self.states[state_index]

    def receive_packet(self, packet):
        # pass ale packets to the current state for handling
        self.state.receive_packet(packet)

    def keep_alive(self):
        if self.state == ale.ALE.STATE_CONNECTED:
            self.state.keep_alive()

    def call(self, address):
        self.change_state(ale.ALE.STATE_CALLING)
        self.state.call_address = address
        self.state.enter_state()

    def tick(self):
        self.state.tick()


