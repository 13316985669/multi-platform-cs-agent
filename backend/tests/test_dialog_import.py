"""历史对话 xlsx 导入为停用 FAQ，待补不入库。"""
import io
import uuid
import zipfile

from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from app.api.deps import get_current_agent
from app.core.security import hash_password
from app.main import app
from app.models import Agent, Conversation, Customer, KnowledgeChunk, KnowledgeDoc, Message


def _sheet_xml() -> str:
    price = (
        "顾客 2026-09-10 10:00:00 怎么收费&#xA;"
        "刘小姐 2026-09-10 10:00:05 现在是我们10周年庆典3980的活动价(含3900的官费)，"
        "包含秘书服务，秘书地址，绿盒子全套"
    )
    interest = (
        "顾客 2026-09-10 11:00:00 我要注册香港公司&#xA;"
        "刘小姐 2026-09-10 11:00:05 您留个联系方式"
    )

    def row(n, valid, missed, content):
        return (
            f'<row r="{n}">'
            f'<c r="A{n}" t="str"><v>c{n}</v></c>'
            f'<c r="C{n}" t="str"><v>{valid}</v></c>'
            f'<c r="D{n}" t="str"><v>{missed}</v></c>'
            f'<c r="V{n}" t="str"><v>注册</v></c>'
            f'<c r="Y{n}" t="str"><v>香港林</v></c>'
            f'<c r="BS{n}" t="str"><v>2026-09-10 10:00:00</v></c>'
            f'<c r="CK{n}" t="str"><v>{content}</v></c>'
            f"</row>"
        )

    headers = (
        '<row r="1">'
        '<c r="A1" t="str"><v>对话ID</v></c>'
        '<c r="C1" t="str"><v>有效对话</v></c>'
        '<c r="D1" t="str"><v>遗漏对话</v></c>'
        '<c r="BS1" t="str"><v>对话创建时间</v></c>'
        '<c r="CK1" t="str"><v>对话内容</v></c>'
        "</row>"
    )
    rows = [
        headers,
        row(2, "有效", "未遗漏", price),
        row(3, "有效", "未遗漏", interest),
        row(4, "有效", "遗漏", price),
        row(5, "无效", "未遗漏", price),
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<sheetData>{''.join(rows)}</sheetData></worksheet>"
    )


def _xlsx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/worksheets/sheet1.xml", _sheet_xml())
    return buf.getvalue()


def test_import_dialogs_creates_disabled_faq_and_skips_gap(db):
    agent = Agent(
        username=f"import_{uuid.uuid4().hex[:8]}", display_name="导入", role="agent",
        status="offline", password_hash=hash_password("x"),
    )
    db.add(agent)
    db.commit()
    app.dependency_overrides[get_current_agent] = lambda: agent
    client = TestClient(app)
    titles = ("香港公司注册费用", "想注册香港公司")
    try:
        resp = client.post(
            "/api/knowledge/import-dialogs",
            files={"file": ("历史对话.xlsx", _xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["created"] == 1
        assert body["imported"] == 1
        assert body["skipped_missed"] == 1
        assert body["skipped_invalid"] == 1
        assert "想注册香港公司" in body["gap_titles"]

        doc = db.query(KnowledgeDoc).filter(KnowledgeDoc.title == "香港公司注册费用").one()
        assert doc.status == "disabled"
        assert doc.doc_type == "faq"
        assert doc.content.startswith("问：")
        assert "3980" in doc.content
        assert db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id == doc.id).count() == 0
        assert db.query(KnowledgeDoc).filter(KnowledgeDoc.title == "想注册香港公司").count() == 0

        again = client.post(
            "/api/knowledge/import-dialogs",
            files={"file": ("历史对话.xlsx", _xlsx(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
        assert again.status_code == 200, again.text
        assert again.json()["created"] == 0
        assert again.json()["updated"] == 1
        db.expire_all()
        docs = db.query(KnowledgeDoc).filter(KnowledgeDoc.title == "香港公司注册费用").all()
        assert len(docs) == 1
        assert docs[0].status == "disabled"
        assert docs[0].version == 2

        rejected = client.post(
            "/api/knowledge/import-dialogs",
            files={"file": ("notes.txt", b"hello", "text/plain")},
        )
        assert rejected.status_code == 400
    finally:
        app.dependency_overrides.pop(get_current_agent, None)
        doc_ids = [
            row.id for row in db.query(KnowledgeDoc).filter(KnowledgeDoc.title.in_(titles)).all()
        ]
        if doc_ids:
            db.query(KnowledgeChunk).filter(KnowledgeChunk.doc_id.in_(doc_ids)).delete(synchronize_session=False)
            db.query(KnowledgeDoc).filter(KnowledgeDoc.id.in_(doc_ids)).delete(synchronize_session=False)
        db.query(Agent).filter(Agent.id == agent.id).delete(synchronize_session=False)
        db.commit()


def test_offer_query_keeps_marketing_category():
    from app.rag.pipeline import _blocked_categories

    assert "营销话术" in _blocked_categories("注册需要什么资料")
    assert "留资话术" in _blocked_categories("注册多少钱")
    assert "营销话术" not in _blocked_categories("注册多少钱")


def test_intent_is_explicit_only():
    from app.agent.lead_flow import wants_to_proceed

    assert wants_to_proceed("我要注册香港公司")
    assert wants_to_proceed("可以，办吧")
    assert not wants_to_proceed("注册需要什么资料")
    assert not wants_to_proceed("怎么收费")


async def test_silence_followup_stops_at_two(db, monkeypatch):
    from app.services import sweep_lead_followups

    customer = Customer(platform="mock", platform_user_id=uuid.uuid4().hex[:12], nickname="测")
    db.add(customer)
    db.flush()
    conversation = Conversation(
        customer_id=customer.id, platform="mock", mode="ai", status="open",
        lead_followups=0, last_message_at=datetime.utcnow() - timedelta(minutes=10),
    )
    db.add(conversation)
    db.flush()
    db.add(Message(
        conversation_id=conversation.id, sender_type="ai", content="注册资料发给您了",
        created_at=conversation.last_message_at,
    ))
    db.add(KnowledgeDoc(
        title="还在线", doc_type="faq", category="跟进话术", status="active",
        content="问：跟进\n答：老板，您还在线吗？留个联系方式",
    ))
    db.commit()
    calls = []

    async def fake_send(conversation_id, content, **kwargs):
        calls.append(content)
        return Message(id=len(calls), conversation_id=conversation_id, sender_type="ai", content=content)

    monkeypatch.setattr("app.services.send_outbound", fake_send)
    assert await sweep_lead_followups() == 1
    assert await sweep_lead_followups() == 1
    assert await sweep_lead_followups() == 0
    db.expire_all()
    saved = db.get(Conversation, conversation.id)
    assert saved.lead_followups == 2
    assert len(calls) == 2
    db.query(KnowledgeDoc).filter(KnowledgeDoc.category == "跟进话术").delete(synchronize_session=False)
    db.query(Message).filter(Message.conversation_id == conversation.id).delete(synchronize_session=False)
    db.delete(saved)
    db.delete(customer)
    db.commit()


async def test_laike_silence_followup_is_skipped(db, monkeypatch):
    from app.config import settings
    from app.services import sweep_lead_followups

    monkeypatch.setattr(settings, "douyin_channel", "rpa")
    monkeypatch.setattr(settings, "douyin_account_type", "laike")
    customer = Customer(platform="douyin", platform_user_id=uuid.uuid4().hex[:12], nickname="来客")
    db.add(customer)
    db.flush()
    conversation = Conversation(
        customer_id=customer.id, platform="douyin",
        platform_conversation_id=f"lk_{uuid.uuid4().hex[:6]}:conv",
        mode="ai", status="open", lead_followups=0,
        last_message_at=datetime.utcnow() - timedelta(minutes=10),
    )
    db.add(conversation)
    db.flush()
    db.add(Message(
        conversation_id=conversation.id, sender_type="ai", content="已经回复过了",
        created_at=conversation.last_message_at,
    ))
    db.commit()
    calls = []

    async def fake_send(conversation_id, content, **kwargs):
        calls.append(content)
        return Message(id=1, conversation_id=conversation_id, sender_type="ai", content=content)

    monkeypatch.setattr("app.services.send_outbound", fake_send)
    assert await sweep_lead_followups() == 0
    assert calls == []
    db.query(Message).filter(Message.conversation_id == conversation.id).delete(synchronize_session=False)
    db.delete(conversation)
    db.delete(customer)
    db.commit()
