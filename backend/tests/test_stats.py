"""P1 指标测试：3 分钟回复率 / token 用量与成本 / badcase 导出 / 审计日志 / 看板扩展字段。"""
import json
import uuid
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.core.security import hash_password
from app.models import (Agent, AuditLog, Conversation, Customer, LlmUsage, Message,
                        RpaOutbox)


def _admin(db) -> Agent:
    agent = Agent(username=f"admin_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x"),
                  display_name="管理员", role="admin")
    db.add(agent)
    db.commit()
    return agent


def _conv_with_exchange(db, user_at: datetime, reply_delay_s: int) -> Conversation:
    uid = uuid.uuid4().hex[:8]
    customer = Customer(platform="mock", platform_user_id=f"u_{uid}", nickname="测试", tags=[])
    db.add(customer)
    db.flush()
    conv = Conversation(customer_id=customer.id, platform="mock",
                        platform_conversation_id=f"conv_{uid}", mode="ai", status="open",
                        created_at=user_at)
    db.add(conv)
    db.flush()
    db.add(Message(conversation_id=conv.id, sender_type="user", msg_type="text",
                   content="在吗", created_at=user_at))
    db.add(Message(conversation_id=conv.id, sender_type="ai", msg_type="text",
                   content="在的呢", created_at=user_at + timedelta(seconds=reply_delay_s)))
    db.commit()
    return conv


class TestReplyWithin3Min:
    def test_rate_counts_180s_boundary(self, db):
        """首响 ≤180s 计入；>180s 不计入。"""
        from app.api.stats import _reply_within_3min_rate
        start = datetime.utcnow()  # 之后的会话才被统计，隔离其他测试数据
        _conv_with_exchange(db, start + timedelta(seconds=1), 100)   # 计入
        _conv_with_exchange(db, start + timedelta(seconds=1), 300)   # 不计入
        _conv_with_exchange(db, start + timedelta(seconds=1), 180)   # 边界：计入

        rate = _reply_within_3min_rate(db, start)
        assert rate == round(2 / 3, 3)

    def test_no_data_returns_zero(self, db):
        from app.api.stats import _reply_within_3min_rate
        future = datetime.utcnow() + timedelta(days=1)
        assert _reply_within_3min_rate(db, future) == 0.0


class TestTokenUsage:
    def test_record_usage_persisted(self, db):
        """engine 用量记录落库（usage 为 OpenAI 风格对象）。"""
        from app.llm import record_usage

        class U:
            prompt_tokens = 10
            completion_tokens = 5
            total_tokens = 15

        record_usage(999001, f"test-model-{uuid.uuid4().hex[:6]}", U())
        row = db.query(LlmUsage).filter(LlmUsage.conversation_id == 999001).first()
        assert row is not None and row.total_tokens == 15

    def test_record_usage_none_silent(self):
        from app.llm import record_usage
        record_usage(0, "m", None)  # 不抛异常

    def test_token_usage_endpoint_with_cost(self, db, monkeypatch):
        """日用量聚合 + 按单价估算成本。"""
        from app.api import stats as stats_api
        monkeypatch.setattr(settings, "llm_price_input_per_1k", 0.001)
        monkeypatch.setattr(settings, "llm_price_output_per_1k", 0.002)

        before = stats_api.token_usage(1, None, db)[-1]
        db.add(LlmUsage(conversation_id=1, model="m", prompt_tokens=1000,
                        completion_tokens=500, total_tokens=1500))
        db.commit()
        after = stats_api.token_usage(1, None, db)[-1]

        assert after["prompt_tokens"] - before["prompt_tokens"] == 1000
        assert after["completion_tokens"] - before["completion_tokens"] == 500
        # 增量成本：1000/1000*0.001 + 500/1000*0.002 = 0.002 元
        assert after["cost_yuan"] - before["cost_yuan"] == pytest.approx(0.002, abs=1e-4)


class TestBadcaseExport:
    def test_mark_with_note_and_export(self, db):
        """标记 badcase（带备注）→ 导出为 cases.json 草稿格式。"""
        from app.api import evals as evals_api
        from app.api.messages import mark_bad_case
        from app.schemas import BadCaseMark

        admin = _admin(db)
        conv = _conv_with_exchange(db, datetime.utcnow(), 5)
        ai_msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                          Message.sender_type == "ai").first()

        mark_bad_case(ai_msg.id, BadCaseMark(bad_case=True, note="答非所问"), admin, db)
        db.refresh(ai_msg)
        assert ai_msg.bad_case is True
        assert ai_msg.extra["badcase_note"] == "答非所问"

        resp = evals_api.export_badcases(admin, db)
        cases = json.loads(resp.body)
        target = [c for c in cases if c["name"] == f"badcase-{ai_msg.id}"]
        assert len(target) == 1
        assert target[0]["user"] == "在吗"
        assert target[0]["review"]["note"] == "答非所问"
        assert target[0]["review"]["bad_reply"] == "在的呢"
        assert "expect" in target[0]  # 占位，人工审阅补全

    def test_export_requires_admin(self, db):
        from fastapi import HTTPException
        from app.api.deps import require_admin
        normal = Agent(username=f"agent_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x"),
                       display_name="客服", role="agent")
        with pytest.raises(HTTPException) as exc:
            require_admin(normal)
        assert exc.value.status_code == 403


class TestAuditLog:
    def test_ai_switch_writes_audit(self, db, monkeypatch):
        """AI 开关切换留痕，审计列表可查。"""
        from app.api import settings as settings_api
        from app.api.settings import AiSwitchIn

        monkeypatch.setattr(settings, "ai_globally_enabled", True)
        admin = _admin(db)
        settings_api.set_ai_switch(AiSwitchIn(enabled=False), admin, db)

        logs = settings_api.list_audit_logs(100, admin, db)["items"]
        hit = [l for l in logs if l["action"] == "ai_switch" and l["agent_name"] == "管理员"]
        assert hit and "关闭" in hit[0]["detail"]

    def test_audit_log_helper_never_breaks(self, db):
        """audit.log 异常时静默（不影响主流程）。"""
        from app.core import audit
        audit.log(db, None, "system_action", target="t", detail="d")
        row = db.query(AuditLog).filter(AuditLog.action == "system_action").first()
        assert row is not None and row.agent_id == 0 and row.agent_name == "系统"


class TestOverviewExtras:
    def test_total_unread_and_platform_breakdown(self, db):
        """overview 返回未读总数与今日平台分布。"""
        from app.api import stats as stats_api
        admin = _admin(db)
        before = stats_api.overview(admin, db)

        conv = _conv_with_exchange(db, datetime.utcnow(), 5)
        conv.unread_count = 3
        db.commit()

        after = stats_api.overview(admin, db)
        assert after.total_unread >= before.total_unread + 3
        assert after.platform_breakdown.get("mock", 0) >= 1

    def test_outbox_status_backfilled_in_messages(self, db):
        """RPA 出站消息（platform_msg_id=rpa_{id}）在消息列表中回填 outbox 状态。"""
        from app.api.messages import list_messages
        admin = _admin(db)
        conv = _conv_with_exchange(db, datetime.utcnow(), 5)

        row = RpaOutbox(account="shopT", platform="douyin",
                        platform_conversation_id=conv.platform_conversation_id,
                        content="测试", status="acked")
        db.add(row)
        db.commit()
        ai_msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                          Message.sender_type == "ai").first()
        ai_msg.platform_msg_id = f"rpa_{row.id}"
        db.commit()

        msgs = list_messages(conv.id, admin, db)
        target = [m for m in msgs if m.id == ai_msg.id]
        assert target and target[0].extra["outbox_status"] == "acked"

    def test_conversation_counts(self, db):
        """三 tab 计数：active(ai/human) / pending / closed 各自独立。"""
        from app.api.conversations import conversation_counts
        admin = _admin(db)
        before = conversation_counts(admin, db)

        c1 = _conv_with_exchange(db, datetime.utcnow(), 5)          # open + ai → active
        c2 = _conv_with_exchange(db, datetime.utcnow(), 5)
        c2.mode = "pending"                                          # open + pending
        c3 = _conv_with_exchange(db, datetime.utcnow(), 5)
        c3.mode = "human"                                            # open + human → active
        c4 = _conv_with_exchange(db, datetime.utcnow(), 5)
        c4.status = "closed"                                         # closed
        db.commit()

        after = conversation_counts(admin, db)
        assert after["active"] == before["active"] + 2
        assert after["pending"] == before["pending"] + 1
        assert after["closed"] == before["closed"] + 1
