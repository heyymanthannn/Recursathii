"""Light symmetric encryption for stored gateway credentials.

HMAC-CTR keystream XOR with a random 16-byte prefix per value, keyed on APP_SECRET.
Keeps Razorpay keys out of plaintext in the database file. APP_SECRET must be set
in production; the dev default exists only so tests run.
"""
import base64, hashlib, hmac, os

SECRET = os.environ.get("APP_SECRET", "dev-secret-change-me").encode()

def _keystream(nonce, n):
    out = b""
    counter = 0
    while len(out) < n:
        out += hmac.new(SECRET, nonce + str(counter).encode(), hashlib.sha256).digest()
        counter += 1
    return out[:n]

def encrypt(s: str) -> str:
    if not s:
        return ""
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(s.encode(), _keystream(nonce, len(s))))
    return base64.b64encode(nonce + ct).decode()

def decrypt(b64: str) -> str:
    if not b64:
        return ""
    raw = base64.b64decode(b64)
    nonce, ct = raw[:16], raw[16:]
    return bytes(a ^ b for a, b in zip(ct, _keystream(nonce, len(ct)))).decode()
