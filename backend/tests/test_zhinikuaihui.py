"""知你快回回复接口：按插件 1.7.0 的请求/响应契约测试。"""
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import Conversation, Customer, Message

client = TestClient(app)


@pytest.fixture()
def zhini_key():
    previous = settings.zhini_reply_api_key
    settings.zhini_reply_api_key = "test-zhini-key"
    yield "test-zhini-key"
    settings.zhini_reply_api_key = previous


def _auth(key: str = "test-zhini-key", scheme: str = "bearer") -> dict:
    if scheme == "x-api-key":
        return {"X-API-Key": key}
    return {"Authorization": f"Bearer {key}"}


def _body(**overrides) -> dict:
    suffix = uuid.uuid4().hex[:8]
    body = {
        "event": "message.received",
        "request_id": f"req-{suffix}",
        "platform": "douyin",
        "conversation_id": f"chat-{suffix}",
        "conversation_name": "张先生",
        "is_group": False,
        "customer_id": f"customer-{suffix}",
        "customer_nickname": f"张先生{suffix}",
        "message": {
            "id": "msg-9",
            "text": "套餐怎么收费？",
            "direction": "incoming",
            "sender_id": f"customer-{suffix}",
            "sender_name": "张先生",
            "timestamp": 1784817000000,
            "type": "text",
        },
        "messages": [{
            "id": f"hist-{suffix}",
            "text": "在吗",
            "direction": "incoming",
            "sender_id": f"customer-{suffix}",
            "sender_name": "张先生",
            "timestamp": 1784816000000,
            "type": "text",
        }],
        "sent_at": "2026-07-23T14:30:00.000Z",
    }
    body.update(overrides)
    return body


class TestZhiniContract:
    def test_health_reports_zhini(self, zhini_key):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["zhini"] is True

    def test_missing_key_is_503_with_message(self):
        previous = settings.zhini_reply_api_key
        settings.zhini_reply_api_key = ""
        try:
            resp = client.post("/api/integrations/zhinikuaihui/reply", json=_body())
        finally:
            settings.zhini_reply_api_key = previous
        assert resp.status_code == 503
        assert "message" in resp.json()

    def test_bad_key_is_401_with_message(self, zhini_key):
        resp = client.post(
            "/api/integrations/zhinikuaihui/reply",
            json=_body(),
            headers=_auth("wrong-key"),
        )
        assert resp.status_code == 401
        assert resp.json()["message"]

    def test_connection_test_on_other_platform_does_not_persist(self, zhini_key):
        db = SessionLocal()
        try:
            before = db.query(Customer).count()
        finally:
            db.close()
        resp = client.post(
            "/api/integrations/zhinikuaihui/reply",
            json={
                "event": "message.received",
                "request_id": "connection-test-1",
                "platform": "wechat-channels",
                "conversation_id": "connection-test",
                "conversation_name": "接口连接测试",
                "message": {
                    "id": "test-message",
                    "text": "你好，请回复一条简短的连接测试内容。",
                    "direction": "incoming",
                    "timestamp": 1,
                    "type": "text",
                },
                "messages": [],
            },
            headers=_auth(zhini_key),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["action"] == "reply"
        assert body["reply"].strip()
        db = SessionLocal()
        try:
            assert db.query(Customer).count() == before
        finally:
            db.close()

    def test_x_api_key_auth(self, zhini_key):
        resp = client.post(
            "/api/integrations/zhinikuaihui/reply",
            json={
                "event": "message.received",
                "request_id": "connection-test-x",
                "platform": "douyin",
                "conversation_id": "connection-test",
                "message": {"id": "test-message", "text": "你好", "direction": "incoming"},
            },
            headers=_auth(zhini_key, "x-api-key"),
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "reply"

    def test_douyin_and_xhs_reply_without_platform_send(self, zhini_key):
        with patch("app.agent.engine.send_outbound", new_callable=AsyncMock) as send:
            for platform in ("douyin", "xiaohongshu", "xiaohongshu-sxt"):
                resp = client.post(
                    "/api/integrations/zhinikuaihui/reply",
                    json=_body(platform=platform),
                    headers=_auth(zhini_key),
                )
                assert resp.status_code == 200, platform
                assert resp.json()["action"] == "reply"
                assert resp.json()["reply"].strip()
            send.assert_not_called()

        db = SessionLocal()
        try:
            customers = db.query(Customer).filter(Customer.platform == "xiaohongshu").all()
            assert any(row.platform_user_id.startswith("zn:") and len(row.platform_user_id) == 63
                       for row in customers)
            stored = db.query(Message).filter(Message.platform_msg_id.like("zn:%")).all()
            assert stored
            assert all(len(row.platform_msg_id) == 63 for row in stored)
        finally:
            db.close()

    def test_short_page_id_does_not_collide_with_existing_message(self, zhini_key, conversation):
        db = SessionLocal()
        try:
            db.add(Message(
                conversation_id=conversation.id,
                sender_type="user",
                content="已有平台消息",
                platform_msg_id="msg-9",
            ))
            db.commit()
        finally:
            db.close()

        resp = client.post(
            "/api/integrations/zhinikuaihui/reply",
            json=_body(),
            headers=_auth(zhini_key),
        )
        assert resp.status_code == 200
        assert resp.json()["action"] == "reply"
        db = SessionLocal()
        try:
            original = db.query(Message).filter(Message.platform_msg_id == "msg-9").one()
            assert original.conversation_id == conversation.id
            assert original.content == "已有平台消息"
            zhini_rows = [
                row for row in db.query(Message).all()
                if (row.extra or {}).get("channel") == "zhinikuaihui"
                and (row.extra or {}).get("zhini_message_id") == "msg-9"
                and row.sender_type == "user"
            ]
            assert zhini_rows
            assert all(row.platform_msg_id.startswith("zn:") for row in zhini_rows)
        finally:
            db.close()

    def test_duplicate_request_does_not_generate_twice(self, zhini_key):
        body = _body()
        calls = {"n": 0}
        from app.agent.engine import process_ai_reply as real

        async def wrapped(conversation_id: int, local: bool = False, allow_owned: bool = False) -> str:
            calls["n"] += 1
            return await real(conversation_id, local=local, allow_owned=allow_owned)

        with patch("app.integrations.zhinikuaihui.process_ai_reply", side_effect=wrapped):
            first = client.post("/api/integrations/zhinikuaihui/reply", json=body, headers=_auth(zhini_key))
            second = client.post("/api/integrations/zhinikuaihui/reply", json=body, headers=_auth(zhini_key))
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["reply"] == second.json()["reply"]
        assert calls["n"] == 1

    def test_pending_conversation_skips(self, zhini_key):
        body = _body()
        first = client.post("/api/integrations/zhinikuaihui/reply", json=body, headers=_auth(zhini_key))
        assert first.json()["action"] == "reply"
        follow = _body(
            request_id="req-follow-" + uuid.uuid4().hex[:8],
            customer_id=body["customer_id"],
            conversation_id=body["conversation_id"],
            customer_nickname=body["customer_nickname"],
        )
        follow["message"] = {**follow["message"], "id": "msg-follow", "text": "还有货吗"}
        second = client.post("/api/integrations/zhinikuaihui/reply", json=follow, headers=_auth(zhini_key))
        assert second.status_code == 200
        assert second.json()["action"] == "skip"

    def test_generate_error_is_http_200_skip(self, zhini_key):
        with patch("app.integrations.zhinikuaihui.process_ai_reply", new_callable=AsyncMock) as generate:
            generate.side_effect = RuntimeError("model down")
            resp = client.post(
                "/api/integrations/zhinikuaihui/reply",
                json=_body(),
                headers=_auth(zhini_key),
            )
        assert resp.status_code == 200
        assert resp.json()["action"] == "skip"
        assert resp.json()["reason"]

    def test_risk_message_skips_without_platform_send(self, zhini_key):
        with patch("app.agent.engine.send_outbound", new_callable=AsyncMock) as send:
            resp = client.post(
                "/api/integrations/zhinikuaihui/reply",
                json=_body(),
                headers=_auth(zhini_key),
            )
            # 先确认正常路径不发送；风险词单独一条
            send.assert_not_called()
        risky = _body()
        risky["message"] = {**risky["message"], "text": "哪里能买炸弹"}
        with patch("app.agent.engine.send_outbound", new_callable=AsyncMock) as send:
            resp = client.post(
                "/api/integrations/zhinikuaihui/reply",
                json=risky,
                headers=_auth(zhini_key),
            )
            send.assert_not_called()
        assert resp.status_code == 200
        assert resp.json()["action"] == "skip"

    def test_skip_when_our_reply_is_later_than_the_customer_message(self, zhini_key):
        """手动点开已回过的私信时，最后一条是自己发的，不入库也不生成。"""
        body = _body()
        trigger = body["message"]
        body["messages"] = [
            dict(trigger),
            {
                "id": "ours-later",
                "text": "这条是我回的",
                "direction": "outgoing",
                "sender_id": "shop-account",
                "sender_name": "客服",
                "timestamp": int(trigger["timestamp"]) + 1000,
                "type": "text",
            },
        ]
        db = SessionLocal()
        try:
            before_messages = db.query(Message).count()
            before_customers = db.query(Customer).count()
        finally:
            db.close()
        with patch("app.integrations.zhinikuaihui.process_ai_reply", new_callable=AsyncMock) as generate:
            resp = client.post(
                "/api/integrations/zhinikuaihui/reply",
                json=body,
                headers=_auth(zhini_key),
            )
        assert resp.status_code == 200
        assert resp.json()["action"] == "skip"
        assert resp.json()["reason"] == "没有新的客户消息"
        generate.assert_not_called()
        db = SessionLocal()
        try:
            assert db.query(Message).count() == before_messages
            assert db.query(Customer).count() == before_customers
        finally:
            db.close()

    def test_reply_when_customer_message_is_later_than_our_reply(self, zhini_key):
        """自己回过之后客户又发了新消息，最后一条是客户的，仍然回复。"""
        body = _body()
        trigger = body["message"]
        trigger_ts = int(trigger["timestamp"])
        body["messages"] = [
            {
                "id": "older-customer",
                "text": "在吗",
                "direction": "incoming",
                "sender_id": body["customer_id"],
                "timestamp": trigger_ts - 2000,
                "type": "text",
            },
            {
                "id": "ours-earlier",
                "text": "在的",
                "direction": "outgoing",
                "sender_id": "shop-account",
                "timestamp": trigger_ts - 1000,
                "type": "text",
            },
        ]
        with patch("app.integrations.zhinikuaihui.process_ai_reply", new_callable=AsyncMock) as generate:
            generate.return_value = "继续回复"
            resp = client.post(
                "/api/integrations/zhinikuaihui/reply",
                json=body,
                headers=_auth(zhini_key),
            )
        assert resp.status_code == 200
        assert resp.json()["action"] == "reply"
        assert resp.json()["reply"] == "继续回复"
        generate.assert_awaited()


def _zhini_conversation(db, *, mode: str = "human"):
    uid = uuid.uuid4().hex[:8]
    customer = Customer(
        platform="douyin",
        platform_user_id=f"zn:user{uid}",
        nickname="张先生",
        tags=[],
    )
    db.add(customer)
    db.flush()
    conversation = Conversation(
        customer_id=customer.id,
        platform="douyin",
        platform_conversation_id=f"zn:conv{uid}",
        mode=mode,
        status="open",
    )
    db.add(conversation)
    db.commit()
    return conversation


def _forbid_platform_send(monkeypatch):
    def fail_send(_platform):
        raise AssertionError("知你快回会话不应调用平台发送")

    monkeypatch.setattr(settings, "douyin_channel", "rpa")
    monkeypatch.setattr("app.services.get_send_adapter", fail_send)
    monkeypatch.setattr("app.services.get_rpa_fallback_adapter", lambda _platform: None)


@pytest.mark.asyncio
async def test_agent_reply_on_zhini_stays_in_workbench(db, monkeypatch):
    """工作台人工回复只入库，不调开放平台，也不写 RPA outbox。"""
    from app.api.conversations import agent_reply
    from app.core.security import hash_password
    from app.models import Agent, RpaOutbox
    from app.schemas import AgentMessageSend

    _forbid_platform_send(monkeypatch)
    conversation = _zhini_conversation(db)
    before = db.query(RpaOutbox).count()
    agent = Agent(
        username=f"zhini_{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("x"),
        display_name="客服",
        role="agent",
        status="active",
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)

    msg = await agent_reply(
        conversation.id, AgentMessageSend(content="我在工作台回你"), agent, db,
    )
    assert msg.sender_type == "agent"
    assert msg.content == "我在工作台回你"
    assert msg.extra["channel"] == "zhinikuaihui"
    assert msg.extra["not_delivered_to_page"] is True
    db.expire_all()
    stored = db.get(Message, msg.id)
    assert stored.platform_msg_id is None
    assert db.query(RpaOutbox).count() == before


@pytest.mark.asyncio
async def test_zhini_sweep_close_message_stays_local(db, monkeypatch):
    """超时结束语只写在本系统，然后关闭会话。"""
    from app.main import _sweep_once
    from app.models import RpaOutbox

    _forbid_platform_send(monkeypatch)
    conversation = _zhini_conversation(db, mode="ai")
    conversation.last_message_at = datetime.utcnow() - timedelta(minutes=60)
    db.commit()
    before = db.query(RpaOutbox).count()

    closed = await _sweep_once()

    assert conversation.id in closed
    db.expire_all()
    db.refresh(conversation)
    assert conversation.status == "closed"
    close_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id, Message.sender_type == "ai")
        .one()
    )
    assert close_msg.extra["channel"] == "zhinikuaihui"
    assert close_msg.extra["not_delivered_to_page"] is True
    assert close_msg.platform_msg_id is None
    assert db.query(RpaOutbox).count() == before


@pytest.mark.asyncio
async def test_replay_skips_zhini_conversation(db, monkeypatch):
    """启动补跑不给知你快回会话再生成一条送不出去的回复。"""
    from app.services import replay_unanswered_phrase

    conversation = _zhini_conversation(db, mode="ai")
    phrase = f"面签资料清单{uuid.uuid4().hex[:8]}"
    db.add(Message(
        conversation_id=conversation.id,
        sender_type="user",
        msg_type="text",
        content=phrase,
    ))
    db.commit()
    called = []

    async def spy(conversation_id, user_message_id):
        called.append(conversation_id)

    monkeypatch.setattr("app.services._resume_if_unanswered", spy)
    count = await replay_unanswered_phrase(phrase)
    assert count == 0
    assert conversation.id not in called


_MIANQIAN_ITEMS = [
    "1、香港开户请勿与开户经理提及被制裁国家，您的所有生意和转账地区仅限kyc内填写的地区国家。",
    "2、去银行开户仅需要面签人员进场，其他人员不要进入银行。",
    "3、去银行不要左顾右盼，不要戴耳机。对工作人员礼貌一些。",
    "4、对自己的生意模式、合作伙伴、公司基本信息需要了如指掌，不能一问三不知。",
    "5、在银行内部不要拍照、拍视频等。",
    "6、开户请勿提及付钱开户，一律回复自己预约的银行开户。",
    "7、如果经理推理财保险，不需要的话请委婉拒绝说要先了解一下",
    "8、董事手机提前开通好漫游，用来接受银行短信",
]


def _glued_mianqian() -> str:
    """开头两个导语粘在一起，第 8 条末尾再粘上一份同样的清单。"""
    one = "您要的我都列下面啦\n\n" + "\n".join(_MIANQIAN_ITEMS)
    return "您要的我都列下面啦" + one.rstrip("\n") + one


def test_glued_notice_is_returned_once(zhini_key):
    """两份面签提示粘在一起时，插件只收到一份，1 到 8 条都还在。"""
    from app.services import record_local_ai_message

    written = _glued_mianqian()

    async def generate(conversation_id, local=True, allow_owned=False):
        await record_local_ai_message(conversation_id, written, allow_owned=allow_owned)
        return written

    with patch("app.integrations.zhinikuaihui.process_ai_reply", side_effect=generate), \
            patch("app.integrations.zhinikuaihui._shorten_written_reply", new_callable=AsyncMock) as shorten:
        resp = client.post(
            "/api/integrations/zhinikuaihui/reply",
            json=_body(),
            headers=_auth(zhini_key),
        )
    assert resp.status_code == 200
    reply = resp.json()["reply"]
    assert reply.count("您要的我都列下面啦") == 1
    for item in _MIANQIAN_ITEMS:
        assert reply.count(item) == 1
    assert len(reply) <= 500
    shorten.assert_not_called()
    db = SessionLocal()
    try:
        stored = db.query(Message).filter(Message.sender_type == "ai", Message.content == written).one()
        assert stored.content == written
    finally:
        db.close()


def test_over_limit_shortens_the_written_reply(zhini_key):
    """去重后仍超过 500 字时，缩短的是这条已写好的长回复。"""
    written = "".join(
        f"第{i}句：面签时要带齐各自的证件并说清各自的生意情况，这句和别的句子都不一样。"
        for i in range(1, 25)
    )
    assert len(written) > 500
    brief = "面签时带齐证件，说清生意，不要提敏感国家。"
    seen = {}

    async def generate(conversation_id, local=True, allow_owned=False):
        return written

    async def shorten(text):
        seen["text"] = text
        return brief

    with patch("app.integrations.zhinikuaihui.process_ai_reply", side_effect=generate), \
            patch("app.integrations.zhinikuaihui._shorten_written_reply", side_effect=shorten):
        resp = client.post(
            "/api/integrations/zhinikuaihui/reply",
            json=_body(),
            headers=_auth(zhini_key),
        )
    assert resp.status_code == 200
    assert resp.json()["reply"] == brief
    assert seen["text"] == written
    assert len(resp.json()["reply"]) <= 500


@pytest.mark.asyncio
async def test_shorten_failure_cuts_at_complete_entries():
    """模型没有给出可用结果时，从最后一个完整条目处收到 500 字以内。"""
    from app.integrations.zhinikuaihui import prepare_plugin_reply

    lines = [f"{i}、这是一条足够长的面签注意事项，用来把全文撑过五百字。" for i in range(1, 21)]
    written = "\n".join(lines)
    assert len(written) > 500

    async def fail(_text):
        return ""

    with patch("app.integrations.zhinikuaihui._shorten_written_reply", side_effect=fail):
        reply = await prepare_plugin_reply(written)
    assert len(reply) <= 500
    kept = reply.split(" ")
    assert kept
    assert kept[0] == lines[0]
    assert kept[-1] in lines
    assert lines[lines.index(kept[-1]) + 1] not in kept


def test_clean_multi_line_reply_is_sent_to_plugin_single_line(zhini_key):
    """多行干净回复压成单行：插件对多行回复的输入框粘贴会重复拼接。"""
    written = (
        "现在是我们10周年庆典3980的活动价，含3900的官费，包含秘书服务、秘书地址、绿盒子全套。\n"
        "您留个联系方式，给您预留一个优惠名额。\n"
        "注册香港公司一般1-3个工作日可以下来，注册不用本人到港。"
    )

    async def generate(conversation_id, local=True, allow_owned=False):
        return written

    with patch("app.integrations.zhinikuaihui.process_ai_reply", side_effect=generate), \
            patch("app.integrations.zhinikuaihui._shorten_written_reply", new_callable=AsyncMock) as shorten:
        resp = client.post(
            "/api/integrations/zhinikuaihui/reply",
            json=_body(),
            headers=_auth(zhini_key),
        )
    assert resp.status_code == 200
    reply = resp.json()["reply"]
    assert "\n" not in reply
    for part in written.split("\n"):
        assert reply.count(part) == 1
    shorten.assert_not_called()


def test_duplicated_prose_segments_collapse_before_shorten(zhini_key):
    """插件式重复段落 [A][A][B][C][A][B][C] 先句级去重再判断长度，不再调缩短模型。"""
    a = "现在是我们10周年庆典3980的活动价，含3900的官费，包含秘书服务、秘书地址、绿盒子全套。"
    b = "您留个联系方式，给您预留一个优惠名额。"
    c = "注册香港公司一般1-3个工作日可以下来，注册不用本人到港。"
    written = f"{a}{a}{b}{c}{a}{b}{c}" * 3
    assert len(written) > 500

    async def generate(conversation_id, local=True, allow_owned=False):
        return written

    with patch("app.integrations.zhinikuaihui.process_ai_reply", side_effect=generate), \
            patch("app.integrations.zhinikuaihui._shorten_written_reply", new_callable=AsyncMock) as shorten:
        resp = client.post(
            "/api/integrations/zhinikuaihui/reply",
            json=_body(),
            headers=_auth(zhini_key),
        )
    assert resp.status_code == 200
    assert resp.json()["reply"] == f"{a}{b}{c}"
    shorten.assert_not_called()


def test_flattened_list_items_are_space_joined(zhini_key):
    """清单条目压平后用空格相连，条目正文一字不改。"""
    written = (
        "您要的我都列下面啦\n\n"
        "您好，香港公司开户主要涉及：\n"
        "1.股东及董事相关身份信息；\n"
        "2.港澳通行证或者护照；\n"
        "3.个人及公司银行流水\n"
        "4.香港公司相关资料及业务合同。\n"
        "以上资料您有什么不理解的地方吗？"
    )

    async def generate(conversation_id, local=True, allow_owned=False):
        return written

    with patch("app.integrations.zhinikuaihui.process_ai_reply", side_effect=generate), \
            patch("app.integrations.zhinikuaihui._shorten_written_reply", new_callable=AsyncMock) as shorten:
        resp = client.post(
            "/api/integrations/zhinikuaihui/reply",
            json=_body(),
            headers=_auth(zhini_key),
        )
    assert resp.status_code == 200
    reply = resp.json()["reply"]
    assert "\n" not in reply
    assert reply == " ".join(line for line in written.split("\n") if line.strip())
    shorten.assert_not_called()
