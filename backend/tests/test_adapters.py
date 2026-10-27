"""适配器 normalize 测试：用抖音/小红书真实 webhook payload 样例。"""
import hashlib
import json
import time
from datetime import datetime, timedelta

import pytest

from app.adapters.douyin import DouyinAdapter
from app.adapters.xiaohongshu import XiaohongshuAdapter
from app.adapters.mock import MockAdapter
from app.config import settings
from app.models import PlatformToken

# 抖音 im_receive_msg 真实事件结构（来自官方文档）
DOUYIN_PAYLOAD = {
    "event": "im_receive_msg",
    "from_user_id": "ou_test_user_123",
    "to_user_id": "ou_enterprise_456",
    "client_key": "test_client_key",
    "log_id": "log_001",
    "content": json.dumps({
        "conversation_short_id": "6789012345",
        "server_message_id": "sm_987654",
        "conversation_type": 1,
        "message_type": "text",
        "text": "这个多少钱",
        "create_time": 1726700000000,
        "user_infos": [
            {"open_id": "ou_test_user_123", "nick_name": "抖音小明", "avatar": "https://example.com/a.jpg"}
        ],
    }, ensure_ascii=False),
}

XHS_PAYLOAD = {
    "msg_id": "xhs_msg_001",
    "conversation_id": "xhs_conv_001",
    "msg_type": "text",
    "content": "这个还有货吗",
    "sender": {"user_id": "xhs_user_001", "nickname": "红薯妹妹", "avatar": ""},
}

# 小红书 ark 消息推送信封结构：[{msgTag, sellerId, data}]，data 为 JSON 字符串
# （webhooks 层会把数组逐条拆包成单条 dict 再交给 normalize）
XHS_ARK_PAYLOAD = {
    "msgTag": "im_receive_msg",
    "sellerId": "seller_001",
    "data": json.dumps({
        "msgId": "ark_msg_001",
        "conversationId": "ark_conv_001",
        "msgType": "TEXT",
        "content": json.dumps({"text": "这个还有货吗"}, ensure_ascii=False),
        "fromUserId": "ark_user_001",
        "sender": {"nickname": "红薯姐姐", "avatar": ""},
    }, ensure_ascii=False),
}


class TestDouyinAdapter:
    @pytest.mark.asyncio
    async def test_normalize_receive_msg(self):
        adapter = DouyinAdapter()
        msg = await adapter.normalize(DOUYIN_PAYLOAD)
        assert msg is not None
        assert msg.platform == "douyin"
        assert msg.platform_user_id == "ou_test_user_123"
        assert msg.platform_conversation_id == "6789012345"
        assert msg.platform_msg_id == "sm_987654"
        assert msg.msg_type == "text"
        assert msg.content == "这个多少钱"
        assert msg.nickname == "抖音小明"

    @pytest.mark.asyncio
    async def test_ignore_send_receipt(self):
        """自己发出的消息回执（im_send_msg）应忽略，否则死循环。"""
        adapter = DouyinAdapter()
        payload = {**DOUYIN_PAYLOAD, "event": "im_send_msg"}
        assert await adapter.normalize(payload) is None

    @pytest.mark.asyncio
    async def test_ignore_enter_event(self):
        adapter = DouyinAdapter()
        payload = {**DOUYIN_PAYLOAD, "event": "im_enter_direct_msg"}
        assert await adapter.normalize(payload) is None

    @pytest.mark.asyncio
    async def test_send_degraded_without_credentials(self):
        """未配置凭证时发送降级，不抛异常。"""
        adapter = DouyinAdapter()
        msg_id = await adapter.send("conv_1", "user_1", "你好")
        assert msg_id.startswith("dy_out_")


@pytest.fixture()
def clean_xhs_token(db):
    """platform_tokens 表跨测试隔离（session 级测试库，行会残留）。"""
    db.query(PlatformToken).filter(PlatformToken.platform == "xiaohongshu").delete()
    db.commit()
    yield
    db.query(PlatformToken).filter(PlatformToken.platform == "xiaohongshu").delete()
    db.commit()


class TestXhsAdapter:
    @pytest.mark.asyncio
    async def test_normalize(self):
        """扁平结构（联调期/Mock 兼容）。"""
        adapter = XiaohongshuAdapter()
        msg = await adapter.normalize(XHS_PAYLOAD)
        assert msg is not None
        assert msg.platform == "xiaohongshu"
        assert msg.platform_user_id == "xhs_user_001"
        assert msg.content == "这个还有货吗"
        assert msg.nickname == "红薯妹妹"

    @pytest.mark.asyncio
    async def test_normalize_ark_envelope(self):
        """ark 推送信封：msgTag + data(JSON 字符串)，字段多候选键兼容。"""
        adapter = XiaohongshuAdapter()
        msg = await adapter.normalize(dict(XHS_ARK_PAYLOAD))
        assert msg is not None
        assert msg.platform == "xiaohongshu"
        assert msg.platform_user_id == "ark_user_001"
        assert msg.platform_conversation_id == "ark_conv_001"
        assert msg.platform_msg_id == "ark_msg_001"
        assert msg.msg_type == "text"
        assert msg.content == "这个还有货吗"
        assert msg.nickname == "红薯姐姐"

    @pytest.mark.asyncio
    async def test_ignore_test_push(self):
        """平台推送地址检测包 {"test": true} 应忽略。"""
        adapter = XiaohongshuAdapter()
        assert await adapter.normalize({"test": True}) is None

    @pytest.mark.asyncio
    async def test_ignore_non_im_event(self):
        """订单/售后等非私信业务推送不入客服消息流。"""
        adapter = XiaohongshuAdapter()
        payload = {"msgTag": "order_created", "sellerId": "s1",
                   "data": json.dumps({"orderId": "o1"})}
        assert await adapter.normalize(payload) is None

    @pytest.mark.asyncio
    async def test_send_degraded_without_credentials(self, clean_xhs_token):
        adapter = XiaohongshuAdapter()
        msg_id = await adapter.send("conv_1", "user_1", "你好")
        assert msg_id.startswith("xhs_out_")


class TestXhsWebhookVerify:
    @pytest.mark.asyncio
    async def test_pass_without_secret(self, monkeypatch):
        monkeypatch.setattr(settings, "xhs_app_secret", "")
        adapter = XiaohongshuAdapter()
        assert await adapter.verify_webhook({}, b"{}") is True

    @pytest.mark.asyncio
    async def test_query_sign(self, monkeypatch):
        """ark 推送验签：URL query（除 sign）排序 & 连接，首尾拼 secret 取 MD5。"""
        monkeypatch.setattr(settings, "xhs_app_secret", "sk_test")
        adapter = XiaohongshuAdapter()
        query = {"a": "1", "b": "2"}
        joined = "&".join(f"{k}={query[k]}" for k in sorted(query))
        sign = hashlib.md5(("sk_test" + joined + "sk_test").encode()).hexdigest()
        assert await adapter.verify_webhook({}, b"{}", {**query, "sign": sign}) is True
        assert await adapter.verify_webhook({}, b"{}", {**query, "sign": "bad"}) is False
        assert await adapter.verify_webhook({}, b"{}", {}) is False


class TestXhsTokenLifecycle:
    @pytest.mark.asyncio
    async def test_refresh_stores_token(self, clean_xhs_token, monkeypatch):
        """刷新成功后 token 落 platform_tokens 表（重启不丢失）。"""
        monkeypatch.setattr(settings, "xhs_app_id", "app_test")
        monkeypatch.setattr(settings, "xhs_refresh_token", "rt_old")
        adapter = XiaohongshuAdapter()

        async def fake_call(self, method, extra=None):
            assert method == "oauth.refreshToken"
            assert extra == {"refreshToken": "rt_old"}
            return {"success": True, "error_code": 0, "data": {
                "accessToken": "at_new", "refreshToken": "rt_new",
                "accessTokenExpiresAt": int(time.time()) + 7 * 24 * 3600,
                "refreshTokenExpiresAt": int(time.time()) + 14 * 24 * 3600,
            }}

        monkeypatch.setattr(XiaohongshuAdapter, "_call_gateway", fake_call)
        token = await adapter.force_refresh()
        assert token == "at_new"
        stored = adapter._load_stored_token()
        assert stored[0] == "at_new" and stored[1] == "rt_new"
        assert stored[2] is not None and stored[2] > datetime.utcnow()

    @pytest.mark.asyncio
    async def test_send_retries_after_token_refresh(self, clean_xhs_token, monkeypatch):
        """发送遇 token 失效错误码：自动强制刷新并重试一次。"""
        monkeypatch.setattr(settings, "xhs_app_id", "app_test")
        monkeypatch.setattr(settings, "xhs_access_token", "at_old")
        monkeypatch.setattr(settings, "xhs_refresh_token", "rt_old")
        calls = []

        async def fake_call(self, method, extra=None):
            calls.append(method)
            if method == "im.sendMessage" and extra.get("accessToken") == "at_old":
                return {"success": False, "error_code": 10001, "error_msg": "accessToken expired"}
            if method == "oauth.refreshToken":
                return {"success": True, "error_code": 0, "data": {
                    "accessToken": "at_new", "refreshToken": "rt_new",
                    "accessTokenExpiresAt": int(time.time()) + 7 * 24 * 3600,
                }}
            if method == "im.sendMessage":
                return {"success": True, "error_code": 0, "data": {"msgId": "m_123"}}
            raise AssertionError(f"unexpected call: {method}")

        monkeypatch.setattr(XiaohongshuAdapter, "_call_gateway", fake_call)
        adapter = XiaohongshuAdapter()
        msg_id = await adapter.send("conv_1", "user_1", "你好")
        assert msg_id == "m_123"
        assert calls == ["im.sendMessage", "oauth.refreshToken", "im.sendMessage"]

    @pytest.mark.asyncio
    async def test_send_failure_non_token_error_no_retry(self, clean_xhs_token, monkeypatch):
        """非 token 类错误直接抛出，不刷新（由队列/调用方重试）。"""
        from app.adapters.xiaohongshu import XhsSendError

        monkeypatch.setattr(settings, "xhs_app_id", "app_test")
        monkeypatch.setattr(settings, "xhs_access_token", "at_old")
        calls = []

        async def fake_call(self, method, extra=None):
            calls.append(method)
            return {"success": False, "error_code": 40001, "error_msg": "频控超限"}

        monkeypatch.setattr(XiaohongshuAdapter, "_call_gateway", fake_call)
        adapter = XiaohongshuAdapter()
        with pytest.raises(XhsSendError):
            await adapter.send("conv_1", "user_1", "你好")
        assert calls == ["im.sendMessage"]

    @pytest.mark.asyncio
    async def test_maybe_refresh_skips_fresh_token(self, clean_xhs_token, monkeypatch):
        """周期任务：token 有效期充足时不刷新（官方规则 >30min 刷新为 no-op）。"""
        monkeypatch.setattr(settings, "xhs_app_id", "app_test")
        adapter = XiaohongshuAdapter()
        adapter._store_token("at_fresh", "rt_x",
                             datetime.utcnow() + timedelta(days=6), None)
        called = False

        async def fake_call(self, method, extra=None):
            nonlocal called
            called = True
            return {}

        monkeypatch.setattr(XiaohongshuAdapter, "_call_gateway", fake_call)
        assert await adapter.maybe_refresh() is False
        assert called is False

    @pytest.mark.asyncio
    async def test_maybe_refresh_when_expiring(self, clean_xhs_token, monkeypatch):
        """周期任务：剩余有效期不足余量时刷新并落库。"""
        monkeypatch.setattr(settings, "xhs_app_id", "app_test")
        adapter = XiaohongshuAdapter()
        adapter._store_token("at_old", "rt_x",
                             datetime.utcnow() + timedelta(minutes=10), None)

        async def fake_call(self, method, extra=None):
            return {"success": True, "data": {
                "accessToken": "at_new", "refreshToken": "rt_new",
                "accessTokenExpiresAt": int(time.time()) + 7 * 24 * 3600,
            }}

        monkeypatch.setattr(XiaohongshuAdapter, "_call_gateway", fake_call)
        assert await adapter.maybe_refresh() is True
        assert adapter._load_stored_token()[0] == "at_new"


class TestMockAdapter:
    @pytest.mark.asyncio
    async def test_normalize(self):
        adapter = MockAdapter()
        msg = await adapter.normalize({"platform": "douyin", "user_id": "u1", "content": "在吗"})
        assert msg is not None
        assert msg.platform == "douyin"
        assert msg.content == "在吗"
        assert msg.platform_msg_id  # 自动生成幂等键
