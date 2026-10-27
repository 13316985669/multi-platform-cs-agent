"""Embedding：OpenAI 兼容接口（默认硅基流动 bge-m3）。未配置时返回 None，上层降级纯 BM25。"""
import logging

from openai import AsyncOpenAI

from ..config import settings
from ..core import monitor

logger = logging.getLogger(__name__)


def _client() -> AsyncOpenAI | None:
    if not settings.embedding_configured:
        return None
    return AsyncOpenAI(base_url=settings.embedding_base_url, api_key=settings.embedding_api_key)


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """批量向量化。未配置或调用失败返回 None（降级信号）。"""
    client = _client()
    if client is None:
        return None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            resp = await client.embeddings.create(
                model=settings.embedding_model, input=texts, timeout=15.0,
            )
            return [item.embedding for item in resp.data]
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("Embedding 调用失败，第 %d 次", attempt + 1)
    logger.error("Embedding 两次都失败，降级纯 BM25: %s", last_error)
    monitor.record("embedding_failure", str(last_error or "")[:150])
    return None


async def embed_query(text: str) -> list[float] | None:
    result = await embed_texts([text])
    return result[0] if result else None
