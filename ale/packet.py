# 7-bit packet overhead (not including addresses or data)
# tx time using 20-bit RNS addresses (no data, 47 characters): 1.25 seconds

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

    def unpack(self, raw):
        len_preamble = len(Packet.PREAMBLE)
        len_command = 2
        len_separator = len(Packet.SEPARATOR)
        address_separator = raw.find(Packet.SEPARATOR)
        data_separator = raw.find(Packet.SEPARATOR, address_separator + len_separator)

        # remove preamble
        raw = raw[len_preamble:]
        self.command = raw[:len_command]
        self.origin = raw[len_command:address_separator]
        self.destination = raw[address_separator + len_sepatator:data_separator]
        self.data = raw[data_separator + len_separator:]

    def pack(self):
        packet = Packet.PREAMBLE + self.command + self.origin + Packet.SEPARATOR + self.destination + Packet.SEPARATOR + data
        return packet

