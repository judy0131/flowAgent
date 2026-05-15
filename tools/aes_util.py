
from Crypto.Cipher import AES
import base64

# pip install pycryptodome

def pkcs7_pad(data: bytes, bl: int = 16) -> bytes:
    pad = bl - len(data) % bl
    return data + bytes([pad] * pad)

def aes_encrypt_cbc(plaintext: str, key: str, iv: str) -> str:
    """
    与前端 CryptoJS.AES.encrypt(plaintext, key, {iv: iv, mode: CryptoJS.mode.CBC, padding: CryptoJS.pad.Pkcs7}) 结果一致
    """
    key_bytes = key.encode('utf-8')      # 16 字节
    iv_bytes  = iv.encode('utf-8')       # 16 字节
    padded = pkcs7_pad(plaintext.encode('utf-8'))

    cipher = AES.new(key_bytes, AES.MODE_CBC, iv_bytes)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode('utf-8')