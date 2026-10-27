"""RPA 通道适配器：无官方 API 资质时的降级通道（见 docs/rpa-workers.md）。

- 入站：Worker 通过 /api/rpa/incoming 上报接近标准化的 payload，这里只做字段透传
  （platform 保持 douyin/xiaohongshu 不变，工作台/统计/频控无感）
- 出站：不调官方 API，写 rpa_outbox 表，等 Worker 轮询拉取；
  返回占位 ID（rpa_ 前缀），真实发送结果由 Worker ack 回执确认

合规提示：RPA 违反平台用户协议，仅限自有账号、低频拟人化使用。
"""
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import or_

from ..schemas import InboundMessage
from .base import PlatformAdapter

logger = logging.getLogger(__name__)


_DM_REPLY_TYPES = ("text", "image", "voice")


def snapshot_dm_question(db, platform_user_id: str, before: datetime | None = None) -> str:
    """记下该用户在 before 之前的最新一条用户消息。不按会话号匹配。"""
    from ..models import Conversation, Customer, Message

    if not platform_user_id:
        return ""
    query = (
        db.query(Message)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(Customer, Customer.id == Conversation.customer_id)
        .filter(
            Customer.platform_user_id == platform_user_id,
            Message.sender_type == "user",
        )
    )
    if before is not None:
        query = query.filter(or_(Message.created_at.is_(None), Message.created_at <= before))
    msg = query.order_by(Message.id.desc()).first()
    return ((msg.content if msg is not None else "") or "")[:500]


def backfill_reply_questions() -> None:
    """给还没记下问题的旧私信和评论补一次。已有 question 的不再改。"""
    from ..database import SessionLocal
    from ..models import PostComment, RpaOutbox

    db = SessionLocal()
    try:
        rows = (
            db.query(RpaOutbox)
            .filter(or_(RpaOutbox.question.is_(None), RpaOutbox.question == ""))
            .filter(RpaOutbox.msg_type.in_(_DM_REPLY_TYPES + ("comment_reply",)))
            .all()
        )
        changed = False
        for row in rows:
            text = ""
            if row.msg_type == "comment_reply":
                try:
                    payload = json.loads(row.content or "")
                except json.JSONDecodeError:
                    payload = None
                comment_id = payload.get("comment_id") if isinstance(payload, dict) else ""
                if isinstance(comment_id, str) and comment_id:
                    comment = (
                        db.query(PostComment)
                        .filter(PostComment.platform_comment_id == comment_id)
                        .first()
                    )
                    text = (comment.content if comment is not None else "") or ""
            else:
                text = snapshot_dm_question(db, row.platform_user_id or "", row.created_at)
            if text:
                row.question = text[:500]
                changed = True
        if changed:
            db.commit()
    finally:
        db.close()


def resnapshot_dm_questions() -> None:
    """按各条私信自己的发出时间，重算已记下的问题。评论不改。"""
    from ..database import SessionLocal
    from ..models import RpaOutbox

    db = SessionLocal()
    try:
        rows = db.query(RpaOutbox).filter(RpaOutbox.msg_type.in_(_DM_REPLY_TYPES)).all()
        changed = False
        for row in rows:
            text = snapshot_dm_question(db, row.platform_user_id or "", row.created_at)
            if (row.question or "") != text:
                row.question = text
                changed = True
        if changed:
            db.commit()
    finally:
        db.close()


def split_account(platform_conversation_id: str) -> tuple[str, str]:
    """会话 ID 形如 '<account>:<页面会话标识>'，拆出 (account, 页面会话标识)。"""
    if ":" in platform_conversation_id:
        account, conv = platform_conversation_id.split(":", 1)
        return account, conv
    return "", platform_conversation_id


class RpaAdapter(PlatformAdapter):
    """入站用无参实例（platform="rpa"）；出站按平台实例化（RpaAdapter("douyin")），
    使 outbox 记录携带正确平台标识。"""

    def __init__(self, platform: str = "rpa"):
        self.platform = platform

    async def verify_webhook(self, headers: dict[str, str], body: bytes,
                             query: dict[str, str] | None = None) -> bool:
        # RPA 接口走 X-Rpa-Key 鉴权（api/rpa.py 中完成），不经过 webhook 验签
        return True

    async def normalize(self, payload: dict[str, Any]) -> InboundMessage | None:
        msg_type = payload.get("msg_type", "text")
        if msg_type not in ("text", "image", "voice"):
            msg_type = "text"
        account = payload.get("account", "")
        conv_id = payload.get("conversation_id", "")
        # 多账号隔离：会话 ID 统一带 account 前缀
        if account and conv_id and not conv_id.startswith(f"{account}:"):
            conv_id = f"{account}:{conv_id}"
        return InboundMessage(
            platform=payload.get("platform", "douyin"),
            platform_user_id=payload.get("user_id", ""),
            platform_conversation_id=conv_id,
            platform_msg_id=payload.get("msg_id", ""),
            msg_type=msg_type,
            content=payload.get("content", "") or "",
            nickname=payload.get("nickname", ""),
            media_id=payload.get("media_id", ""),
            sender_side=payload.get("sender_side", "user"),
            account=account,
            raw_payload=payload,
        )

    async def send(self, platform_conversation_id: str, platform_user_id: str,
                   content: str, msg_type: str = "text", media_id: str = "") -> str:
        """写 outbox 表（不直接发送），返回占位 ID。Worker 拉取后页面发送并 ack。"""
        from ..database import SessionLocal
        from ..models import RpaOutbox

        account, conv_id = split_account(platform_conversation_id)
        if not account:
            # 会话 ID 无 '<account>:' 前缀（如 mock 面板建的会话切到 RPA 通道）：
            # outbox 记录永远不会有 Worker 来拉取，消息将卡在 pending，必须显式告警
            from ..core import monitor
            logger.warning("[RPA 通道] 会话 %s 缺少 account 前缀，outbox 消息将无 Worker 拉取（死信）",
                           platform_conversation_id)
            monitor.record("rpa_outbox_no_account", f"conv={platform_conversation_id}")
        db = SessionLocal()
        try:
            row = RpaOutbox(
                account=account,
                platform=self.platform if self.platform != "rpa" else "",
                platform_conversation_id=conv_id,
                platform_user_id=platform_user_id,
                content=content,
                question=snapshot_dm_question(db, platform_user_id, datetime.utcnow()),
                msg_type=msg_type,
                media_id=media_id,
                status="pending",
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            outbox_id = row.id
        finally:
            db.close()
        logger.info("[RPA 通道] 消息入 outbox account=%s conv=%s type=%s id=%s", account, conv_id, msg_type, outbox_id)
        return f"rpa_{outbox_id}"
