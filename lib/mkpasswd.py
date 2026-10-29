"""Produce a SHA-512 crypt hash, so every bake can use the same password.

    mkpasswd.py <password>

The sandbox's python has no `crypt` module, and the preseed and autoinstall files in
this repository already express passwords as $6$ strings. Rather than depend on
`mkpasswd` or `openssl passwd -6` being installed in each place this runs, the algorithm
is implemented here - it is fixed, published, and 50 lines.

    $6$<salt>$<hash>

where the salt is up to 16 characters from the crypt base64 alphabet and the digest is
the 5000-round SHA-512 construction from Ulrich Drepper's specification.
"""

import hashlib
import os
import sys

ALPHABET = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# Which of the 64 digest bytes go into each group of four output characters. This table
# is the one difference from a plain base64: SHA-512 crypt interleaves the three
# 21-byte blocks, so the order is fixed by the specification rather than by the data.
ORDER = [
    (0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4),
    (47, 5, 26), (6, 27, 48), (28, 49, 7), (50, 8, 29), (9, 30, 51),
    (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13), (56, 14, 35),
    (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19),
    (62, 20, 41),
]


def sha512_crypt(password: str, salt: str | None = None, rounds: int = 5000) -> str:
    if salt is None:
        salt = "".join(ALPHABET[b % len(ALPHABET)] for b in os.urandom(16))
    salt = salt[:16]

    p = password.encode()
    s = salt.encode()

    # Step 1-8 of the specification: mix password and salt, then repeatedly fold the
    # digest in, driven by the bit pattern of the password length.
    b = hashlib.sha512(p + s + p).digest()
    a = hashlib.sha512(p + s)
    n = len(p)
    while n > 64:
        a.update(b)
        n -= 64
    a.update(b[:n])
    n = len(p)
    while n:
        a.update(b if n & 1 else p)
        n >>= 1
    a = a.digest()

    # Per-round sequences.
    dp = hashlib.sha512(p * len(p)).digest()
    p_seq = (dp * (len(p) // 64 + 1))[:len(p)]
    ds = hashlib.sha512(s * (16 + a[0])).digest()
    s_seq = (ds * (len(s) // 64 + 1))[:len(s)]

    c = a
    for i in range(rounds):
        h = hashlib.sha512()
        h.update(p_seq if i & 1 else c)
        if i % 3:
            h.update(s_seq)
        if i % 7:
            h.update(p_seq)
        h.update(c if i & 1 else p_seq)
        c = h.digest()

    out = []
    for x, y, z in ORDER:
        v = (c[x] << 16) | (c[y] << 8) | c[z]
        for _ in range(4):
            out.append(ALPHABET[v & 0x3F])
            v >>= 6
    v = c[63]
    for _ in range(2):
        out.append(ALPHABET[v & 0x3F])
        v >>= 6

    return "$6$%s$%s" % (salt, "".join(out))


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    print(sha512_crypt(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
