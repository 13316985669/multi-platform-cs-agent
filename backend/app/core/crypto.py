"""凭证加密：矩阵账号 token 等敏感字段的 Fernet 对称加解密。

key 派生：sha256(SECRET_KEY) → urlsafe_b64encode → Fernet key。
SECRET_KEY 变更会导致存量密文无法解密（账号需重新授权），请妥善保管 .env。
"""
import base64
import hashlib
import json
import logging
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..config import settings

logger = logging.getLogger(__name__)


def _fernet() -> Fernet:
    key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest())
    return Fernet(key)


def encrypt_json(data: dict[str, Any]) -> str:
    """dict → 加密字符串。空 dict 也正常加密（调用方自行决定是否存储）。"""
    raw = json.dumps(data, ensure_ascii=False).encode()
    return _fernet().encrypt(raw).decode()


def decrypt_json(token: str) -> dict[str, Any]:
    """加密字符串 → dict。密文为空或解密失败（key 变更/数据损坏）返回空 dict 并记日志。"""
    if not token:
        return {}
    try:
        raw = _fernet().decrypt(token.encode())
        data = json.loads(raw.decode())
        return data if isinstance(data, dict) else {}
    except (InvalidToken, json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("凭证解密失败（SECRET_KEY 是否变更？）: %s", type(exc).__name__)
        return {}
