# 7-bit packet overhead (not including addresses or data)
#
# ALE packet transmit time using 20-bit RNS addresses (no data, 47 characters, 300 baud): 1.25 seconds
# Note: RNS github discussion #72 proposing move to 128-bit address space (263 characters: 7 second transmit time)

class Packet:

    PREAMBLE    = b'ALE'
    SEPARATOR   = b':'

    def __init__(self, origin=b'', destination=b'', command=b'', data=b''):
        self.origin = origin
        self.destination = destination
        self.command = command
        self.data = data
        self.timestamp = 0
        self.confidence = None
        self.channel = None

    def __repr__(self):
        try:
            return 'ale.Packet[' + self.command.decode('utf-8') + ' : ' + self.origin.decode('utf-8') + ' : ' + self.destination.decode('utf-8') + ']'
        except:
            return 'ale.Packet[ : : ]'

    def pack(self):
        packet = Packet.PREAMBLE + self.command + self.origin + Packet.SEPARATOR + self.destination + Packet.SEPARATOR + self.data
        return packet

    def unpack(self, raw):
        # remove preamble
        len_preamble = len(Packet.PREAMBLE)
        raw = raw[len_preamble:]

        len_command = 2
        len_separator = len(Packet.SEPARATOR)
        address_separator = raw.find(Packet.SEPARATOR)
        data_separator = raw.find(Packet.SEPARATOR, address_separator + len_separator)

        self.command = raw[:len_command]
        self.origin = raw[len_command:address_separator]
        self.destination = raw[address_separator + len_separator:data_separator]
        self.data = raw[data_separator + len_separator:]

    def to_dict(self):
        packet = {}
        packet['origin'] = self.origin.decode('utf-8')
        packet['destination'] = self.destination.decode('utf-8')
        packet['command'] = self.command.decode('utf-8')
        packet['data'] = self.data.decode('utf-8')
        packet['timestamp'] = self.timestamp
        packet['confidence'] = self.confidence
        packet['channel'] = self.channel

        return packet

    def from_dict(self, packet):
        self.origin = packet['origin'].encode('utf-8')
        self.destination = packet['destination'].encode('utf-8')
        self.command = packet['command'].encode('utf-8')
        self.data = packet['data'].encode('utf-8')
        self.timestamp = packet['timestamp']
        self.confidence = packet['confidence']
        self.channel = packet['channel']

