"""WoW 1.12 Authentication Server (port 3724)."""
import asyncio
import struct
import logging
from srp6 import SRP6Server, g, N, pad32
from database import get_account, set_session_key
from opcodes import (CMD_AUTH_LOGON_CHALLENGE, CMD_AUTH_LOGON_PROOF,
                     CMD_REALM_LIST)
import config

log = logging.getLogger("auth")

# Fixed CRC salt used by vanilla WoW for client binary verification
VERSION_CHALLENGE = bytes([
    0xBA, 0xA3, 0x1E, 0x99, 0xA0, 0x0B, 0x21, 0x57,
    0xFC, 0x37, 0x3F, 0xB3, 0x69, 0xCD, 0xD2, 0xF1,
])


class AuthSession(asyncio.Protocol):
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.transport = None
        self.buf = bytearray()
        self.srp = None
        self.username = None

    def connection_made(self, transport):
        self.transport = transport
        peer = transport.get_extra_info("peername")
        log.info(f"Auth connection from {peer}")

    def connection_lost(self, exc):
        log.info(f"Auth disconnected: {self.username} exc={exc}")

    def data_received(self, data: bytes):
        log.info(f"Auth recv ({len(data)} bytes): {data[:40].hex()}{'...' if len(data)>40 else ''}")
        self.buf += data
        while self.buf:
            if not self._dispatch():
                break

    def _dispatch(self) -> bool:
        if not self.buf:
            return False
        opcode = self.buf[0]
        if opcode == CMD_AUTH_LOGON_CHALLENGE:
            return self._handle_logon_challenge()
        elif opcode == CMD_AUTH_LOGON_PROOF:
            return self._handle_logon_proof()
        elif opcode == CMD_REALM_LIST:
            return self._handle_realm_list()
        else:
            log.warning(f"Unknown auth opcode: 0x{opcode:02X}")
            self.buf.clear()
            return False

    def _handle_logon_challenge(self) -> bool:
        # Header is 34 bytes before the username field
        if len(self.buf) < 34:
            return False
        username_len = self.buf[33]
        total = 34 + username_len
        if len(self.buf) < total:
            return False

        username = self.buf[34:34 + username_len].decode("utf-8", errors="replace")
        self.buf = self.buf[total:]
        self.username = username.upper()

        log.info(f"Logon challenge: {self.username}")

        account = get_account(self.db_path, self.username)
        if not account:
            log.warning(f"Unknown account: {self.username}")
            # AUTH_UNKNOWN_ACCOUNT = 4
            self.transport.write(bytes([CMD_AUTH_LOGON_CHALLENGE, 0, 4]))
            return True

        salt = bytes(account["salt"])
        verifier = bytes(account["verifier"])
        self.srp = SRP6Server(self.username, verifier, salt)

        # Build response
        resp = bytearray()
        resp += bytes([CMD_AUTH_LOGON_CHALLENGE, 0, 0])  # cmd, unk, error=0
        resp += self.srp.B_bytes                          # B (32 bytes LE)
        resp += bytes([1, g])                             # g_len, g
        resp += bytes([32]) + pad32(N)                    # N_len, N (32 bytes LE)
        resp += salt                                      # salt (32 bytes)
        resp += VERSION_CHALLENGE                         # CRC salt (16 bytes)
        resp += bytes([0])                                # security flags
        log.info(f"  >> Challenge response ({len(resp)} bytes)")
        log.info(f"     B={self.srp.B_bytes.hex()}")
        log.info(f"     salt={salt.hex()}")
        log.info(f"     resp hex: {resp.hex()}")
        self.transport.write(bytes(resp))
        return True

    def _handle_logon_proof(self) -> bool:
        # A(32) + M1(20) + crc_hash(20) + number_of_keys(1) + security_flags(1) = 74
        if len(self.buf) < 75:
            return False
        A_bytes  = bytes(self.buf[1:33])
        M1_bytes = bytes(self.buf[33:53])
        self.buf = self.buf[75:]

        if not self.srp:
            return True

        log.info(f"Logon proof from {self.username}: A={A_bytes[:8].hex()}... M1={M1_bytes.hex()}")
        try:
            K, M2 = self.srp.verify_proof(A_bytes, M1_bytes)
        except ValueError as e:
            log.warning(f"Auth proof failed for {self.username}: {e}")
            # INCORRECT_PASSWORD = 5 — vanilla error is just cmd + error (2 bytes)
            self.transport.write(bytes([CMD_AUTH_LOGON_PROOF, 5]))
            return True

        set_session_key(self.db_path, self.username, K)
        log.info(f"Authenticated: {self.username}  K={K.hex()}")

        # Vanilla proof response: cmd(1) + error(1) + M2(20) + survey_id(4) = 26 bytes
        resp = bytearray()
        resp += bytes([CMD_AUTH_LOGON_PROOF, 0])  # cmd, error=0
        resp += M2                                 # M2 (20 bytes)
        resp += struct.pack("<I", 0)               # survey_id
        self.transport.write(bytes(resp))
        return True

    def _handle_realm_list(self) -> bool:
        if len(self.buf) < 5:
            return False
        self.buf = self.buf[5:]
        log.info(f"Realm list request from {self.username}")

        # Build realm list
        realm_data = bytearray()
        realm_data += struct.pack("<I", 0)  # unk
        realm_data += bytes([1])            # number of realms (uint8 in 1.12)

        # One realm entry (vanilla format: no lock field, type is uint32)
        realm_data += struct.pack("<I", 0)          # type (0=Normal)
        realm_data += bytes([0])                    # flags (0x00=online)
        realm_data += config.REALM_NAME.encode() + b"\x00"
        realm_data += f"{config.REALM_IP}:{config.WORLD_PORT}".encode() + b"\x00"
        realm_data += struct.pack("<f", 0.0)        # population
        realm_data += bytes([0])                    # num chars
        realm_data += bytes([1])                    # timezone
        realm_data += bytes([0])                    # realm id

        realm_data += struct.pack("<H", 0x0002)     # footer (vanilla)

        # Outer packet: cmd(1) + size(2) + data
        pkt = bytearray()
        pkt += bytes([CMD_REALM_LIST])
        pkt += struct.pack("<H", len(realm_data))
        pkt += realm_data
        log.info(f"  Sending realm list: {config.REALM_NAME} @ {config.REALM_IP}:{config.WORLD_PORT}")
        log.info(f"  Realm packet ({len(pkt)} bytes): {pkt.hex()}")
        self.transport.write(bytes(pkt))
        return True


def make_auth_factory(db_path: str):
    def factory():
        return AuthSession(db_path)
    return factory


async def start_auth_server(db_path: str):
    loop = asyncio.get_running_loop()
    server = await loop.create_server(
        make_auth_factory(db_path),
        config.AUTH_HOST, config.AUTH_PORT,
    )
    log.info(f"Auth server listening on {config.AUTH_HOST}:{config.AUTH_PORT}")
    return server
