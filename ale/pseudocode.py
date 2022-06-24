# Call Handshake
#
# Step  Station A     Station B
#
# 1     call ->       -> call
#       (calling)     (connecting)
#
# 2     ack <-        <- ack
#       (connected)   (connecting)    # station A knows station B can hear them
#
# 3     ack ->        -> ack
#       (connected)   (connected)     # station B knows station A can hear them


# states
scanning
calling
connecting
connected
sounding

#commands
sound
ack
call
end


self.state = scanning

receive(raw, confidence):
    if raw.preamble != ALE.Packet.PREAMBLE:
        if self.state == connected:
            # pass packets to data handling application when connected
            callback.receive()
        else:
            return

    packet = ALE.Packet()
    packet.unpack(raw)
    packet.confidence == confidence
    packet.timestamp = time.time()
    
    # store all ALE packets as link quality analysis data
    lqa.store(packet)

    if self.state == scanning:
        if packet.command == sound:
            ack()
        elif packet.command == ack:
            pass
        elif packet.command == call:
            self.call_address = packet.origin_address
            self.state = connecting
            ack()
        elif packet.command == end:
            pass
    
    if self.state == calling:
        if packet.command == sound:
            pass
        elif packet.command == ack:
            if packet.origin_address == self.call_address and packet.destination_address == self.address:
                self.state = connected
                callback.connected()
                ack()
        elif packet.command == call:
            if packet.origin_address == self.call_address and packet.destination_address == self.address:
                self.state = connecting
                ack()
            #TODO else send busy command?
        elif packet.command == end:
            if packet.origin_address == self.call_address and packet.destination_address == self.address:
                self.state = scanning
                callback.disconnected()
    
    if self.state == connecting:
        if packet.command == sound:
            pass
        elif packet.command == ack:
            if packet.origin_address == self.call_address and packet.destination_address == self.address:
                self.state = connected
                callback.connected()
                ack()
        elif packet.command == call:
            if packet.origin_address == self.call_address and packet.destination_address == self.address:
                self.state = connecting
                ack()
            #TODO else send busy command?
        elif packet.command == end:
            if packet.origin_address == self.call_address and packet.destination_address == self.address:
                self.state = scanning
                callback.disconnected()
    
    if self.state == connected:
        if packet.command == sound:
            pass
        elif packet.command == ack:
            pass
        elif packet.command == call:
            pass
            #TODO send busy command?
            #TODO reject or add to call?
        elif packet.command == end:
            if packet.origin_address == self.call_address and packet.destination_address == self.address:
                self.state = scanning
                callback.disconnected()
    
    if self.state == sounding:
        if packet.command == sound:
            pass
        elif packet.command == ack:
            pass
        elif packet.command == call:
            self.call_address = packet.origin_address
            self.state = connecting
            ack()
        elif packet.command == end:
            pass
    



# async operations managing state and timing
job_loop():
    while self.online:

        if modem.carrier_sense:
            last_carrier_sense = time.time()

        if self.state == scanning:
            should_change_channel = last_channel_change + scan_window > time.time()
            channel_activity = last_carrier_sense + scan_window < time.time()
            if should_change_channel and not channel_activity:
                # sound if the link quality analysis data for the channel is state before changing channels
                if lqa.channel_stale(self.channel):
                    sound()
                else:
                    next_channel()
    
        elif self.state == calling:
            if time.time() > call_timeout:
                # call timed out, disconnect
                callback.disconnected()
                self.state = scanning

            # send call packet (1 sec length) every scan window (3 sec)
            if time.time() > send_next_call_packet:
                call()
    
        elif self.state == connecting:
            if time.time() > call_timeout:
                # call timed out, disconnect
                callback.disconnected()
                self.state = scanning
    
            # send ack packet (1 sec length) every scan window (3 sec)
            if time.time() > send_next_ack_packet:
                ack()
    
        elif self.state == connected:
            if time.time() > call_timeout:
                # call timed out, disconnect
                callback.disconnected()
                self.state = scanning
    
        elif self.state == sounding:
            if time.time() > sound_timeout:
                # sounding timed out
                self.state = scanning

            # send sound packet (1 sec length) every scan window (3 sec)
            if time.time() > send_next_ack_packet:
                sound()
    
            



















