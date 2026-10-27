"""从已标不佳和未命中问题起草评测案例。只返回 JSON，不写入 cases.json。"""
import json
import logging

from openai import AsyncOpenAI
from sqlalchemy.orm import Session

from .config import settings
from .models import Conversation, Message, MissedQuestion

logger = logging.getLogger(__name__)


def _retrieval_knowledge(extra: dict | None) -> tuple[str, bool]:
    retrieval = (extra or {}).get("retrieval") if isinstance(extra, dict) else None
    if not isinstance(retrieval, dict):
        return "", True
    selected = retrieval.get("selected") or []
    if not selected:
        return "", True
    blocks = []
    for i, item in enumerate(selected):
        if not isinstance(item, dict):
            continue
        source = item.get("source") or ""
        blocks.append(f"【资料{i + 1}】（来源：{source}）")
    if not blocks:
        return "", True
    return "\n\n".join(blocks), False


async def _suggest_expect(user: str, bad_reply: str, note: str) -> dict:
    if not settings.llm_configured:
        return {"handoff": False}
    prompt = (
        "根据客服不佳回复，给评测期望一个 JSON。"
        "只输出 {\"handoff\": false, \"reply_not_contains\": []}。"
        "reply_not_contains 放不该出现的短词，没有就空数组。\n"
        f"用户：{user}\n不佳回复：{bad_reply}\n备注：{note}"
    )
    try:
        client = AsyncOpenAI(
            base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=8.0,
        )
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001
        logger.exception("评测期望建议失败")
        return {"handoff": False}
    expect = {"handoff": bool(data.get("handoff"))}
    banned = data.get("reply_not_contains")
    if isinstance(banned, list) and banned:
        expect["reply_not_contains"] = [str(item) for item in banned[:5]]
    return expect


async def build_eval_drafts(db: Session) -> list[dict]:
    drafts: list[dict] = []
    bad_rows = (
        db.query(Message)
        .filter(Message.bad_case.is_(True))
        .order_by(Message.id)
        .limit(100)
        .all()
    )
    for message in bad_rows:
        user_msg = (
            db.query(Message)
            .filter(
                Message.conversation_id == message.conversation_id,
                Message.sender_type == "user",
                Message.id < message.id,
            )
            .order_by(Message.id.desc())
            .first()
        )
        knowledge, retrieve = _retrieval_knowledge(user_msg.extra if user_msg else None)
        note = (message.extra or {}).get("badcase_note", "")
        user = user_msg.content if user_msg else ""
        drafts.append({
            "id": f"badcase-{message.id}",
            "name": f"badcase-{message.id}",
            "user": user,
            "knowledge": knowledge,
            "retrieve": retrieve,
            "expect": await _suggest_expect(user, message.content or "", note),
            "review": {
                "conversation_id": message.conversation_id,
                "bad_reply": message.content,
                "note": note,
            },
        })
    missed_rows = (
        db.query(MissedQuestion)
        .filter(MissedQuestion.status == "pending")
        .order_by(MissedQuestion.count.desc())
        .limit(50)
        .all()
    )
    for row in missed_rows:
        conv = db.get(Conversation, row.conversation_id) if row.conversation_id else None
        drafts.append({
            "id": f"missed-{row.id}",
            "name": f"missed-{row.id}",
            "user": row.question,
            "knowledge": "",
            "retrieve": True,
            "expect": {"handoff": False},
            "review": {
                "conversation_id": row.conversation_id,
                "platform": conv.platform if conv else row.platform,
                "count": row.count,
            },
        })
    return drafts
