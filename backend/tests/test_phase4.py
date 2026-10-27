"""Phase 4 测试：WXBizMsgCrypt 往返/验签、回调 URL 验证、加粉归因、欢迎语、push_wecom_code 工具。"""
import base64
import hashlib
import uuid

import pytest
import respx
import httpx

from app import models
from app.config import settings

# 43 位测试 EncodingAESKey（Base64 解码后 32 字节）
TEST_AES_KEY = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()[:43]
TEST_TOKEN = "test_token"


@pytest.fixture(autouse=True)
def _wecom_settings(monkeypatch):
    monkeypatch.setattr(settings, "wecom_token", TEST_TOKEN)
    monkeypatch.setattr(settings, "wecom_encoding_aes_key", TEST_AES_KEY)
    monkeypatch.setattr(settings, "wecom_corp_id", "ww_test_corp")
    monkeypatch.setattr(settings, "wecom_secret", "secret")
    monkeypatch.setattr(settings, "wecom_agent_user_id", "zhangsan")
    from app.wecom import channel_code
    channel_code.reset_token_cache()


def _signed_params(encrypt: str) -> dict:
    from app.wecom.crypto import make_signature
    ts, nonce = "1700000000", "nonce123"
    return {
        "msg_signature": make_signature(TEST_TOKEN, ts, nonce, encrypt),
        "timestamp": ts, "nonce": nonce,
    }


def _event_xml(state: str, welcome_code: str = "WC1") -> str:
    return f"""<xml>
<ToUserName><![CDATA[ww_test_corp]]></ToUserName>
<MsgType><![CDATA[event]]></MsgType>
<Event><![CDATA[change_external_contact]]></Event>
<ChangeType><![CDATA[add_external_contact]]></ChangeType>
<UserID><![CDATA[zhangsan]]></UserID>
<ExternalUserID><![CDATA[wm_ext_1]]></ExternalUserID>
<State><![CDATA[{state}]]></State>
<WelcomeCode><![CDATA[{welcome_code}]]></WelcomeCode>
</xml>"""


class TestWxBizMsgCrypt:
    def test_encrypt_decrypt_roundtrip(self):
        from app.wecom.crypto import decrypt_message, encrypt_message

        msg = "<xml><Foo>你好</Foo></xml>"
        encrypted = encrypt_message(msg, receiveid="ww_test_corp")
        plain, receiveid = decrypt_message(encrypted)
        assert plain == msg
        assert receiveid == "ww_test_corp"

    def test_signature(self):
        from app.wecom.crypto import check_signature, make_signature

        sig = make_signature(TEST_TOKEN, "1", "2", "abc")
        check_signature(TEST_TOKEN, "1", "2", "abc", sig)  # 不抛异常
        with pytest.raises(Exception):
            check_signature(TEST_TOKEN, "1", "2", "abc", "bad_sig")

    def test_signature_matches_official_algorithm(self):
        """与官方算法对照：sha1(sort(token, ts, nonce, encrypt))。"""
        from app.wecom.crypto import make_signature

        expect = hashlib.sha1("".join(sorted([TEST_TOKEN, "t", "n", "e"])).encode()).hexdigest()
        assert make_signature(TEST_TOKEN, "t", "n", "e") == expect


class TestCallback:
    def test_url_verify(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.wecom.crypto import encrypt_message

        echostr = encrypt_message("hello_verify", receiveid="ww_test_corp")
        client = TestClient(app)
        resp = client.get("/api/wecom/callback", params={
            **_signed_params(echostr), "echostr": echostr})
        assert resp.status_code == 200
        assert resp.text == "hello_verify"

    def test_url_verify_bad_signature_403(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.wecom.crypto import encrypt_message

        echostr = encrypt_message("x", receiveid="ww_test_corp")
        client = TestClient(app)
        resp = client.get("/api/wecom/callback", params={
            "msg_signature": "bad", "timestamp": "1", "nonce": "2", "echostr": echostr})
        assert resp.status_code == 403

    @pytest.mark.asyncio
    @respx.mock
    async def test_add_contact_attribution_and_welcome(self, db):
        """加粉事件：按 state 归因活码 → FunnelEvent(wecom) + 客户 wecom_added_at + 20s 内发欢迎语。"""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.wecom import channel_code
        from app.wecom.crypto import encrypt_message

        state = uuid.uuid4().hex[:16]
        account = models.MatrixAccount(
            platform="douyin", account_name=f"dy_{uuid.uuid4().hex[:6]}",
            auth_type="api", open_id=f"oid_{uuid.uuid4().hex[:8]}", status="active")
        db.add(account)
        db.commit()
        db.add(models.WecomChannelCode(
            name="测试活码", state=state, qr_url="https://wework.qpic.cn/qr",
            bound_account_id=account.id))
        customer = models.Customer(platform="douyin",
                                   platform_user_id=f"u_{uuid.uuid4().hex[:8]}",
                                   nickname="线索客户", tags=[])
        db.add(customer)
        db.commit()
        db.add(models.FunnelEvent(stage="lead", platform="douyin",
                                  account_id=account.id, customer_id=customer.id,
                                  guide_code="价格"))
        db.commit()

        token_resp = {"errcode": 0, "access_token": "tok_1", "expires_in": 7200}
        welcome_route = respx.post(channel_code.SEND_WELCOME_MSG_URL).mock(
            return_value=httpx.Response(200, json={"errcode": 0}))
        respx.get(channel_code.GET_TOKEN_URL).mock(
            return_value=httpx.Response(200, json=token_resp))

        encrypt = encrypt_message(_event_xml(state), receiveid="ww_test_corp")
        client = TestClient(app)
        resp = client.post("/api/wecom/callback", params=_signed_params(encrypt),
                           content=f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>",
                           headers={"Content-Type": "text/xml"})
        assert resp.status_code == 200
        assert resp.text == "success"

        event = db.query(models.FunnelEvent).filter(
            models.FunnelEvent.stage == "wecom",
            models.FunnelEvent.guide_code == state).first()
        assert event is not None
        assert event.account_id == account.id
        assert event.customer_id == customer.id
        db.refresh(customer)
        assert customer.wecom_added_at is not None
        assert welcome_route.called  # 欢迎语已在回调内同步调用

    @pytest.mark.asyncio
    async def test_unknown_state_still_200(self, db):
        """未匹配 state 的加粉事件：记录未归因事件且返回 success（防重推）。"""
        from fastapi.testclient import TestClient
        from app.main import app
        from app.wecom.crypto import encrypt_message

        state = f"unknown_{uuid.uuid4().hex[:8]}"
        encrypt = encrypt_message(_event_xml(state, welcome_code=""), receiveid="ww_test_corp")
        client = TestClient(app)
        resp = client.post("/api/wecom/callback", params=_signed_params(encrypt),
                           content=f"<xml><Encrypt><![CDATA[{encrypt}]]></Encrypt></xml>",
                           headers={"Content-Type": "text/xml"})
        assert resp.status_code == 200
        event = db.query(models.FunnelEvent).filter(
            models.FunnelEvent.stage == "wecom",
            models.FunnelEvent.guide_code == state).first()
        assert event is not None
        assert event.account_id == 0

    def test_bad_payload_still_200(self):
        from fastapi.testclient import TestClient
        from app.main import app

        client = TestClient(app)
        resp = client.post("/api/wecom/callback",
                           params={"msg_signature": "x", "timestamp": "1", "nonce": "2"},
                           content="<xml><Encrypt>not_base64!!!</Encrypt></xml>",
                           headers={"Content-Type": "text/xml"})
        assert resp.status_code == 200


class TestPushWecomCodeTool:
    def _ctx(self, customer_id: int):
        from app.agent.tools import ToolContext
        return ToolContext(conversation_id=1, customer_id=customer_id, platform="douyin")

    @pytest.mark.asyncio
    async def test_ok_path_writes_lead_event(self, db, monkeypatch):
        from app.agent.tools import execute_tool

        monkeypatch.setattr(settings, "wecom_corp_id", "ww_x")
        monkeypatch.setattr(settings, "wecom_secret", "s")
        customer = models.Customer(platform="douyin",
                                   platform_user_id=f"u_{uuid.uuid4().hex[:8]}",
                                   nickname="留资用户", tags=[], source_guide_code="价格")
        db.add(customer)
        db.commit()
        db.add(models.WecomChannelCode(name="默认码", state="st_1",
                                       qr_url="https://wework.qpic.cn/qr1"))
        db.commit()

        result = await execute_tool("push_wecom_code", {}, self._ctx(customer.id))
        assert result["ok"] is True
        # 全量跑时可能选到 TestCallback 持久化的抖音绑定活码，只断言返回了有效二维码链接
        assert "wework.qpic.cn" in result["data"]["text"]
        event = db.query(models.FunnelEvent).filter(
            models.FunnelEvent.stage == "lead",
            models.FunnelEvent.customer_id == customer.id).first()
        assert event is not None
        assert event.guide_code == "价格"

    @pytest.mark.asyncio
    async def test_not_configured_returns_false(self, db, monkeypatch):
        from app.agent.tools import execute_tool

        monkeypatch.setattr(settings, "wecom_corp_id", "")
        monkeypatch.setattr(settings, "wecom_secret", "")
        customer = models.Customer(platform="douyin",
                                   platform_user_id=f"u_{uuid.uuid4().hex[:8]}",
                                   nickname="用户B", tags=[])
        db.add(customer)
        db.commit()

        result = await execute_tool("push_wecom_code", {}, self._ctx(customer.id))
        assert result["ok"] is False
        assert "企业微信未配置" in result["error"]


def _override_agent(db, role="admin"):
    """用 dependency_overrides 注入测试客服（offline 避免污染 auto_assign）。"""
    from app.api.deps import get_current_agent, require_admin
    from app.core.security import hash_password
    from app.main import app

    agent = models.Agent(username=f"t_{uuid.uuid4().hex[:8]}", display_name="测试",
                         role=role, status="offline", password_hash=hash_password("x"))
    db.add(agent)
    db.commit()
    app.dependency_overrides[get_current_agent] = lambda: agent
    if role == "admin":
        app.dependency_overrides[require_admin] = lambda: agent
    return agent


def _clear_overrides(db, agent):
    from app.main import app
    app.dependency_overrides.clear()
    try:
        db.delete(agent)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


class TestChannelCodeApi:
    @respx.mock
    def test_create_code_via_api(self, db):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.wecom import channel_code

        agent = _override_agent(db)
        channel_code.reset_token_cache()
        respx.get(channel_code.GET_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={
                "errcode": 0, "access_token": "tok", "expires_in": 7200}))
        respx.post(channel_code.ADD_CONTACT_WAY_URL).mock(
            return_value=httpx.Response(200, json={
                "errcode": 0, "config_id": "cfg_1", "qr_code": "https://wework.qpic.cn/qr9"}))

        try:
            client = TestClient(app)
            resp = client.post("/api/funnel/codes", json={"name": "抖音A号活码"})
            assert resp.status_code == 200
            body = resp.json()
            assert body["config_id"] == "cfg_1"
            assert body["qr_url"].endswith("qr9")
            assert body["state"]
            assert db.query(models.WecomChannelCode).filter_by(name="抖音A号活码").count() == 1
        finally:
            _clear_overrides(db, agent)

    def test_create_code_manual_requires_qr(self, db, monkeypatch):
        monkeypatch.setattr(settings, "wecom_corp_id", "")
        monkeypatch.setattr(settings, "wecom_secret", "")
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            client = TestClient(app)
            resp = client.post("/api/funnel/codes", json={"name": "手工码"})
            assert resp.status_code == 400
            resp2 = client.post("/api/funnel/codes",
                                json={"name": "手工码", "qr_url": "https://wework.qpic.cn/x"})
            assert resp2.status_code == 200
            assert resp2.json()["qr_url"].endswith("/x")
        finally:
            _clear_overrides(db, agent)


class TestAutoSwitchesApi:
    def test_get_and_set(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            client = TestClient(app)
            resp = client.get("/api/settings/auto-switches")
            assert resp.status_code == 200
            original = resp.json()

            resp2 = client.put("/api/settings/auto-switches",
                               json={"comment_auto_reply_enabled": True})
            assert resp2.status_code == 200
            assert resp2.json()["comment_auto_reply_enabled"] is True
            assert settings.comment_auto_reply_enabled is True

            # 还原，避免污染其他用例
            client.put("/api/settings/auto-switches", json=original)
        finally:
            _clear_overrides(db, agent)

    def test_non_admin_forbidden(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db, role="agent")
        try:
            client = TestClient(app)
            resp = client.put("/api/settings/auto-switches",
                              json={"publish_auto_enabled": True})
            assert resp.status_code == 403
        finally:
            _clear_overrides(db, agent)
