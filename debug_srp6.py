"""
Diagnostic script for WoW 1.12 SRP6 auth mismatch.
Uses actual values captured from the debug log.
Run: python3 debug_srp6.py
"""
import hashlib

# Known WoW SRP6 constants
N = int("894B645E89E1535BBDAD5B8B290650530801B18EBFBF5E8FAB3C82872A3E9BB7", 16)
g = 7

# Values from the latest debug session (screenshot 3, H="little")
A_hex    = "64e62314086f82178d44d440c9d700d99b6581e48778865337b0cd0796415d77"
B_hex    = "31a487c1faa74c36fe2f2349cbbf4073eebf2462748617aa1f6d66e8850bb42a"
salt_hex = "cc79fbff12acb5bd31535f89e9408df87ddff0b9c9348768dd417ede772437e3"
K_server_hex = "330ae81db41f89886f8aaf489e3bd116810a85d086dec3e928c167bd2cda6fbf4d6735306fcaac3f"
received_M1_hex = "a50b973aa1ec5c01f47d5be48b9632b1513ba053"
USERNAME = "FUCK"
PASSWORD = "FUCK"

A_bytes  = bytes.fromhex(A_hex)
B_bytes  = bytes.fromhex(B_hex)
salt     = bytes.fromhex(salt_hex)
K_server = bytes.fromhex(K_server_hex)
received_M1 = bytes.fromhex(received_M1_hex)


def sha1(*parts):
    h = hashlib.sha1()
    for p in parts:
        h.update(p)
    return h.digest()


def pad32(n):
    return n.to_bytes(32, "little")


def compute_M1(K, A, B, salt, username):
    Nh = sha1(pad32(N))
    gh = sha1(bytes([g]))
    ng_xor = bytes(a ^ b for a, b in zip(Nh, gh))
    return sha1(ng_xor, sha1(username.upper().encode()), salt, A, B, K)


def H_big(*parts):
    return int.from_bytes(sha1(*parts), "big")


def H_little(*parts):
    return int.from_bytes(sha1(*parts), "little")


print("=" * 70)
print("STEP 1: Does K_server produce the received M1?")
print("(If yes → K is correct, M1 formula is wrong)")
print("(If no  → K is WRONG, S computation differs)")
print("=" * 70)
computed = compute_M1(K_server, A_bytes, B_bytes, salt, USERNAME)
print(f"  K_server :      {K_server.hex()}")
print(f"  M1 with K_srv : {computed.hex()}")
print(f"  received M1 :   {received_M1.hex()}")
print(f"  MATCH: {'YES ✓' if computed == received_M1 else 'NO ✗ → K is wrong'}")

print()
print("=" * 70)
print("STEP 2: Verify SRP6 round-trip for each endianness + k combo")
print("(This tests internal consistency, not WoW client compatibility)")
print("=" * 70)

import os

for endian in ("little", "big"):
    H = H_little if endian == "little" else H_big
    for k in (1, 3):
        # Create verifier from password+salt
        x = H(salt, sha1((USERNAME + ":" + PASSWORD).encode()))
        v = pow(g, x, N)

        # Server generates b
        b = int.from_bytes(os.urandom(19), "little") % N
        gmod = pow(g, b, N)
        B_sim = (k * v + gmod) % N
        B_sim_bytes = pad32(B_sim)

        # Client generates a
        a = int.from_bytes(os.urandom(19), "little") % N
        A_sim = pow(g, a, N)
        A_sim_bytes = pad32(A_sim)

        # Shared u
        u = H(A_sim_bytes, B_sim_bytes)

        # Client S: (B - k*g^x)^(a + u*x)
        gx = pow(g, x, N)
        S_client = pow((B_sim - k * gx) % N, (a + u * x), N)

        # Server S: (A * v^u)^b
        S_server = pow(A_sim * pow(v, u, N) % N, b, N)

        # K for each
        def make_K(S):
            S_bytes = pad32(S)
            even = bytes(S_bytes[i] for i in range(0, 32, 2))
            odd  = bytes(S_bytes[i] for i in range(1, 32, 2))
            K_b = bytearray(40)
            eh = sha1(even); oh = sha1(odd)
            for i in range(20):
                K_b[i*2] = eh[i]; K_b[i*2+1] = oh[i]
            return bytes(K_b)

        K_c = make_K(S_client)
        K_s = make_K(S_server)
        M1_c = compute_M1(K_c, A_sim_bytes, B_sim_bytes, salt, USERNAME)
        M1_s = compute_M1(K_s, A_sim_bytes, B_sim_bytes, salt, USERNAME)
        ok = (K_c == K_s) and (M1_c == M1_s)
        print(f"  H={endian:6s} k={k}: {'PASS ✓' if ok else 'FAIL ✗'}", end="")
        if not ok:
            print(f"  S_match={S_client==S_server} K_match={K_c==K_s}", end="")
        print()

print()
print("=" * 70)
print("STEP 3: What x,v does our server compute for FUCK/FUCK?")
print("(Cross-check this with a working WoW server's values)")
print("=" * 70)
for endian in ("little", "big"):
    H = H_little if endian == "little" else H_big
    x = H(salt, sha1((USERNAME + ":" + PASSWORD).encode()))
    v = pow(g, x, N)
    v_bytes = pad32(v)
    print(f"  H={endian:6s}: x (20 bytes) = {sha1(salt, sha1((USERNAME+':'+PASSWORD).encode())).hex()}")
    print(f"           x int (big)  = {int.from_bytes(sha1(salt, sha1((USERNAME+':'+PASSWORD).encode())), 'big'):064x}")
    print(f"           x int (lil)  = {int.from_bytes(sha1(salt, sha1((USERNAME+':'+PASSWORD).encode())), 'little'):064x}")
    print(f"           v (stored LE)= {v_bytes.hex()}")
    print()

print("=" * 70)
print("STEP 4: u computation with actual A/B from debug")
print("=" * 70)
u_raw = sha1(A_bytes, B_bytes)
print(f"  SHA1(A||B) raw: {u_raw.hex()}")
print(f"  u as big-endian int:    {int.from_bytes(u_raw,'big'):040x}")
print(f"  u as little-endian int: {int.from_bytes(u_raw,'little'):040x}")
