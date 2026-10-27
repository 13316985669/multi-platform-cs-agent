"""Rerank 精排：bge-reranker（OpenAI 兼容的 /rerank 接口，硅基流动等支持）。未配置时跳过精排。"""
import logging

import httpx

from ..config import settings
from ..core import monitor

logger = logging.getLogger(__name__)


def parse_rerank_payload(data: object) -> list[dict] | None:
    """兼容 results/data 与 relevance_score/score。空结构返回 None，由上层退回混合检索。"""
    if not isinstance(data, dict):
        return None
    raw = data.get("results")
    if raw is None:
        raw = data.get("data")
    if isinstance(raw, dict):
        nested = raw.get("results")
        raw = nested if nested is not None else raw.get("data")
    if not isinstance(raw, list) or not raw:
        return None
    parsed: list[dict] = []
    for item in raw:
        if not isinstance(item, dict) or "index" not in item:
            continue
        score = item.get("relevance_score")
        if score is None:
            score = item.get("score", 0.0)
        try:
            score_f = float(score)
            index = int(item["index"])
        except (TypeError, ValueError):
            continue
        parsed.append({"index": index, "score": score_f})
    if not parsed:
        return None
    return sorted(parsed, key=lambda x: x["score"], reverse=True)


async def rerank(query: str, documents: list[str], top_n: int = 3) -> list[dict] | None:
    """对候选文档精排。返回 [{index, score}] 按分数降序；未配置、失败或空结果返回 None。"""
    if not settings.rerank_configured or not documents:
        return None
    last_error: Exception | None = None
    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{settings.rerank_base_url.rstrip('/')}/rerank",
                    headers={"Authorization": f"Bearer {settings.rerank_api_key}"},
                    json={
                        "model": settings.rerank_model,
                        "query": query,
                        "documents": documents,
                        "top_n": min(top_n, len(documents)),
                    },
                )
                if resp.status_code >= 400:
                    raise RuntimeError(f"rerank http {resp.status_code}")
                parsed = parse_rerank_payload(resp.json())
            if not parsed:
                logger.info("Rerank 返回空结果，第 %d 次", attempt + 1)
                continue
            return parsed
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("Rerank 调用失败，第 %d 次", attempt + 1)
    logger.error("Rerank 两次都不可用，退回混合检索: %s", last_error)
    monitor.record("rerank_failure", str(last_error or "empty")[:150])
    return None
