"""企业微信回调消息加解密（WXBizMsgCrypt 标准算法，不依赖官方 SDK）。

- AES-256-CBC：key = Base64 解码后的 EncodingAESKey（43 位字符，补 '=' 后解码为 32 字节），IV = key 前 16 字节
- 明文结构：16 字节随机串 + 4 字节消息长度（网络字节序）+ 消息体 + receiveid(corpid)
- 签名：SHA1(sort(token, timestamp, nonce, encrypt_msg) 拼接)
"""
import base64
import hashlib
import os
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..config import settings


class WecomCryptoError(Exception):
    """加解密/签名验证失败。"""


def _aes_key() -> bytes:
    key = base64.b64decode(settings.wecom_encoding_aes_key + "=")
    if len(key) != 32:
        raise WecomCryptoError("EncodingAESKey 解码后必须为 32 字节")
    return key


def _signature(token: str, timestamp: str, nonce: str, encrypt_msg: str) -> str:
    raw = "".join(sorted([token, timestamp, nonce, encrypt_msg]))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def check_signature(token: str, timestamp: str, nonce: str, encrypt_msg: str,
                    msg_signature: str) -> None:
    """签名不匹配抛 WecomCryptoError。"""
    if _signature(token, timestamp, nonce, encrypt_msg) != msg_signature:
        raise WecomCryptoError("msg_signature 校验失败")


def decrypt_message(encrypted_msg: str) -> tuple[str, str]:
    """解密密文，返回 (明文消息, receiveid)。"""
    try:
        key = _aes_key()
        cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
        decryptor = cipher.decryptor()
        plain = decryptor.update(base64.b64decode(encrypted_msg)) + decryptor.finalize()
    except Exception as exc:  # noqa: BLE001
        raise WecomCryptoError(f"AES 解密失败: {exc}") from exc

    # PKCS7 去填充
    pad = plain[-1]
    if pad < 1 or pad > 32:
        raise WecomCryptoError("PKCS7 填充非法")
    plain = plain[:-pad]

    if len(plain) < 20:
        raise WecomCryptoError("明文长度非法")
    msg_len = struct.unpack("!I", plain[16:20])[0]
    msg = plain[20:20 + msg_len].decode("utf-8")
    receiveid = plain[20 + msg_len:].decode("utf-8")
    return msg, receiveid


def encrypt_message(msg: str, receiveid: str | None = None) -> str:
    """加密明文（主要用于测试往返 / 被动回复加密）。"""
    key = _aes_key()
    receive = (receiveid if receiveid is not None else settings.wecom_corp_id).encode()
    raw = os.urandom(16) + struct.pack("!I", len(msg.encode())) + msg.encode() + receive
    # PKCS7 填充
    pad = 32 - (len(raw) % 32)
    raw += bytes([pad] * pad)
    cipher = Cipher(algorithms.AES(key), modes.CBC(key[:16]))
    encryptor = cipher.encryptor()
    return base64.b64encode(encryptor.update(raw) + encryptor.finalize()).decode()


def make_signature(token: str, timestamp: str, nonce: str, encrypt_msg: str) -> str:
    """生成签名（测试/响应用）。"""
    return _signature(token, timestamp, nonce, encrypt_msg)
