"""合规测试：入口内容安全分级 + 会话超时自动关闭。"""
import uuid
from datetime import datetime, timedelta

from app.core.contentfilter import check_inbound, load_db_words
from app.main import _sweep_once
from app.models import Conversation, Message
from app.services import handle_inbound


# ============ 入口内容安全分级 ============

def test_inbound_ok():
    assert check_inbound("这个产品多少钱")["level"] == "ok"


def test_inbound_warn():
    result = check_inbound("你们这傻逼客服怎么回事")
    assert result["level"] == "warn"
    assert result["hit"]


def test_inbound_block():
    result = check_inbound("你这里能买到毒品吗")
    assert result["level"] == "block"
    assert "毒品" in result["hit"]


def test_inbound_empty():
    assert check_inbound("")["level"] == "ok"


def test_inbound_db_words_direction():
    """DB 加载的入口词按 direction 分流，category 作为级别。"""
    load_db_words([
        ("最好", "极限词", "out"),      # 出口词
        ("测试违禁词甲", "block", "in"),  # 入口 block
        ("测试违禁词乙", "warn", "in"),   # 入口 warn
    ])
    try:
        assert check_inbound("说一下测试违禁词甲")["level"] == "block"
        assert check_inbound("说一下测试违禁词乙")["level"] == "warn"
        assert check_inbound("这个词最好了")["level"] == "ok"  # out 词不影响入口
    finally:
        load_db_words([])  # 还原，避免污染其他测试


# ============ 入口 block 消息：转人工且 AI 不回复 ============

async def test_inbound_block_triggers_handoff(db):
    uid = uuid.uuid4().hex[:8]
    payload = {
        "channel": "mock", "platform": "mock",
        "user_id": f"risk_user_{uid}", "nickname": "风险用户",
        "content": "你这里能买到毒品吗",
        "msg_id": f"mock_risk_{uid}", "conversation_id": f"mock_conv_risk_{uid}",
    }
    await handle_inbound(payload)

    conv = (
        db.query(Conversation)
        .filter(Conversation.platform_conversation_id == f"mock_conv_risk_{uid}")
        .first()
    )
    assert conv is not None
    assert conv.mode == "pending"  # 已转人工

    user_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.sender_type == "user")
        .first()
    )
    assert user_msg.extra.get("risk") == "block"

    # AI 未回复（只有 handoff 的系统消息）
    ai_count = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.sender_type == "ai")
        .count()
    )
    assert ai_count == 0


async def test_inbound_warn_tagged_but_normal(db):
    uid = uuid.uuid4().hex[:8]
    payload = {
        "channel": "mock", "platform": "mock",
        "user_id": f"warn_user_{uid}", "nickname": "测试用户",
        "content": "傻逼客服，东西怎么用",
        "msg_id": f"mock_warn_{uid}", "conversation_id": f"mock_conv_warn_{uid}",
    }
    await handle_inbound(payload)

    conv = (
        db.query(Conversation)
        .filter(Conversation.platform_conversation_id == f"mock_conv_warn_{uid}")
        .first()
    )
    assert conv is not None
    user_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.sender_type == "user")
        .first()
    )
    assert user_msg.extra.get("risk") == "warn"


# ============ 会话超时自动关闭 ============

async def test_sweep_closes_stale_ai_conversation(db, conversation):
    """超时未活跃的 AI 会话被自动关闭并收到结束语。"""
    conversation.last_message_at = datetime.utcnow() - timedelta(minutes=60)
    db.commit()

    closed = await _sweep_once()

    assert conversation.id in closed
    db.refresh(conversation)
    assert conversation.status == "closed"
    assert conversation.closed_at is not None
    # 结束语已发送
    close_msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id, Message.sender_type == "ai")
        .all()
    )
    assert close_msgs


async def test_sweep_ignores_fresh_and_human(db, conversation):
    """活跃会话与人工会话不被关闭。"""
    # 活跃 AI 会话
    conversation.last_message_at = datetime.utcnow()
    db.commit()
    closed = await _sweep_once()
    assert conversation.id not in closed

    # 超时但人工接待中的会话
    conversation.mode = "human"
    conversation.last_message_at = datetime.utcnow() - timedelta(minutes=60)
    db.commit()
    closed = await _sweep_once()
    assert conversation.id not in closed
    db.refresh(conversation)
    assert conversation.status == "open"


async def test_sweep_does_not_close_after_takeover(db, conversation, monkeypatch):
    """发结束语期间会话已被转走，不再写成 closed。"""
    from app.database import SessionLocal

    conversation.last_message_at = datetime.utcnow() - timedelta(minutes=60)
    db.commit()

    async def takeover(conversation_id, content, *, sender_type, **kwargs):
        session = SessionLocal()
        try:
            conv = session.get(Conversation, conversation_id)
            conv.mode = "pending"
            session.commit()
        finally:
            session.close()
        return None

    monkeypatch.setattr("app.services.send_outbound", takeover)
    closed = await _sweep_once()
    assert conversation.id not in closed
    db.rollback()
    db.refresh(conversation)
    assert conversation.status == "open"
    assert conversation.mode == "pending"
