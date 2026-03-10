"""企微回调消息加解密

实现企微 callback 的签名验证和 AES-256-CBC 加解密。
协议文档: https://developer.work.weixin.qq.com/document/path/90968

加密方案:
- AES key = Base64Decode(EncodingAESKey + "=") (32 bytes)
- IV = AES key[:16]
- Plaintext = random(16) + msg_len(4 bytes big-endian) + msg + receive_id
- PKCS#7 padding to AES block size (32 bytes)
- Signature = SHA1(sort([token, timestamp, nonce, encrypted_msg]))
"""

from __future__ import annotations

import base64
import hashlib
import os
import struct
import xml.etree.ElementTree as ET
from typing import Tuple


def _sign(*parts: str) -> str:
    """SHA1 签名: sort → join → sha1"""
    sorted_parts = sorted(parts)
    return hashlib.sha1("".join(sorted_parts).encode("utf-8")).hexdigest()


def verify_signature(token: str, timestamp: str, nonce: str, echostr: str, signature: str) -> bool:
    """验证企微回调签名"""
    return _sign(token, timestamp, nonce, echostr) == signature


def _pkcs7_pad(data: bytes, block_size: int = 32) -> bytes:
    """PKCS#7 填充 (企微用 32 字节块)"""
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _pkcs7_unpad(data: bytes) -> bytes:
    """PKCS#7 去填充"""
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 32:
        raise ValueError(f"Invalid PKCS#7 padding: {pad_len}")
    if data[-pad_len:] != bytes([pad_len] * pad_len):
        raise ValueError("PKCS#7 padding verification failed")
    return data[:-pad_len]


def _get_aes_key(encoding_aes_key: str) -> bytes:
    """从 EncodingAESKey 派生 AES 密钥 (32 bytes)"""
    return base64.b64decode(encoding_aes_key + "=")


def decrypt(encoding_aes_key: str, encrypted: str) -> Tuple[str, str]:
    """解密企微消息

    Args:
        encoding_aes_key: 43 字符的 EncodingAESKey
        encrypted: Base64 编码的密文

    Returns:
        (明文消息, receive_id)
    """
    from Crypto.Cipher import AES

    key = _get_aes_key(encoding_aes_key)
    iv = key[:16]

    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = _pkcs7_unpad(cipher.decrypt(base64.b64decode(encrypted)))

    # 格式: random(16) + msg_len(4) + msg + receive_id
    msg_len = struct.unpack(">I", decrypted[16:20])[0]
    msg = decrypted[20:20 + msg_len].decode("utf-8")
    receive_id = decrypted[20 + msg_len:].decode("utf-8")

    return msg, receive_id


def encrypt(encoding_aes_key: str, msg: str, receive_id: str) -> str:
    """加密消息 (用于被动回复)

    Returns:
        Base64 编码的密文
    """
    from Crypto.Cipher import AES

    key = _get_aes_key(encoding_aes_key)
    iv = key[:16]

    msg_bytes = msg.encode("utf-8")
    receive_id_bytes = receive_id.encode("utf-8")

    plaintext = (
        os.urandom(16)
        + struct.pack(">I", len(msg_bytes))
        + msg_bytes
        + receive_id_bytes
    )
    padded = _pkcs7_pad(plaintext)

    cipher = AES.new(key, AES.MODE_CBC, iv)
    encrypted = cipher.encrypt(padded)
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_callback(
    token: str,
    encoding_aes_key: str,
    msg_signature: str,
    timestamp: str,
    nonce: str,
    post_data: str,
) -> str:
    """解密企微回调 POST 数据 (XML 格式)

    Returns:
        解密后的 XML 字符串
    """
    # 从 XML 中提取 Encrypt 字段
    root = ET.fromstring(post_data)
    encrypted = root.find("Encrypt")
    if encrypted is None or not encrypted.text:
        raise ValueError("Missing <Encrypt> in callback XML")
    encrypt_text = encrypted.text

    # 验证签名
    expected_sig = _sign(token, timestamp, nonce, encrypt_text)
    if expected_sig != msg_signature:
        raise ValueError(f"Signature mismatch: expected={expected_sig}, got={msg_signature}")

    # 解密
    msg, _ = decrypt(encoding_aes_key, encrypt_text)
    return msg


def encrypt_reply(
    token: str,
    encoding_aes_key: str,
    reply_msg: str,
    timestamp: str,
    nonce: str,
    receive_id: str,
) -> str:
    """加密回复消息，返回企微要求的 XML 格式"""
    encrypted = encrypt(encoding_aes_key, reply_msg, receive_id)
    signature = _sign(token, timestamp, nonce, encrypted)

    return (
        f"<xml>"
        f"<Encrypt><![CDATA[{encrypted}]]></Encrypt>"
        f"<MsgSignature><![CDATA[{signature}]]></MsgSignature>"
        f"<TimeStamp>{timestamp}</TimeStamp>"
        f"<Nonce><![CDATA[{nonce}]]></Nonce>"
        f"</xml>"
    )
