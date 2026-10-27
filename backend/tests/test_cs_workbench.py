"""客服工作台：分配上限、回头客、RAG 未命中硬转人工。"""
import asyncio
import time
import uuid
from datetime import datetime, timedelta

import pytest

from app.agent import engine, router
from app.core.security import hash_password
from app.models import Agent, Conversation, Customer, Message, MissedQuestion
from app.rag import pipeline as rag_pipeline


def _agent(db, *, status="active", max_concurrent=20, name="客服"):
    a = Agent(
        username=f"a_{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("x"),
        display_name=name,
        role="agent",
        status=status,
        max_concurrent=max_concurrent,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def test_auto_assign_least_load(db, conversation):
    db.query(Agent).update({Agent.status: "offline"})
    db.commit()
    busy = _agent(db, name="忙")
    idle = _agent(db, name="闲")
    other = Conversation(customer_id=conversation.customer_id, platform="mock",
                         mode="human", status="open", assignee_id=busy.id)
    db.add(other)
    conversation.mode = "pending"
    db.commit()

    assigned = router.auto_assign(db, conversation.id)
    db.commit()
    assert assigned == idle.id
    db.refresh(conversation)
    assert conversation.mode == "human"
    assert conversation.assignee_id == idle.id


def test_auto_assign_respects_capacity(db, conversation):
    db.query(Agent).update({Agent.status: "offline"})
    db.commit()
    full = _agent(db, max_concurrent=1, name="满")
    other = Conversation(customer_id=conversation.customer_id, platform="mock",
                         mode="human", status="open", assignee_id=full.id)
    db.add(other)
    conversation.mode = "pending"
    conversation.assignee_id = None
    db.commit()

    assigned = router.auto_assign(db, conversation.id)
    assert assigned is None
    db.refresh(conversation)
    assert conversation.mode == "pending"


def test_auto_assign_returning_customer(db, conversation):
    db.query(Agent).update({Agent.status: "offline"})
    db.commit()
    last = _agent(db, name="旧人")
    newbie = _agent(db, name="新人")
    closed = Conversation(
        customer_id=conversation.customer_id, platform="mock",
        mode="human", status="closed", assignee_id=last.id,
    )
    db.add(closed)
    conversation.mode = "pending"
    conversation.assignee_id = None
    db.commit()

    assigned = router.auto_assign(db, conversation.id)
    assert assigned == last.id
    assert assigned != newbie.id


@pytest.mark.asyncio
async def test_rag_miss_hard_handoff(db, conversation, monkeypatch):
    db.add(Message(conversation_id=conversation.id, sender_type="user",
                   msg_type="text", content="火星移民政策"))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(passed=False, contexts=[])

    llm_called = []

    async def fake_llm(*_a, **_k):
        llm_called.append(1)
        from app.schemas import AgentReply
        return AgentReply(reply_messages=["我先让同事确认"], intent="other", confidence=0.2, handoff=True, handoff_reason="low_confidence")

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.refresh(conversation)
    assert conversation.mode in ("pending", "human")
    assert llm_called
    missed = db.query(MissedQuestion).filter(MissedQuestion.conversation_id == conversation.id).first()
    assert missed is not None
    ai = db.query(Message).filter(Message.conversation_id == conversation.id, Message.sender_type == "ai").first()
    assert ai is not None
    assert "确认" in ai.content


def _latest_ai_message(db, conversation_id):
    return (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id, Message.sender_type == "ai")
        .order_by(Message.id.desc())
        .first()
    )


@pytest.mark.asyncio
async def test_prefixed_wechat_id_arranges_specialist(db, conversation, monkeypatch):
    """「微信 1208230482」：存留资并回安排专员，不再掉进 RAG 生成。"""
    from app.agent.lead_flow import ARRANGE
    from app.models import LeadCapture

    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    db.add(Message(conversation_id=conversation.id, sender_type="user",
                   msg_type="text", content="微信 1208230482"))
    db.commit()

    await engine.process_ai_reply(conversation.id)

    lead = db.query(LeadCapture).filter(LeadCapture.conversation_id == conversation.id).first()
    assert lead is not None
    assert lead.contact == "1208230482"
    assert lead.contact_type == "wechat"
    ai = _latest_ai_message(db, conversation.id)
    assert ai is not None
    assert ai.content == ARRANGE


@pytest.mark.asyncio
async def test_contact_question_replies_consistent_script(db, conversation, monkeypatch):
    """「怎么联系你们」和「好的 怎么联系你们」回复必须一致：确定性话术，不进 RAG。"""
    from app.agent.lead_flow import ASK_FULL

    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    for text in ("怎么联系你们", "好的 怎么联系你们"):
        db.add(Message(conversation_id=conversation.id, sender_type="user",
                       msg_type="text", content=text))
        db.commit()
        await engine.process_ai_reply(conversation.id)
        ai = _latest_ai_message(db, conversation.id)
        assert ai is not None
        assert ai.content == ASK_FULL


@pytest.mark.asyncio
async def test_ours_contact_uses_library_script(db, conversation, monkeypatch):
    """问「你们微信多少」：优先用话术库留资话术原句解释平台限制。"""
    from app.models import KnowledgeDoc
    from app.rag import pipeline as rag_pipeline

    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    doc = KnowledgeDoc(
        doc_type="faq", status="active", category="留资话术", title="留个电话",
        content="问：留个电话\n答：平台限制发不了联系方式，您发下电话或者微信，安排专员给您介绍",
    )
    db.add(doc)
    db.commit()
    try:
        db.add(Message(conversation_id=conversation.id, sender_type="user",
                       msg_type="text", content="你们微信多少"))
        db.commit()
        await engine.process_ai_reply(conversation.id)
        ai = _latest_ai_message(db, conversation.id)
        assert ai is not None
        assert "平台限制" in ai.content
    finally:
        # 共享测试库：删掉自种文档并失效 FAQ 钉住缓存，避免影响后续用例的兜底话术
        db.query(KnowledgeDoc).filter(KnowledgeDoc.id == doc.id).delete()
        db.commit()
        rag_pipeline.invalidate_faq_pins()


@pytest.mark.asyncio
async def test_pure_ack_waits_when_contact_asked(db, conversation, monkeypatch):
    """纯应答：上一条在要联系方式时回「请您稍等」，冷会话回通用接话。"""
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    db.add(Message(conversation_id=conversation.id, sender_type="user",
                   msg_type="text", content="好的"))
    db.commit()
    await engine.process_ai_reply(conversation.id)
    ai = _latest_ai_message(db, conversation.id)
    assert ai is not None
    assert ai.content == "好嘞，有啥想了解的随时问我"

    db.add(Message(conversation_id=conversation.id, sender_type="ai",
                   msg_type="text",
                   content="您这边联系方式多少呢，我们专员给您讲解一下办理的流程、资料和费用，让您参考了解下"))
    db.commit()
    db.add(Message(conversation_id=conversation.id, sender_type="user",
                   msg_type="text", content="好的"))
    db.commit()
    await engine.process_ai_reply(conversation.id)
    ai = _latest_ai_message(db, conversation.id)
    assert ai is not None
    assert ai.content == "好嘞，请您稍等"


@pytest.mark.asyncio
async def test_ai_send_stopped_when_taken_over(db, conversation, monkeypatch):
    """生成过程中切到人工后，后续 AI 气泡不再发出。"""
    from app.database import SessionLocal
    from app.schemas import AgentReply

    db.add(Message(conversation_id=conversation.id, sender_type="user",
                   msg_type="text", content="多少钱"))
    db.commit()

    async def flip_mode(query, history=None, top_k=3, summary=""):
        session = SessionLocal()
        try:
            conv = session.get(Conversation, conversation.id)
            conv.mode = "human"
            session.commit()
        finally:
            session.close()
        return rag_pipeline.RetrievalResult(
            passed=True, contexts=[{"content": "99 元", "source": "价格", "doc_id": 1}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["标准款 99"], intent="consult_price", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", flip_mode)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.rollback()
    ai = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).count()
    assert ai == 0
    db.refresh(conversation)
    assert conversation.mode == "human"


@pytest.mark.asyncio
async def test_agent_reply_reloads_after_send(db, conversation):
    """人工回复在另一个会话里提交后，接口按消息 id 重新读取，不再 refresh 已关闭对象。"""
    from app.api.conversations import agent_reply
    from app.core.security import hash_password
    from app.models import Agent
    from app.schemas import AgentMessageSend

    agent = Agent(
        username=f"reply_{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("x"),
        display_name="客服",
        role="agent",
        status="active",
    )
    db.add(agent)
    conversation.mode = "human"
    db.commit()
    db.refresh(agent)
    db.get(Conversation, conversation.id)

    msg = await agent_reply(
        conversation.id, AgentMessageSend(content="您好，我来处理"), agent, db,
    )
    assert msg.id
    assert msg.sender_type == "agent"
    assert "您好" in msg.content


def test_bad_case_records_user_question(db, conversation):
    from app.api.messages import mark_bad_case
    from app.core.security import hash_password
    from app.models import Agent, MissedQuestion
    from app.schemas import BadCaseMark

    agent = Agent(
        username=f"bad_{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("x"),
        display_name="客服",
        role="agent",
        status="active",
    )
    db.add(agent)
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="能开发票吗"))
    db.flush()
    ai = Message(conversation_id=conversation.id, sender_type="ai", msg_type="text", content="可以的")
    db.add(ai)
    db.commit()

    mark_bad_case(ai.id, BadCaseMark(bad_case=True, note="答错了"), agent, db)
    missed = db.query(MissedQuestion).filter(MissedQuestion.question == "能开发票吗").one()
    assert missed.status == "pending"
    mark_bad_case(ai.id, BadCaseMark(bad_case=True, note="再记一次"), agent, db)
    db.expire_all()
    missed = db.query(MissedQuestion).filter(MissedQuestion.question == "能开发票吗").one()
    assert missed.count == 2


def _owned_conversation(db, conversation, content, mode="human"):
    conversation.mode = mode
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content=content))
    db.commit()


@pytest.mark.asyncio
async def test_human_mode_hit_sends_reply(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    _owned_conversation(db, conversation, "面签资料清单")

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": "面签资料清单：身份证", "source": "注册资料"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["带上身份证就行"], intent="consult_feature", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id, allow_owned=True)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "human"
    ai = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one()
    assert "身份证" in ai.content


@pytest.mark.asyncio
async def test_human_mode_miss_leaves_note_and_reason(db, conversation, monkeypatch):
    _owned_conversation(db, conversation, "火星移民政策")

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(passed=False, reason="no_hits", rewritten_queries=[query])

    async def fake_llm(*_a, **_k):
        raise AssertionError("未命中不应生成")

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id, allow_owned=True)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "human"
    user = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "user",
    ).one()
    assert user.extra["retrieval"]["reason"] == "no_hits"
    note = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "system",
    ).one()
    assert note.content == "这条没对上资料，需要人工回复"
    assert db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).count() == 0


@pytest.mark.asyncio
async def test_human_mode_name_uses_fallback_not_internal_note(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    _owned_conversation(db, conversation, "我叫小明")

    async def fake_retrieve(query, history=None, top_k=3, summary="", broaden=True):
        return rag_pipeline.RetrievalResult(passed=False, reason="no_hits", rewritten_queries=[query])

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["好，小明，记下了"], intent="chitchat", confidence=0.9, handoff=False)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id, allow_owned=True)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "human"
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert "小明" in sent
    assert db.query(Message).filter(
        Message.conversation_id == conversation.id,
        Message.content == "这条没对上资料，需要人工回复",
    ).count() == 0


@pytest.mark.asyncio
async def test_ai_mode_name_stays_with_model_sentence(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="我叫小明"))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary="", broaden=True):
        return rag_pipeline.RetrievalResult(passed=False, reason="no_hits", rewritten_queries=[query])

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["好，小明，记下了"], intent="chitchat", confidence=0.9, handoff=False)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "ai"
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert "小明" in sent


@pytest.mark.asyncio
async def test_pure_greeting_stays_with_ai(db, conversation, monkeypatch):
    called = []

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        called.append(query)
        return rag_pipeline.RetrievalResult(passed=False, reason="no_hits")

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    for text in ("你好", "你好呀", "在吗？"):
        db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content=text))
        db.commit()
        await engine.process_ai_reply(conversation.id)
        db.expire_all()
        db.refresh(conversation)
        assert conversation.mode == "ai"
        ai = (
            db.query(Message)
            .filter(Message.conversation_id == conversation.id, Message.sender_type == "ai")
            .order_by(Message.id.desc())
            .first()
        )
        assert "没查到靠谱资料" not in ai.content
        assert ai.content == "您好老板，这边可以提供注册，开户，年审，地址挂靠等服务，您是想咨询什么呢"
        assert ai.extra["timing"]["total_ms"] >= 0
        assert ai.extra["timing"]["draft_ms"] == 0
    assert called == []
    assert db.query(MissedQuestion).filter(MissedQuestion.conversation_id == conversation.id).count() == 0
    assert db.query(Message).filter(
        Message.conversation_id == conversation.id,
        Message.content == "这条没对上资料，需要人工回复",
    ).count() == 0


@pytest.mark.asyncio
async def test_reply_total_includes_send_wait(db, conversation, monkeypatch):
    """总耗时在平台发送返回之后才写入，所以不小于这次发送等待。"""
    send_ms = 0

    class SlowAdapter:
        async def send(self, *_args, **_kwargs):
            nonlocal send_ms
            started = time.monotonic()
            await asyncio.sleep(0.05)
            send_ms = int((time.monotonic() - started) * 1000)
            return "slow-out"

    monkeypatch.setattr("app.services.get_send_adapter", lambda _platform: SlowAdapter())
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="你好"))
    db.commit()

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    ai = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id, Message.sender_type == "ai")
        .one()
    )
    assert send_ms > 0
    assert ai.extra["timing"]["total_ms"] >= send_ms
    assert ai.extra["reply_ms"] == ai.extra["timing"]["total_ms"]
    assert ai.extra["timing"]["draft_ms"] == 0


@pytest.mark.asyncio
async def test_greeting_plus_question_still_retrieves(db, conversation, monkeypatch):
    seen = []

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        seen.append(query)
        return rag_pipeline.RetrievalResult(passed=False, reason="no_hits", rewritten_queries=[query])

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="你好，面签资料清单",
    ))
    db.commit()
    await engine.process_ai_reply(conversation.id)
    assert seen == ["你好，面签资料清单"]


@pytest.mark.asyncio
async def test_human_greeting_sends_without_mode_change(db, conversation, monkeypatch):
    _owned_conversation(db, conversation, "你好")
    await engine.process_ai_reply(conversation.id, allow_owned=True)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "human"
    ai = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one()
    assert ai.content == "您好老板，这边可以提供注册，开户，年审，地址挂靠等服务，您是想咨询什么呢"
    assert db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "system",
    ).count() == 0


@pytest.mark.asyncio
async def test_empty_model_reply_hands_off(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="怎么办理"))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": "办理说明", "source": "资料"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=[], handoff=False)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode in ("pending", "human")
    ai = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one()
    assert "确认" in ai.content


@pytest.mark.asyncio
async def test_replay_only_unanswered_phrase(db, conversation, monkeypatch):
    from app import services
    from app.schemas import AgentReply

    customer_id = conversation.customer_id
    conversation.mode = "human"
    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text", content="面签资料清单",
    ))
    answered = Conversation(
        customer_id=customer_id, platform="mock", mode="human", status="open",
        platform_conversation_id=f"done_{uuid.uuid4().hex[:8]}",
    )
    greeting = Conversation(
        customer_id=customer_id, platform="mock", mode="ai", status="open",
        platform_conversation_id=f"hi_{uuid.uuid4().hex[:8]}",
    )
    db.add_all([answered, greeting])
    db.flush()
    db.add(Message(conversation_id=answered.id, sender_type="user", msg_type="text", content="面签资料清单"))
    db.flush()
    db.add(Message(conversation_id=answered.id, sender_type="ai", msg_type="text", content="已经回过"))
    db.add(Message(conversation_id=greeting.id, sender_type="user", msg_type="text", content="你好"))
    db.commit()

    seen = []

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        seen.append(query)
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": "面签资料清单：身份证", "source": "注册资料"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["带上身份证"], intent="consult_feature", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    count = await services.replay_unanswered_phrase("面签资料清单")
    db.expire_all()
    db.refresh(conversation)
    assert count == 1
    assert seen == ["面签资料清单"]
    assert conversation.mode == "human"
    reply = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one()
    assert "身份证" in reply.content
    assert db.query(Message).filter(
        Message.conversation_id == answered.id, Message.sender_type == "ai",
    ).count() == 1
    assert db.query(Message).filter(
        Message.conversation_id == greeting.id, Message.sender_type == "ai",
    ).count() == 0


@pytest.mark.asyncio
async def test_numbered_list_reply_uses_source_items(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="面签资料清单"))
    db.commit()
    source = "\n".join([
        "【面签资料清单】：",
        "1、董事个人身份证和港澳通行证",
        "2、 香港公司全套注册资料原件",
        "3、公司业务证明和个人地址证明",
        "4、开户调查问卷",
    ])

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": source, "source": "注册.md"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["需要准备开户调查问卷"], intent="consult_feature", confidence=0.8)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    monkeypatch.setattr(engine.settings, "humanize_base_delay_ms", 0)
    monkeypatch.setattr(engine.settings, "humanize_per_char_ms", 0)

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "ai"
    sent = "\n".join(
        row.content for row in db.query(Message).filter(
            Message.conversation_id == conversation.id, Message.sender_type == "ai",
        )
    )
    assert "董事个人身份证和港澳通行证" in sent
    assert "香港公司全套注册资料原件" in sent
    assert "公司业务证明和个人地址证明" in sent
    assert "开户调查问卷" in sent
    assert "需要准备开户调查问卷" not in sent
    assert "您要的我都列下面啦\n\n1、董事个人身份证和港澳通行证\n2、香港公司全套注册资料原件" in sent
    assert "2、 香港" not in sent
    assert db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).count() == 1


@pytest.mark.asyncio
async def test_colon_list_keeps_every_source_item(db, conversation, monkeypatch):
    """「1：」清单按原文整段发出，模型只写了前两条也不截断。"""
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="办理需要哪些材料",
    ))
    db.commit()
    source = "\n".join([
        "问：办理需要哪些材料",
        "相似：注册需要什么资料",
        "答：注册香港公司主要涉及：",
        "1：公司名称",
        "2：地址（我司提供）",
        "3：经营范围",
        "4：注册资金",
        "5：股东董事相关身份信息",
    ])

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": source, "source": "香港公司注册资料"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(
            reply_messages=["注册香港公司主要涉及：\n1：公司名称\n2：地址（我司提供）"],
            intent="consult_feature", confidence=0.8,
        )

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    monkeypatch.setattr(engine.settings, "humanize_base_delay_ms", 0)
    monkeypatch.setattr(engine.settings, "humanize_per_char_ms", 0)

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "ai"
    rows = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).all()
    assert len(rows) == 1
    sent = rows[0].content
    assert sent.startswith("注册香港公司主要涉及：")
    assert "1：公司名称" in sent
    assert "2：地址（我司提供）" in sent
    assert "3：经营范围" in sent
    assert "4：注册资金" in sent
    assert "5：股东董事相关身份信息" in sent
    assert "您要的我都列下面啦" not in sent
    assert "相似" not in sent


@pytest.mark.asyncio
async def test_duplicate_faq_styles_answer_list_only_once(db, conversation, monkeypatch):
    """两个 FAQ 只差标点风格（1. 名称；/ 1：名称）时，清单只发一份，不重复。"""
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="香港公司注册资料",
    ))
    db.commit()
    doc_a = "\n".join([
        "问：注册香港公司需要什么资料/条件/手续/材料",
        "相似：注册香港公司需要什么资料",
        "答：您好，注册香港公司主要涉及：",
        "1. 公司名称；",
        "2. 地址（我司提供）；",
        "3. 经营范围；",
        "4. 注册资金；",
        "5. 股东董事相关身份信息。",
        "以上资料您有什么不理解的地方吗？",
        "您留联系方式，专员给您讲解资料和费用。",
    ])
    doc_b = "\n".join([
        "问：注册需要什么资料",
        "答：您好，注册香港公司主要涉及：",
        "1：公司名称",
        "2：地址（我司提供），",
        "3：经营范围",
        "4：注册资金",
        "5:   股东董事相关身份信息",
    ])

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": doc_a, "source": "注册香港公司需要什么资料"},
                      {"content": doc_b, "source": "注册需要什么资料"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(
            reply_messages=["您好，注册香港公司主要涉及这些资料"],
            intent="consult_feature", confidence=0.8,
        )

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    monkeypatch.setattr(engine.settings, "humanize_base_delay_ms", 0)
    monkeypatch.setattr(engine.settings, "humanize_per_char_ms", 0)

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    rows = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).all()
    assert len(rows) == 1
    sent = rows[0].content
    # 每个条目只出现一份，另一种标点风格的副本被去掉
    assert sent.count("公司名称") == 1
    assert sent.count("经营范围") == 1
    assert sent.count("股东董事相关身份信息") == 1
    assert "1：公司名称" not in sent
    assert "以上资料您有什么不理解的地方吗" in sent


@pytest.mark.asyncio
async def test_catchup_answers_every_squeezed_question(db, conversation, monkeypatch):
    """连发多条不同问题时，被新消息挤掉没等到回复的旧问题逐条补答。"""
    from app.schemas import AgentReply

    for text in ("注册需要什么条件", "香港公司如何注册", "香港公司注册资料"):
        db.add(Message(
            conversation_id=conversation.id, sender_type="user", msg_type="text",
            content=text,
        ))
    db.commit()

    answered = []  # 每次应答实际处理的那条问题

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        answered.append(query)
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": f"问：{query}\n答：已为您解答", "source": query}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(
            reply_messages=["好的，这就为您解答"],
            intent="consult_feature", confidence=0.8,
        )

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    monkeypatch.setattr(engine.settings, "humanize_base_delay_ms", 0)
    monkeypatch.setattr(engine.settings, "humanize_per_char_ms", 0)

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    # 三条问题全部进入过应答流程（最新一条 + 两条补答）
    assert set(answered) == {"注册需要什么条件", "香港公司如何注册", "香港公司注册资料"}
    # 每条用户消息后面都有配对的 AI 回复，不再永久漏答
    rows = db.query(Message).filter(
        Message.conversation_id == conversation.id,
        Message.is_internal.is_(False),
    ).order_by(Message.id).all()
    assert [r.sender_type for r in rows] == ["user", "user", "user", "ai", "ai", "ai"]


@pytest.mark.asyncio
async def test_laike_skips_catchup(db, conversation, monkeypatch):
    """来客只答最新一条，不再把更早的未答问题补发出去。飞鸽/mock 的补答见上一个用例。"""
    from app.schemas import AgentReply

    conversation.platform = "douyin"
    conversation.platform_conversation_id = "lk_shop:conv_1"
    for text in ("注册需要什么条件", "香港公司如何注册", "香港公司注册资料"):
        db.add(Message(
            conversation_id=conversation.id, sender_type="user", msg_type="text",
            content=text,
        ))
    db.commit()
    answered = []

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        answered.append(query)
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": f"问：{query}\n答：已为您解答", "source": query}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(
            reply_messages=["好的，这就为您解答"],
            intent="consult_feature", confidence=0.8,
        )

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    monkeypatch.setattr(engine.settings, "humanize_base_delay_ms", 0)
    monkeypatch.setattr(engine.settings, "humanize_per_char_ms", 0)
    monkeypatch.setattr(engine.settings, "douyin_channel", "rpa")
    monkeypatch.setattr(engine.settings, "douyin_account_type", "laike")

    await engine.process_ai_reply(conversation.id)
    assert answered == ["香港公司注册资料"]


@pytest.mark.asyncio
async def test_kb_miss_and_empty_model_asks_contact(db, conversation, monkeypatch):
    """知识库没有，模型也没有回复时，用留资原句，不转人工。"""
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="我要注册香港公司",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(passed=False, reason="no_hits", rewritten_queries=[query])

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=[], intent="other", confidence=0.0, handoff=False)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "ai"
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert sent == "您这边联系方式多少呢，我们专员给您讲解一下注册香港公司"


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


@pytest.mark.asyncio
async def test_single_question_list_is_one_verbatim_message(db, conversation, monkeypatch):
    """问法没有「清单」，第二段召回里的 8 条也要原文进同一条消息。"""
    from app.schemas import AgentReply

    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="面签遵循哪些提示"))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[
                {"content": "面签时注意不要提及敏感国家", "source": "面签说明"},
                {"content": "\n".join(_MIANQIAN_ITEMS), "source": "面签提示"},
            ],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(
            reply_messages=["不要提及敏感国家", "进场的只有面签人员，别的不要进哈"],
            intent="other", confidence=0.9,
        )

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    rows = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).all()
    assert len(rows) == 1
    sent = rows[0].content
    for item in (
        "被制裁国家", "kyc", "不要进入银行", "不要戴耳机", "不能一问三不知",
        "不要拍照", "付钱开户", "理财保险", "开通好漫游",
    ):
        assert item in sent
    assert "不要提及敏感国家" not in sent


@pytest.mark.asyncio
async def test_other_list_without_list_wording_stays_verbatim(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="退货要注意什么"))
    db.commit()
    source = "\n".join([
        "1、保持包装完整",
        "2、七天内寄回",
        "3、附上订单号",
    ])

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": source, "source": "退货"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["包装别拆就行"], intent="after_sale", confidence=0.8)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    rows = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).all()
    assert len(rows) == 1
    sent = rows[0].content
    assert "保持包装完整" in sent
    assert "七天内寄回" in sent
    assert "附上订单号" in sent
    assert "包装别拆就行" not in sent


@pytest.mark.asyncio
async def test_named_one_list_item_does_not_dump_the_rest(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="开户调查问卷"))
    db.commit()
    source = "\n".join([
        "1、董事个人身份证和港澳通行证",
        "2、香港公司全套注册资料原件",
        "3、开户调查问卷",
    ])

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": source, "source": "开户"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["问卷填一下就行"], intent="consult_feature", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert "开户调查问卷" in sent
    assert "董事个人身份证" not in sent
    assert "香港公司全套注册资料" not in sent


@pytest.mark.asyncio
async def test_price_without_numbered_list_stays_price(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="这款多少钱"))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": "这款 99 元", "source": "价格"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["这款 99 元"], intent="consult_price", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert "99" in sent
    assert "您要的我都列下面啦" not in sent


@pytest.mark.asyncio
async def test_two_questions_stay_separate_messages(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="多少钱，还包邮吗",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rewritten_queries=[query],
            contexts=[{"content": "多少钱是 99 元，包邮是全国包邮", "source": "价格"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["这款 99 元", "全国包邮"], intent="consult_price", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    rows = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).all()
    assert len(rows) == 2
    sent = "\n".join(row.content for row in rows)
    assert "99" in sent
    assert "包邮" in sent


@pytest.mark.asyncio
async def test_miss_answers_from_earlier_dialogue(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="agent", msg_type="text",
        content="明天可以去香港开户",
    ))
    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="我说的是哪天去香港开户",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=False, reason="rerank_below_threshold", rewritten_queries=[query],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["你说的是明天"], intent="other", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "ai"
    reply = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one()
    assert "明天" in reply.content
    assert db.query(Message).filter(
        Message.conversation_id == conversation.id,
        Message.content == "这条没对上资料，需要人工回复",
    ).count() == 0
    assert db.query(MissedQuestion).filter(
        MissedQuestion.conversation_id == conversation.id,
    ).count() == 0


def test_earlier_dialogue_keeps_user_lines_and_drops_ungrounded_ai():
    rows = engine._earlier_dialogue("那我叫啥名字", [
        {"sender_type": "user", "content": "我叫小明啊"},
        {"sender_type": "ai", "content": "编的价格 99 元", "grounded": False},
        {"sender_type": "ai", "content": "你好，小明", "grounded": True},
        {"sender_type": "user", "content": "那我叫啥名字"},
    ])
    assert [row["content"] for row in rows] == ["我叫小明啊", "你好，小明"]


async def _miss_then_answer(monkeypatch, answer: str):
    from app.schemas import AgentReply

    prompts = []

    async def fake_retrieve(query, history=None, top_k=3, summary="", broaden=True):
        return rag_pipeline.RetrievalResult(passed=False, reason="no_hits", rewritten_queries=[query])

    async def fake_llm(rendered, *_a, **_k):
        prompts.append(rendered)
        return AgentReply(reply_messages=[answer], intent="other", confidence=0.9, handoff=False)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    return prompts


@pytest.mark.asyncio
async def test_followup_uses_name_the_user_already_said(db, conversation, monkeypatch):
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="我叫小明啊"))
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="那我叫啥名字"))
    db.commit()
    prompts = await _miss_then_answer(monkeypatch, "你叫小明")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "ai"
    rows = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).all()
    # 连发两问：最新一条先答，被挤掉的旧一条由补答跟上，每问都有配对回复
    assert len(rows) == 2
    sent = "\n".join(row.content for row in rows)
    assert "小明" in sent
    assert "你叫啥名字" not in sent
    assert any("我叫小明啊" in prompt and "不要再问一遍" in prompt for prompt in prompts)


@pytest.mark.asyncio
async def test_followup_uses_place_the_user_already_said(db, conversation, monkeypatch):
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="我今天要去香港"))
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="我刚才说去哪"))
    db.commit()
    prompts = await _miss_then_answer(monkeypatch, "你刚才说去香港")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    rows = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).all()
    # 连发两问逐条补答：每问都有配对回复
    assert len(rows) == 2
    sent = "\n".join(row.content for row in rows)
    assert "香港" in sent
    assert "去哪" not in sent
    assert any("我今天要去香港" in prompt for prompt in prompts)


@pytest.mark.asyncio
async def test_user_stated_price_is_not_repeated_as_fact(db, conversation, monkeypatch):
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="我觉得这个 3800 元"))
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="多少钱"))
    db.commit()
    prompts = await _miss_then_answer(monkeypatch, "是 3800 元")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    sent = "\n".join(
        row.content for row in db.query(Message).filter(
            Message.conversation_id == conversation.id, Message.sender_type == "ai",
        ).all()
    )
    assert "3800" not in sent
    assert any("3800" in prompt for prompt in prompts)


@pytest.mark.asyncio
async def test_rate_limit_keeps_body_when_already_human(db, conversation, monkeypatch):
    from app.core import ratelimit
    from app.services import send_outbound

    conversation.platform = "douyin"
    conversation.mode = "human"
    db.commit()
    ratelimit.reset_all()
    key = conversation.platform_conversation_id
    for _ in range(6):
        assert ratelimit.check_and_count("douyin", key)[0] is True

    sent = await send_outbound(conversation.id, "面签要带身份证", sender_type="ai", allow_owned=True)
    db.expire_all()
    assert sent is None
    assert db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).count() == 0
    note = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "system",
        Message.is_internal.is_(True),
    ).one()
    assert "频控" in note.content
    assert "身份证" in note.content
    assert conversation.mode == "human"
    ratelimit.reset_all()


def _msg(conversation, sender_type, content, *, minutes_ago=0, internal=False, extra=None):
    return Message(
        conversation_id=conversation.id,
        sender_type=sender_type,
        msg_type="text",
        content=content,
        is_internal=internal,
        extra=extra or {},
        created_at=datetime.utcnow() - timedelta(minutes=minutes_ago),
    )


def test_wait_badge_clears_after_ai_or_agent_reply(db, conversation):
    """最后一条用户消息后面有非内部 AI 或人工回复时，不再显示等待分钟数。"""
    from app.api.conversations import _to_out

    conversation.mode = "human"
    db.add(_msg(conversation, "user", "在吗", minutes_ago=5))
    db.commit()
    waiting = _to_out(db, conversation)
    assert waiting.awaiting_first_response is True
    assert waiting.wait_seconds >= 60

    db.add(_msg(conversation, "ai", "在的"))
    db.commit()
    assert _to_out(db, conversation).awaiting_first_response is False

    db.add(_msg(conversation, "user", "再问一下", minutes_ago=1))
    db.commit()
    again = _to_out(db, conversation)
    assert again.awaiting_first_response is True

    db.add(_msg(conversation, "agent", "我来回复"))
    db.commit()
    assert _to_out(db, conversation).awaiting_first_response is False


def test_internal_note_does_not_clear_wait_badge(db, conversation):
    from app.api.conversations import _to_out

    conversation.mode = "pending"
    db.add(_msg(conversation, "user", "需要人工", minutes_ago=2))
    db.add(_msg(conversation, "agent", "同事先看一下", internal=True))
    db.commit()
    out = _to_out(db, conversation)
    assert out.awaiting_first_response is True
    assert out.wait_seconds > 0


def test_ai_reply_keeps_transferred_in_flag(db, conversation):
    """AI 补答清掉等待计时，但不算人工接手，转入标记还在。"""
    from app.api.conversations import _to_out

    agent = _agent(db, name="接手")
    conversation.mode = "human"
    conversation.assignee_id = agent.id
    db.add(_msg(conversation, "user", "转过来了", minutes_ago=3))
    db.add(_msg(
        conversation, "system", "会话已转接",
        extra={"event": "transfer", "to_agent_id": agent.id},
    ))
    db.add(_msg(conversation, "ai", "我先答一下"))
    db.commit()

    out = _to_out(db, conversation, viewer_id=agent.id)
    assert out.awaiting_first_response is False
    assert out.transferred_in is True


_REGISTRATION_SECTION = "\n".join([
    "二、香港公司注册资料收集确认",
    "1、公司名称",
    "中文名：",
    "英文名：",
    "2、注册资本：X万港币",
    "3、经营范围：",
    "4、注册地址：",
    "董事+股东：证件号码、住址",
    "①证件照片（护照人像页，身份证正反面）",
    "②手持证件照片",
    "③签名样式（将名字签在白纸上）",
    "④住址证明材料（如果提供的证件照片上没有地址，就需要额外提供）",
])

_ACCOUNT_FAQ = "\n".join([
    "问：香港开户资料",
    "答：1、香港公司全套注册资料（CR/BR/NNC1 / 公司章程）；",
    "2、董事 / 股东个人资料：身份证 + 港澳通行证 / 护照（有效期 6 个月以上）",
    "3、公司业务证明：国内公司营业执照（如有）+ 近 3 个月购销合同 / 提单 / 发票 / 银行流水（2-3 套，体现真实经营）；",
    "4、董事 / 股东个人银行流水（近 6 个月，无断月，体现个人资金往来）；",
    "5、开户调查问卷（我司提供模板，需您如实填写签字）。",
])


@pytest.mark.asyncio
async def test_registration_howto_drops_account_opening_list(db, conversation, monkeypatch):
    """精排分再高，开户段排在前面也只发注册这一节。"""
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="行，香港公司我要注册，怎么弄",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, reason="passed", rerank_top_score=0.9792618751525879,
            contexts=[
                {"content": _ACCOUNT_FAQ, "source": "香港开户资料", "doc_id": 6},
                {"content": _REGISTRATION_SECTION, "source": "注册.md", "doc_id": 5},
            ],
        )

    async def verdicts(_user, contexts, *, timeout=4.0):
        return [0, 1][:len(contexts)]

    async def fake_llm(*_a, **_k):
        return AgentReply(
            reply_messages=["您要的我都列下面啦\n\n1、开户调查问卷"],
            intent="consult_feature", confidence=0.95,
        )

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr("app.agent.confidence.judge_relevance", verdicts)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "ai"
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one()
    assert "公司名称" in sent.content
    assert "注册资本" in sent.content
    assert "经营范围" in sent.content
    assert "证件照片" in sent.content
    assert "开户调查问卷" not in sent.content
    assert "购销合同" not in sent.content
    assert sent.content.count("4、") == 1
    score = sent.extra["answer_confidence"]
    assert score["score"] >= 0.6
    assert score["context_precision"] >= 0.6
    assert score["raw_context_precision"] < 0.6


@pytest.mark.asyncio
async def test_unscored_materials_question_hands_off(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="行，香港公司我要注册，怎么弄",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        # 0.85 < rag_judge_skip_score(0.9)：高分但不到快路径阈值，仍走 judge
        return rag_pipeline.RetrievalResult(
            passed=True, rerank_top_score=0.85,
            contexts=[{"content": _ACCOUNT_FAQ, "source": "香港开户资料", "doc_id": 6}],
        )

    async def no_verdict(*_a, **_k):
        return None

    from app.schemas import ToolCall

    tools_called = []

    async def fake_llm(*_a, **_k):
        return AgentReply(
            reply_messages=["1、开户调查问卷"], intent="consult_feature", confidence=0.99,
            tool_call=ToolCall(name="query_order", args={"phone": "13800138000"}),
        )

    async def fake_tool(*_a, **_k):
        tools_called.append(True)
        return {"ok": False, "data": None, "error": "不应执行"}

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr("app.agent.confidence.judge_relevance", no_verdict)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.tools, "execute_tool", fake_tool)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode in ("pending", "human")
    sent = "\n".join(
        row.content for row in db.query(Message).filter(
            Message.conversation_id == conversation.id, Message.sender_type == "ai",
        )
    )
    assert "确认" in sent
    assert "开户调查问卷" not in sent
    assert "免责" not in sent
    assert tools_called == []


@pytest.mark.asyncio
async def test_two_relevant_topics_ask_which_one(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="行，香港公司我要注册，怎么弄",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True,
            contexts=[
                {"content": _ACCOUNT_FAQ, "source": "香港开户资料"},
                {"content": _REGISTRATION_SECTION, "source": "注册.md"},
            ],
        )

    async def both(_user, contexts, *, timeout=4.0):
        return [1] * len(contexts)

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["1、开户调查问卷\n4、注册地址"], intent="consult_feature", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr("app.agent.confidence.judge_relevance", both)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "ai"
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert "注册" in sent and "开户" in sent
    assert "开户调查问卷" not in sent
    assert "1、" not in sent


@pytest.mark.asyncio
async def test_scope_question_without_knowledge_stays_in_scope(db, conversation, monkeypatch):
    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="做不做香港开户",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(passed=False, contexts=[])

    async def fake_llm(*_a, **_k):
        from app.schemas import AgentReply
        return AgentReply(reply_messages=["开户服务费 3800，资料要准备流水"], intent="consult_feature", confidence=0.2)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert conversation.mode == "ai"
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert "开户" in sent
    assert "3800" not in sent
    assert "流水" not in sent


@pytest.mark.asyncio
async def test_greeting_skips_answer_confidence(db, conversation, monkeypatch):
    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="你好"))
    db.commit()
    called = []

    async def verdicts(*_a, **_k):
        called.append(1)
        return [1]

    monkeypatch.setattr("app.agent.confidence.judge_relevance", verdicts)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    ai = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one()
    assert called == []
    assert "answer_confidence" not in (ai.extra or {})


async def _irrelevant_then_miss(monkeypatch, user_text: str):
    """第一次检索通过，判定都不相关，放宽后再检也没有。"""
    from app.agent import engine

    calls = {"n": 0}

    async def fake_retrieve(query, history=None, top_k=3, summary="", broaden=True):
        calls["n"] += 1
        if calls["n"] == 1:
            return rag_pipeline.RetrievalResult(
                passed=True, contexts=[{"content": "无关段落", "source": "其他"}],
            )
        return rag_pipeline.RetrievalResult(passed=False, contexts=[])

    async def none_relevant(_user, contexts, *, timeout=4.0):
        return [0] * len(contexts or [])

    async def fake_broaden(*_a, **_k):
        return "放宽后的问法"

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr("app.agent.confidence.judge_relevance", none_relevant)
    monkeypatch.setattr(engine.rewrite, "broaden_query", fake_broaden)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")


@pytest.mark.asyncio
async def test_irrelevant_chitchat_uses_fallback_sentence(db, conversation, monkeypatch):
    from app.agent import engine
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="哈哈哈你们回复好快",
    ))
    db.commit()
    await _irrelevant_then_miss(monkeypatch, "哈哈哈你们回复好快")
    called = []

    async def fake_llm(*_a, **_k):
        called.append(1)
        return AgentReply(
            reply_messages=["哈哈那是，我一直盯着呢"],
            intent="chitchat", confidence=0.9, handoff=False,
        )

    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    db.refresh(conversation)
    assert called
    assert conversation.mode == "ai"
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert "哈哈那是" in sent


@pytest.mark.asyncio
async def test_irrelevant_materials_question_does_not_invent_list(db, conversation, monkeypatch):
    from app.agent import engine
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="香港公司注册要准备什么",
    ))
    db.commit()
    await _irrelevant_then_miss(monkeypatch, "香港公司注册要准备什么")

    async def fake_llm(*_a, **_k):
        return AgentReply(
            reply_messages=["1、护照和公司章程", "注册费3800元", "我先让同事确认"],
            intent="consult_feature", confidence=0.4, handoff=True, handoff_reason="low_confidence",
        )

    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    sent = "\n".join(
        row.content for row in db.query(Message).filter(
            Message.conversation_id == conversation.id, Message.sender_type == "ai",
        )
    )
    assert "确认" in sent
    assert "3800" not in sent
    assert "1、" not in sent
    assert "护照" not in sent


@pytest.mark.asyncio
async def test_unscored_does_not_call_fallback_model(db, conversation, monkeypatch):
    from app.agent import engine

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="注册要准备什么资料",
    ))
    db.commit()
    called = []

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, contexts=[{"content": "无关段落", "source": "其他"}],
        )

    async def no_verdict(*_a, **_k):
        return None

    async def fake_llm(*_a, **_k):
        called.append(1)
        from app.schemas import AgentReply
        return AgentReply(reply_messages=["1、护照"], intent="other", confidence=0.2)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr("app.agent.confidence.judge_relevance", no_verdict)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    await engine.process_ai_reply(conversation.id)
    await asyncio.sleep(0)
    db.expire_all()
    # 判定失败前可能已经发出一次草稿，但不再补答，句子也不入库。
    assert called == [1]
    sent = "\n".join(
        row.content for row in db.query(Message).filter(
            Message.conversation_id == conversation.id, Message.sender_type == "ai",
        )
    )
    assert "确认" in sent
    assert "护照" not in sent


@pytest.mark.asyncio
async def test_filtered_context_discards_speculative_draft(db, conversation, monkeypatch):
    """判定筛掉一段后，按留下的资料重写，不发按旧段落写的草稿。"""
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="注册按什么走",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, rewritten_queries=[query],
            contexts=[
                {"content": "开户要准备护照原件", "source": "开户", "doc_id": 1, "chunk_id": "a"},
                {"content": "注册按资料走，不另收费", "source": "注册", "doc_id": 2, "chunk_id": "b"},
            ],
        )

    async def verdicts(_user, contexts, *, timeout=4.0):
        return [0, 1][:len(contexts)]

    async def fake_llm(prompt, **_k):
        if "护照" in prompt:
            return AgentReply(reply_messages=["开户要准备护照原件"], intent="consult_feature", confidence=0.9)
        return AgentReply(reply_messages=["注册按资料走，不另收费"], intent="consult_feature", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr("app.agent.confidence.judge_relevance", verdicts)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert "不另收费" in sent
    assert "护照" not in sent


@pytest.mark.asyncio
async def test_broadened_context_discards_speculative_draft(db, conversation, monkeypatch):
    """放宽后换成另一批资料时，不发按第一批写的草稿。"""
    from app.schemas import AgentReply

    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="那笔账怎么记",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary="", broaden=True):
        if query == "更宽的问法":
            return rag_pipeline.RetrievalResult(
                passed=True, rewritten_queries=[query],
                contexts=[{"content": "记账按资料每月一次", "source": "记账", "doc_id": 9, "chunk_id": "n"}],
            )
        return rag_pipeline.RetrievalResult(
            passed=True, rewritten_queries=[query],
            contexts=[{"content": "这段毫不相关", "source": "旧", "doc_id": 3, "chunk_id": "o"}],
        )

    async def fake_judge(_user, contexts, *, timeout=4.0):
        blob = " ".join((item.get("content") or "") for item in contexts)
        if "每月一次" in blob:
            return [1]
        return [0]

    async def fake_broaden(*_a, **_k):
        return "更宽的问法"

    async def fake_llm(prompt, **_k):
        if "毫不相关" in prompt:
            return AgentReply(reply_messages=["旧草稿不该出现"], intent="other", confidence=0.9)
        return AgentReply(reply_messages=["记账按资料每月一次"], intent="other", confidence=0.9)

    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr("app.agent.confidence.judge_relevance", fake_judge)
    monkeypatch.setattr(engine.rewrite, "broaden_query", fake_broaden)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    sent = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one().content
    assert "每月一次" in sent
    assert "旧草稿" not in sent


@pytest.mark.asyncio
async def test_cache_hit_ignores_parallel_draft(db, conversation, monkeypatch):
    from app.schemas import AgentReply

    db.add(Message(conversation_id=conversation.id, sender_type="user", msg_type="text", content="多少钱"))
    db.commit()

    async def fake_lookup(_query):
        return ["标准款 99 元"]

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, rewritten_queries=[query],
            contexts=[{"content": "标准款 99 元", "source": "价格", "doc_id": 1, "chunk_id": "c1"}],
        )

    async def fake_llm(*_a, **_k):
        return AgentReply(reply_messages=["并行草稿不该出现"], intent="consult_price", confidence=0.9)

    monkeypatch.setattr(engine.answer_cache, "lookup", fake_lookup)
    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    ai = db.query(Message).filter(
        Message.conversation_id == conversation.id, Message.sender_type == "ai",
    ).one()
    assert ai.content == "标准款 99 元"
    assert "并行草稿" not in ai.content
    assert ai.extra.get("cache_hit") is True
