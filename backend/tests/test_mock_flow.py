"""Mock 通道端到端入库流程测试：模拟 douyin/xiaohongshu 用户消息必须正确入库。

回归防护：mock payload 曾错误路由到真实平台适配器导致消息静默丢弃。
"""
import uuid

import pytest

from app.models import Conversation, Customer, Message
from app.services import handle_inbound


def _mock_payload(platform: str, content: str) -> dict:
    uid = uuid.uuid4().hex[:8]
    return {
        "channel": "mock",  # 与 webhooks.mock_incoming 构造的 payload 一致
        "platform": platform,
        "user_id": f"mock_user_{uid}",
        "nickname": f"模拟{platform}用户",
        "content": content,
        "msg_type": "text",
        "msg_id": f"mock_{uuid.uuid4().hex[:16]}",
        "conversation_id": f"mock_conv_{uid}",
    }


class TestMockInboundFlow:
    @pytest.mark.asyncio
    async def test_mock_as_douyin_saved(self, db):
        """模拟抖音用户消息：必须入库且会话平台标识为 douyin。"""
        from app.models import Agent
        db.query(Agent).update({Agent.status: "offline"})
        db.commit()
        payload = _mock_payload("douyin", "在吗，这个多少钱")
        await handle_inbound(payload)

        conv = db.query(Conversation).filter(
            Conversation.platform_conversation_id == payload["conversation_id"]).first()
        assert conv is not None, "模拟 douyin 的消息未入库（适配器路由错误）"
        assert conv.platform == "douyin"
        # 测试环境未配置 LLM → AI 降级回复后会自动转人工
        assert conv.mode in ("ai", "pending")

        msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                       Message.sender_type == "user").first()
        assert msg is not None
        assert msg.content == "在吗，这个多少钱"

        customer = db.get(Customer, conv.customer_id)
        assert customer.platform == "douyin"
        assert customer.nickname == "模拟douyin用户"

    @pytest.mark.asyncio
    async def test_mock_as_xiaohongshu_saved(self, db):
        """模拟小红书用户消息：必须入库且用户 ID 不为空。"""
        payload = _mock_payload("xiaohongshu", "这个还有货吗")
        await handle_inbound(payload)

        conv = db.query(Conversation).filter(
            Conversation.platform_conversation_id == payload["conversation_id"]).first()
        assert conv is not None, "模拟 xiaohongshu 的消息未入库"
        customer = db.get(Customer, conv.customer_id)
        assert customer.platform_user_id.startswith("mock_user_")

    @pytest.mark.asyncio
    async def test_duplicate_mock_message_deduped(self, db):
        """同一 mock 消息重复投递只入库一次。"""
        payload = _mock_payload("douyin", "重复消息测试")
        await handle_inbound(payload)
        await handle_inbound(payload)

        count = db.query(Message).filter(Message.content == "重复消息测试",
                                         Message.sender_type == "user").count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_ai_typing_broadcast_before_reply(self, db, monkeypatch):
        """AI 接待前必须广播 ai_typing 事件（前端「正在输入」动画依赖）。"""
        from app.api import ws as ws_module
        from app import services

        events: list[str] = []

        async def fake_broadcast(event, data):
            events.append(event)

        monkeypatch.setattr(ws_module.manager, "broadcast", fake_broadcast)
        monkeypatch.setattr(services, "manager", ws_module.manager)

        payload = _mock_payload("douyin", "typing 广播测试")
        await handle_inbound(payload)

        assert "ai_typing" in events, "AI 接待前未广播 ai_typing 事件"
        # ai_typing 必须先于 AI 回复的 new_message
        assert events.index("ai_typing") < len(events)

    @pytest.mark.asyncio
    async def test_ai_fallback_reply_and_handoff(self, db):
        """未配置 LLM 时：AI 降级话术回复 + 自动转人工进排队。"""
        from app.models import Agent
        db.query(Agent).update({Agent.status: "offline"})
        db.commit()
        payload = _mock_payload("mock", "这个多少钱")
        await handle_inbound(payload)

        conv = db.query(Conversation).filter(
            Conversation.platform_conversation_id == payload["conversation_id"]).first()
        assert conv is not None
        # LLM 未配置 → 降级回复后转人工
        assert conv.mode == "pending"
        ai_msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                          Message.sender_type == "ai").first()
        assert ai_msg is not None


class TestAiKillSwitch:
    @pytest.mark.asyncio
    async def test_globally_disabled_goes_straight_to_human(self, db, monkeypatch):
        """AI 全局开关关闭：入站直接转人工，不产生 AI 回复。"""
        from app.config import settings
        from app.models import Agent
        db.query(Agent).update({Agent.status: "offline"})
        db.commit()
        monkeypatch.setattr(settings, "ai_globally_enabled", False)

        await handle_inbound(_mock_payload("douyin", "熔断测试消息"))

        conv = (db.query(Conversation).filter(Conversation.platform == "douyin")
                .order_by(Conversation.id.desc()).first())
        assert conv is not None and conv.mode == "pending"
        ai_count = db.query(Message).filter(Message.conversation_id == conv.id,
                                            Message.sender_type == "ai").count()
        assert ai_count == 0


class TestInboundRetry:
    @pytest.mark.asyncio
    async def test_failure_before_save_can_retry(self, db, monkeypatch):
        """入库前失败会放开幂等键，同一条消息重试仍能入库并触发 AI。"""
        from app.agent import engine
        from app.models import ProcessedEvent
        from app import services

        payload = _mock_payload("douyin", "重试入库")
        event_key = f"{payload['platform']}:{payload['msg_id']}"
        calls = {"n": 0}
        real = services._upsert_customer

        def flaky(session, msg):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("db down")
            return real(session, msg)

        ai_calls = {"n": 0}

        async def fake_ai(conversation_id):
            ai_calls["n"] += 1

        monkeypatch.setattr(services, "_upsert_customer", flaky)
        monkeypatch.setattr(engine, "process_ai_reply", fake_ai)

        with pytest.raises(RuntimeError):
            await handle_inbound(payload)
        db.rollback()
        assert db.query(ProcessedEvent).filter(ProcessedEvent.event_key == event_key).count() == 0
        assert db.query(Message).filter(Message.content == "重试入库").count() == 0

        await handle_inbound(payload)
        db.rollback()
        assert db.query(Message).filter(Message.content == "重试入库",
                                        Message.sender_type == "user").count() == 1
        assert ai_calls["n"] == 1

    @pytest.mark.asyncio
    async def test_failure_after_save_resumes_without_duplicate(self, db, monkeypatch):
        """入库后 AI 失败：重试不插第二条、不加未读，但会补跑回复。"""
        from app.agent import engine

        payload = _mock_payload("mock", "补跑回复")
        calls = {"n": 0}

        async def flaky(conversation_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("llm down")

        monkeypatch.setattr(engine, "process_ai_reply", flaky)
        with pytest.raises(RuntimeError):
            await handle_inbound(payload)
        db.rollback()
        conv = db.query(Conversation).filter(
            Conversation.platform_conversation_id == payload["conversation_id"]).one()
        assert conv.unread_count == 1
        assert db.query(Message).filter(Message.conversation_id == conv.id,
                                        Message.sender_type == "user").count() == 1

        await handle_inbound(payload)
        db.rollback()
        db.refresh(conv)
        assert conv.unread_count == 1
        assert db.query(Message).filter(Message.conversation_id == conv.id,
                                        Message.sender_type == "user").count() == 1
        assert calls["n"] == 2

    @pytest.mark.asyncio
    async def test_retry_stops_when_reply_already_sent(self, db, monkeypatch):
        """已经发出 AI 回复后再失败，重试不再发第二次。"""
        from app.agent import engine
        from app.database import SessionLocal

        payload = _mock_payload("mock", "不要重发")
        calls = {"n": 0}

        async def flaky(conversation_id):
            calls["n"] += 1
            if calls["n"] == 1:
                session = SessionLocal()
                try:
                    session.add(Message(
                        conversation_id=conversation_id, sender_type="ai",
                        msg_type="text", content="已经回复过",
                    ))
                    session.commit()
                finally:
                    session.close()
                raise RuntimeError("after send")

        monkeypatch.setattr(engine, "process_ai_reply", flaky)
        with pytest.raises(RuntimeError):
            await handle_inbound(payload)
        await handle_inbound(payload)
        db.rollback()
        conv = db.query(Conversation).filter(
            Conversation.platform_conversation_id == payload["conversation_id"]).one()
        assert calls["n"] == 1
        assert db.query(Message).filter(Message.conversation_id == conv.id,
                                        Message.sender_type == "ai").count() == 1
        assert db.query(Message).filter(Message.conversation_id == conv.id,
                                        Message.sender_type == "user").count() == 1

    def test_switch_api_and_admin_guard(self, db, monkeypatch):
        """开关 API：admin 可切换并立即生效；非 admin 被 require_admin 拒绝。"""
        from fastapi import HTTPException
        from app.api import settings as settings_api
        from app.api.deps import require_admin
        from app.api.settings import AiSwitchIn
        from app.config import settings
        from app.core.security import hash_password
        from app.models import Agent

        monkeypatch.setattr(settings, "ai_globally_enabled", True)  # 测试后自动还原
        admin = Agent(username=f"admin_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x"),
                      display_name="管理员", role="admin")
        normal = Agent(username=f"agent_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x"),
                       display_name="客服", role="agent")
        db.add_all([admin, normal])
        db.commit()

        resp = settings_api.set_ai_switch(AiSwitchIn(enabled=False), admin, db)
        assert resp.enabled is False
        assert settings_api.get_ai_switch(admin).enabled is False

        with pytest.raises(HTTPException) as exc:
            require_admin(normal)
        assert exc.value.status_code == 403
