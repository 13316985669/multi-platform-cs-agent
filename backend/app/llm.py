"""共享 LLM 调用设施：AsyncOpenAI client 池 + token 用量入库。

按 (base_url, api_key) 缓存 client，进程内复用连接；timeout 在请求级传，
不锁死在 client 上。用量记录失败静默，绝不影响回复主流程。
"""
import logging

from openai import AsyncOpenAI

from .database import SessionLocal
from .models import LlmUsage

logger = logging.getLogger(__name__)

_clients: dict[tuple[str, str], AsyncOpenAI] = {}


def get_llm_client(base_url: str, api_key: str) -> AsyncOpenAI:
    """按 (base_url, api_key) 复用 client。请求级 timeout 由调用方传。"""
    key = (base_url or "", api_key or "")
    client = _clients.get(key)
    if client is None:
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        _clients[key] = client
    return client


def record_usage(conversation_id: int, model: str, usage) -> None:
    """记录 token 用量。conversation_id 为 0 表示后台辅助调用（改写/判定/小结等）。"""
    if usage is None:
        return
    try:
        db = SessionLocal()
        try:
            db.add(LlmUsage(
                conversation_id=conversation_id, model=model,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.warning("token 用量记录失败", exc_info=True)
