"""WoW 1.12 SRP6 implementation."""
import os
import hashlib
import logging

log = logging.getLogger("srp6")

# WoW SRP6 parameters
N = int("894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7", 16)
g = 7


def sha1(*parts: bytes) -> bytes:
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return h.digest()


def H(*parts: bytes) -> int:
    return int.from_bytes(sha1(*parts), "little")


def pad32(n: int) -> bytes:
    return n.to_bytes(32, "little")


def make_verifier(username: str, password: str, salt: bytes) -> bytes:
    """Create SRP6 verifier from credentials."""
    username = username.upper()
    password = password.upper()
    x = H(salt, sha1((username + ":" + password).encode()))
    v = pow(g, x, N)
    return pad32(v)


def make_salt() -> bytes:
    return os.urandom(32)


class SRP6Server:
    def __init__(self, username: str, v_bytes: bytes, salt: bytes):
        self.username = username.upper()
        self.salt = salt
        self.v = int.from_bytes(v_bytes, "little")

        # Generate b and B  (k=3 per WoW SRP6 spec)
        self._b = int.from_bytes(os.urandom(19), "little") % N
        gmod = pow(g, self._b, N)
        self.B = (3 * self.v + gmod) % N
        self.B_bytes = pad32(self.B)

    def verify_proof(self, A_bytes: bytes, M1_bytes: bytes):
        """Verify client proof. Returns (session_key, M2) or raises."""
        A = int.from_bytes(A_bytes, "little")
        if A % N == 0:
            raise ValueError("Invalid A")

        # u = H(A, B)
        u = H(A_bytes, self.B_bytes)

        # S = (A * v^u)^b mod N
        S = pow(A * pow(self.v, u, N) % N, self._b, N)
        S_bytes = pad32(S)

        # Interleaved hash to produce K (40 bytes)
        # Strip leading zero bytes (from LSB end of LE array) keeping even length
        t = S_bytes
        length = len(t)
        for idx, byte in enumerate(t):
            if byte != 0 and (length - idx) % 2 == 0:
                if idx != 0:
                    t = t[idx:]
                break
        even = t[0::2]
        odd  = t[1::2]
        K_bytes = bytearray(40)
        e_hash = sha1(even)
        o_hash = sha1(odd)
        for i in range(20):
            K_bytes[i * 2]     = e_hash[i]
            K_bytes[i * 2 + 1] = o_hash[i]
        self.K = bytes(K_bytes)

        # Build expected M1
        Nh = sha1(pad32(N))
        gh = sha1(bytes([g]))
        ng_xor = bytes(a ^ b for a, b in zip(Nh, gh))
        expected_M1 = sha1(
            ng_xor,
            sha1(self.username.encode()),
            self.salt,
            A_bytes,
            self.B_bytes,
            self.K,
        )

        if expected_M1 != M1_bytes:
            log.debug("SRP6 M1 mismatch for %s:", self.username)
            log.debug("  received : %s", M1_bytes.hex())
            log.debug("  expected : %s", expected_M1.hex())
            log.debug("  A        : %s", A_bytes.hex())
            log.debug("  B        : %s", self.B_bytes.hex())
            log.debug("  salt     : %s", self.salt.hex())
            log.debug("  K        : %s", self.K.hex())
            raise ValueError("SRP6 proof mismatch")

        # M2
        self.M2 = sha1(A_bytes, M1_bytes, self.K)
        return self.K, self.M2
