"""WoW 1.12 packet header encryption (simple RC4-like with session key)."""


class PacketCrypt:
    """Encrypts/decrypts WoW 1.12 world packet headers using the session key."""

    def __init__(self, session_key: bytes):
        assert len(session_key) == 40
        self.key = session_key
        self._send_i = 0
        self._send_j = 0
        self._recv_i = 0
        self._recv_j = 0
        self.initialized = False

    def init(self):
        self.initialized = True

    def encrypt(self, data: bytearray) -> bytearray:
        for t in range(len(data)):
            self._send_i %= 40
            x = (data[t] ^ self.key[self._send_i]) + self._send_j
            x &= 0xFF
            self._send_i += 1
            data[t] = self._send_j = x
        return data

    def decrypt(self, data: bytearray) -> bytearray:
        for t in range(len(data)):
            self._recv_i %= 40
            x = (data[t] - self._recv_j) ^ self.key[self._recv_i]
            x &= 0xFF
            self._recv_i += 1
            self._recv_j = data[t]
            data[t] = x
        return data
