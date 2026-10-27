"""RPA 通道测试：桥接协议（incoming/outbox/ack/heartbeat/identity/media）+ 通道路由 + 媒体替身。

不依赖真实平台与真实 LLM：API 函数直接调用（绕过 HTTP 层），ASR/视觉走 monkeypatch。
"""
import io
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException, UploadFile
from starlette.datastructures import Headers

from app.adapters import get_send_adapter, send_channel
from app.adapters.rpa import RpaAdapter, split_account
from app.api import rpa as rpa_api
from app.config import settings
from app.core import asr, ratelimit
from app.models import (AccountBinding, Conversation, Customer, MatrixAccount, Message, QueueTask,
                        RpaIdentityMap, RpaMedia, RpaOutbox, RpaWorker)
from app.schemas import RpaAck, RpaHeartbeat, RpaIdentityResolve, RpaIncoming, RpaSelfIdentity
from app.services import handle_inbound, send_outbound


# ============ 辅助 ============

def _incoming(account="shopA", platform="douyin", nickname="买家小王", content="在吗",
              msg_type="text", media_id="", sender_side="user", prev_nickname="",
              conversation_id="") -> RpaIncoming:
    return RpaIncoming(
        account=account, platform=platform, nickname=nickname, content=content,
        msg_type=msg_type, media_id=media_id, msg_id=f"rpa_{uuid.uuid4().hex[:12]}",
        conversation_id=conversation_id, sender_side=sender_side, prev_nickname=prev_nickname,
    )


def _drain_inbound_payload(db):
    """取最新一条 inbound_message 队列任务的 payload（模拟队列 worker 消费）。"""
    task = (
        db.query(QueueTask)
        .filter(QueueTask.task_type == "inbound_message")
        .order_by(QueueTask.id.desc())
        .first()
    )
    assert task is not None
    return task.payload


def _make_upload(data: bytes, filename="test.png", content_type="image/png") -> UploadFile:
    return UploadFile(file=io.BytesIO(data), filename=filename,
                      headers=Headers({"content-type": content_type}))


@pytest.fixture(autouse=True)
def _rpa_key(monkeypatch):
    monkeypatch.setattr(settings, "rpa_api_key", "test-rpa-key")
    yield


# ============ 鉴权 ============

class TestRpaAuth:
    @pytest.mark.asyncio
    async def test_valid_key_passes(self):
        await rpa_api.rpa_key_dep("test-rpa-key")  # 不抛异常即通过

    @pytest.mark.asyncio
    async def test_wrong_key_rejected(self):
        with pytest.raises(HTTPException) as exc:
            await rpa_api.rpa_key_dep("wrong-key")
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_disabled_when_no_key(self, monkeypatch):
        monkeypatch.setattr(settings, "rpa_api_key", "")
        with pytest.raises(HTTPException) as exc:
            await rpa_api.rpa_key_dep("any")
        assert exc.value.status_code == 503


# ============ 身份映射 ============

class TestIdentity:
    def test_new_nickname_gets_stable_id(self, db):
        stable = rpa_api.resolve_identity(db, "shopA", "douyin", "买家小王")
        assert stable.startswith("rpa_shopA_")
        row = db.query(RpaIdentityMap).filter(RpaIdentityMap.stable_user_id == stable).first()
        assert row is not None and row.nickname == "买家小王"

    def test_same_nickname_reuses_id(self, db):
        s1 = rpa_api.resolve_identity(db, "shopA", "douyin", "买家小李")
        s2 = rpa_api.resolve_identity(db, "shopA", "douyin", "买家小李")
        assert s1 == s2

    def test_rename_migration_via_prev_nickname(self, db):
        """改昵称：prev_nickname 命中旧映射 → 稳定 ID 不变，昵称更新。"""
        stable = rpa_api.resolve_identity(db, "shopA", "douyin", "旧昵称")
        stable2 = rpa_api.resolve_identity(db, "shopA", "douyin", "新昵称", prev_nickname="旧昵称")
        assert stable == stable2
        row = db.query(RpaIdentityMap).filter(RpaIdentityMap.stable_user_id == stable).first()
        assert row.nickname == "新昵称"
        # 旧昵称记录已迁移，不会重复
        count = db.query(RpaIdentityMap).filter(
            RpaIdentityMap.account == "shopA", RpaIdentityMap.stable_user_id == stable).count()
        assert count == 1

    def test_accounts_isolated(self, db):
        """不同店铺同昵称 → 不同稳定 ID。"""
        s1 = rpa_api.resolve_identity(db, "shopA", "douyin", "同名买家")
        s2 = rpa_api.resolve_identity(db, "shopB", "douyin", "同名买家")
        assert s1 != s2

    def test_resolve_endpoint(self, db):
        req = RpaIdentityResolve(account="shopA", platform="douyin", nickname="端点用户")
        result = rpa_api.identity_resolve(req, db, None)
        assert result["stable_user_id"].startswith("rpa_shopA_")


# ============ 入站全流程 ============

def _conv_of_user(db, platform: str, user_id: str) -> Conversation | None:
    """按平台 + 稳定用户 ID 定位会话（测试隔离：各用例用户互不相同的昵称 → 不同稳定 ID）。"""
    customer = db.query(Customer).filter(Customer.platform == platform,
                                         Customer.platform_user_id == user_id).first()
    if customer is None:
        return None
    return db.query(Conversation).filter(Conversation.customer_id == customer.id).first()


class TestIncomingFlow:
    @pytest.mark.asyncio
    async def test_text_incoming_full_flow(self, db):
        """Worker 上报 → 身份解析 → 入队 → handle_inbound → 会话/消息入库，平台标识不变。"""
        rpa_api.rpa_incoming(_incoming(nickname="文本买家"), db, None)
        payload = _drain_inbound_payload(db)
        assert payload["channel"] == "rpa"
        assert payload["user_id"].startswith("rpa_shopA_")

        await handle_inbound(payload)

        conv = _conv_of_user(db, "douyin", payload["user_id"])
        assert conv is not None, "RPA 入站消息未入库"
        assert conv.platform == "douyin"
        assert conv.platform_conversation_id.startswith("shopA:")  # 多账号前缀隔离
        msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                       Message.sender_type == "user").first()
        assert msg.content == "在吗"

    @pytest.mark.asyncio
    async def test_agent_bypass_message_no_ai(self, db):
        """人工旁路消息：入库为 agent 消息，不触发 AI、不计未读。"""
        rpa_api.rpa_incoming(_incoming(nickname="旁路买家", sender_side="agent",
                                       content="您好，我是客服"), db, None)
        payload = _drain_inbound_payload(db)
        await handle_inbound(payload)

        conv = _conv_of_user(db, "douyin", payload["user_id"])
        assert conv is not None
        assert conv.mode == "ai"  # 未触发任何模式切换
        assert conv.unread_count == 0
        agent_msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                             Message.sender_type == "agent").first()
        assert agent_msg is not None
        assert agent_msg.extra.get("bypass") is True
        ai_msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                          Message.sender_type == "ai").first()
        assert ai_msg is None, "旁路消息不应触发 AI 回复"

    @pytest.mark.asyncio
    async def test_voice_incoming_asr_substitution(self, db, monkeypatch):
        """语音消息：ASR 成功 → content 为转写文本。"""
        async def fake_transcribe(media_id):
            return "你好，我想退货"
        monkeypatch.setattr(asr, "transcribe_media", fake_transcribe)

        rpa_api.rpa_incoming(_incoming(nickname="语音买家", msg_type="voice",
                                       media_id="m_voice_1", content=""), db, None)
        payload = _drain_inbound_payload(db)
        await handle_inbound(payload)

        conv = _conv_of_user(db, "douyin", payload["user_id"])
        msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                       Message.msg_type == "voice").first()
        assert msg is not None
        assert msg.content == "你好，我想退货"
        assert msg.extra["asr"] is True
        assert msg.extra["media_id"] == "m_voice_1"

    @pytest.mark.asyncio
    async def test_voice_incoming_asr_unconfigured(self, db):
        """ASR 未配置（测试环境默认）：content 降级为占位文本。"""
        rpa_api.rpa_incoming(_incoming(nickname="语音买家2", msg_type="voice",
                                       media_id="m_voice_2", content=""), db, None)
        payload = _drain_inbound_payload(db)
        await handle_inbound(payload)

        conv = _conv_of_user(db, "douyin", payload["user_id"])
        msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                       Message.msg_type == "voice").first()
        assert msg is not None
        assert msg.content == "[语音消息]"
        assert msg.extra["asr"] is False

    @pytest.mark.asyncio
    async def test_image_incoming_vision_substitution(self, db, monkeypatch):
        """图片消息：视觉描述成功 → content 为 [图片] 描述。"""
        async def fake_describe(media_id):
            return "一张订单截图，订单号 12345"
        monkeypatch.setattr(asr, "describe_media", fake_describe)

        rpa_api.rpa_incoming(_incoming(nickname="图片买家", msg_type="image",
                                       media_id="m_img_1", content=""), db, None)
        payload = _drain_inbound_payload(db)
        await handle_inbound(payload)

        conv = _conv_of_user(db, "douyin", payload["user_id"])
        msg = db.query(Message).filter(Message.conversation_id == conv.id,
                                       Message.msg_type == "image").first()
        assert msg is not None
        assert msg.content == "[图片] 一张订单截图，订单号 12345"
        assert msg.extra["vision"] is True


# ============ 媒体上下行 ============

class TestMedia:
    def test_upload_and_download(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
        result = rpa_api.rpa_upload_media(_make_upload(b"\x89PNG fake"), kind="image", db=db, _=None)
        media_id = result["media_id"]

        row = db.get(RpaMedia, media_id)
        assert row is not None and row.kind == "image"
        assert row.size == len(b"\x89PNG fake")

        resp = rpa_api.rpa_download_media(media_id, db, None)
        assert resp.path == row.path

    def test_upload_rejects_bad_mime(self, db, tmp_path, monkeypatch):
        monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
        with pytest.raises(HTTPException) as exc:
            rpa_api.rpa_upload_media(
                _make_upload(b"MZ", "evil.exe", "application/x-msdownload"), db=db, _=None)
        assert exc.value.status_code == 400


# ============ outbox（出站队列） ============

def _make_douyin_conversation(db, account: str | None = None) -> tuple[Conversation, str]:
    """创建 douyin 会话（human 模式避免 AI 干扰），返回 (会话, account)。account 默认随机，保证测试隔离。"""
    account = account or f"shop_{uuid.uuid4().hex[:6]}"
    uid = uuid.uuid4().hex[:8]
    customer = Customer(platform="douyin", platform_user_id=f"rpa_{account}_{uid}",
                        nickname="买家", tags=[])
    db.add(customer)
    db.flush()
    conv = Conversation(customer_id=customer.id, platform="douyin",
                        platform_conversation_id=f"{account}:conv_{uid}",
                        mode="human", status="open")
    db.add(conv)
    db.commit()
    return conv, account


class TestOutbox:
    @pytest.mark.asyncio
    async def test_send_outbound_writes_outbox_in_rpa_channel(self, db, monkeypatch):
        """douyin_channel=rpa 时：send_outbound 写 outbox，不调官方 API。"""
        monkeypatch.setattr(settings, "douyin_channel", "rpa")
        ratelimit.reset_all()
        conv, account = _make_douyin_conversation(db)

        msg = await send_outbound(conv.id, "您好，马上为您处理", sender_type="agent")

        assert msg is not None
        assert msg.platform_msg_id.startswith("rpa_")
        row = db.query(RpaOutbox).filter(RpaOutbox.account == account).first()
        assert row is not None
        assert row.status == "pending"
        assert row.content == "您好，马上为您处理"
        assert row.platform == "douyin"
        assert not row.platform_conversation_id.startswith(f"{account}:")  # 前缀已剥离

    @pytest.mark.asyncio
    async def test_send_outbound_api_channel_no_outbox(self, db, monkeypatch):
        """api 通道（默认）：不写 outbox。"""
        monkeypatch.setattr(settings, "douyin_channel", "api")
        ratelimit.reset_all()
        conv, account = _make_douyin_conversation(db)

        msg = await send_outbound(conv.id, "官方 API 通道", sender_type="agent")

        assert msg is not None
        assert db.query(RpaOutbox).filter(RpaOutbox.account == account).count() == 0

    @pytest.mark.asyncio
    async def test_outbox_pull_lease_and_account_isolation(self, db, monkeypatch):
        monkeypatch.setattr(settings, "douyin_channel", "rpa")
        ratelimit.reset_all()
        conv_a, account_a = _make_douyin_conversation(db)
        conv_b, account_b = _make_douyin_conversation(db)

        await send_outbound(conv_a.id, "A 店消息", sender_type="agent")
        await send_outbound(conv_b.id, "B 店消息", sender_type="agent")

        # A 店的 Worker 只能拉到 A 店消息
        result = rpa_api.pull_outbox("worker-1", account_a, 5, db, None)
        assert len(result["messages"]) == 1
        assert result["messages"][0]["content"] == "A 店消息"

        # 已 leased 的消息不会重复拉出
        again = rpa_api.pull_outbox("worker-1", account_a, 5, db, None)
        assert again["messages"] == []

        # B 店的消息仍在
        result_b = rpa_api.pull_outbox("worker-2", account_b, 5, db, None)
        assert len(result_b["messages"]) == 1

    def test_outbox_ordering(self, db):
        """同账号多条消息按创建顺序拉出（AI 分段回复不能乱序）。"""
        account = f"shop_{uuid.uuid4().hex[:6]}"
        for i in range(3):
            db.add(RpaOutbox(account=account, platform="douyin",
                             platform_conversation_id="conv_1", content=f"第{i+1}段",
                             status="pending"))
        db.commit()
        result = rpa_api.pull_outbox("worker-1", account, 5, db, None)
        contents = [m["content"] for m in result["messages"]]
        assert contents == ["第1段", "第2段", "第3段"]

    def test_lease_expiry_returns_to_pending(self, db, monkeypatch):
        """租约超时未 ack → 自动回滚 pending（防 Worker 崩溃丢消息）。"""
        monkeypatch.setattr(settings, "rpa_outbox_lease_seconds", 60)
        account = f"shop_{uuid.uuid4().hex[:6]}"
        row = RpaOutbox(account=account, platform="douyin", platform_conversation_id="c1",
                        content="待发送", status="leased", worker_id="dead-worker",
                        leased_at=datetime.utcnow() - timedelta(seconds=120), attempts=1)
        db.add(row)
        db.commit()

        result = rpa_api.pull_outbox("worker-new", account, 5, db, None)
        assert len(result["messages"]) == 1
        assert result["messages"][0]["content"] == "待发送"

    def test_ack_success(self, db):
        row = RpaOutbox(account=f"shop_{uuid.uuid4().hex[:6]}", platform="douyin",
                        platform_conversation_id="c1", content="x", status="leased",
                        worker_id="w1", attempts=1, leased_at=datetime.utcnow())
        db.add(row)
        db.commit()
        result = rpa_api.ack_outbox(RpaAck(outbox_id=row.id, worker_id="w1", ok=True), db, None)
        assert result["status"] == "acked"
        assert db.get(RpaOutbox, row.id).acked_at is not None

    def test_ack_failure_retries_then_failed(self, db):
        row = RpaOutbox(account=f"shop_{uuid.uuid4().hex[:6]}", platform="douyin",
                        platform_conversation_id="c1", content="x", status="leased",
                        worker_id="w1", attempts=1, leased_at=datetime.utcnow())
        db.add(row)
        db.commit()
        # 第 1 次失败：回滚 pending 重试
        r1 = rpa_api.ack_outbox(RpaAck(outbox_id=row.id, worker_id="w1", ok=False, error="超时"), db, None)
        assert r1["status"] == "pending"
        # 模拟第 3 次尝试后失败：置 failed
        db.get(RpaOutbox, row.id).attempts = 3
        db.commit()
        r2 = rpa_api.ack_outbox(RpaAck(outbox_id=row.id, worker_id="w1", ok=False, error="元素找不到"), db, None)
        assert r2["status"] == "failed"

    def test_laike_ack_failure_does_not_return_pending(self, db, monkeypatch):
        monkeypatch.setattr(settings, "douyin_account_type", "laike")
        row = RpaOutbox(account=f"shop_{uuid.uuid4().hex[:6]}", platform="douyin",
                        platform_conversation_id="c1", content="x", status="leased",
                        worker_id="w1", attempts=1, leased_at=datetime.utcnow())
        db.add(row)
        db.commit()
        result = rpa_api.ack_outbox(
            RpaAck(outbox_id=row.id, worker_id="w1", ok=False, error="发送后未出现新的我方气泡"),
            db, None,
        )
        assert result["status"] == "failed"
        pulled = rpa_api.pull_outbox("worker-new", row.account, 5, db, None)
        assert pulled["messages"] == []

    def test_laike_expired_lease_becomes_failed(self, db, monkeypatch):
        monkeypatch.setattr(settings, "rpa_outbox_lease_seconds", 60)
        monkeypatch.setattr(settings, "douyin_account_type", "laike")
        account = f"shop_{uuid.uuid4().hex[:6]}"
        row = RpaOutbox(account=account, platform="douyin", platform_conversation_id="c1",
                        content="待发送", status="leased", worker_id="dead-worker",
                        leased_at=datetime.utcnow() - timedelta(seconds=120), attempts=1)
        db.add(row)
        db.commit()
        result = rpa_api.pull_outbox("worker-new", account, 5, db, None)
        assert result["messages"] == []
        assert db.get(RpaOutbox, row.id).status == "failed"

    def test_ack_not_found(self, db):
        with pytest.raises(HTTPException) as exc:
            rpa_api.ack_outbox(RpaAck(outbox_id=99999, worker_id="w1"), db, None)
        assert exc.value.status_code == 404


# ============ outbox 失败处置（工作台侧） ============

class TestOutboxDisposal:
    def _failed_row(self, db) -> RpaOutbox:
        row = RpaOutbox(account=f"shop_{uuid.uuid4().hex[:6]}", platform="douyin",
                        platform_conversation_id="c1", content="失败消息",
                        status="failed", attempts=3, error="发送后未出现新的我方气泡")
        db.add(row)
        db.commit()
        return row

    def test_retry_failed_back_to_pending(self, db):
        """重发：置回 pending、清零计数，Worker 下轮可拉到。"""
        row = self._failed_row(db)
        resp = rpa_api.retry_outbox(row.id, db, None)
        assert resp["status"] == "pending"
        db.refresh(row)
        assert row.attempts == 0 and row.error == ""

        result = rpa_api.pull_outbox("w1", row.account, 5, db, None)
        assert any(m["outbox_id"] == row.id for m in result["messages"])

    def test_discard_failed(self, db):
        """忽略：置 discarded，不再计入 failed 列表。"""
        row = self._failed_row(db)
        resp = rpa_api.discard_outbox(row.id, db, None)
        assert resp["status"] == "discarded"

        result = rpa_api.list_failed_outbox(row.account, db, None)
        assert all(m["outbox_id"] != row.id for m in result["items"])

    def test_failed_list_and_retry_rejects_non_failed(self, db):
        row = self._failed_row(db)
        result = rpa_api.list_failed_outbox(row.account, db, None)
        assert any(m["outbox_id"] == row.id for m in result["items"])

        pending = RpaOutbox(account=row.account, platform="douyin",
                            platform_conversation_id="c2", content="待发送", status="pending")
        db.add(pending)
        db.commit()
        with pytest.raises(HTTPException) as exc:
            rpa_api.retry_outbox(pending.id, db, None)
        assert exc.value.status_code == 400


# ============ 心跳 ============

class TestHeartbeat:
    def test_heartbeat_registers_and_updates(self, db):
        req = RpaHeartbeat(worker_id="w-001", account="shopA", platform="douyin",
                           status="online", meta={"version": "1.0.0"})
        rpa_api.heartbeat(req, db, None)
        worker = db.query(RpaWorker).filter(RpaWorker.worker_id == "w-001").first()
        assert worker is not None and worker.status == "online"

        # 状态上报：选择器漂移
        req2 = RpaHeartbeat(worker_id="w-001", account="shopA", platform="douyin",
                            status="selector_mismatch")
        rpa_api.heartbeat(req2, db, None)
        db.refresh(worker)
        assert worker.status == "selector_mismatch"

    def test_selector_mismatch_clears_stale_login_expired(self, db):
        """已登录但选择器失效时，授权状态不再停在登录过期。"""
        account = MatrixAccount(
            platform="douyin", account_name="来客", auth_type="rpa",
            open_id=f"pending_{uuid.uuid4().hex[:8]}", rpa_account="ads-laike-status",
            status="active",
        )
        db.add(account)
        db.flush()
        binding = AccountBinding(
            account_id=account.id, duty="dm", driver="douyin_laike", provider="adspower",
            adspower_profile_id="pf-status", worker_id="w-laike-status",
            auth_status="login_expired",
        )
        db.add(binding)
        db.commit()
        rpa_api.heartbeat(RpaHeartbeat(
            worker_id="w-laike-status", account="ads-laike-status", platform="douyin",
            status="selector_mismatch", meta={"binding_id": binding.id},
        ), db, None)
        db.refresh(binding)
        assert binding.auth_status == "pending_login"

    def test_online_heartbeat_keeps_unconfirmed_login_name(self, db):
        account = MatrixAccount(
            platform="douyin", account_name="待确认", auth_type="rpa",
            open_id=f"pending_{uuid.uuid4().hex[:8]}", rpa_account="ads-keep-name",
            status="active",
            profile_json={"ads_profile_id": "pf-keep", "ads_profile_name": "环境甲"},
        )
        db.add(account)
        db.flush()
        binding = AccountBinding(
            account_id=account.id, duty="dm", driver="douyin_laike", provider="adspower",
            adspower_profile_id="pf-keep", worker_id="w-keep-name",
            auth_status="pending_login",
        )
        db.add(binding)
        db.commit()
        rpa_api.heartbeat(RpaHeartbeat(
            worker_id="w-keep-name", account="ads-keep-name", platform="douyin",
            status="online", meta={"binding_id": binding.id},
        ), db, None)
        db.refresh(account)
        db.refresh(binding)
        assert binding.auth_status == "authorized"
        assert account.account_name == "待确认"
        assert not (account.profile_json or {}).get("confirmed")

        rpa_api.report_self_identity(RpaSelfIdentity(
            worker_id="w-keep-name", profile_id="pf-keep", account_name="阿茶优选",
        ), db, None)
        db.refresh(account)
        assert account.account_name == "阿茶优选"
        assert (account.profile_json or {}).get("login_name") == "阿茶优选"
        assert not (account.profile_json or {}).get("standby_name")
        assert (account.profile_json or {}).get("confirmed") is True
        assert (account.profile_json or {}).get("ads_profile_name") == "环境甲"

        account.account_name = "抖音环境1"
        db.commit()
        rpa_api.report_self_identity(RpaSelfIdentity(
            worker_id="w-keep-name", profile_id="pf-keep", account_name="授权店铺",
        ), db, None)
        db.refresh(account)
        assert account.account_name == "授权店铺"
        assert (account.profile_json or {}).get("login_name") == "授权店铺"
        assert (account.profile_json or {}).get("standby_name") == "抖音环境1"

    def test_heartbeat_dry_run_not_persisted(self, db):
        """dry_run=True：只验 key 不落库不告警（doctor 自检用，避免误标 online 后被判 offline）。"""
        worker_id = f"w-dry-{uuid.uuid4().hex[:6]}"
        req = RpaHeartbeat(worker_id=worker_id, account="shopA", platform="douyin",
                           status="online", dry_run=True)
        resp = rpa_api.heartbeat(req, db, None)
        assert resp["ok"] is True and resp.get("dry_run") is True
        assert db.query(RpaWorker).filter(RpaWorker.worker_id == worker_id).first() is None

    def test_fleet_heartbeat_is_hidden_and_can_go_offline(self, db):
        from app.core.monitor import count_last_hour
        from app.main import _rpa_housekeep_once
        from app.models import Agent
        from app.core.security import hash_password

        worker_id = f"fleet-test-{uuid.uuid4().hex[:6]}"
        before = count_last_hour("rpa_worker_fleet")
        resp = rpa_api.heartbeat(RpaHeartbeat(
            worker_id=worker_id, account="", platform="fleet", status="fleet",
            meta={"browser_profiles": [{"id": "p1", "name": "环境"}]},
        ), db, None)
        assert resp["ok"] is True
        assert count_last_hour("rpa_worker_fleet") == before
        worker = db.query(RpaWorker).filter(RpaWorker.worker_id == worker_id).one()
        assert worker.status == "fleet"
        agent = Agent(username=f"admin_{uuid.uuid4().hex[:6]}",
                      password_hash=hash_password("x"), display_name="管理员", role="admin")
        db.add(agent)
        db.commit()
        listed = rpa_api.list_workers(agent, db)
        assert all(item.worker_id != worker_id for item in listed)

        worker.last_heartbeat_at = datetime.utcnow() - timedelta(seconds=settings.rpa_worker_offline_seconds + 5)
        db.commit()
        _rpa_housekeep_once()
        db.expire_all()
        stale = db.query(RpaWorker).filter(RpaWorker.worker_id == worker_id).one()
        assert stale.status == "offline"

    def test_list_replies_shows_dm_and_comment_not_publish(self, db):
        import json
        from app.models import Agent
        from app.core.security import hash_password

        account = f"reply_{uuid.uuid4().hex[:6]}"
        db.add(RpaOutbox(
            account=account, platform="douyin", content="私信你好",
            msg_type="text", status="acked",
        ))
        db.add(RpaOutbox(
            account=account, platform="douyin", msg_type="comment_reply", status="pending",
            content=json.dumps({"text": "评论回复呀", "comment_id": "c1"}, ensure_ascii=False),
        ))
        db.add(RpaOutbox(
            account=account, platform="xiaohongshu", content="{}",
            msg_type="publish_note", status="pending",
        ))
        db.commit()
        agent = Agent(
            username=f"admin_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x"),
            display_name="管理员", role="admin",
        )
        db.add(agent)
        db.commit()
        items = [
            item for item in rpa_api.list_replies("", db, agent)["items"]
            if item["account"] == account
        ]
        assert {item["msg_type"] for item in items} == {"text", "comment_reply"}
        by_type = {item["msg_type"]: item for item in items}
        assert by_type["text"]["content"] == "私信你好"
        assert by_type["text"]["kind"] == "dm"
        assert by_type["comment_reply"]["content"] == "评论回复呀"
        assert by_type["comment_reply"]["kind"] == "comment"
        comments = rpa_api.list_replies("comment", db, agent)["items"]
        assert any(item["account"] == account and item["content"] == "评论回复呀" for item in comments)
        assert all(item["kind"] == "comment" for item in comments if item["account"] == account)

    def test_list_replies_search_filter_and_page(self, db):
        import json
        from app.models import Agent
        from app.core.security import hash_password

        account = f"reply_{uuid.uuid4().hex[:6]}"
        other = f"other_{uuid.uuid4().hex[:6]}"
        db.add(RpaOutbox(
            account=account, platform="douyin", content="私信你好特别词",
            msg_type="text", status="acked",
        ))
        db.add(RpaOutbox(
            account=account, platform="xiaohongshu", msg_type="comment_reply", status="failed",
            content=json.dumps({"text": "评论里的特别词", "comment_id": "c1"}, ensure_ascii=False),
            error="选择器失效",
        ))
        db.add(RpaOutbox(
            account=other, platform="douyin", content="无关内容",
            msg_type="text", status="pending",
        ))
        db.add(RpaOutbox(
            account=account, platform="xiaohongshu", content="{}",
            msg_type="publish_note", status="pending",
        ))
        db.commit()
        agent = Agent(
            username=f"admin_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x"),
            display_name="管理员", role="admin",
        )
        db.add(agent)
        db.commit()

        first = rpa_api.list_replies("", db, agent, q="特别词", page=1, page_size=1)
        assert first["total"] == 2
        assert len(first["items"]) == 1
        assert first["items"][0]["content"] == "评论里的特别词"
        second = rpa_api.list_replies("", db, agent, q="特别词", page=2, page_size=1)
        assert second["items"][0]["content"] == "私信你好特别词"

        by_account = rpa_api.list_replies("", db, agent, q=account)
        assert by_account["total"] == 2
        assert {item["msg_type"] for item in by_account["items"]} == {"text", "comment_reply"}

        by_error = rpa_api.list_replies("", db, agent, q="选择器失效")
        assert any(item["account"] == account and item["status"] == "failed" for item in by_error["items"])

        douyin = rpa_api.list_replies("", db, agent, q=account, platform="douyin")
        assert douyin["total"] == 1
        assert douyin["items"][0]["platform"] == "douyin"

        failed = rpa_api.list_replies("", db, agent, q=account, status="failed")
        assert failed["total"] == 1
        assert failed["items"][0]["status"] == "failed"

        with pytest.raises(HTTPException) as kind_exc:
            rpa_api.list_replies("publish", db, agent)
        assert kind_exc.value.status_code == 400
        with pytest.raises(HTTPException) as status_exc:
            rpa_api.list_replies("", db, agent, status="discarded")
        assert status_exc.value.status_code == 400
        with pytest.raises(HTTPException) as platform_exc:
            rpa_api.list_replies("", db, agent, platform="weibo")
        assert platform_exc.value.status_code == 400
        with pytest.raises(HTTPException) as size_exc:
            rpa_api.list_replies("", db, agent, page_size=51)
        assert size_exc.value.status_code == 400
        with pytest.raises(HTTPException) as zero_exc:
            rpa_api.list_replies("", db, agent, page_size=0)
        assert zero_exc.value.status_code == 400

    def test_list_replies_includes_account_and_asker(self, db):
        import json
        from app.models import Agent, ContentItem, ContentVersion, Post, PostComment, PublishTask
        from app.core.security import hash_password

        profile_id = f"pf{uuid.uuid4().hex[:6]}"
        rpa_account = f"ads-{profile_id}"
        account = MatrixAccount(
            platform="douyin", account_name="阿茶优选", auth_type="rpa",
            open_id=f"open_{uuid.uuid4().hex[:8]}", rpa_account=rpa_account,
            profile_json={"ads_profile_id": profile_id, "ads_profile_name": "美妆环境"},
        )
        db.add(account)
        db.flush()
        customer = Customer(
            platform="douyin", platform_user_id=f"u_{uuid.uuid4().hex[:6]}", nickname="小李",
        )
        db.add(customer)
        db.flush()
        conv_tail = f"c_{uuid.uuid4().hex[:6]}"
        conv = Conversation(
            customer_id=customer.id, platform="douyin",
            platform_conversation_id=f"{rpa_account}:{conv_tail}",
        )
        db.add(conv)
        db.flush()
        now = datetime.utcnow()
        db.add(Message(
            conversation_id=conv.id, sender_type="user", content="想注册香港公司",
            created_at=now - timedelta(minutes=5),
        ))
        db.add(Message(
            conversation_id=conv.id, sender_type="user", content="这条是回复之后的",
            created_at=now + timedelta(minutes=5),
        ))
        db.add(RpaOutbox(
            account=rpa_account, platform="douyin", platform_conversation_id=conv_tail,
            platform_user_id=customer.platform_user_id, content="可以的", question="想注册香港公司",
            msg_type="text", status="acked", created_at=now,
        ))

        item = ContentItem(title="笔记")
        db.add(item)
        db.flush()
        version = ContentVersion(content_item_id=item.id, platform="xiaohongshu")
        db.add(version)
        db.flush()
        task = PublishTask(
            content_version_id=version.id, account_id=account.id, scheduled_at=now,
        )
        db.add(task)
        db.flush()
        post = Post(publish_task_id=task.id, account_id=account.id, platform="xiaohongshu")
        db.add(post)
        db.flush()
        comment_id = f"cm_{uuid.uuid4().hex[:8]}"
        db.add(PostComment(
            post_id=post.id, platform="xiaohongshu", platform_comment_id=comment_id,
            author_id="author_9", author_nickname="评论网友", content="这个多少钱",
        ))
        db.add(RpaOutbox(
            account=rpa_account, platform="xiaohongshu", msg_type="comment_reply", status="pending",
            platform_user_id="author_9", question="这个多少钱",
            content=json.dumps(
                {"text": "私信你", "comment_id": comment_id, "author_nickname": "评论网友"},
                ensure_ascii=False,
            ),
        ))
        missing_id = f"missing_{uuid.uuid4().hex[:6]}"
        db.add(RpaOutbox(
            account=rpa_account, platform="xiaohongshu", msg_type="comment_reply", status="pending",
            platform_user_id="fallback_user",
            content=json.dumps(
                {"text": "回复", "comment_id": missing_id, "author_nickname": "回退昵称"},
                ensure_ascii=False,
            ),
        ))
        db.add(RpaOutbox(
            account=rpa_account, platform="xiaohongshu", msg_type="first_comment", status="pending",
            content=json.dumps({"text": "首评引导", "post_id": post.id}, ensure_ascii=False),
        ))
        bare = f"ads-only{uuid.uuid4().hex[:4]}"
        db.add(RpaOutbox(
            account=bare, platform="douyin", content="hi", msg_type="text", status="pending",
        ))
        db.commit()
        agent = Agent(
            username=f"admin_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x"),
            display_name="管理员", role="admin",
        )
        db.add(agent)
        db.commit()

        listed = rpa_api.list_replies("", db, agent, q=rpa_account)["items"]
        by_type = {}
        for row in listed:
            by_type.setdefault(row["msg_type"], []).append(row)
        dm_item = by_type["text"][0]
        assert dm_item["account_name"] == "阿茶优选"
        assert dm_item["profile_id"] == profile_id
        assert dm_item["profile_name"] == "美妆环境"
        assert dm_item["user_account"] == customer.platform_user_id
        assert dm_item["user_name"] == "小李"
        assert dm_item["question"] == "想注册香港公司"

        comment_item = next(row for row in by_type["comment_reply"] if row["question"] == "这个多少钱")
        assert comment_item["user_account"] == "author_9"
        assert comment_item["user_name"] == "评论网友"
        fallback = next(row for row in by_type["comment_reply"] if row["question"] == "")
        assert fallback["user_account"] == "fallback_user"
        assert fallback["user_name"] == "回退昵称"

        first_item = by_type["first_comment"][0]
        assert first_item["user_account"] == ""
        assert first_item["user_name"] == ""
        assert first_item["question"] == ""

        bare_item = next(
            row for row in rpa_api.list_replies("", db, agent, q=bare)["items"]
            if row["account"] == bare
        )
        assert bare_item["account_name"] == ""
        assert bare_item["profile_id"] == bare[4:]
        assert bare_item["profile_name"] == ""

        by_name = rpa_api.list_replies("", db, agent, q="阿茶优选")["items"]
        assert any(row["account"] == rpa_account and row["msg_type"] == "text" for row in by_name)
        by_user = rpa_api.list_replies("", db, agent, q="小李")["items"]
        assert any(row["user_name"] == "小李" and row["account"] == rpa_account for row in by_user)
        by_env = rpa_api.list_replies("", db, agent, q="美妆环境")["items"]
        assert any(row["profile_name"] == "美妆环境" for row in by_env)
        by_question = rpa_api.list_replies("", db, agent, q="这个多少钱")["items"]
        assert any(row["question"] == "这个多少钱" for row in by_question)
        after = rpa_api.list_replies("", db, agent, q="这条是回复之后的")["items"]
        assert all(row["account"] != rpa_account for row in after)

    def test_list_replies_question_stays_in_its_own_conversation(self, db):
        from app.models import Agent
        from app.core.security import hash_password

        rpa_account = f"ads-own{uuid.uuid4().hex[:4]}"
        customer = Customer(
            platform="douyin", platform_user_id=f"u_{uuid.uuid4().hex[:6]}", nickname="林中小晓",
        )
        db.add(customer)
        db.flush()
        now = datetime.utcnow()
        late = Conversation(
            customer_id=customer.id, platform="douyin",
            platform_conversation_id=f"{rpa_account}:late",
        )
        first = Conversation(
            customer_id=customer.id, platform="douyin",
            platform_conversation_id=f"{rpa_account}:fee",
        )
        second = Conversation(
            customer_id=customer.id, platform="douyin",
            platform_conversation_id=f"{rpa_account}:reg",
        )
        db.add_all([late, first, second])
        db.flush()
        db.add(Message(
            conversation_id=late.id, sender_type="user", content="下午才入库的问题",
            created_at=now + timedelta(minutes=30),
        ))
        db.add(Message(
            conversation_id=first.id, sender_type="user", content="多少钱",
            created_at=now - timedelta(minutes=5),
        ))
        db.add(Message(
            conversation_id=second.id, sender_type="user", content="怎么注册",
            created_at=now - timedelta(minutes=4),
        ))
        db.add(RpaOutbox(
            account=rpa_account, platform="douyin", platform_conversation_id="late",
            platform_user_id=customer.platform_user_id, content="已经回复了",
            msg_type="text", status="acked", created_at=now,
        ))
        db.add(RpaOutbox(
            account=rpa_account, platform="douyin", platform_conversation_id="fee",
            platform_user_id=customer.platform_user_id, content="费用回复", question="多少钱",
            msg_type="text", status="acked", created_at=now,
        ))
        db.add(RpaOutbox(
            account=rpa_account, platform="douyin", platform_conversation_id="reg",
            platform_user_id=customer.platform_user_id, content="注册回复", question="怎么注册",
            msg_type="text", status="acked", created_at=now,
        ))
        db.commit()
        agent = Agent(
            username=f"admin_{uuid.uuid4().hex[:6]}", password_hash=hash_password("x"),
            display_name="管理员", role="admin",
        )
        db.add(agent)
        db.commit()
        listed = [
            row for row in rpa_api.list_replies("", db, agent, q=rpa_account)["items"]
            if row["account"] == rpa_account
        ]
        by_reply = {row["content"]: row for row in listed}
        assert by_reply["已经回复了"]["user_name"] == "林中小晓"
        assert by_reply["已经回复了"]["question"] == ""
        assert by_reply["费用回复"]["question"] == "多少钱"
        assert by_reply["注册回复"]["question"] == "怎么注册"
        assert all(row["question"] != "下午才入库的问题" for row in listed)

    @pytest.mark.asyncio
    async def test_enqueue_stores_question_and_backfill_does_not_overwrite(self, db):
        import json
        from app.adapters.rpa import RpaAdapter, backfill_reply_questions
        from app.comments.sender import send_comment_reply
        from app.models import ContentItem, ContentVersion, Post, PostComment, PublishTask

        rpa_account = f"ads-store{uuid.uuid4().hex[:4]}"
        account = MatrixAccount(
            platform="douyin", account_name="店铺", auth_type="rpa",
            open_id=f"open_{uuid.uuid4().hex[:8]}", rpa_account=rpa_account, status="active",
        )
        db.add(account)
        db.flush()
        customer = Customer(
            platform="douyin", platform_user_id=f"u_{uuid.uuid4().hex[:6]}", nickname="小李",
        )
        db.add(customer)
        db.flush()
        conv_tail = "conv_store"
        conv = Conversation(
            customer_id=customer.id, platform="douyin",
            platform_conversation_id=f"{rpa_account}:{conv_tail}",
        )
        db.add(conv)
        db.flush()
        now = datetime.utcnow()
        db.add(Message(
            conversation_id=conv.id, sender_type="user", content="原话",
            created_at=now - timedelta(minutes=1),
        ))
        db.commit()

        adapter = RpaAdapter("douyin")
        sent = await adapter.send(f"{rpa_account}:{conv_tail}", customer.platform_user_id, "回复正文")
        missed = await adapter.send(f"{rpa_account}:none", "u_nobody", "没有原话")
        db.expire_all()
        stored = db.get(RpaOutbox, int(sent.removeprefix("rpa_")))
        empty = db.get(RpaOutbox, int(missed.removeprefix("rpa_")))
        assert stored.question == "原话"
        assert stored.content == "回复正文"
        assert empty.question == ""

        item = ContentItem(title="笔记")
        db.add(item)
        db.flush()
        version = ContentVersion(content_item_id=item.id, platform="douyin")
        db.add(version)
        db.flush()
        task = PublishTask(content_version_id=version.id, account_id=account.id, scheduled_at=now)
        db.add(task)
        db.flush()
        post = Post(publish_task_id=task.id, account_id=account.id, platform="douyin")
        db.add(post)
        db.flush()
        comment = PostComment(
            post_id=post.id, platform="douyin", platform_comment_id=f"cm_{uuid.uuid4().hex[:6]}",
            author_id="author_1", author_nickname="评论人", content="评论原话",
        )
        db.add(comment)
        db.commit()
        await send_comment_reply(db, account, post, comment, "评论回复")
        db.expire_all()
        reply = (
            db.query(RpaOutbox)
            .filter(RpaOutbox.msg_type == "comment_reply", RpaOutbox.account == rpa_account)
            .one()
        )
        assert reply.question == "评论原话"
        assert json.loads(reply.content)["text"] == "评论回复"

        old = RpaOutbox(
            account=rpa_account, platform="douyin", platform_conversation_id=conv_tail,
            platform_user_id=customer.platform_user_id,
            content="旧回复", msg_type="text", status="acked", question="",
            created_at=now,
        )
        db.add(old)
        db.commit()
        backfill_reply_questions()
        db.expire_all()
        assert old.question == "原话"
        old.question = "手工记下的"
        db.commit()
        backfill_reply_questions()
        db.expire_all()
        assert old.question == "手工记下的"
        listed = rpa_api.list_replies("", db, None, q="手工记下的")["items"]
        assert any(row["outbox_id"] == old.id and row["question"] == "手工记下的" for row in listed)

    def test_snapshot_uses_latest_user_message_not_old_conversation(self, db):
        from app.adapters.rpa import resnapshot_dm_questions

        rpa_account = f"ads-hello{uuid.uuid4().hex[:4]}"
        user_id = f"u_{uuid.uuid4().hex[:6]}"
        customer = Customer(platform="douyin", platform_user_id=user_id, nickname="林中小路")
        db.add(customer)
        db.flush()
        old_conv = Conversation(
            customer_id=customer.id, platform="douyin", status="closed",
            platform_conversation_id=f"{rpa_account}:林中小路",
        )
        new_conv = Conversation(
            customer_id=customer.id, platform="douyin", status="open",
            platform_conversation_id=f"{rpa_account}:林中小路",
        )
        db.add_all([old_conv, new_conv])
        db.flush()
        earlier = datetime.utcnow() - timedelta(hours=6)
        hello_at = datetime.utcnow() - timedelta(seconds=1)
        reply_at = datetime.utcnow()
        db.add(Message(
            conversation_id=old_conv.id, sender_type="user", content="香港公司注册",
            created_at=earlier,
        ))
        db.add(Message(
            conversation_id=new_conv.id, sender_type="user", content="哈喽",
            created_at=hello_at,
        ))
        early_reply = RpaOutbox(
            account=rpa_account, platform="douyin", platform_conversation_id="林中小路",
            platform_user_id=user_id, content="旧答案", msg_type="text", status="acked",
            question="香港公司注册", created_at=earlier + timedelta(seconds=5),
        )
        hello_reply = RpaOutbox(
            account=rpa_account, platform="douyin", platform_conversation_id="林中小路",
            platform_user_id=user_id, content="新答案", msg_type="text", status="acked",
            question="香港公司注册", created_at=reply_at,
        )
        db.add_all([early_reply, hello_reply])
        db.commit()
        resnapshot_dm_questions()
        db.expire_all()
        assert early_reply.question == "香港公司注册"
        assert hello_reply.question == "哈喽"

    def test_list_workers_with_counts(self, db):
        from app.models import Agent
        from app.core.security import hash_password
        account = f"shop_{uuid.uuid4().hex[:6]}"
        agent = Agent(username=f"admin_{uuid.uuid4().hex[:6]}",
                      password_hash=hash_password("x"), display_name="管理员", role="admin")
        db.add(agent)
        db.add(RpaWorker(worker_id=f"w-{uuid.uuid4().hex[:6]}", account=account,
                         platform="douyin", status="online"))
        db.add(RpaOutbox(account=account, platform="douyin", content="待发", status="pending"))
        db.add(RpaOutbox(account=account, platform="douyin", content="失败", status="failed"))
        db.commit()

        result = rpa_api.list_workers(agent, db)
        w = next(x for x in result if x.account == account)
        assert w.pending == 1 and w.failed == 1


# ============ 通道路由 ============

class TestChannelRouting:
    def test_send_adapter_routing(self, monkeypatch):
        monkeypatch.setattr(settings, "douyin_channel", "rpa")
        monkeypatch.setattr(settings, "xhs_channel", "api")
        assert isinstance(get_send_adapter("douyin"), RpaAdapter)
        assert get_send_adapter("douyin").platform == "douyin"
        assert not isinstance(get_send_adapter("xiaohongshu"), RpaAdapter)
        assert send_channel("douyin") == "rpa"
        assert send_channel("mock") == "api"

    def test_split_account(self):
        assert split_account("shopA:conv_1") == ("shopA", "conv_1")
        assert split_account("conv_1") == ("", "conv_1")

    def test_rpa_rate_limit_stricter(self, monkeypatch):
        """RPA 通道适用更严的每分钟频控。"""
        ratelimit.reset_all()
        for _ in range(6):
            allowed, _ = ratelimit.check_and_count("douyin_rpa", "conv_x")
            assert allowed
        allowed, rule = ratelimit.check_and_count("douyin_rpa", "conv_x")
        assert not allowed and rule == "rpa_minute"

    def test_enterprise_rate_limit_rules(self):
        """企业号频控：48h/6 条窗口（与抖店 24h/6 条不同）。"""
        ratelimit.reset_all()
        rules = {r.name: r for r in ratelimit.PLATFORM_RULES["douyin_enterprise_rpa"]}
        assert rules["reply_48h"].window_seconds == 48 * 3600
        assert rules["reply_48h"].max_count == 6
        # 每分钟上限仍在（拟人化节奏）
        for _ in range(6):
            allowed, _ = ratelimit.check_and_count("douyin_enterprise_rpa", "conv_e")
            assert allowed
        allowed, rule = ratelimit.check_and_count("douyin_enterprise_rpa", "conv_e")
        assert not allowed and rule == "rpa_minute"

    @pytest.mark.asyncio
    async def test_enterprise_account_type_uses_enterprise_rules(self, db, monkeypatch):
        """DOUYIN_ACCOUNT_TYPE=enterprise 时，douyin RPA 出站走企业号频控规则键。"""
        monkeypatch.setattr(settings, "douyin_channel", "rpa")
        monkeypatch.setattr(settings, "douyin_account_type", "enterprise")
        ratelimit.reset_all()
        conv, account = _make_douyin_conversation(db)

        # 连续 6 条后触发企业号 rpa_minute 限制 → 第 7 条被拦截（返回 None）
        for _ in range(6):
            msg = await send_outbound(conv.id, "回复", sender_type="agent")
            assert msg is not None
        msg = await send_outbound(conv.id, "第7条", sender_type="agent")
        assert msg is None
        # 计数确实记在企业号规则键下（再探 1 条仍被拦），抖店键不受影响（可放行）
        assert not ratelimit.check_and_count("douyin_enterprise_rpa", conv.platform_conversation_id)[0]
        assert ratelimit.check_and_count("douyin_rpa", conv.platform_conversation_id)[0]

    def test_laike_rate_limit_rules(self):
        """来客频控：初期保守对齐企业号 48h/6 条窗口（真账号核实后校准）。"""
        ratelimit.reset_all()
        rules = {r.name: r for r in ratelimit.PLATFORM_RULES["douyin_laike_rpa"]}
        assert rules["reply_48h"].window_seconds == 48 * 3600
        assert rules["reply_48h"].max_count == 6
        # 每分钟上限仍在（拟人化节奏）
        for _ in range(6):
            allowed, _ = ratelimit.check_and_count("douyin_laike_rpa", "conv_l")
            assert allowed
        allowed, rule = ratelimit.check_and_count("douyin_laike_rpa", "conv_l")
        assert not allowed and rule == "rpa_minute"

    @pytest.mark.asyncio
    async def test_laike_account_type_uses_laike_rules(self, db, monkeypatch):
        """DOUYIN_ACCOUNT_TYPE=laike 时，douyin RPA 出站走来客频控规则键。"""
        monkeypatch.setattr(settings, "douyin_channel", "rpa")
        monkeypatch.setattr(settings, "douyin_account_type", "laike")
        ratelimit.reset_all()
        conv, account = _make_douyin_conversation(db)

        # 连续 6 条后触发来客 rpa_minute 限制 → 第 7 条被拦截（返回 None）
        for _ in range(6):
            msg = await send_outbound(conv.id, "回复", sender_type="agent")
            assert msg is not None
        msg = await send_outbound(conv.id, "第7条", sender_type="agent")
        assert msg is None
        # 计数确实记在来客规则键下；抖店/企业号键不受影响（可放行）
        assert not ratelimit.check_and_count("douyin_laike_rpa", conv.platform_conversation_id)[0]
        assert ratelimit.check_and_count("douyin_rpa", conv.platform_conversation_id)[0]
        assert ratelimit.check_and_count("douyin_enterprise_rpa", conv.platform_conversation_id)[0]

    @pytest.mark.asyncio
    async def test_laike_account_hour_stops_eleventh_send(self, db, monkeypatch):
        """同一来客账号 1 小时内第 11 条出站不再写 outbox。"""
        monkeypatch.setattr(settings, "douyin_channel", "rpa")
        monkeypatch.setattr(settings, "douyin_account_type", "laike")
        ratelimit.reset_all()
        conv_a, _ = _make_douyin_conversation(db, "lk_hour")
        conv_b, _ = _make_douyin_conversation(db, "lk_hour")
        for _ in range(6):
            assert await send_outbound(conv_a.id, "回复", sender_type="agent") is not None
        for _ in range(4):
            assert await send_outbound(conv_b.id, "回复", sender_type="agent") is not None
        blocked = await send_outbound(conv_b.id, "第11条", sender_type="agent")
        assert blocked is None
        pending = db.query(RpaOutbox).filter(
            RpaOutbox.account == "lk_hour", RpaOutbox.content == "第11条",
        ).count()
        assert pending == 0

    @pytest.mark.asyncio
    async def test_per_account_rate_rules_coexist(self, db, monkeypatch):
        """同一部署里，企业号账号和来客账号各自走自己的频控键。"""
        monkeypatch.setattr(settings, "douyin_channel", "rpa")
        monkeypatch.setattr(settings, "douyin_account_type", "feige")
        ratelimit.reset_all()
        ent_key = f"ent_{uuid.uuid4().hex[:6]}"
        laike_key = f"lk_{uuid.uuid4().hex[:6]}"
        for key, kind in ((ent_key, "enterprise"), (laike_key, "laike")):
            db.add(MatrixAccount(
                platform="douyin", account_name=key, auth_type="rpa",
                open_id=f"pending_{uuid.uuid4().hex[:8]}", rpa_account=key,
                douyin_account_type=kind, status="active",
            ))
        db.commit()
        ent_conv, _ = _make_douyin_conversation(db, ent_key)
        laike_conv, _ = _make_douyin_conversation(db, laike_key)

        for _ in range(6):
            assert await send_outbound(ent_conv.id, "回复", sender_type="agent") is not None
        assert await send_outbound(ent_conv.id, "第7条", sender_type="agent") is None
        assert await send_outbound(laike_conv.id, "来客回复", sender_type="agent") is not None
        assert not ratelimit.check_and_count(
            "douyin_enterprise_rpa", ent_conv.platform_conversation_id)[0]
        assert ratelimit.check_and_count(
            "douyin_laike_rpa", laike_conv.platform_conversation_id)[0]
        assert ratelimit.check_and_count(
            "douyin_rpa", ent_conv.platform_conversation_id)[0]
