"""WoW 1.12 World Server — auth handshake + packet routing to modules."""
import asyncio
import hashlib
import logging
import os
import struct

from crypto import PacketCrypt
from database import get_session_key, get_account
from opcodes import CMSG_AUTH_SESSION, SMSG_AUTH_CHALLENGE, SMSG_AUTH_RESPONSE, SMSG_MESSAGECHAT
from packets import ByteBuffer
import config

log = logging.getLogger("world")

CHAT_MSG_SYSTEM = 10


class WorldSession(asyncio.Protocol):
    def __init__(self, server):
        self.server    = server
        self.db_path   = server.db_path
        self.transport = None
        self.buf       = bytearray()
        self.crypt     = None
        self.account   = None
        self.gm_level  = 0
        self.char      = None      # current character dict (or None)
        self.char_guid = 0
        self.seed      = os.urandom(4)

    # ------------------------------------------------------------------

    def connection_made(self, transport):
        self.transport = transport
        peer = transport.get_extra_info('peername')
        log.info(f"World connection from {peer}")
        # WoW 1.12.1: payload is just the 4-byte seed (no uint32(1) prefix — that's 3.3.5+)
        pkt = self._build_raw_packet(SMSG_AUTH_CHALLENGE, self.seed)
        log.info(f"  >> SMSG_AUTH_CHALLENGE ({len(pkt)} bytes): {pkt.hex()}")
        log.info(f"     server_seed = {self.seed.hex()}")
        self._send_raw(pkt)

    def connection_lost(self, exc):
        log.info(f"World disconnected: {self.account}")
        if self.account:
            self.server.remove_session(self.account)

    def data_received(self, data: bytes):
        self.buf += data
        while True:
            pkt = self._read_packet()
            if pkt is None:
                break
            opcode, payload = pkt
            log.debug(f"  << opcode=0x{opcode:04X} payload={len(payload)} bytes")
            self._handle(opcode, payload)

    # ------------------------------------------------------------------
    # Packet I/O
    # ------------------------------------------------------------------

    def _read_packet(self):
        if len(self.buf) < 6:
            return None
        header = bytearray(self.buf[:6])
        if self.crypt and self.crypt.initialized:
            # Save crypt state in case we need to abort (not enough data)
            save_i, save_j = self.crypt._recv_i, self.crypt._recv_j
            header = self.crypt.decrypt(header)
        size   = struct.unpack_from(">H", header, 0)[0]
        opcode = struct.unpack_from("<I", header, 2)[0]
        total  = 2 + size
        if len(self.buf) < total:
            # Restore crypt state — we'll decrypt this header again next time
            if self.crypt and self.crypt.initialized:
                self.crypt._recv_i, self.crypt._recv_j = save_i, save_j
            return None
        payload = bytes(self.buf[6:total])
        self.buf = self.buf[total:]
        return opcode, payload

    @staticmethod
    def _build_raw_packet(opcode: int, data: bytes = b"") -> bytes:
        header = struct.pack(">H", len(data) + 2) + struct.pack("<H", opcode)
        return header + data

    def _send_raw(self, data: bytes):
        self.transport.write(data)

    def _send(self, opcode: int, data: bytes = b""):
        header = bytearray(struct.pack(">H", len(data) + 2) + struct.pack("<H", opcode))
        if self.crypt and self.crypt.initialized:
            header = self.crypt.encrypt(header)
        self.transport.write(bytes(header) + data)

    def send_sys_msg(self, msg: str):
        """Dispatch to modules.gm.send_sys_msg so that 'reload gm' hot-reloads
        the implementation without needing a server restart.
        The actual packet format lives in gm.py — update it there.
        """
        import sys
        gm = sys.modules.get("modules.gm")
        if gm and hasattr(gm, "send_sys_msg"):
            log.debug(f"send_sys_msg → gm module (msg={msg!r})")
            try:
                gm.send_sys_msg(self, msg)
            except Exception as exc:
                log.exception(f"send_sys_msg raised: {exc}")
        else:
            log.warning(f"send_sys_msg fallback — gm={gm} (msg={msg!r})")
            msg_bytes = msg.encode("utf-8")
            buf = ByteBuffer()
            buf.uint8(CHAT_MSG_SYSTEM)
            buf.uint32(0); buf.uint64(0); buf.uint32(0)
            buf.uint32(len(msg_bytes) + 1)
            buf.raw(msg_bytes + b"\x00")
            buf.uint8(0)
            self._send(SMSG_MESSAGECHAT, buf.bytes())

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _handle(self, opcode: int, payload: bytes):
        if opcode == CMSG_AUTH_SESSION:
            log.info(f"  >> handling CMSG_AUTH_SESSION (0x{CMSG_AUTH_SESSION:04X})")
            self._on_auth_session(payload)
            return
        if not self.server.dispatch_packet(self, opcode, payload):
            log.debug(f"  Unhandled opcode 0x{opcode:04X} ({len(payload)} bytes)")

    # ------------------------------------------------------------------
    # Auth session (stays here — sets up encryption, can't be in a module)
    # ------------------------------------------------------------------

    def _on_auth_session(self, payload: bytes):
        log.info(f"  _on_auth_session: payload={len(payload)} bytes")
        log.info(f"     raw payload: {payload[:64].hex()}{'...' if len(payload)>64 else ''}")
        try:
            build = struct.unpack_from("<I", payload, 0)[0]
            server_id = struct.unpack_from("<I", payload, 4)[0]
            log.info(f"     build={build} server_id={server_id}")
            # build(4) + login_server_id(4) + account_name(null-term) + client_seed(4) + digest(20)
            end = payload.index(b"\x00", 8)
            username = payload[8:end].decode()
            offset = end + 1
            client_seed   = payload[offset:offset + 4]
            client_digest = payload[offset + 4:offset + 24]
            log.info(f"     username='{username}' client_seed={client_seed.hex()}")
            log.info(f"     client_digest={client_digest.hex()}")
        except Exception as e:
            log.error(f"Auth session parse error: {e}")
            log.error(f"  raw={payload.hex()}")
            return

        self.account = username.upper()
        log.info(f"  World auth session: {self.account}")

        session_key = get_session_key(self.db_path, self.account)
        if session_key is None:
            log.warning(f"  No session key for {self.account}")
            self._send(SMSG_AUTH_RESPONSE, bytes([0x0D]))
            return
        log.info(f"     session_key ({len(session_key)} bytes): {session_key.hex()}")

        h = hashlib.sha1()
        h.update(self.account.encode())
        h.update(b"\x00" * 4)
        h.update(client_seed)
        h.update(self.seed)
        h.update(session_key)
        expected = h.digest()
        log.info(f"     server_seed={self.seed.hex()}")
        log.info(f"     expected_digest={expected.hex()}")
        log.info(f"     client_digest ={client_digest.hex()}")
        log.info(f"     MATCH={'YES' if expected == client_digest else 'NO'}")
        if expected != client_digest:
            log.warning(f"  World auth digest mismatch for {self.account}")
            self._send(SMSG_AUTH_RESPONSE, bytes([0x15]))
            return

        self.crypt = PacketCrypt(session_key)
        self.crypt.init()

        # Load GM level
        acct = get_account(self.db_path, self.account)
        self.gm_level = acct["gm_level"] if acct else 0

        # AUTH_OK = 0x0C
        buf = ByteBuffer()
        buf.uint8(0x0C); buf.uint32(0); buf.uint8(0); buf.uint32(0)
        log.info(f"  >> SMSG_AUTH_RESPONSE AUTH_OK ({len(buf.bytes())} bytes)")
        self._send(SMSG_AUTH_RESPONSE, buf.bytes())

        self.server.add_session(self.account, self)
        log.info(f"  World authenticated: {self.account} (GM={self.gm_level})")


# ------------------------------------------------------------------
# Server factory
# ------------------------------------------------------------------

def make_world_factory(server):
    def factory():
        return WorldSession(server)
    return factory


async def start_world_server(server):
    loop = asyncio.get_running_loop()
    srv = await loop.create_server(
        make_world_factory(server),
        config.WORLD_HOST, config.WORLD_PORT,
    )
    log.info(f"World server listening on {config.WORLD_HOST}:{config.WORLD_PORT}")
    return srv
