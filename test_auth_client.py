"""
Python WoW 1.12 auth client test.
Connects to the running auth server and performs a full SRP6 exchange.
If this passes but the real WoW.exe fails, the WoW client uses different SRP6.

Usage: python3 test_auth_client.py <username> <password>
       python3 test_auth_client.py FUCK FUCK
"""
import socket
import sys
import os
import hashlib
import struct

# WoW SRP6 constants (must match server)
N = int("894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7", 16)
g = 7
AUTH_HOST = "127.0.0.1"
AUTH_PORT = 3724


def sha1(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return h.digest()


def H(*parts):
    """Same H function as server — matches srp6.py."""
    return int.from_bytes(sha1(*parts), "little")


def pad32(n):
    return n.to_bytes(32, "little")


def interleave_hash(S):
    S_bytes = pad32(S)
    even = bytes(S_bytes[i] for i in range(0, 32, 2))
    odd  = bytes(S_bytes[i] for i in range(1, 32, 2))
    K = bytearray(40)
    eh = sha1(even)
    oh = sha1(odd)
    for i in range(20):
        K[i * 2]     = eh[i]
        K[i * 2 + 1] = oh[i]
    return bytes(K)


def compute_M1(username, salt, A_bytes, B_bytes, K):
    Nh     = sha1(pad32(N))
    gh     = sha1(bytes([g]))
    ng_xor = bytes(a ^ b for a, b in zip(Nh, gh))
    return sha1(ng_xor, sha1(username.upper().encode()), salt, A_bytes, B_bytes, K)


def build_challenge_packet(username):
    """Build CMD_AUTH_LOGON_CHALLENGE client packet."""
    I = username.upper().encode()
    # Fixed fields (30 bytes) + username
    pkt = bytearray()
    pkt += bytes([0x00])          # cmd
    pkt += bytes([0x08])          # unk (protocol version)
    size = 30 + len(I)
    pkt += struct.pack("<H", size)  # remaining size
    pkt += b"WoW "                  # game name
    pkt += bytes([1, 12, 1])        # version 1.12.1
    pkt += struct.pack("<H", 5875)  # build
    pkt += b"x86 "                  # platform
    pkt += b"Win "                  # os
    pkt += b"enUS"                  # locale
    pkt += struct.pack("<I", 0)     # timezone bias
    pkt += bytes([127, 0, 0, 1])    # IP
    pkt += bytes([len(I)])          # username length
    pkt += I                        # username
    return bytes(pkt)


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 test_auth_client.py <username> <password>")
        sys.exit(1)

    username = sys.argv[1].upper()
    password = sys.argv[2].upper()

    print(f"[*] Connecting to {AUTH_HOST}:{AUTH_PORT}")
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(5)
    try:
        s.connect((AUTH_HOST, AUTH_PORT))
    except Exception as e:
        print(f"[!] Connection failed: {e}")
        sys.exit(1)

    # ── 1. Send challenge ──────────────────────────────────────────────────
    pkt = build_challenge_packet(username)
    print(f"[>] Sending challenge ({len(pkt)} bytes)")
    s.sendall(pkt)

    # ── 2. Receive challenge response ──────────────────────────────────────
    data = s.recv(1024)
    if len(data) < 119:
        print(f"[!] Challenge response too short: {len(data)} bytes = {data.hex()}")
        s.close()
        sys.exit(1)

    if data[0] != 0x00:
        print(f"[!] Wrong cmd in response: 0x{data[0]:02X}")
    if data[2] != 0:
        print(f"[!] Auth error: {data[2]}")
        s.close()
        sys.exit(1)

    B_bytes  = data[3:35]    # server public key B (32 bytes LE)
    g_len    = data[35]
    g_val    = data[36]
    N_len    = data[37]
    N_bytes  = data[38:70]   # N (32 bytes LE)
    salt     = data[70:102]  # salt (32 bytes)

    B_int    = int.from_bytes(B_bytes, "little")
    N_int    = int.from_bytes(N_bytes, "little")

    print(f"[<] Got challenge: g={g_val} N_len={N_len}")
    print(f"    B    = {B_bytes.hex()}")
    print(f"    salt = {salt.hex()}")

    assert N_int == N, "Server sent different N!"
    assert g_val == g, "Server sent different g!"

    # ── 3. Client-side SRP6 ───────────────────────────────────────────────
    # Generate client private key a
    a = int.from_bytes(os.urandom(19), "little") % N
    A = pow(g, a, N)
    A_bytes = pad32(A)

    # Compute x from password and received salt
    x = H(salt, sha1((username + ":" + password).encode()))

    # Compute u from A and B
    u = H(A_bytes, B_bytes)

    # Client S = (B - k*g^x)^(a + u*x) mod N  with k=3
    gx = pow(g, x, N)
    S_client = pow((B_int - 3 * gx) % N, (a + u * x), N)

    # K (session key)
    K = interleave_hash(S_client)

    # M1
    M1 = compute_M1(username, salt, A_bytes, B_bytes, K)

    print(f"[*] Client SRP6:")
    print(f"    A    = {A_bytes.hex()}")
    print(f"    u    = {u:040x}")
    print(f"    S    = {S_client:064x}")
    print(f"    K    = {K.hex()}")
    print(f"    M1   = {M1.hex()}")

    # Build proof packet
    # CRC hash: SHA1 of client binary (send zeros)
    crc_hash = bytes(20)
    proof = bytearray()
    proof += bytes([0x01])   # CMD_AUTH_LOGON_PROOF
    proof += A_bytes         # A (32 bytes)
    proof += M1              # M1 (20 bytes)
    proof += crc_hash        # crc_hash (20 bytes)
    proof += bytes([0])      # num_keys
    proof += bytes([0])      # security_flags

    # ── 4. Send proof ─────────────────────────────────────────────────────
    print(f"[>] Sending proof ({len(proof)} bytes)")
    s.sendall(bytes(proof))

    # ── 5. Receive proof response ─────────────────────────────────────────
    resp = s.recv(1024)
    print(f"[<] Got proof response ({len(resp)} bytes): {resp.hex()}")

    if len(resp) < 2:
        print("[!] Response too short")
        s.close()
        sys.exit(1)

    cmd, error = resp[0], resp[1]
    if cmd != 0x01:
        print(f"[!] Wrong cmd: 0x{cmd:02X}")
    elif error == 0:
        M2_server = resp[2:22]
        # Verify M2
        M2_expected = sha1(A_bytes, M1, K)
        print(f"    M2 from server   = {M2_server.hex()}")
        print(f"    M2 we expected   = {M2_expected.hex()}")
        if M2_server == M2_expected:
            print("[✓] AUTH SUCCESS — Python client authenticated!")
        else:
            print("[!] M2 mismatch — server bug in M2 computation")
    else:
        print(f"[✗] Auth FAILED — server returned error code: {error}")
        print("    → Server SRP6 and Python client SRP6 disagree")
        print("    → The WoW client must use a DIFFERENT SRP6 formula")

    s.close()


if __name__ == "__main__":
    main()
