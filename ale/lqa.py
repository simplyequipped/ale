import os
import threading
import time
import random
import pickle

import ale

#TODO
# - test LQA time settings

class LQA:
    SOUND_WINDOW  = 60 * 60 # 60 minutes
    MAX_HISTORY = 1000

    SHOULD_ACK_PACKET_COUNT = 3
    SHOULD_ACK_MIN_CONFIDENCE = 1.7

    def __init__(self, owner):
        self.owner = owner
        self.history = []
        self.next_sound = {}
        self.next_history_cull_timestamp = time.time() + LQA.SOUND_WINDOW
        self.history_path = os.path.join(self.owner.config_dir, 'lqa_history')

        if os.path.exists(self.history_path):
            self.load_history()

        for channel in self.owner.channels.keys():
            self.set_next_sounding(channel)

        thread = threading.Thread(target=self._jobs)
        thread.setDaemon = True
        thread.start()

    def store(self, packet):
        self.history.append(packet)
        self.set_next_sounding(packet.channel)

    def best_channel(self, address=None, exclude=None):
        max_channel_confidence = 0.0
        max_address_confidence = 0.0
        best_by_channel = None
        best_by_address = None
        exclude_channels = []

        if isinstance(exclude, list):
            exclude_channels = exclude
        elif isinstance(exclude, str):
            exclude_channels.append(exclude)

        current_time = time.time()
        for i in range(len(self.history)):
            packet = self.history.pop(0)

            # discard the packet if stale
            if current_time > (packet.timestamp + LQA.SOUND_WINDOW):
                continue
            
            self.history.append(packet)

            # skip the packet if excluding associated channel
            if packet.channel in exclude_channels:
                continue
            
            if address != None and packet.origin == address and packet.confidence > max_address_confidence:
                max_address_confidence = packet.confidence
                best_by_address = packet.channel
            elif packet.confidence > max_channel_confidence:
                max_channel_confidence = packet.confidence
                best_by_channel = packet.channel
            
        # use address-specific confidence if it is at least 90% of channel confidence
        # ensures use of best channel even if an address is specified
        if address != None and max_address_confidence >= (max_channel_confidence * 0.9):
            best_by_channel = best_by_address

        # if history is empty, return the next unexcluded channel
        if best_by_channel == None:
            channels = list(self.owner.channels.keys())
            for i in range(len(channels)):
                channel = channels.pop(0)
                if channel not in exclude_channels:
                    channels.append(channel)

            return channels[0]

        else:
            return best_by_channel
            
    def channel_stale(self, channel):
        if channel in self.owner.channels.keys() and time.time() > self.next_sound[channel]:
                return True

        return False

    def set_next_sounding(self, channel):
        random_interval = random.randint(0, 15) * 60 # 5-15 minutes
        self.next_sound[channel] = time.time() + LQA.SOUND_WINDOW + random_interval    

    # avoid congestion by not ack-ing a sounding if other strong stations already ack-ed
    def should_ack_sound(self, channel, origin):
        packet_count = 0
        current_time = time.time()

        # start at the end for most recent packets
        for i in range(len(self.history)):
            packet = self.history.pop()

            # drop stale packets out of convience
            if current_time > packet.timestamp + LQA.SOUND_WINDOW:
                continue

            # if packet matches channel, origin address, and confidence, and is not too old
            packet_stale_timestamp = packet.timestamp + self.owner.sound_timeout
            if packet.channel == channel and packet.origin == origin and current_time < packet_stale_timestamp and packet.confidence >= LQA.SHOULD_ACK_MIN_CONFIDENCE:
                packet_count += 1

            self.history.insert(0, packet)
            
            if packet_count == LQA.SHOULD_ACK_PACKET_COUNT:
                return False

        # make sure the owner hasn't changed channels
        if self.owner.sound_packet.channel != self.owner.channel:
            return False
        else:
            return True

    def save_history(self):
        history = []

        try:
            for packet in self.history:
                history.append(packet.to_dict())
            
            with open(self.history_path, 'wb') as fd:
                pickle.dump(history, fd)

        except:
            return None

    def load_history(self):
        try:
            with open(self.history_path, 'rb') as fd:
                history = pickle.load(fd)
    
            for entry in history:
                packet = ale.Packet()
                packet.from_dict(entry)
                self.history.append(packet)
    
            self._cull_history()

        except:
            return None
    
    def _cull_history(self):
        current_time = time.time()

        for i in range(len(self.history)):
            packet = self.history.pop(0)
            if current_time < (packet.timestamp + LQA.SOUND_WINDOW) and i < LQA.MAX_HISTORY:
                self.history.append(packet)
        
            self.next_history_cull_timestamp = current_time + LQA.SOUND_WINDOW

    def _jobs(self):
        while self.owner.online:
            if time.time() > self.next_history_cull_timestamp:
                self._cull_history()

            time.sleep(1)

