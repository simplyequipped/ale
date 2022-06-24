import threading
import time
import random

#TODO
# - random delay (0.0-3.0 sec) before sound ack
# - don't ack sound if heard >= 3 acks with confidence > 1.X

class LQA:
    SOUND_RATE  = 10 * 60 # 10 minutes
    STALE_TIME  = 60 * 60 # 60 minutes
    MAX_HISTORY = 1000

    #TODO test these settings
    NO_ACK_PACKET_COUNT = 3
    NO_ACK_CONFIDENCE = 1.5

    def __int__(self, owner):
        self.owner = owner
        self.history = []
        self.next_sound = {}

        for channel in self.owner.channels.keys():
            random_interval = random.randint(0, 5) * 60 # 0-5 minutes
            self.next_sound[channel] = time.time() + (LQA.SOUND_RATE) + random_interval

        thread = threading.Thread(target=self._jobs)
        thread.setDaemon = True
        thread.start()

    def store(self, packet):
        self.history.append(packet)

        if len(self.history) > LQA.MAX_HISTORY:
            self.history.pop(0)

        random_interval = random.randint(0, 5) * 60 # 0-5 minutes
        self.next_sound[packet.channel] = time.time() + LQA.SOUND_RATE + random_interval

    def best_channel(self, address=None):
        max_channel_confidence = 0.0
        max_address_confidence = 0.0
        best_by_channel = None
        best_by_address = None

        current_time = time.time()
        for i in range(len(self.history)):
            packet = self.history.pop(0)
            if current_time > packet.timestamp + LQA.STALE_TIME:
                continue
            
            if address != None and packet.origin == address and packet.confidence > max_address_confidence:
                max_address_confidence = packet.confidence
                best_by_address = packet.channel
            elif packet.confidence > max_channel_confidence:
                max_channel_confidence = packet.confidence
                best_by_channel = packet.channel
                
            self.history.append(packet)

        # use address-specific confidence if it is at least 90% of channel confidence
        # ensures use of best channel even if an address is specified
        if address != None and max_address_confidence >= (max_channel_confidence * 0.9):
            best_by_channel = best_by_address

        return best_by_channel
            
    def channel_stale(self, channel):
        if channel in self.owner.channels:
            if time.time() > self.next_sound[channel]:
                return True

        return False

    # avoid congestion by not ack-ing a sounding if other strong stations already ack-ed
    def should_ack_sound(self, channel, origin):
        packet_count = 0
        current_time = time.time()

        # start at the end for most recent packets
        for i in range(len(self.history)):
            packet = self.history.pop()
            if current_time > packet.timestamp + LQA.STALE_TIME:
                continue

            # if packet matches channel, origin address, and confidence, and is less than 2 minutes old
            if packet.channel == channel and packet.origin == origin and current_time < (packet.timestamp + 120) and packet.confidence >= LQA.NO_ACK_CONFIDENCE:
                packet_count += 1

            self.history.insert(0, packet)
            
            if packet_count == LQA.NO_ACK_PACKET_COUNT:
                return False

        return True

    def _jobs(self):
        next_history_cull = time.time() + LQA.SOUND_RATE

        while owner.online:
            current_time = time.time()
            if current_time > next_history_cull:
                for i in range(len(self.history)):
                    packet = self.history.pop(0)
                    if current_time < packet.timestamp + LQA.STALE_TIME:
                        self.history.append(packet)

                next_history_cull = time.time() + LQA.SOUND_RATE
            time.sleep(1)

        





