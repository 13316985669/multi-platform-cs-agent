"""已核对回答的语义缓存。只保存知识库命中且没转人工、没查订单物流的回复。"""
import logging
import math
import threading

from . import embeddings

logger = logging.getLogger(__name__)

SIMILARITY_MIN = 0.92

_lock = threading.Lock()
_entries: list[dict] = []


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return dot / (left_norm * right_norm)


def _index_version() -> str:
    from .ingest import INDEX_VERSION
    return INDEX_VERSION


def invalidate_docs(doc_ids: list[int]) -> None:
    """文档重新入库或删除时，丢掉引用了这些文档的缓存。"""
    banned = set(doc_ids)
    if not banned:
        return
    with _lock:
        kept = [item for item in _entries if banned.isdisjoint(item["doc_ids"])]
        _entries[:] = kept


def invalidate_messages(messages: list[str]) -> None:
    """当前知识对不上这条缓存时丢掉它，避免下一轮再命中。"""
    target = list(messages)
    with _lock:
        _entries[:] = [item for item in _entries if item["messages"] != target]


def clear() -> None:
    with _lock:
        _entries.clear()


async def lookup(query: str, vector: list[float] | None = None) -> list[str] | None:
    if vector is None:
        vector = await embeddings.embed_query(query)
    if not vector:
        return None
    with _lock:
        best_score = 0.0
        best: list[str] | None = None
        for item in _entries:
            if item["version"] != _index_version():
                continue
            score = _cosine(vector, item["vector"])
            if score > best_score:
                best_score = score
                best = item["messages"]
    if best is not None and best_score >= SIMILARITY_MIN:
        return list(best)
    return None


async def store(query: str, messages: list[str], doc_ids: list[int]) -> None:
    if not messages or not doc_ids:
        return
    vector = await embeddings.embed_query(query)
    if not vector:
        return
    entry = {
        "vector": vector,
        "messages": list(messages),
        "doc_ids": list(doc_ids),
        "version": _index_version(),
    }
    with _lock:
        _entries.append(entry)
        if len(_entries) > 500:
            del _entries[: len(_entries) - 500]
    logger.info("已缓存核对过的回答，文档 %s", doc_ids)
