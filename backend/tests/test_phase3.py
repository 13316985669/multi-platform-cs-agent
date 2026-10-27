"""Phase 3 测试：评论引擎（幂等/意图/差评/暗号/频控）+ 抖音评论客户端 + webhook 分流。"""
import uuid
from datetime import datetime

import pytest
import respx
import httpx

from app import models
from app.config import settings
from app.core import ratelimit


def _make_post(db, platform="mock", auth_type="api"):
    account = models.MatrixAccount(
        platform=platform, account_name=f"账号_{uuid.uuid4().hex[:6]}",
        auth_type=auth_type, open_id=f"openid_{uuid.uuid4().hex[:8]}",
        rpa_account=f"rpa_{uuid.uuid4().hex[:6]}", status="active",
        daily_comment_limit=100)
    db.add(account)
    db.commit()
    item = models.ContentItem(title="测试选题", topic="测试", selling_points="")
    db.add(item)
    db.commit()
    version = models.ContentVersion(content_item_id=item.id, platform=platform,
                                    title="t", body="b")
    db.add(version)
    db.commit()
    task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                              scheduled_at=datetime.now(), status="success")
    db.add(task)
    db.commit()
    post = models.Post(publish_task_id=task.id, account_id=account.id, platform=platform,
                       platform_post_id=f"post_{uuid.uuid4().hex[:8]}",
                       url=f"https://example.com/{uuid.uuid4().hex[:8]}", title="测试作品")
    db.add(post)
    db.commit()
    return post, account


def _comment_payload(post, content="这个多少钱", cid=None):
    return {
        "platform": post.platform,
        "platform_post_id": post.platform_post_id,
        "platform_comment_id": cid or f"c_{uuid.uuid4().hex[:10]}",
        "author_id": "u_1", "author_nickname": "路人甲", "content": content,
    }


def _get_comment(db, payload):
    """按 platform_comment_id 精确查询（测试库跨用例共享，不能用 .first()）。"""
    return db.query(models.PostComment).filter(
        models.PostComment.platform_comment_id == payload["platform_comment_id"]).first()


def _mock_intent(monkeypatch, intent="price", confidence=0.9):
    """mock 意图分类 LLM。"""
    from app.comments import engine
    from app.creator.contracts import CommentIntent

    async def fake_llm(prompt, contract):
        if contract is CommentIntent:
            return CommentIntent(intent=intent, confidence=confidence)
        # 兜底回复生成
        class R:
            reply = "已私您啦"
        return R()
    monkeypatch.setattr(engine, "call_llm_json", fake_llm)


class TestCommentEngine:
    @pytest.mark.asyncio
    async def test_idempotent_ingest(self, db, monkeypatch):
        """同一 platform_comment_id 重复投递只入库一次。"""
        from app.comments.engine import handle_inbound_comment

        _mock_intent(monkeypatch)
        monkeypatch.setattr(settings, "comment_auto_reply_enabled", False)
        post, _ = _make_post(db)
        payload = _comment_payload(post, cid="c_dup_1")
        await handle_inbound_comment(payload)
        await handle_inbound_comment(payload)
        count = db.query(models.PostComment).filter(
            models.PostComment.platform_comment_id == "c_dup_1").count()
        assert count == 1

    @pytest.mark.asyncio
    async def test_intent_classified_and_funnel_event(self, db, monkeypatch):
        """评论入库后意图落库 + FunnelEvent(comment) 写入。"""
        from app.comments.engine import handle_inbound_comment

        _mock_intent(monkeypatch, intent="price")
        monkeypatch.setattr(settings, "comment_auto_reply_enabled", False)
        post, account = _make_post(db)
        payload = _comment_payload(post, "这个多少钱")
        await handle_inbound_comment(payload)

        comment = _get_comment(db, payload)
        assert comment.intent == "price"
        assert comment.status == "pending"  # 开关关闭：只采集分类不自动回复
        event = db.query(models.FunnelEvent).filter(
            models.FunnelEvent.stage == "comment",
            models.FunnelEvent.comment_id == comment.id).first()
        assert event is not None
        assert event.account_id == account.id

    @pytest.mark.asyncio
    async def test_complaint_skipped_no_auto_reply(self, db, monkeypatch):
        """差评：置 skipped 不自动回复。"""
        from app.comments import engine
        from app.comments.engine import handle_inbound_comment

        _mock_intent(monkeypatch, intent="complaint")
        monkeypatch.setattr(settings, "comment_auto_reply_enabled", True)
        sent = []

        async def fake_send(db, account, post, comment, text):
            sent.append(text)
        monkeypatch.setattr(engine, "send_comment_reply", fake_send)

        post, _ = _make_post(db)
        payload = _comment_payload(post, "垃圾产品，被骗了")
        await handle_inbound_comment(payload)
        comment = _get_comment(db, payload)
        assert comment.status == "skipped"
        assert sent == []

    @pytest.mark.asyncio
    async def test_rule_based_auto_reply(self, db, monkeypatch):
        """规则命中 → 模板回复发送 + 状态 replied。"""
        from app.comments import engine
        from app.comments.engine import handle_inbound_comment

        _mock_intent(monkeypatch, intent="price")
        monkeypatch.setattr(settings, "comment_auto_reply_enabled", True)
        ratelimit.reset_all()
        sent = []

        async def fake_send(db, account, post, comment, text):
            sent.append(text)
        monkeypatch.setattr(engine, "send_comment_reply", fake_send)

        post, _ = _make_post(db)
        db.add(models.CommentRule(
            platform="", intent="price", keywords=["多少钱"],
            reply_templates=["价格私您啦"], guide_code="价格", enabled=True, priority=10))
        db.commit()

        payload = _comment_payload(post, "这个多少钱")
        await handle_inbound_comment(payload)
        comment = _get_comment(db, payload)
        assert comment.status == "replied"
        assert comment.reply_content == "价格私您啦"
        assert sent == ["价格私您啦"]

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_reply(self, db, monkeypatch):
        """频控超限：不发送，评论保持 pending。"""
        from app.comments import engine
        from app.comments.engine import handle_inbound_comment

        _mock_intent(monkeypatch, intent="price")
        monkeypatch.setattr(settings, "comment_auto_reply_enabled", True)
        ratelimit.reset_all()
        sent = []

        async def fake_send(db, account, post, comment, text):
            sent.append(text)
        monkeypatch.setattr(engine, "send_comment_reply", fake_send)

        post, account = _make_post(db)
        db.add(models.CommentRule(
            platform="", intent="price", keywords=["多少钱"],
            reply_templates=["价格私您啦"], guide_code="价格", enabled=True, priority=10))
        db.commit()

        # 打满 mock_comment 的小时额度（100 条）
        for _ in range(100):
            ratelimit.check_and_count("mock_comment", account.account_name)

        payload = _comment_payload(post, "这个多少钱")
        await handle_inbound_comment(payload)
        comment = _get_comment(db, payload)
        assert comment.status == "pending"
        assert sent == []
        ratelimit.reset_all()

    @pytest.mark.asyncio
    async def test_drain_word_reply_blocked(self, db, monkeypatch):
        """回复文本含引流词 → 出口守卫拦截，不发送。"""
        from app.comments import engine
        from app.comments.engine import handle_inbound_comment
        from app.creator.contracts import CommentIntent

        async def fake_llm(prompt, contract):
            if contract is CommentIntent:
                return CommentIntent(intent="price", confidence=0.9)
            class R:
                reply = "加我微信有优惠"  # LLM 违规输出
            return R()
        monkeypatch.setattr(engine, "call_llm_json", fake_llm)
        monkeypatch.setattr(settings, "comment_auto_reply_enabled", True)
        ratelimit.reset_all()
        sent = []

        async def fake_send(db, account, post, comment, text):
            sent.append(text)
        monkeypatch.setattr(engine, "send_comment_reply", fake_send)

        post, _ = _make_post(db)
        # 无规则命中 → 走 LLM 兜底生成 → 引流词被拦截
        payload = _comment_payload(post, "怎么联系你们")
        await handle_inbound_comment(payload)
        comment = _get_comment(db, payload)
        assert comment.status == "pending"
        assert sent == []


class TestGuideCodeHook:
    @pytest.mark.asyncio
    async def test_guide_code_tags_customer(self, db):
        """私信命中暗号 → customer 打标 + source_guide_code + FunnelEvent(dm)。"""
        from app.services import handle_inbound

        code = f"暗号{uuid.uuid4().hex[:6]}"  # 唯一暗号，避免命中其他用例持久化的规则
        db.add(models.CommentRule(
            platform="", intent="price", keywords=["多少钱"],
            reply_templates=["私您啦"], guide_code=code, enabled=True, priority=10))
        db.commit()

        uid = uuid.uuid4().hex[:8]
        payload = {
            "channel": "mock", "platform": "douyin",
            "user_id": f"mock_user_{uid}", "nickname": "测试用户",
            "content": code, "msg_type": "text",
            "msg_id": f"mock_{uuid.uuid4().hex[:16]}",
            "conversation_id": f"mock_conv_{uid}",
        }
        await handle_inbound(payload)

        customer = db.query(models.Customer).filter(
            models.Customer.platform_user_id == f"mock_user_{uid}").first()
        assert customer is not None
        assert f"暗号:{code}" in (customer.tags or [])
        assert customer.source_guide_code == code
        event = db.query(models.FunnelEvent).filter(
            models.FunnelEvent.stage == "dm",
            models.FunnelEvent.customer_id == customer.id).first()
        assert event is not None
        assert event.guide_code == code


class TestDouyinCommentApi:
    @pytest.mark.asyncio
    @respx.mock
    async def test_list_and_reply(self, db):
        from app.comments.douyin_api import (COMMENT_LIST_URL, COMMENT_REPLY_URL,
                                             list_comments, reply_comment)
        from app.core import crypto

        respx.get(COMMENT_LIST_URL).mock(return_value=httpx.Response(200, json={
            "data": {"error_code": 0, "comments": [
                {"comment_id": "c1", "content": "多少钱", "nickname": "用户A"}]}}))
        respx.post(COMMENT_REPLY_URL).mock(
            return_value=httpx.Response(200, json={"data": {"error_code": 0}}))

        account = models.MatrixAccount(
            platform="douyin", account_name="dy", auth_type="api",
            open_id="oid_1", status="active",
            credentials_enc=crypto.encrypt_json({"access_token": "tok"}))
        db.add(account)
        db.commit()

        data = await list_comments(account, "item_1")
        assert data["comments"][0]["comment_id"] == "c1"
        await reply_comment(account, "item_1", "c1", "私您啦")

    @pytest.mark.asyncio
    @respx.mock
    async def test_reply_error_raises(self, db):
        from app.comments.douyin_api import COMMENT_REPLY_URL, reply_comment
        from app.core import crypto

        respx.post(COMMENT_REPLY_URL).mock(return_value=httpx.Response(
            200, json={"data": {"error_code": 2190008, "description": "无权限"}}))
        account = models.MatrixAccount(
            platform="douyin", account_name="dy2", auth_type="api",
            open_id="oid_2", status="active",
            credentials_enc=crypto.encrypt_json({"access_token": "tok"}))
        db.add(account)
        db.commit()
        with pytest.raises(RuntimeError, match="评论回复失败"):
            await reply_comment(account, "item_1", "c1", "text")


class TestWebhookRouting:
    def test_douyin_comment_event_routed_to_comment_queue(self):
        """抖音 comment 事件 → inbound_comment 队列（不进私信流）。"""
        from fastapi.testclient import TestClient
        from app.api import webhooks
        from app.main import app

        captured = []

        def fake_enqueue(task_type, payload):
            captured.append((task_type, payload))

        original = webhooks.enqueue
        webhooks.enqueue = fake_enqueue
        try:
            client = TestClient(app)
            resp = client.post("/webhooks/douyin", json={
                "event": "item_comment",
                "content": '{"comment_id": "c_1", "item_id": "it_1", '
                           '"content": "多少钱", "nickname": "用户A", "user_id": "u1"}',
            })
            assert resp.status_code == 200
        finally:
            webhooks.enqueue = original

        assert len(captured) == 1
        assert captured[0][0] == "inbound_comment"
        assert captured[0][1]["platform_comment_id"] == "c_1"
        assert captured[0][1]["platform_post_id"] == "it_1"

    def test_douyin_message_event_still_goes_to_message_queue(self):
        """抖音私信事件仍走进站消息队列（回归防护）。"""
        from fastapi.testclient import TestClient
        from app.api import webhooks
        from app.main import app

        captured = []
        original = webhooks.enqueue
        webhooks.enqueue = lambda t, p: captured.append((t, p))
        try:
            client = TestClient(app)
            resp = client.post("/webhooks/douyin", json={
                "event": "im_receive_msg",
                "content": '{"conversation_short_id": 1, "server_message_id": "m1", '
                           '"message_type": "text", "text": "在吗"}',
                "from_user_id": "u1",
            })
            assert resp.status_code == 200
        finally:
            webhooks.enqueue = original
        assert len(captured) == 1
        assert captured[0][0] == "inbound_message"
