"""内容查重：SimHash 64 位指纹 + 汉明距离相似度（纯 Python，零依赖）。

用途：
- 生成落库时计算 dup_report（同平台近 90 天版本对比），版本卡片展示查重 badge
- 发布前软拦截：相似度 > 0.9 需显式 force 确认（矩阵账号同质化是限流主因之一）

算法：中文按字符二元组（bigram）分词，英文按单词；token 哈希加权累加生成指纹。
相似度 = 1 - hamming_distance / 64。0.9 ≈ 仅差 6 bit，基本视为同一内容。
"""
import hashlib
import logging
import re
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import ContentVersion

logger = logging.getLogger(__name__)

BITS = 64
COMPARE_DAYS = 90          # 对比窗口：同平台近 90 天版本
COMPARE_LIMIT = 500        # 单次最多对比版本数
SOFT_BLOCK_THRESHOLD = 0.9  # 发布软拦截阈值

_WORD_RE = re.compile(r"[a-zA-Z0-9]+")


def _tokenize(text: str) -> list[str]:
    """中文 bigram + 英文单词混合分词（标点/空白丢弃）。"""
    tokens: list[str] = []
    # 英文/数字词
    for m in _WORD_RE.finditer(text):
        tokens.append(m.group(0).lower())
    # 中文 bigram
    han = re.sub(r"[^一-鿿]", "", text)
    tokens.extend(han[i:i + 2] for i in range(len(han) - 1))
    return tokens


def simhash(text: str) -> int:
    """计算 64 位 SimHash 指纹。空文本返回 0。"""
    tokens = _tokenize(text)
    if not tokens:
        return 0
    vector = [0] * BITS
    for token in tokens:
        digest = int.from_bytes(
            hashlib.md5(token.encode("utf-8")).digest()[:8], "big")
        for i in range(BITS):
            vector[i] += 1 if (digest >> i) & 1 else -1
    fingerprint = 0
    for i in range(BITS):
        if vector[i] > 0:
            fingerprint |= 1 << i
    return fingerprint


def similarity(fp1: int, fp2: int) -> float:
    """两个指纹的相似度（1 - 汉明距离/64）。"""
    distance = bin(fp1 ^ fp2).count("1")
    return 1.0 - distance / BITS


def version_text(version: ContentVersion) -> str:
    """参与查重的文本：标题 + 正文（脚本/标签波动大，不计入）。"""
    return f"{version.title or ''}\n{version.body or ''}"


def check_duplicate(db: Session, text: str, platform: str,
                    exclude_version_id: int = 0, exclude_item_id: int = 0) -> dict:
    """与同平台近期版本对比，返回查重报告。

    exclude_item_id：排除同选题的版本——一稿多版的多个变体是刻意备选（Phase 7），
    不应互相触发去重软拦截。

    报告：{"max_similarity": 0-1, "similar_version_id": int, "checked_at": iso, "compared": n}
    """
    report = {"max_similarity": 0.0, "similar_version_id": 0,
              "checked_at": datetime.utcnow().isoformat(), "compared": 0}
    fp = simhash(text)
    if fp == 0:
        return report

    since = datetime.utcnow() - timedelta(days=COMPARE_DAYS)
    q = db.query(ContentVersion).filter(
        ContentVersion.platform == platform,
        ContentVersion.created_at >= since)
    if exclude_version_id:
        q = q.filter(ContentVersion.id != exclude_version_id)
    if exclude_item_id:
        q = q.filter(ContentVersion.content_item_id != exclude_item_id)
    candidates = q.order_by(ContentVersion.id.desc()).limit(COMPARE_LIMIT).all()

    for other in candidates:
        other_fp = simhash(version_text(other))
        if other_fp == 0:
            continue
        sim = similarity(fp, other_fp)
        report["compared"] += 1
        if sim > report["max_similarity"]:
            report["max_similarity"] = round(sim, 4)
            report["similar_version_id"] = other.id
    return report
