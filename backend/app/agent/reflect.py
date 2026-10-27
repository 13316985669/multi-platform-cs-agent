"""生成后的一次证据核对。只在有风险且还有时间时调用。失败就沿用规则校验结果。"""
import json
import logging

from openai import AsyncOpenAI

from ..config import settings
from ..llm import get_llm_client, record_usage

logger = logging.getLogger(__name__)

_HEAD = (
    "你在核对客服回复是否都能在证据里对上。"
    "不能补充证据里没有的价格、政策、时效、名单。"
    "只输出 JSON："
)
_SCHEMA = '{"action":"keep","messages":["保留或改写后的句子"],"issues":["对不上的点"]}'


async def review_reply(messages: list[str], evidence_text: str, *, timeout: float) -> dict | None:
    """返回 {action, messages, issues}。action 为 keep、revise 或 handoff。"""
    if not settings.llm_configured or not messages:
        return None
    prompt = (
        _HEAD + _SCHEMA
        + "\naction=keep：句子都能对上，messages 原样返回。"
        + "\naction=revise：删掉或改成证据里有的说法，messages 只留改后的句子。"
        + "\naction=handoff：事实对不上且没法只靠证据改，messages 留一句请同事确认。"
        + f"\n\n证据：\n{evidence_text or '（无）'}"
        + "\n\n待发句子：\n" + "\n".join(messages)
    )
    try:
        client = get_llm_client(settings.llm_base_url, settings.llm_api_key)
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=300,
            response_format={"type": "json_object"},
            timeout=max(1.0, timeout),
        )
        record_usage(0, settings.llm_model, getattr(resp, "usage", None))
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001
        logger.exception("回复自检失败，沿用规则校验")
        return None
    action = data.get("action")
    if action not in ("keep", "revise", "handoff"):
        return None
    raw_messages = data.get("messages") if isinstance(data.get("messages"), list) else messages
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    return {
        "action": action,
        "messages": [str(item).strip() for item in raw_messages if str(item).strip()],
        "issues": [str(item) for item in issues][:5],
    }
