"""工具调用架构测试：注册表 / 执行兜底 / 契约 tool_call 解析 / 两阶段流程。"""
from app.agent import engine, tools
from app.agent.tools import ToolContext
from app.models import Message, Ticket
from app.rag import pipeline as rag_pipeline
from app.schemas import AgentReply, ToolCall


def _ctx(conversation) -> ToolContext:
    return ToolContext(
        conversation_id=conversation.id,
        customer_id=conversation.customer_id,
        platform="mock",
    )


def _isolate_rag(monkeypatch):
    """隔离 RAG 检索（避免测试触发真实 LLM/Embedding 网络调用）。"""
    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True,
            contexts=[{"content": "测试资料", "source": "test"}],
        )
    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)


# ============ 注册表 ============

def test_builtin_tools_registered():
    assert "query_order" in tools.TOOL_REGISTRY
    assert "query_logistics" in tools.TOOL_REGISTRY
    assert "create_ticket" in tools.TOOL_REGISTRY


def test_tools_prompt_text():
    text = tools.tools_prompt_text()
    assert "query_order" in text
    assert "手机号" in text


# ============ 执行兜底 ============

async def test_execute_unknown_tool(conversation):
    result = await tools.execute_tool("not_exist", {}, _ctx(conversation))
    assert result["ok"] is False
    assert "不存在" in result["error"]


async def test_query_order_unconfigured(conversation):
    result = await tools.execute_tool("query_order", {"phone": "13812345678"}, _ctx(conversation))
    assert result["ok"] is False
    assert "未接入" in result["error"]
    blob = str(result)
    assert "DD20240901001" not in blob
    assert "中通" not in blob
    assert "杭州转运中心" not in blob


async def test_query_order_bad_phone(conversation):
    result = await tools.execute_tool("query_order", {"phone": "123"}, _ctx(conversation))
    assert result["ok"] is False


async def test_query_logistics_requires_order_id(conversation):
    result = await tools.execute_tool("query_logistics", {"order_id": ""}, _ctx(conversation))
    assert result["ok"] is False


async def test_create_ticket(db, conversation):
    result = await tools.execute_tool(
        "create_ticket",
        {"type": "refund", "title": "榨汁杯漏水退货", "content": "用户反馈商品破损"},
        _ctx(conversation),
    )
    assert result["ok"] is True
    ticket = db.query(Ticket).filter(Ticket.ticket_no == result["data"]["ticket_no"]).first()
    assert ticket is not None
    assert ticket.type == "refund"
    assert ticket.status == "open"
    assert ticket.conversation_id == conversation.id


# ============ 契约解析 ============

def test_contract_with_tool_call():
    raw = (
        '{"reply_messages": [], "intent": "after_sale", "confidence": 0.9, "handoff": false,'
        ' "handoff_reason": "", "tags": [], "lead": {"phone": "", "wechat": "", "note": ""},'
        ' "quick_action": "none", "tool_call": {"name": "query_order", "args": {"phone": "13812345678"}}}'
    )
    reply = engine._parse_contract(raw)
    assert reply.tool_call is not None
    assert reply.tool_call.name == "query_order"
    assert reply.tool_call.args["phone"] == "13812345678"


def test_contract_without_tool_call_compatible():
    """旧格式（无 tool_call 字段）必须兼容。"""
    raw = '{"reply_messages": ["在的呢"], "intent": "chitchat", "confidence": 0.9}'
    reply = engine._parse_contract(raw)
    assert reply.tool_call is None
    assert reply.reply_messages == ["在的呢"]


# ============ 两阶段流程 ============

async def test_two_phase_tool_flow(db, conversation, monkeypatch):
    """第一轮 LLM 输出 tool_call → 执行工具 → 结果回填 → 第二轮输出最终回复。"""
    # 准备用户消息
    db.add(Message(conversation_id=conversation.id, sender_type="user",
                   msg_type="text", content="我的快递到哪了"))
    db.commit()

    prompts: list[str] = []
    first = AgentReply(
        reply_messages=[], intent="after_sale", confidence=0.9,
        tool_call=ToolCall(name="query_logistics", args={"order_id": "DD20240901001"}),
    )
    final = AgentReply(
        reply_messages=["查到啦", "快递到杭州转运中心了"], intent="after_sale", confidence=0.9,
    )

    async def fake_llm(prompt: str, **_kwargs):
        prompts.append(prompt)
        return first if len(prompts) == 1 else final

    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    _isolate_rag(monkeypatch)

    await engine.process_ai_reply(conversation.id)

    assert len(prompts) == 2
    assert "工具调用结果" in prompts[1]
    assert "query_logistics" in prompts[1]
    ai_msgs = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id, Message.sender_type == "ai")
        .all()
    )
    blob = " ".join(m.content for m in ai_msgs)
    assert "杭州转运中心" not in blob
    assert "中通" not in blob
    assert "DD20240901001" not in blob


async def test_tool_failure_degrades_to_handoff(db, conversation, monkeypatch):
    """工具失败 → LLM 收到错误信息 → 输出转人工。"""
    db.add(Message(conversation_id=conversation.id, sender_type="user",
                   msg_type="text", content="查下快递"))
    db.commit()

    first = AgentReply(
        reply_messages=[], intent="after_sale", confidence=0.9,
        tool_call=ToolCall(name="query_logistics", args={"order_id": ""}),  # 缺参数 → 工具失败
    )
    final = AgentReply(
        reply_messages=["系统有点卡，我让同事帮您查哈"], intent="after_sale", confidence=0.5,
        handoff=True, handoff_reason="low_confidence",
    )
    prompts: list[str] = []

    async def fake_llm(prompt: str, **_kwargs):
        prompts.append(prompt)
        return first if len(prompts) == 1 else final

    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    _isolate_rag(monkeypatch)

    await engine.process_ai_reply(conversation.id)

    db.refresh(conversation)
    # 无在线客服时留 pending；有 active 客服时 auto_assign 会改成 human
    assert conversation.mode in ("pending", "human")
    assert "ok" in prompts[1]  # 工具错误结果已回填


async def test_tool_rounds_exhausted_handoff(db, conversation, monkeypatch):
    """工具轮次用尽且没有回复时，短句加转人工，不让客人消息空着。"""
    db.add(Message(conversation_id=conversation.id, sender_type="user",
                   msg_type="text", content="帮我查一下"))
    db.commit()

    async def always_tool(prompt: str, **_kwargs):
        return AgentReply(
            reply_messages=[], intent="after_sale", confidence=0.4,
            tool_call=ToolCall(name="query_logistics", args={"order_id": "DD1"}),
        )

    monkeypatch.setattr(engine, "_call_llm_with_retry", always_tool)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")
    _isolate_rag(monkeypatch)

    await engine.process_ai_reply(conversation.id)
    db.rollback()
    db.refresh(conversation)
    assert conversation.mode in ("pending", "human")
    ai = (
        db.query(Message)
        .filter(Message.conversation_id == conversation.id, Message.sender_type == "ai")
        .all()
    )
    assert any("卡了一下" in m.content for m in ai)
