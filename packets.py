"""Packet building helpers for WoW 1.12."""
import struct


def build_server_packet(opcode: int, data: bytes = b"") -> bytes:
    """Build a server->client packet: [size BE uint16][opcode LE uint16][data]
    size = len(data) + 2 (opcode is included in size)
    """
    size = len(data) + 2
    header = struct.pack(">H", size) + struct.pack("<H", opcode)
    return header + data


def pack_guid(guid: int) -> bytes:
    """Pack a GUID as PackedGUID (variable length)."""
    if guid == 0:
        return b"\x00"
    mask = 0
    parts = []
    for i in range(8):
        byte = (guid >> (i * 8)) & 0xFF
        if byte:
            mask |= (1 << i)
            parts.append(byte)
    return bytes([mask]) + bytes(parts)


class ByteBuffer:
    def __init__(self):
        self._buf = bytearray()

    def uint8(self, v):   self._buf += struct.pack("<B", v & 0xFF); return self
    def uint16(self, v):  self._buf += struct.pack("<H", v & 0xFFFF); return self
    def uint32(self, v):  self._buf += struct.pack("<I", v & 0xFFFFFFFF); return self
    def uint64(self, v):  self._buf += struct.pack("<Q", v); return self
    def float32(self, v): self._buf += struct.pack("<f", v); return self
    def raw(self, b):     self._buf += b; return self
    def cstring(self, s): self._buf += s.encode() + b"\x00"; return self
    def bytes(self):      return bytes(self._buf)
