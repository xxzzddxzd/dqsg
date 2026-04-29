import os
import hmac
import hashlib
import zlib
import base64

from Crypto.Cipher import AES, PKCS1_OAEP
from Crypto.PublicKey import RSA
from Crypto.Util.Padding import pad, unpad

STARTUP_KEY = bytes.fromhex(
    "5d0c9d1a34076488136525cc60e39581f8a9134374d2983400a50a6879c82498"
)

RSA_XML_MODULUS = "poGCjm3VkroQY425+eih2vgbWZ6sDno36CtqItVMYB+wkMR+9TFWm44/L0eAVnWwbeV/G3DRSyjRfp9dvPzBwCo3Jq5j3AT1xerZ26npDA5xHuKFYfoO322wyHxq6C7vb2Wdf8vBFJ/7n8iSXeQ4pzn0ZqntdC/gs4TuW6i2Sxk="
RSA_XML_EXPONENT = "AQAB"

BASE_URL = os.environ.get("DQSG_BASE_URL", "https://api.gl.smgr.klabgames.net/ep01004")

_modulus = int.from_bytes(base64.b64decode(RSA_XML_MODULUS), "big")
_exponent = int.from_bytes(base64.b64decode(RSA_XML_EXPONENT), "big")
RSA_KEY = RSA.construct((_modulus, _exponent))


def encrypt_request(key: bytes, request_path: str, plaintext: bytes) -> bytes:
    compressed = zlib.compress(plaintext, level=6)[2:-4]
    iv = os.urandom(16)
    cipher = AES.new(key, AES.MODE_CBC, iv)
    ct = cipher.encrypt(pad(compressed, 16))
    hmac_input = iv + ct + request_path.encode("utf-8")
    mac = hmac.new(key, hmac_input, hashlib.sha1).digest()
    return iv + ct + mac


def decrypt_response(key: bytes, request_path: str, data: bytes) -> bytes:
    iv = data[:16]
    mac = data[-20:]
    ct = data[16:-20]
    hmac_input = iv + ct + request_path.encode("utf-8")
    expected_mac = hmac.new(key, hmac_input, hashlib.sha1).digest()
    if mac != expected_mac:
        raise ValueError(f"HMAC mismatch: got {mac.hex()}, expected {expected_mac.hex()}")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    compressed = unpad(cipher.decrypt(ct), 16)
    if not compressed:
        return b""
    return zlib.decompress(compressed, -15)


def xor_bytes(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def rsa_public_encrypt(data: bytes) -> bytes:
    from Crypto.Hash import SHA1
    cipher = PKCS1_OAEP.new(RSA_KEY, hashAlgo=SHA1)
    return cipher.encrypt(data)
