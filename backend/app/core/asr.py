"""语音转写（ASR）与图片理解（Vision）：OpenAI 兼容协议，未配置时降级返回 None。

- transcribe_media：RPA 语音消息 → 文本（走 /audio/transcriptions，如 Whisper / SenseVoice）
- describe_media：RPA 图片消息 → 一句话描述（走视觉模型 chat/completions）
两者失败均返回 None，由调用方降级为占位文本（[语音消息] / [图片消息]）。
"""
import base64
import logging
import os

import httpx

from ..config import settings
from ..database import SessionLocal
from ..models import RpaMedia

logger = logging.getLogger(__name__)


def _media_row(media_id: str) -> tuple[str, str] | None:
    """返回 (文件路径, MIME)，不存在返回 None。"""
    if not media_id:
        return None
    db = SessionLocal()
    try:
        row = db.get(RpaMedia, media_id)
        if row is None or not os.path.exists(row.path):
            return None
        return row.path, row.mime or "application/octet-stream"
    finally:
        db.close()


async def transcribe_media(media_id: str) -> str | None:
    """语音 → 文本。未配置 ASR 或失败返回 None。"""
    if not settings.asr_configured:
        return None
    found = _media_row(media_id)
    if found is None:
        return None
    path, mime = found
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            with open(path, "rb") as f:
                resp = await client.post(
                    f"{settings.asr_base_url.rstrip('/')}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {settings.asr_api_key}"},
                    files={"file": (os.path.basename(path), f, mime)},
                    data={"model": settings.asr_model},
                )
        data = resp.json()
        text = (data.get("text") or "").strip()
        return text or None
    except Exception:  # noqa: BLE001
        logger.exception("ASR 转写失败 media_id=%s", media_id)
        return None


async def describe_media(media_id: str) -> str | None:
    """图片 → 一句话描述（30 字内）。未配置视觉模型或失败返回 None。"""
    if not settings.vision_configured:
        return None
    found = _media_row(media_id)
    if found is None:
        return None
    path, mime = found
    base_url = (settings.vision_base_url or settings.llm_base_url).rstrip("/")
    api_key = settings.vision_api_key or settings.llm_api_key
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": settings.vision_model,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "这是客服对话中用户发来的图片。用一句话（30字以内）客观描述图片关键内容（如商品、截图文字、问题部位），只输出描述本身。"},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ]}],
                    "temperature": 0,
                    "max_tokens": 100,
                },
            )
        data = resp.json()
        text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        return text or None
    except Exception:  # noqa: BLE001
        logger.exception("图片描述失败 media_id=%s", media_id)
        return None
