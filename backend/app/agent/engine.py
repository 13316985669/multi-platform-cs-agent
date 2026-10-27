"""Agent 引擎：提示词渲染 → LLM 调用 → 契约校验 → 工具调用（两阶段）→ 动作执行。

动作：分条发送回复 / 打标签 / 留资提取 / 转人工 / 业务工具调用（查订单/查物流/建工单）。
工具调用循环：LLM 输出 tool_call → 执行工具 → 结果回填提示词 → 二次生成，最多 2 次防死循环。
LLM 未配置时降级：固定话术 + 直接转人工，保证全流程可跑通。
主模型硬失败（网络/服务异常）时自动切换备用模型（LLM_FALLBACK_*）。
"""
import asyncio
import contextvars
import json
import logging
import re
import time
from datetime import datetime

from openai import APITimeoutError
from pydantic import ValidationError

from ..config import settings
from ..core import monitor
from ..database import SessionLocal
from ..llm import get_llm_client, record_usage
from ..models import Conversation, Customer, KnowledgeDoc, Message
from ..rag import cache as answer_cache, pipeline, rewrite
from ..schemas import AgentReply
from ..services import handoff, note_unmatched, record_missed_question, send_outbound
from . import confidence, context, evidence, humanize, lead_flow, prompt as prompt_mod, tools

logger = logging.getLogger(__name__)

_STAGE_KEYS = ("rewrite_ms", "embed_ms", "rerank_ms", "judge_ms", "draft_ms", "fallback_ms")
_reply_trace: contextvars.ContextVar[dict | None] = contextvars.ContextVar("reply_trace", default=None)

MAX_TOOL_ROUNDS = 2  # 工具调用上限（防死循环）
CATCHUP_MAX_MESSAGES = 3  # 一次接待后最多补答几条夹在中间的旧问题（防风暴）
REPLY_BUDGET_SECONDS = 45
DRAFT_RESERVE_SECONDS = 15
MIN_LLM_SECONDS = 4
FALLBACK_TIMEOUT_SECONDS = 15
_CONFIRM_REPLY = "我先让同事帮您确认"
_TASK_RE = re.compile(r"订单|物流|快递|退货|退换|换货|退款")
_FACT_TOOLS = frozenset({"query_order", "query_logistics"})
_HELLO_REPLY = "您好老板，这边可以提供注册，开户，年审，地址挂靠等服务，您是想咨询什么呢"
_THANKS_REPLY = "客气啦，有问题随时喊我"
_LAUGH_REPLY = "哈哈，注册还是开户，您想先看哪块"
_GREETING_REPLIES = {"谢谢": _THANKS_REPLY, "哈哈": _LAUGH_REPLY}
_LIST_ITEM_RE = re.compile(r"^\s*(?:\d+\s*[.、．:：)）]|[①②③④⑤⑥⑦⑧⑨⑩])\s*\S")
_LIST_MARKER_RE = re.compile(r"^(?:\d+\s*[.、．:：)）]|[①②③④⑤⑥⑦⑧⑨⑩])\s*")
_COLON_ITEM_RE = re.compile(r"^\d+\s*[:：]")
_LIST_ANNOUNCE_RE = re.compile(r"(?:如下|分别是|主要包括)[：:。．]?$")
# 条目去重用的标点归一：同一份清单常见「1：公司名称」和「1. 公司名称；」两种风格，
# 只按原文精确比较会把同一项收两遍，答案里清单就重复了。
_ITEM_PUNCT_RE = re.compile(r"[\s。；，、,;.:：()（）·…！!?？\"'“”‘’]+")
_LIST_LEAD = "您要的我都列下面啦"
_EMPTY_REPLY = "不好意思，我这边没说清楚，先帮您转给同事哈"
_MISS_REPLY = "这个问题我这边没查到靠谱资料，先帮您转给同事哈"
_GREETINGS = frozenset({
    "你好", "您好", "在吗", "在么", "嗨", "哈喽", "早上好", "谢谢", "哈哈",
    "hi", "hello",
})
_GREETING_TAILS = "呀啊呢哈哦"
# 纯应答：整条只是「好的/嗯/ok」。上一条客服消息在要联系方式或安排专员时，应答=等专员。
_ACKS = frozenset({
    "好的", "好嘞", "好滴", "好呀", "好", "嗯", "嗯嗯", "哦", "哦哦", "噢",
    "ok", "okay", "行", "可以", "中",
})
_ACK_WAIT_REPLY = "好嘞，请您稍等"
_ACK_GENERIC_REPLY = "好嘞，有啥想了解的随时问我"
_ACK_CONTEXT_RE = re.compile(r"联系方式|电话|微信|专员|稍等")
# 联系意图：问怎么联系/要联系方式。答案固定，走确定性话术，不进 RAG。
_CONTACT_ASK_RE = re.compile(
    r"怎么联系|如何联系|怎么沟通|如何沟通|联系方式|联系你们|微信多少|电话多少|号码多少|联系电话|留个电话"
)
# 问的是「我们的」联系方式：平台发不了，用留资话术原句解释并请对方留联系方式。
_OURS_CONTACT_RE = re.compile(
    r"(微信|电话|号码|联系方式)(是多少|多少)|你们.{0,3}(微信|电话|号码|联系方式)|留个电话"
)


def _greeting_key(text: str) -> str | None:
    """抽出纯招呼的词。后面还带着具体问题时返回 None。"""
    cleaned = re.sub(r"[\s，。！？、~～!?,.·…]+", "", text or "")
    if cleaned in _GREETINGS:
        return cleaned
    if cleaned and cleaned[-1] in _GREETING_TAILS and cleaned[:-1] in _GREETINGS:
        return cleaned[:-1]
    return None


def is_pure_greeting(text: str) -> bool:
    """整句只是招呼。后面还带着具体问题时返回 False，继续检索。"""
    return _greeting_key(text) is not None


def greeting_reply(text: str) -> str:
    """你好、哈喽用开场；谢谢和哈哈用短句，不套业务介绍。"""
    return _GREETING_REPLIES.get(_greeting_key(text) or "", _HELLO_REPLY)


def is_pure_ack(text: str) -> bool:
    """整条只是应答（好的/嗯/ok）。后面还带着问题时返回 False，继续走检索。"""
    cleaned = re.sub(r"[\s，。！？、~～!?,.·…]+", "", text or "")
    return cleaned.lower() in _ACKS


def _ack_reply(recent: list[dict]) -> str:
    """看最近一条客服消息：在要联系方式或安排专员时，应答=请稍等；否则通用接话。"""
    for row in reversed(list(recent or [])):
        if row.get("sender_type") in ("ai", "agent"):
            if _ACK_CONTEXT_RE.search(row.get("content") or ""):
                return _ACK_WAIT_REPLY
            return _ACK_GENERIC_REPLY
    return _ACK_GENERIC_REPLY


def asks_contact(text: str) -> bool:
    """在问怎么联系/要联系方式。只匹配固定短语，不碰「联系我」这类交代语。"""
    return bool(_CONTACT_ASK_RE.search(text or ""))


def _contact_intent_reply(user_text: str, recent: list[dict]) -> str:
    """问我们的联系方式：优先留资话术原句。问怎么联系：按业务拼 contact_ask。"""
    business = lead_flow.business_phrase(user_text, recent)
    if _OURS_CONTACT_RE.search(user_text or ""):
        return (
            lead_flow.library_script("留资话术")
            or lead_flow.contact_ask(business)
        )
    return lead_flow.contact_ask(business)


def _activate_trace() -> contextvars.Token:
    stages = {key: 0 for key in _STAGE_KEYS}
    return _reply_trace.set({"started": time.monotonic(), "stages": stages, "logged": False})


def _trace_started() -> float:
    state = _reply_trace.get()
    if state is None:
        return time.monotonic()
    return state["started"]


def _add_stage_ms(key: str, elapsed_ms: int) -> None:
    state = _reply_trace.get()
    if state is None:
        return
    state["stages"][key] = int(state["stages"].get(key, 0)) + int(elapsed_ms)


def _stamp_extra(extra: dict | None) -> dict:
    """把这一轮各段耗时写进即将发出的消息。总耗时留到入库前再写。"""
    merged = dict(extra or {})
    state = _reply_trace.get()
    if state is None:
        return merged
    merged["timing"] = {key: int(state["stages"].get(key, 0)) for key in _STAGE_KEYS}
    # 本轮正在回答的用户消息 id：补答判定的显式配对依据，随每条 AI 气泡入库
    if state.get("answer_message_id") is not None and "answers_message_id" not in merged:
        merged["answers_message_id"] = state["answer_message_id"]
    return merged


def refresh_reply_timing(extra: dict | None) -> dict:
    """复制 extra，并在本轮计时里写下到此刻的总耗时。没有计时上下文时只复制。"""
    merged = dict(extra or {})
    state = _reply_trace.get()
    if state is None:
        return merged
    timing = dict(merged.get("timing") or {})
    for key in _STAGE_KEYS:
        timing.setdefault(key, int(state["stages"].get(key, 0)))
    total_ms = int((time.monotonic() - state["started"]) * 1000)
    timing["total_ms"] = total_ms
    merged["timing"] = timing
    merged["reply_ms"] = total_ms
    return merged


def _log_reply_timing() -> None:
    """一条回答结束时打一次总耗时。没入库的尝试也记这一行。"""
    state = _reply_trace.get()
    if state is None or state.get("logged"):
        return
    state["logged"] = True
    total_ms = int((time.monotonic() - state["started"]) * 1000)
    stages = state["stages"]
    logger.info(
        "客服回复耗时 total_ms=%s rewrite_ms=%s embed_ms=%s rerank_ms=%s "
        "judge_ms=%s draft_ms=%s fallback_ms=%s",
        total_ms, int(stages.get("rewrite_ms", 0)), int(stages.get("embed_ms", 0)),
        int(stages.get("rerank_ms", 0)), int(stages.get("judge_ms", 0)),
        int(stages.get("draft_ms", 0)), int(stages.get("fallback_ms", 0)),
    )


def _abandon_draft(task: asyncio.Task | None) -> None:
    """不等作废草稿。回调取走异常，避免未等待的任务报警。"""
    if task is None:
        return

    def _done(done: asyncio.Task) -> None:
        if done.cancelled():
            return
        try:
            exc = done.exception()
        except asyncio.CancelledError:
            return
        if exc:
            logger.debug("丢弃的草稿结束", exc_info=exc)

    if task.done():
        _done(task)
    else:
        task.add_done_callback(_done)


def _same_contexts(left: list[dict], right: list[dict]) -> bool:
    if len(left) != len(right):
        return False
    return all(_context_key(a) == _context_key(b) for a, b in zip(left, right))


async def _timed_llm(rendered: str, conversation_id: int, timeout: float):
    started = time.monotonic()
    reply = await _call_llm_with_retry(rendered, conversation_id=conversation_id, timeout=timeout)
    return reply, int((time.monotonic() - started) * 1000)


def _remember_draft(task: asyncio.Task) -> None:
    state = _reply_trace.get()
    if state is not None:
        state["draft"] = task


def _take_draft() -> asyncio.Task | None:
    state = _reply_trace.get()
    if state is None:
        return None
    task = state.get("draft")
    state["draft"] = None
    return task


async def process_ai_reply(conversation_id: int, *, local: bool = False,
                           allow_owned: bool = False) -> str:
    token = _activate_trace()
    try:
        text = await _process_ai_reply_impl(conversation_id, local=local, allow_owned=allow_owned)
    finally:
        _finish_reply_trace(token)
    if local:
        return text  # 同步回复接口一次只答一问
    # 来客只答刚上报的这一条。库里更早的未答问题不再逐条补发出去。
    if _conversation_is_laike(conversation_id):
        return text
    # 本轮只答了最新一条。用户连发多问时，中间可能还夹着没答上的旧问题：逐条补答（上限防风暴）
    for _ in range(CATCHUP_MAX_MESSAGES):
        pending_ids = _unanswered_user_message_ids(conversation_id)
        if not pending_ids:
            break
        pending_id = pending_ids[0]
        token = _activate_trace()
        try:
            await _process_ai_reply_impl(
                conversation_id, local=False, allow_owned=allow_owned,
                answer_message_id=pending_id,
            )
        finally:
            _finish_reply_trace(token)
    return text


def _conversation_is_laike(conversation_id: int) -> bool:
    from ..services import conversation_is_laike

    db = SessionLocal()
    try:
        return conversation_is_laike(db, db.get(Conversation, conversation_id))
    finally:
        db.close()


def _finish_reply_trace(token: contextvars.Token) -> None:
    """收尾一轮接待：不等作废草稿，恢复计时上下文。"""
    state = _reply_trace.get()
    if state is not None:
        _abandon_draft(state.get("draft"))
    _reply_trace.reset(token)


def _unanswered_user_message_ids(conversation_id: int) -> list[int]:
    """按时间序返回还没被答上的用户消息 id（补答与防重复作答的判定基础）。

    一条用户消息算已答，满足其一：
    1) 显式配对：某条回复消息的 extra.answers_message_id 指向它；
    2) 位置配对（兼容历史无标记数据）：紧跟其后、且在下一条用户消息之前
       出现了非用户消息（即这条回复答的是它，而不是更新的问题）。
    """
    db = SessionLocal()
    try:
        rows = (
            db.query(Message.id, Message.sender_type, Message.extra)
            .filter(Message.conversation_id == conversation_id,
                    Message.is_internal.is_(False))
            .order_by(Message.id)
            .all()
        )
        linked = {
            (extra or {}).get("answers_message_id")
            for _id, sender, extra in rows
            if sender != "user" and (extra or {}).get("answers_message_id") is not None
        }
        unanswered: list[int] = []
        for index, (msg_id, sender, _extra) in enumerate(rows):
            if sender != "user":
                continue
            if msg_id in linked:
                continue
            # 位置配对：看它后面第一条消息是谁
            if index + 1 < len(rows) and rows[index + 1][1] != "user":
                continue  # 紧跟回复，已答
            unanswered.append(msg_id)
        return unanswered
    finally:
        db.close()


def _message_answered(conversation_id: int, user_message_id: int) -> bool:
    """这条用户消息是否已被回复答上（含显式链接与位置配对两种情况）。"""
    return user_message_id not in _unanswered_user_message_ids(conversation_id)


async def _process_ai_reply_impl(conversation_id: int, *, local: bool = False,
                                 allow_owned: bool = False,
                                 answer_message_id: int | None = None) -> str:
    """对一条用户消息执行完整 AI 接待流程。

    local=True 时只在本系统入库并返回拼成一段的正文，不走平台发送，也不做打字延迟。
    供知你快回同步回复接口使用。默认路径的返回值可忽略。
    answer_message_id 指定要答的那条用户消息（补答夹在中间的旧问题）；默认答最新一条。
    """
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        owned = allow_owned and conversation is not None and conversation.mode in ("pending", "human")
        if conversation is None or conversation.status != "open":
            return ""
        if conversation.mode != "ai" and not owned:
            return ""
        customer = db.get(Customer, conversation.customer_id)
        if answer_message_id is not None:
            # 补答指定旧问题：处理期间可能已被别的执行答上，答上就跳过
            last_user_msg = (
                db.query(Message)
                .filter(Message.conversation_id == conversation_id,
                        Message.id == answer_message_id,
                        Message.sender_type == "user")
                .first()
            )
            if last_user_msg is not None and _message_answered(conversation_id, answer_message_id):
                last_user_msg = None
        else:
            last_user_msg = (
                db.query(Message)
                .filter(Message.conversation_id == conversation_id, Message.sender_type == "user")
                .order_by(Message.id.desc())
                .first()
            )
            if last_user_msg is not None and _message_answered(conversation_id, last_user_msg.id):
                # 同一会话的排队任务可能多次触发接待：最新问题已被答上就不再重复作答
                last_user_msg = None
        if last_user_msg is None:
            return ""
        user_text = last_user_msg.content
        platform = conversation.platform
        conversation_id_ = conversation.id
        customer_id = customer.id
        nickname = customer.nickname if customer is not None else ""
        user_message_id = last_user_msg.id
        # 记入本轮接待上下文：这一轮发出的每条 AI 气泡都会带上 answers_message_id
        state = _reply_trace.get()
        if state is not None:
            state["answer_message_id"] = user_message_id
    finally:
        db.close()

    if not settings.ai_globally_enabled:
        if not owned:
            await handoff(conversation_id_, reason="ai_globally_disabled")
        return ""

    if is_pure_greeting(user_text):
        text, _sent = await _deliver_ai(
            conversation_id_, [greeting_reply(user_text)], local=local, allow_owned=owned,
            extra={"intent": "chitchat", "confidence": 1.0, "citations": []},
            anchor_user_message_id=user_message_id if local else None,
        )
        return text

    # LLM 未配置：AI 接待降级转人工。已经是排队或人工时不再发降级句，留给客服。
    if not settings.llm_configured:
        if owned:
            return ""
        text, _sent = await _deliver_ai(
            conversation_id_, ["这会儿咨询有点多，我先帮您叫同事过来哈"], local=local,
            anchor_user_message_id=user_message_id if local else None,
        )
        await handoff(conversation_id_, reason="llm_not_configured")
        return text

    started = _trace_started()
    history_text, recent, summary = context.get_history_for_prompt(conversation_id_)
    if is_pure_ack(user_text):
        text, _sent = await _deliver_ai(
            conversation_id_, [_ack_reply(recent)], local=local, allow_owned=owned,
            extra={"intent": "chitchat", "confidence": 1.0, "citations": []},
            anchor_user_message_id=user_message_id if local else None,
        )
        return text
    if not lead_flow.must_handoff(user_text):
        phone = lead_flow.phone_in_message(user_text)
        wechat = "" if phone else lead_flow.wechat_in_message(user_text)
        if phone or wechat:
            lead_flow.save_lead(
                customer_id=customer_id,
                conversation_id=conversation_id_,
                platform=platform,
                nickname=nickname,
                contact=phone or wechat,
                contact_type="phone" if phone else "wechat",
                business=lead_flow.business_phrase(user_text, recent),
            )
            text, _sent = await _deliver_ai(
                conversation_id_, [lead_flow.ARRANGE], local=local, allow_owned=owned,
                extra={"intent": "consult_feature", "confidence": 0.95, "citations": [], "grounded": False},
                anchor_user_message_id=user_message_id if local else None,
            )
            return text
    if asks_contact(user_text) and not lead_flow.must_handoff(user_text):
        # 联系意图答案固定：走确定性话术，不进 RAG，保证同类问法回复恒一致。
        text, _sent = await _deliver_ai(
            conversation_id_, [_contact_intent_reply(user_text, recent)], local=local, allow_owned=owned,
            extra={"intent": "consult_feature", "confidence": 0.9, "citations": [], "grounded": True},
            anchor_user_message_id=user_message_id if local else None,
        )
        return text
    retrieval = await _retrieve_covering(user_text, recent, summary)
    for key in ("rewrite_ms", "embed_ms", "rerank_ms"):
        _add_stage_ms(key, int(retrieval.stage_ms.get(key, 0)))
    exact_messages = _exact_faq_messages(user_text, retrieval)
    if exact_messages:
        _save_retrieval_trace(user_message_id, retrieval)
        payload = confidence.answer_confidence_payload(
            faithfulness_score=1.0, precision=1.0, cause="", retried=False, raw_precision=1.0,
        )
        text, sent = await _deliver_ai(
            conversation_id_, exact_messages, local=local, allow_owned=owned,
            extra={
                "intent": "consult_feature", "confidence": 0.95,
                "citations": _citation_payload(retrieval.contexts),
                "grounded": True, "answer_confidence": payload,
            },
            anchor_user_message_id=user_message_id if local else None,
        )
        if sent:
            _bump_knowledge_hits(retrieval.contexts)
        return text
    if retrieval.query_vector:
        cached = await answer_cache.lookup(user_text, retrieval.query_vector)
    else:
        embed_started = time.monotonic()
        cached = await answer_cache.lookup(user_text)
        _add_stage_ms("embed_ms", int((time.monotonic() - embed_started) * 1000))
    if _over_budget(started):
        return await _confirm_and_handoff(conversation_id_, user_text, platform, local=local, allow_owned=owned, owned=owned)

    _save_retrieval_trace(user_message_id, retrieval)
    agent_lines = _agent_fact_lines(recent)
    confidence_meta = None
    draft_contexts: list[dict] | None = None
    draft_prompt = ""
    draft_timeout = _llm_timeout(started) if retrieval.passed and not retrieval.exact_faq else None
    if draft_timeout is not None:
        draft_contexts = list(retrieval.contexts)
        draft_prompt = _render_knowledge_prompt(
            conversation_id_, platform, user_text, history_text, retrieval,
        )
        _remember_draft(asyncio.create_task(
            _timed_llm(draft_prompt, conversation_id_, draft_timeout),
        ))
    if retrieval.passed:
        # 高置信快路径：精排最高分极高（如精确命中）时跳过 judge LLM（省 3-8s/条）。
        # 草稿已并行预取，judge 跳过后立刻取；faithfulness 门槛/证据核对护栏不变。
        skip_score = settings.rag_judge_skip_score
        if (skip_score > 0 and not retrieval.exact_faq
                and retrieval.rerank_top_score is not None
                and retrieval.rerank_top_score >= skip_score
                and 0 < len(retrieval.contexts) <= 2):
            confidence_meta = {
                "action": "use",
                "cause": "",
                "retried": False,
                "contexts": list(retrieval.contexts),
                "verdicts": [1] * len(retrieval.contexts),
                "raw_precision": 1.0,
                "clarify": "",
            }
            _add_stage_ms("judge_ms", 0)
        else:
            judge_started = time.monotonic()
            confidence_meta = await _select_for_answer(user_text, retrieval, recent, summary, started)
            _add_stage_ms("judge_ms", int((time.monotonic() - judge_started) * 1000))
        if confidence_meta["action"] == "clarify":
            payload = confidence.answer_confidence_payload(
                faithfulness_score=1.0, precision=confidence_meta["raw_precision"],
                cause="ambiguous", retried=True, raw_precision=confidence_meta["raw_precision"],
            )
            text, _sent = await _deliver_ai(
                conversation_id_, [confidence_meta["clarify"]], local=local, allow_owned=owned,
                extra={"intent": "consult_feature", "confidence": 0.9, "citations": [],
                       "grounded": True, "answer_confidence": payload},
                anchor_user_message_id=user_message_id if local else None,
            )
            return text
        if confidence_meta["action"] == "stop":
            if confidence_meta.get("cause") != "out_of_kb":
                return await _risk_stop(
                    conversation_id_, user_text, platform, confidence_meta,
                    local=local, allow_owned=owned, owned=owned,
                )
            retrieval.passed = False
            retrieval.contexts = []
        else:
            retrieval.contexts = confidence_meta["contexts"]
    cache_ok = bool(cached) and _cache_still_grounded(cached, retrieval, agent_lines)
    if cache_ok and confidence_meta is not None:
        cache_ok = confidence.faithfulness(cached, retrieval.contexts) >= confidence.PASS_SCORE
    if cache_ok:
        text, _sent = await _deliver_ai(
            conversation_id_, cached, local=local, allow_owned=owned,
            extra={"intent": "other", "confidence": 1.0, "citations": [], "cache_hit": True, "grounded": True},
            anchor_user_message_id=user_message_id if local else None,
        )
        return text
    if cached:
        answer_cache.invalidate_messages(cached)

    if not retrieval.passed and confidence.risk_tier(user_text) == "medium":
        scope = confidence.scope_reply(user_text)
        if scope:
            text, _sent = await _deliver_ai(
                conversation_id_, [scope], local=local, allow_owned=owned,
                extra={"intent": "consult_feature", "confidence": 0.9, "citations": [], "grounded": True},
                anchor_user_message_id=user_message_id if local else None,
            )
            return text

    tool_results: list[dict] = []
    tool_names_used: list[str] = []
    draft_task = _take_draft()
    use_draft = (
        draft_task is not None
        and draft_contexts is not None
        and _same_contexts(draft_contexts, retrieval.contexts)
        and retrieval.passed
    )
    if retrieval.passed:
        if use_draft:
            reply, draft_ms = await draft_task
            tool_started = time.monotonic()
            reply = await _consume_tools(
                conversation_id_, customer_id, platform, draft_prompt, reply,
                tool_results, tool_names_used, started,
            )
            _add_stage_ms("draft_ms", draft_ms + int((time.monotonic() - tool_started) * 1000))
        else:
            _abandon_draft(draft_task)
            if _llm_timeout(started) is None:
                return await _confirm_and_handoff(
                    conversation_id_, user_text, platform, local=local, allow_owned=owned, owned=owned,
                )
            draft_started = time.monotonic()
            reply = await _answer_from_knowledge(
                conversation_id_, customer_id, platform, user_text, history_text, retrieval, started,
                tool_results, tool_names_used,
            )
            _add_stage_ms("draft_ms", int((time.monotonic() - draft_started) * 1000))
    else:
        _abandon_draft(draft_task)
        reply = await _answer_without_knowledge(
            conversation_id_, customer_id, platform, user_text, recent, started,
            tool_results, tool_names_used, local=local, allow_owned=owned, owned=owned,
        )
        if isinstance(reply, str):
            return reply
        if reply is None:
            reply = AgentReply()

    exhausted = reply is not None and _tool_rounds_exhausted(reply, len(tool_names_used))
    if (retrieval.passed and reply is None) or exhausted:
        text, _sent = await _deliver_ai(
            conversation_id_, ["不好意思，我这边卡了一下"], local=local, allow_owned=owned,
            anchor_user_message_id=user_message_id if local else None,
        )
        reason = "tool_rounds_exhausted" if exhausted else "llm_parse_failed"
        await handoff(conversation_id_, reason=reason)
        return text

    if reply is None:
        reply = AgentReply()

    if _over_budget(started) and not reply.reply_messages:
        return await _confirm_and_handoff(conversation_id_, user_text, platform, local=local, allow_owned=owned, owned=owned)

    listed = _reply_with_source_list(user_text, retrieval.contexts, retrieval.facets)
    messages = listed if listed is not None else list(reply.reply_messages)
    blob = evidence.evidence_text(retrieval.contexts, tool_results, agent_lines)
    kept, dropped = evidence.partition_messages(messages, blob, retrieval.contexts)
    gaps = list(retrieval.gaps)
    forced_human = reply.handoff_reason in ("complaint", "explicit_human", "sensitive", "out_of_scope")
    tool_ok = _tool_answered(tool_names_used, tool_results)
    need_fallback = (not forced_human) and (bool(gaps) or not kept) and not tool_ok
    used_fallback = False
    if need_fallback and _llm_timeout(started) is not None:
        fallback_started = time.monotonic()
        fallback = await _llm_fallback(
            conversation_id_, user_text, kept, gaps, recent, started,
        )
        _add_stage_ms("fallback_ms", int((time.monotonic() - fallback_started) * 1000))
        used_fallback = True
        if fallback is not None:
            fb_kept, fb_dropped = evidence.partition_messages(fallback.reply_messages, blob, retrieval.contexts)
            dropped.extend(fb_dropped)
            for line in fb_kept:
                if line not in kept:
                    kept.append(line)
            if fallback.handoff:
                reply.handoff = True
                reply.handoff_reason = fallback.handoff_reason or reply.handoff_reason or "low_confidence"
            if not reply.intent or reply.intent == "other":
                reply.intent = fallback.intent
    elif need_fallback:
        if not any(evidence.is_handoff_guide(line) for line in kept):
            kept.append(_CONFIRM_REPLY)
        reply.handoff = True
        reply.handoff_reason = reply.handoff_reason or "low_confidence"

    kept, list_dropped = _without_ungrounded_lists(kept, blob)
    dropped.extend(list_dropped)
    if not any(line.strip() for line in kept):
        answer = _single_faq_answer(retrieval.contexts)
        if answer:
            kept = [answer]
            reply.handoff = False
            reply.handoff_reason = ""
    shop_gap = bool(gaps) or ((not retrieval.passed) and _expects_shop_fact(user_text) and not tool_ok)
    if not kept:
        kept = [_CONFIRM_REPLY]
        reply.handoff = True
        reply.handoff_reason = reply.handoff_reason or "low_confidence"
    if shop_gap or (reply.handoff and forced_human):
        reply.handoff = True
        reply.handoff_reason = reply.handoff_reason or "low_confidence"
    if used_fallback and not shop_gap and not forced_human and kept and (
        not reply.handoff or confidence.risk_tier(user_text) == "low"
    ):
        reply.handoff = False
    if _contact_when_unanswered(
        user_text, kept, retrieval_passed=retrieval.passed, forced_human=forced_human, tool_ok=tool_ok,
    ):
        line = lead_flow.script_for_miss(user_text, recent)
        if line:
            kept = [line]
            dropped = []
            reply.handoff = False
            reply.handoff_reason = ""
            reply.confidence = max(reply.confidence, 0.9)
            shop_gap = False
            record_missed_question(user_text, platform, conversation_id_)
    reply_blob = "\n".join(kept)
    if confidence_meta is None and ((reply.confidence < 0.6 and _expects_shop_fact(reply_blob)) or bool(dropped)):
        reply.handoff = True
        reply.handoff_reason = reply.handoff_reason or "low_confidence"

    gate_payload = None
    if confidence_meta is not None and confidence_meta.get("action") == "use" and not forced_human:
        score_contexts = list(retrieval.contexts)
        tool_blob = evidence.evidence_text([], tool_results, agent_lines)
        if tool_blob.strip():
            score_contexts.append({"content": tool_blob, "source": "tool"})
        faith = confidence.faithfulness(kept, score_contexts)
        precision = confidence.used_precision(confidence_meta["verdicts"])
        if faith < confidence.PASS_SCORE and not confidence_meta.get("retried"):
            stripped = confidence.supported_messages(kept, score_contexts)
            confidence_meta["retried"] = True
            confidence_meta["cause"] = "unfaithful"
            kept = stripped
            faith = confidence.faithfulness(kept, score_contexts)
        gate_payload = confidence.answer_confidence_payload(
            faithfulness_score=faith,
            precision=precision,
            cause=confidence_meta.get("cause") or "",
            retried=bool(confidence_meta.get("retried")),
            raw_precision=confidence_meta.get("raw_precision"),
        )
        if not confidence.passed(faith, precision):
            confidence_meta["faithfulness"] = faith
            confidence_meta["precision"] = precision
            return await _risk_stop(
                conversation_id_, user_text, platform, confidence_meta,
                local=local, allow_owned=owned, owned=owned,
            )
        if not shop_gap:
            reply.handoff = False
            reply.handoff_reason = ""

    reflection = None
    remaining = REPLY_BUDGET_SECONDS - (time.monotonic() - started)
    risky = bool(dropped) or reply.confidence < 0.6 or bool(re.search(r"\d", reply_blob))
    if gate_payload is None and retrieval.passed and remaining > 3 and risky and kept and not _over_budget(started):
        from . import reflect
        reflection = await reflect.review_reply(kept, blob, timeout=min(4.0, remaining - 0.5))
        if reflection is not None:
            action = reflection.get("action")
            revised = [line for line in (reflection.get("messages") or []) if isinstance(line, str) and line.strip()]
            if action == "handoff":
                reply.handoff = True
                reply.handoff_reason = reply.handoff_reason or "low_confidence"
                if revised:
                    kept = revised
            elif action == "revise" and revised:
                kept, more_dropped = evidence.partition_messages(revised, blob, retrieval.contexts)
                dropped.extend(more_dropped)
                if not kept:
                    kept = [_CONFIRM_REPLY]
                    reply.handoff = True
                    reply.handoff_reason = reply.handoff_reason or "low_confidence"

    messages = kept[:3]
    if len(retrieval.facets) < 2 and len(messages) > 1:
        messages = ["\n".join(messages)]
    citations = _citation_payload(retrieval.contexts)
    must_handoff = bool(reply.handoff) or shop_gap
    if not must_handoff and messages and lead_flow.wants_to_proceed(user_text):
        if lead_flow.phone_in_message(user_text) or lead_flow.wechat_in_message(user_text):
            line = lead_flow.ARRANGE
        else:
            line = lead_flow.library_script("留资话术") or lead_flow.script_for_miss(user_text, recent) or ""
        if line and all(line not in part for part in messages):
            messages = [*messages, line][:3]
    extra = {
        "intent": reply.intent,
        "confidence": reply.confidence,
        "citations": citations,
        "grounded": not must_handoff,
        "facets": list(retrieval.facets),
        "gaps": gaps,
        "dropped": dropped,
        "fallback": used_fallback,
        "reflection": reflection,
        "handoff_reason": reply.handoff_reason if must_handoff else "",
        "answer_confidence": gate_payload,
    }
    if tool_names_used:
        extra["tool_calls"] = tool_names_used
    if messages:
        text, sent = await _deliver_ai(
            conversation_id_, messages, local=local, allow_owned=owned, extra=extra,
            anchor_user_message_id=user_message_id if local else None,
        )
        if sent:
            _apply_side_effects(
                customer_id, reply, conversation_id=conversation_id_, platform=platform,
                user_text=user_text, recent=recent,
            )
        if sent and retrieval.passed and not must_handoff:
            _bump_knowledge_hits(retrieval.contexts)
            if not _FACT_TOOLS.intersection(tool_names_used):
                doc_ids = [item["doc_id"] for item in retrieval.contexts if isinstance(item.get("doc_id"), int)]
                await answer_cache.store(user_text, messages, doc_ids)
    else:
        text = ""
    if must_handoff:
        if shop_gap or not retrieval.passed:
            record_missed_question(gaps[0] if gaps else user_text, platform, conversation_id_)
        await handoff(conversation_id_, reason=reply.handoff_reason or "low_confidence")
        return text
    if local:
        asyncio.create_task(context.maybe_update_summary(conversation_id_))
    else:
        await context.maybe_update_summary(conversation_id_)
    return text


_SHOP_FACT = re.compile(
    r"多少钱|什么价|价格|几元|几块|包邮|运费|邮费|发货|退货|退换|换货|退款|发票|质保|保修|"
    r"库存|有货|营业|几点|地址|尺码|颜色|优惠|折扣|几天|多久|周末"
)


def _over_budget(started: float) -> bool:
    return time.monotonic() - started > REPLY_BUDGET_SECONDS


def _expects_shop_fact(user_text: str) -> bool:
    return bool(_SHOP_FACT.search(user_text or ""))


def _contact_when_unanswered(user_text: str, kept: list[str], *, retrieval_passed: bool,
                             forced_human: bool, tool_ok: bool) -> bool:
    """知识库没有，模型也没留下能发的句子时，才改用留资原句。"""
    if retrieval_passed or forced_human or tool_ok or lead_flow.must_handoff(user_text):
        return False
    if _TASK_RE.search(user_text or ""):
        return False
    return not any(
        line for line in kept
        if line and line != _CONFIRM_REPLY and not evidence.is_handoff_guide(line)
    )


def _contact_line(user_text: str, recent: list[dict] | None = None) -> str:
    """留资话术优先，没有再用按业务拼的原句。"""
    return (
        lead_flow.library_script("留资话术")
        or lead_flow.script_for_miss(user_text, recent)
        or lead_flow.ASK_FULL
    )


def _without_ungrounded_lists(messages: list[str], blob: str) -> tuple[list[str], list[str]]:
    """编号行必须整句出现在证据里。没有证据的清单不能发出。"""
    compact_blob = re.sub(r"\s+", "", blob or "")
    kept: list[str] = []
    dropped: list[str] = []
    for message in messages:
        lines: list[str] = []
        for raw in (message or "").splitlines():
            line = raw.strip()
            if not line:
                lines.append("")
                continue
            if _LIST_ITEM_RE.match(line):
                body = re.sub(r"\s+", "", _LIST_MARKER_RE.sub("", line))
                if not body or body not in compact_blob:
                    dropped.append(line)
                    continue
            lines.append(line)
        if not any(_LIST_ITEM_RE.match(line) for line in lines if line):
            announced: list[str] = []
            for line in lines:
                if line and _LIST_ANNOUNCE_RE.search(re.sub(r"\s+", "", line)):
                    dropped.append(line)
                    continue
                announced.append(line)
            lines = announced
        while lines and not lines[0]:
            lines.pop(0)
        while lines and not lines[-1]:
            lines.pop()
        if any(lines):
            kept.append("\n".join(lines))
    return kept, dropped


def _context_key(item: dict) -> tuple:
    return (item.get("chunk_id"), item.get("doc_id"), item.get("content"))


async def _retrieve_covering(user_text: str, recent: list[dict], summary: str) -> pipeline.RetrievalResult:
    """单问只检索一次。多问先整句，没盖住的子问题再各检一次，合并最多 3 条。"""
    primary = await pipeline.retrieve(user_text, history=recent, summary=summary)
    facets = pipeline.split_facets(user_text)
    primary.facets = facets
    if len(facets) < 2:
        return primary
    merged = list(primary.contexts)
    seen = {_context_key(item) for item in merged}
    gaps: list[str] = []
    for facet in facets:
        if pipeline.facet_covered(facet, merged):
            continue
        extra = await pipeline.retrieve(facet, history=recent, summary=summary)
        for key in ("rewrite_ms", "embed_ms", "rerank_ms"):
            primary.stage_ms[key] = int(primary.stage_ms.get(key, 0)) + int(extra.stage_ms.get(key, 0))
        for item in extra.contexts:
            key = _context_key(item)
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        if not pipeline.facet_covered(facet, merged):
            gaps.append(facet)
    primary.contexts = merged[:3]
    primary.gaps = gaps
    if primary.contexts and not primary.passed:
        primary.passed = True
        primary.reason = "passed"
    return primary


def _cache_still_grounded(messages: list[str], retrieval: pipeline.RetrievalResult,
                          agent_lines: list[str]) -> bool:
    if not retrieval.passed or retrieval.gaps:
        return False
    if _cached_misses_numbered_items(messages, retrieval.contexts):
        return False
    blob = evidence.evidence_text(retrieval.contexts, [], agent_lines)
    kept, dropped = evidence.partition_messages(messages, blob, retrieval.contexts)
    return bool(kept) and not dropped


def _cached_misses_numbered_items(messages: list[str], contexts: list[dict]) -> bool:
    """缓存句漏了召回里的编号正文时，不能再直接发出。"""
    items = _collect_numbered_items(contexts)
    if len(items) < 2:
        return False
    blob = re.sub(r"\s+", "", "\n".join(messages or []))
    for item in items:
        body = re.sub(r"\s+", "", _LIST_MARKER_RE.sub("", item).strip())
        if body and body not in blob:
            return True
    return False


async def _llm_fallback(conversation_id: int, user_text: str, kept: list[str], gaps: list[str],
                        recent: list[dict], started: float) -> AgentReply | None:
    """知识或主回复接不住时再叫一次模型。只许复述已核对事实，或把缺口说成转人工。"""
    timeout = _llm_timeout(started)
    if timeout is None:
        return None
    facts = "\n".join(kept) or "（没有已核对事实）"
    missing = "、".join(gaps) or "知识库没有直接答案"
    lines = []
    for row in _earlier_dialogue(user_text, recent):
        role = "用户" if row.get("sender_type") == "user" else "客服"
        lines.append(f"{role}: {row.get('content') or ''}")
    prompt = (
        "你是赢态财务的客服小赢，用 1 到 2 句微信口语回复。"
        "不要说「根据资料」「知识库显示」「您好，很高兴为您服务」。\n"
        "没有核对过的事实时，可以接话、追问办的是注册还是开户、或问手机号。\n"
        "用户报名字、问放假、说今天去哪里，直接接一句，handoff 填 false，不要说对不上资料。\n"
        "先看用户前面已经说过的事，包括名字、称呼、去哪、要办什么、放假、计划。"
        "现在问到其中一件，就用那些原话回答或用一两句概括，不要再问一遍。\n"
        "不要用自己的记忆补充价格、费用、办理天数、银行名单、资料清单、办理步骤、开户或注册能否办成、优惠、政策、折扣、库存、是否包邮、能否退换、快递公司或单号。\n"
        "用户自己说过的数字也不能当成这些事实。\n"
        "用户在要这些内容时，只说请同事确认，不要写步骤或编号清单，handoff 填 true，handoff_reason 填 low_confidence。\n"
        "只是闲聊，或要追问办哪一项、手机号时，直接接话，handoff 填 false。\n"
        "投诉、辱骂、用户明确要真人时，先安抚再转人工，handoff 填 true。\n"
        f"已核对事实：\n{facts}\n还缺：{missing}\n"
        "已有对话：\n" + ("\n".join(lines) or "（无）") + f"\n用户说：{user_text}\n"
        "只输出 JSON："
        '{"reply_messages":["一句"],"intent":"other","confidence":0.4,'
        '"handoff":false,"handoff_reason":"","tags":[],'
        '"lead":{"phone":"","wechat":"","note":""},"quick_action":"none","tool_call":null}'
    )
    return await _call_llm_with_retry(
        prompt, conversation_id=conversation_id, timeout=timeout,
    )


def _tool_answered(names: list[str], results: list[dict]) -> bool:
    return any(name in _FACT_TOOLS and result.get("ok") for name, result in zip(names, results))


def _agent_fact_lines(recent: list[dict]) -> list[str]:
    lines = []
    for row in recent or []:
        content = (row.get("content") or "").strip()
        if not content:
            continue
        if row.get("sender_type") == "agent" or (row.get("sender_type") == "ai" and row.get("grounded")):
            lines.append(content)
    return lines


async def _confirm_and_handoff(conversation_id: int, user_text: str, platform: str, *,
                               local: bool, allow_owned: bool, owned: bool, record: bool = True,
                               meta: dict | None = None) -> str:
    if record:
        record_missed_question(user_text, platform, conversation_id)
    if owned:
        await note_unmatched(conversation_id)
        return ""
    extra = {"intent": "other", "confidence": 0.0, "citations": [], "grounded": False}
    if meta:
        extra.update(meta)
    text, _sent = await _deliver_ai(
        conversation_id, [_CONFIRM_REPLY], local=local, allow_owned=allow_owned, extra=extra,
    )
    await handoff(conversation_id, reason="low_confidence")
    return text


async def _select_for_answer(user_text: str, retrieval, recent: list[dict], summary: str,
                             started: float) -> dict:
    """写草稿前先判定段落是否相关。精排分不参与。失败或来不及打分则停。"""
    remaining = REPLY_BUDGET_SECONDS - (time.monotonic() - started)
    if not retrieval.exact_faq and remaining < DRAFT_RESERVE_SECONDS + 2:
        return _stopped("unscored", retried=False, raw_precision=0.0)
    if retrieval.exact_faq:
        verdicts = [1] * len(retrieval.contexts)
    else:
        verdicts = await confidence.judge_relevance(
            user_text, retrieval.contexts,
            timeout=min(4.0, remaining - DRAFT_RESERVE_SECONDS),
        )
    if verdicts is None:
        return _stopped("unscored", retried=False, raw_precision=0.0)
    return await _route_verdicts(
        user_text, list(retrieval.contexts), verdicts, recent, summary, started, retried=False,
    )


def _stopped(cause: str, *, retried: bool, raw_precision: float) -> dict:
    return {
        "action": "stop",
        "cause": cause,
        "retried": retried,
        "contexts": [],
        "verdicts": [],
        "raw_precision": raw_precision,
        "clarify": "",
    }


async def _route_verdicts(user_text: str, contexts: list[dict], verdicts: list[int],
                          recent: list[dict], summary: str, started: float, *, retried: bool) -> dict:
    raw = confidence.context_precision(verdicts)
    cause = confidence.diagnose(user_text, contexts, verdicts)
    if cause == "ambiguous":
        keys = confidence.ambiguous_keys(user_text, contexts, verdicts)
        return {
            "action": "clarify",
            "cause": "ambiguous",
            "retried": True,
            "contexts": [],
            "verdicts": verdicts,
            "raw_precision": raw,
            "clarify": confidence.clarify_line(keys),
        }
    if cause == "recall" and not retried:
        wider = await rewrite.broaden_query(
            user_text, [], "irrelevant_context", recent, summary,
        )
        if not wider or _over_budget(started):
            return _stopped("out_of_kb", retried=True, raw_precision=raw)
        second = await pipeline.retrieve(wider, history=recent, summary=summary, top_k=3, broaden=False)
        if not second.passed or not second.contexts:
            return _stopped("out_of_kb", retried=True, raw_precision=raw)
        if second.exact_faq:
            second_verdicts = [1] * len(second.contexts)
        else:
            remaining = REPLY_BUDGET_SECONDS - (time.monotonic() - started)
            if remaining < DRAFT_RESERVE_SECONDS + 2:
                return _stopped("unscored", retried=True, raw_precision=raw)
            second_verdicts = await confidence.judge_relevance(
                user_text, second.contexts,
                timeout=min(4.0, remaining - DRAFT_RESERVE_SECONDS),
            )
        if second_verdicts is None:
            return _stopped("unscored", retried=True, raw_precision=raw)
        return await _route_verdicts(
            user_text, list(second.contexts), second_verdicts, recent, summary, started, retried=True,
        )
    if cause == "recall":
        return _stopped("out_of_kb", retried=retried, raw_precision=raw)
    kept_contexts = [item for item, verdict in zip(contexts, verdicts) if verdict]
    if not kept_contexts:
        return _stopped("out_of_kb", retried=retried or cause == "noise", raw_precision=raw)
    return {
        "action": "use",
        "cause": "noise" if cause == "noise" else "",
        "retried": retried or cause == "noise",
        "contexts": kept_contexts,
        "verdicts": [1] * len(kept_contexts),
        "raw_precision": raw,
        "clarify": "",
    }


async def _risk_stop(conversation_id: int, user_text: str, platform: str, meta: dict, *,
                     local: bool, allow_owned: bool, owned: bool) -> str:
    """重试后仍低。高风险转人工，做不做只答范围，闲聊用固定话术。"""
    payload = confidence.answer_confidence_payload(
        faithfulness_score=meta.get("faithfulness", 0.0),
        precision=meta.get("precision", 0.0),
        cause=meta.get("cause") or "low_confidence",
        retried=bool(meta.get("retried")),
        raw_precision=meta.get("raw_precision"),
    )
    tier = confidence.risk_tier(user_text)
    if tier == "medium":
        scope = confidence.scope_reply(user_text)
        if scope:
            text, _sent = await _deliver_ai(
                conversation_id, [scope], local=local, allow_owned=allow_owned,
                extra={"intent": "consult_feature", "confidence": 0.9, "citations": [],
                       "grounded": True, "answer_confidence": payload},
            )
            return text
    if tier == "low":
        text, _sent = await _deliver_ai(
            conversation_id, ["有啥想了解的随时问呀"], local=local, allow_owned=allow_owned,
            extra={"intent": "chitchat", "confidence": 0.9, "citations": [],
                   "grounded": True, "answer_confidence": payload},
        )
        return text
    return await _confirm_and_handoff(
        conversation_id, user_text, platform, local=local, allow_owned=allow_owned, owned=owned,
        meta={"answer_confidence": payload},
    )


def _render_knowledge_prompt(conversation_id: int, platform: str, user_text: str,
                             history_text: str, retrieval) -> str:
    rewritten = retrieval.rewritten_queries[0] if retrieval.rewritten_queries else user_text
    return prompt_mod.render_prompt(
        platform=platform,
        knowledge_context=_format_knowledge_context(
            retrieval.contexts, retrieval.gaps, user_text,
        ),
        history_text=history_text,
        user_message=user_text,
        customer_profile=context.customer_profile_text(conversation_id),
        rewritten_question=rewritten,
    )


async def _answer_from_knowledge(conversation_id: int, customer_id: int, platform: str, user_text: str,
                                 history_text: str, retrieval, started: float,
                                 tool_results: list[dict], tool_names: list[str]) -> AgentReply | None:
    timeout = _llm_timeout(started)
    if timeout is None:
        return None
    rendered = _render_knowledge_prompt(conversation_id, platform, user_text, history_text, retrieval)
    reply = await _call_llm_with_retry(
        rendered, conversation_id=conversation_id, timeout=timeout,
    )
    return await _consume_tools(
        conversation_id, customer_id, platform, rendered, reply, tool_results, tool_names, started,
    )


async def _answer_without_knowledge(conversation_id: int, customer_id: int, platform: str, user_text: str,
                                    recent: list[dict], started: float, tool_results: list[dict],
                                    tool_names: list[str], *, local: bool, allow_owned: bool, owned: bool):
    """没检索到时：办事先调工具，否则只用人工原话，再不行就兜底并转人工。"""
    if _llm_timeout(started) is None:
        return await _confirm_and_handoff(
            conversation_id, user_text, platform, local=local, allow_owned=allow_owned, owned=owned)
    if _TASK_RE.search(user_text or ""):
        task_timeout = _llm_timeout(started)
        if task_timeout is None:
            return await _confirm_and_handoff(
                conversation_id, user_text, platform, local=local, allow_owned=allow_owned, owned=owned)
        rendered = (
            "你是赢态财务的客服小赢。用户在查订单、物流、办理进度或退换。可以调用工具。"
            "没有真实结果就不要编造单号、快递、价格、开户费用、办理周期、开户成功率和银行名单。"
            "查到之后用 1 到 2 句微信口语说结果，不要说「根据查询结果」。"
            "只输出 JSON 契约。\n\n"
            + tools.tools_prompt_text()
            + f"\n\n用户说：{user_text}"
        )
        reply = await _call_llm_with_retry(
            rendered, conversation_id=conversation_id, timeout=task_timeout,
        )
        reply = await _consume_tools(
            conversation_id, customer_id, platform, rendered, reply, tool_results, tool_names, started,
        )
        if reply is not None and (_tool_answered(tool_names, tool_results) or reply.reply_messages):
            reply.handoff = not _tool_answered(tool_names, tool_results)
            if reply.handoff:
                reply.handoff_reason = reply.handoff_reason or "low_confidence"
            return reply
    remembered = await _reply_from_dialogue(
        conversation_id, user_text, recent, local=local, allow_owned=allow_owned, started=started)
    if remembered is not None:
        return remembered
    if owned and confidence.risk_tier(user_text) == "high":
        await note_unmatched(conversation_id)
        record_missed_question(user_text, platform, conversation_id)
        return ""
    return None


async def _consume_tools(conversation_id: int, customer_id: int, platform: str, rendered: str,
                         reply: AgentReply | None, tool_results: list[dict], tool_names: list[str],
                         started: float) -> AgentReply | None:
    tool_ctx = tools.ToolContext(conversation_id=conversation_id, customer_id=customer_id, platform=platform)
    rounds = 0
    while reply is not None and reply.tool_call and rounds < MAX_TOOL_ROUNDS:
        round_timeout = _llm_timeout(started)
        if round_timeout is None:
            break
        rounds += 1
        call = reply.tool_call
        tool_names.append(call.name)
        result = await tools.execute_tool(call.name, call.args, tool_ctx)
        tool_results.append(result)
        rendered = rendered + tools.tool_result_text(call.name, result, allow_chain=rounds < MAX_TOOL_ROUNDS)
        reply = await _call_llm_with_retry(
            rendered, conversation_id=conversation_id, timeout=round_timeout,
        )
    return reply


def _earlier_dialogue(user_text: str, recent: list[dict]) -> list[dict]:
    """去掉当前这句。留下用户原话、人工原话和已经核对过的 AI 回复。"""
    rows = list(recent or [])
    if rows and rows[-1].get("sender_type") == "user" and rows[-1].get("content") == user_text:
        rows = rows[:-1]
    kept = []
    for row in rows:
        content = (row.get("content") or "").strip()
        if not content:
            continue
        sender = row.get("sender_type")
        if sender == "user" or sender == "agent" or (sender == "ai" and row.get("grounded")):
            kept.append(row)
    return kept


def _confirmed_dialogue_text(rows: list[dict]) -> str:
    """费用和资料只认人工原话和已核对的 AI 回复，不认用户自己说的数字。"""
    lines = []
    for row in rows:
        sender = row.get("sender_type")
        if sender == "agent" or (sender == "ai" and row.get("grounded")):
            content = (row.get("content") or "").strip()
            if content:
                lines.append(content)
    return "\n".join(lines)


async def _reply_from_dialogue(conversation_id: int, user_text: str, recent: list[dict], *,
                               local: bool, allow_owned: bool, started: float) -> str | None:
    """知识库没有时，只根据刚才的对话回答。对话里没有这个事实就返回 None。"""
    earlier = _earlier_dialogue(user_text, recent)
    if not earlier:
        return None
    lines = []
    for row in earlier:
        role = {"user": "用户", "ai": "客服", "agent": "人工客服"}.get(row["sender_type"], "客服")
        lines.append(f"{role}: {row['content']}")
    rendered = (
        "下面是已经发生的对话。用户现在又问了一句。"
        "先看用户前面已经说过的事，包括名字、称呼、去哪、要办什么、放假、计划。"
        "现在问到其中一件，就用那些原话回答，或用一两句概括，不要再问一遍。"
        "只有对话里已经说过的事才能回答，不要查知识库，不要编造。"
        "价格、费用、办理天数、银行、资料、政策不能用用户自己说的数字来回答。"
        "能回答时输出 JSON：{\"reply_messages\":[\"一句短回复\"],\"intent\":\"other\",\"confidence\":0.9,\"handoff\":false}。"
        "对话里没有这个事实时输出 JSON：{\"reply_messages\":[],\"intent\":\"other\",\"confidence\":0,\"handoff\":false}。\n\n"
        f"已有对话：\n" + "\n".join(lines) + f"\n\n用户现在问：{user_text}"
    )
    dialogue_timeout = _llm_timeout(started)
    if dialogue_timeout is None:
        return None
    dialogue_started = time.monotonic()
    reply = await _call_llm_with_retry(
        rendered, conversation_id=conversation_id, timeout=dialogue_timeout,
    )
    _add_stage_ms("draft_ms", int((time.monotonic() - dialogue_started) * 1000))
    if reply is None or reply.handoff or not reply.reply_messages:
        return None
    if evidence.claims_unsupported(reply.reply_messages[:1], _confirmed_dialogue_text(earlier)):
        return None
    text, _sent = await _deliver_ai(
        conversation_id, reply.reply_messages[:1], local=local, allow_owned=allow_owned,
        extra={"intent": "other", "confidence": reply.confidence, "citations": [], "from_dialogue": True, "grounded": True},
    )
    return text


def _faq_answer_body(content: str) -> str:
    """取出 FAQ 切片里「答：」之后的正文。问句和相似问不发出。"""
    lines = (content or "").splitlines()
    start = None
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("答：") or stripped.startswith("答:"):
            start = index
            break
    if start is None:
        return ""
    first = lines[start].strip()
    first = first.removeprefix("答：").removeprefix("答:").strip()
    rest = [line.strip() for line in lines[start + 1:] if line.strip()]
    parts = ([first] if first else []) + rest
    return "\n".join(parts).strip()


def _exact_faq_messages(user_text: str, retrieval) -> list[str] | None:
    """精确命中单条 FAQ 时用原文。清单仍走现有格式，其余发答正文。"""
    if not getattr(retrieval, "exact_faq", False) or len(retrieval.contexts) != 1:
        return None
    context = retrieval.contexts[0]
    if context.get("doc_type") not in (None, "", "faq"):
        return None
    listed = _reply_with_source_list(user_text, retrieval.contexts, list(retrieval.facets or []))
    if listed:
        return listed
    body = _faq_answer_body(context.get("content") or "")
    return [body] if body else None


def _single_faq_answer(contexts: list[dict]) -> str:
    """检索结果只剩一条 FAQ 时，用它的答正文兜底。"""
    if len(contexts or []) != 1:
        return ""
    context = contexts[0]
    if context.get("doc_type") not in (None, "", "faq"):
        return ""
    return _faq_answer_body(context.get("content") or "")


def _numbered_items(text: str) -> list[str]:
    items = []
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("答："):
            stripped = stripped.removeprefix("答：").strip()
        elif stripped.startswith("答:"):
            stripped = stripped.removeprefix("答:").strip()
        if _LIST_ITEM_RE.match(stripped):
            items.append(stripped)
    return items


def _format_list_item(item: str) -> str:
    """编号和正文之间不留空格，正文保持原样。"""
    match = _LIST_MARKER_RE.match(item.strip())
    if match is None:
        return item.strip()
    marker = re.sub(r"\s+", "", match.group(0))
    body = item.strip()[match.end():].strip()
    return f"{marker}{body}"


def _colon_marked(items: list[str]) -> bool:
    """「1：公司名称」这种带冒号的条目，原文自己有导语，不再加清单导语。"""
    return any(_COLON_ITEM_RE.match((item or "").strip()) for item in items)


def _format_source_list(items: list[str]) -> str:
    lines = [_format_list_item(item) for item in items]
    return f"{_LIST_LEAD}\n\n" + "\n".join(lines)


def _reply_with_source_list(user_text: str, contexts: list[dict], facets: list[str]) -> list[str] | None:
    """召回正文里的编号条目用原文作答。不同主题不拼成一条。点名一条时只发那一条。"""
    if len(facets) >= 2:
        groups: list[list[str]] = []
        for facet in facets:
            own = [item for item in contexts if pipeline.facet_covered(facet, [item])]
            if _topic_count(own) >= 2:
                return None
            chosen = _choose_numbered_items(user_text, _collect_numbered_items(own))
            if not chosen:
                return None
            groups.append(chosen)
        return [_format_source_list(items) for items in groups]
    if _topic_count(contexts) >= 2:
        return None
    lines = _section_lines(contexts)
    numbered = [line for line in lines if _LIST_ITEM_RE.match(line)]
    chosen = _choose_numbered_items(user_text, numbered)
    if not chosen:
        return None
    if len(chosen) == 1 and len(numbered) > 1:
        return [_format_source_list(chosen)]
    if any(not _LIST_ITEM_RE.match(line) for line in lines):
        body = [_format_list_item(line) if _LIST_ITEM_RE.match(line) else line for line in lines]
        text = "\n".join(body)
        if _colon_marked(numbered):
            return [text]
        return [f"{_LIST_LEAD}\n\n" + text]
    return [_format_source_list(chosen)]


def _topic_count(contexts: list[dict]) -> int:
    keys = []
    for item in contexts or []:
        key = confidence.topic_key(confidence.heading_of(item))
        if key and key not in keys:
            keys.append(key)
    return len(keys)


def _dedup_key(line: str) -> str:
    """清单/段落去重键：空白与标点归一后再比较，编号风格的差异不算两条。"""
    return _ITEM_PUNCT_RE.sub("", line or "")


def _section_lines(contexts: list[dict]) -> list[str]:
    """同一主题里带编号的段落，保留夹在条目之间的字段行。"""
    seen: set[str] = set()
    lines: list[str] = []
    for item in contexts or []:
        raw_lines = []
        for raw in (item.get("content") or "").splitlines():
            line = raw.strip()
            if line.startswith("答：") or line.startswith("答:"):
                line = line.split("：", 1)[-1].split(":", 1)[-1].strip()
            if line:
                raw_lines.append(line)
        if not any(_LIST_ITEM_RE.match(line) for line in raw_lines):
            continue
        heading = confidence.heading_of(item)
        for line in raw_lines:
            if not line or line.startswith("【"):
                continue
            if line.startswith("问：") or line.startswith("问:"):
                continue
            if line.startswith("相似：") or line.startswith("相似:"):
                continue
            if heading and line == heading:
                continue
            key = _dedup_key(line)
            if not key or key in seen:
                continue
            seen.add(key)
            lines.append(line)
    return lines


def _collect_numbered_items(contexts: list[dict]) -> list[str]:
    """只收这一轮召回正文里的编号条目，不去数据库补同一篇文档。"""
    seen: set[str] = set()
    items: list[str] = []
    for item in contexts or []:
        for line in _numbered_items(item.get("content") or ""):
            key = _dedup_key(line)
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(line)
    return items


def _choose_numbered_items(user_text: str, items: list[str]) -> list[str] | None:
    if len(items) < 2:
        return None
    named = _items_user_named(user_text, items)
    if len(named) == 1:
        return named
    return items


def _items_user_named(user_text: str, items: list[str]) -> list[str]:
    """用户原话点到了某条正文，才算点名。"""
    compact_user = re.sub(r"\s+", "", user_text or "")
    if len(compact_user) < 4:
        return []
    named = []
    for item in items:
        body = re.sub(r"\s+", "", _LIST_MARKER_RE.sub("", item).strip())
        if len(body) < 4:
            continue
        if body in compact_user or compact_user in body:
            named.append(item)
    return named


def _has_ungrounded_amount(messages: list[str], contexts: list[dict]) -> bool:
    """回复里的数字和政策必须能在召回正文里对上。"""
    return evidence.claims_unsupported(messages, evidence.evidence_text(contexts, [], []))


def _save_retrieval_trace(message_id: int, retrieval: pipeline.RetrievalResult) -> None:
    """把这一轮检索写到用户消息上。未通过时也写，方便看出为什么转人工。"""
    db = SessionLocal()
    try:
        msg = db.get(Message, message_id)
        if msg is None:
            return
        extra = dict(msg.extra or {})
        extra["retrieval"] = {
            "queries": list(retrieval.rewritten_queries),
            "dense_count": retrieval.dense_count,
            "bm25_count": retrieval.bm25_count,
            "fused_top": list(retrieval.fused_top),
            "rerank_top_score": retrieval.rerank_top_score,
            "reason": retrieval.reason,
            "rerank_status": retrieval.rerank_status,
            "facets": list(retrieval.facets),
            "gaps": list(retrieval.gaps),
            "selected": [
                {
                    "chunk_id": item.get("chunk_id"),
                    "doc_id": item.get("doc_id"),
                    "source": item.get("source"),
                    "score": item.get("score"),
                }
                for item in retrieval.contexts
            ],
        }
        msg.extra = extra
        db.commit()
    finally:
        db.close()


_CHUNK_BUDGET = 800
_TOTAL_BUDGET = 2400


def _format_knowledge_context(contexts: list[dict], gaps: list[str] | None = None,
                              query: str = "") -> str:
    """每条资料分开给。超预算时只留和问句有词面重叠的句子，来源标签保留。"""
    blocks = []
    used = 0
    for i, item in enumerate(contexts):
        room = _TOTAL_BUDGET - used
        if room <= 0:
            break
        content = _trim_knowledge(item.get("content") or "", query, min(_CHUNK_BUDGET, room))
        used += len(content)
        blocks.append(f"【资料{i + 1}】（来源：{item['source']}）\n{content}")
    if gaps:
        blocks.append("这些问题没有资料，不要编，请同事确认：" + "、".join(gaps))
    return "\n\n".join(blocks)


def _trim_knowledge(content: str, query: str, budget: int) -> str:
    """超预算时标题行和编号条目整行保留，散文只留与问句有词面重叠的完整句子。不硬切条目。"""
    text = content or ""
    if len(text) <= budget:
        return text
    needles = _overlap_needles(query)
    kept: list[str] = []
    used = 0

    def _room_for(part: str) -> bool:
        nonlocal used
        if kept and used + len(part) + 1 > budget:
            return False
        kept.append(part)
        used += len(part) + 1
        return True

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("【") or _LIST_ITEM_RE.match(line):
            if not _room_for(line):
                break
            continue
        full = True
        for sentence in (s for s in re.split(r"(?<=[。！？])", line) if s.strip()):
            if not any(needle in sentence for needle in needles):
                continue
            if not _room_for(sentence.strip()):
                full = False
                break
        if not full:
            break
    if not kept:
        return text[:budget]
    return "\n".join(kept)


def _overlap_needles(query: str) -> list[str]:
    compact = re.sub(r"\s+", "", query or "")
    needles = []
    for size in (4, 2):
        for i in range(0, max(0, len(compact) - size + 1)):
            piece = compact[i:i + size]
            if piece not in needles:
                needles.append(piece)
    return needles or ([compact] if compact else [])


def _bump_knowledge_hits(contexts: list[dict]) -> None:
    """一次接待只给每篇被引用的知识加 1，不按气泡条数重复计。"""
    doc_ids: list[int] = []
    seen: set[int] = set()
    for item in contexts:
        doc_id = item.get("doc_id")
        if not isinstance(doc_id, int) or doc_id in seen:
            continue
        seen.add(doc_id)
        doc_ids.append(doc_id)
    if not doc_ids:
        return
    db = SessionLocal()
    try:
        docs = db.query(KnowledgeDoc).filter(KnowledgeDoc.id.in_(doc_ids)).all()
        for doc in docs:
            doc.hit_count = (doc.hit_count or 0) + 1
        db.commit()
    finally:
        db.close()


def _llm_timeout(started: float) -> float | None:
    """主回复这次调用最多再用剩余预算。不足 4 秒就不要打，避免 1 秒读超时。"""
    remaining = REPLY_BUDGET_SECONDS - (time.monotonic() - started)
    if remaining < MIN_LLM_SECONDS:
        return None
    return remaining


def _has_newer_user_message(conversation_id: int, user_message_id: int) -> bool:
    db = SessionLocal()
    try:
        latest = (
            db.query(Message.id)
            .filter(Message.conversation_id == conversation_id, Message.sender_type == "user")
            .order_by(Message.id.desc())
            .first()
        )
        return latest is not None and latest[0] != user_message_id
    finally:
        db.close()


def _citation_payload(contexts: list[dict]) -> list[dict]:
    items = []
    for item in contexts:
        items.append({
            "title": item.get("source") or "",
            "doc_id": item.get("doc_id"),
            "chunk_id": item.get("chunk_id"),
        })
    return items


async def _deliver_ai(conversation_id: int, messages: list[str], *, local: bool,
                      extra: dict | None = None, allow_owned: bool = False,
                      anchor_user_message_id: int | None = None) -> tuple[str, bool]:
    """发出 AI 正文。返回 (给调用方的文本, 是否至少有一条真正发出或入库)。"""
    extra = _stamp_extra(extra)
    try:
        return await _deliver_ai_body(
            conversation_id, messages, extra=extra, local=local, allow_owned=allow_owned,
            anchor_user_message_id=anchor_user_message_id,
        )
    finally:
        _log_reply_timing()


async def _deliver_ai_body(conversation_id: int, messages: list[str], *, extra: dict,
                           local: bool, allow_owned: bool,
                           anchor_user_message_id: int | None) -> tuple[str, bool]:
    parts = [m.strip() for m in messages if isinstance(m, str) and m.strip()]
    if not parts:
        return "", False
    if anchor_user_message_id is not None and _has_newer_user_message(conversation_id, anchor_user_message_id):
        logger.info("已有更新的用户消息，放弃这次发送 conversation=%s", conversation_id)
        return "", False
    if local:
        from ..services import record_local_ai_message
        saved = await record_local_ai_message(
            conversation_id, "\n".join(parts)[:4000], extra, allow_owned=allow_owned)
        if saved is None:
            return "", False
        return saved.content, True
    if len(parts) == 1:
        sent = await send_outbound(
            conversation_id, parts[0], sender_type="ai", extra=extra, allow_owned=allow_owned)
        ok = sent is not None and not (sent.extra or {}).get("send_failed")
        shown = parts[0] if allow_owned or ok else ""
        return shown, ok
    ok = await _send_replies(conversation_id, parts, extra, allow_owned=allow_owned)
    shown = "\n".join(parts) if allow_owned or ok else ""
    return shown, ok


async def _send_replies(conversation_id: int, messages: list[str], extra: dict,
                        allow_owned: bool = False) -> bool:
    """逐条发送。某一条没发出去时，把后面几条记成未送达，不再整段重生成。"""
    from ..services import save_unsent_outbound

    chunks: list[str] = []
    for msg in messages:
        chunks.extend(humanize.split_long_message(msg.strip()))
    chunks = [c for c in chunks if c]
    delivered = False
    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(humanize.typing_delay_ms(chunks[i - 1]) / 1000)
        sent = await send_outbound(
            conversation_id, chunk, sender_type="ai", extra=dict(extra), allow_owned=allow_owned)
        if sent is None:
            return delivered
        if (sent.extra or {}).get("send_failed"):
            for rest in chunks[i + 1:]:
                await save_unsent_outbound(conversation_id, rest, extra=dict(extra))
            return delivered
        delivered = True
    return delivered


def _tool_rounds_exhausted(reply: AgentReply, tool_rounds: int) -> bool:
    """工具打满上限后仍在要工具、且一条回复都没有。已声明转人工的交给后面的 handoff。"""
    return (
        tool_rounds >= MAX_TOOL_ROUNDS
        and reply.tool_call is not None
        and not reply.reply_messages
        and not reply.handoff
    )


async def _call_llm_with_retry(rendered_prompt: str, *, conversation_id: int = 0,
                               timeout: float | None = None) -> AgentReply | None:
    """主模型调用（解析失败重试 1 次）。硬失败或解析失败时，改走备用模型再试一轮。"""
    reply, hard_failed = await _call_llm_once(
        rendered_prompt, conversation_id=conversation_id, timeout=timeout,
        base_url=settings.llm_base_url, api_key=settings.llm_api_key, model=settings.llm_model,
    )
    if reply is not None or not settings.llm_fallback_configured:
        return reply
    if hard_failed:
        logger.warning("主 LLM 不可用，切换备用模型 %s", settings.llm_fallback_model)
        monitor.record("llm_failure", f"主模型 {settings.llm_model} 失败，切换备用模型")
    else:
        logger.warning("主 LLM 没有给出可用 JSON，切换备用模型 %s", settings.llm_fallback_model)
    reply, _ = await _call_llm_once(
        rendered_prompt, conversation_id=conversation_id, timeout=FALLBACK_TIMEOUT_SECONDS,
        base_url=settings.llm_fallback_base_url or settings.llm_base_url,
        api_key=settings.llm_fallback_api_key, model=settings.llm_fallback_model,
    )
    return reply


async def _call_llm_once(rendered_prompt: str, *, base_url: str, api_key: str,
                         model: str, conversation_id: int = 0,
                         timeout: float | None = None) -> tuple[AgentReply | None, bool]:
    """调用指定模型并用 pydantic 校验契约；解析失败带错误反馈重试 1 次。

    返回 (reply, hard_failed)：hard_failed=True 表示网络/服务级异常（调用方可用备用模型兜底）。
    timeout 为 None 时沿用 SDK 默认。接待路径传入剩余预算。
    """
    client = get_llm_client(base_url, api_key)
    messages = [{"role": "user", "content": rendered_prompt}]
    raw = ""

    for attempt in range(2):
        try:
            create_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": 0.3,
                "response_format": {"type": "json_object"},
            }
            if timeout is not None:
                create_kwargs["timeout"] = timeout
            resp = await client.chat.completions.create(**create_kwargs)
            record_usage(conversation_id, model, getattr(resp, "usage", None))
            raw = resp.choices[0].message.content or ""
            return _parse_contract(raw), False
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("契约解析失败（第 %d 次）: %s", attempt + 1, exc)
            if attempt == 0:
                # 把错误反馈给模型重试
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                    f"你的输出不符合契约，错误：{exc}。请只输出正确的 JSON 对象，不要输出其他内容。"})
            continue
        except APITimeoutError:
            logger.warning("LLM 读超时 model=%s timeout=%s", model, timeout)
            monitor.record("llm_failure", f"model={model} timeout={timeout}")
            return None, True
        except Exception:  # noqa: BLE001
            logger.exception("LLM 调用失败 model=%s", model)
            monitor.record("llm_failure", f"model={model}")
            return None, True
    return None, False


def _parse_contract(raw: str) -> AgentReply:
    """容错解析：剥掉 markdown 代码块，提取第一个 JSON 对象；reply_messages 预清洗。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise json.JSONDecodeError("未找到 JSON 对象", text, 0)
    data = json.loads(text[start:end + 1])
    # 预清洗：过滤空消息、截断到 3 条（在校验前，避免模型给 4 条时整体校验失败）
    if isinstance(data.get("reply_messages"), list):
        data["reply_messages"] = [
            m.strip() for m in data["reply_messages"] if isinstance(m, str) and m.strip()
        ][:3]
    return AgentReply.model_validate(data)


def _apply_side_effects(customer_id: int, reply: AgentReply, *, conversation_id: int = 0,
                        platform: str = "", user_text: str = "", recent: list | None = None):
    """标签合并 + 留资更新。号码必须出现在当前这条用户消息里。"""
    db = SessionLocal()
    nickname = ""
    try:
        customer = db.get(Customer, customer_id)
        if customer is None:
            return
        nickname = customer.nickname or ""
        if reply.tags:
            existing = list(customer.tags or [])
            for tag in reply.tags:
                if tag and tag not in existing:
                    existing.append(tag)
            customer.tags = existing
        customer.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
    phone = lead_flow.phone_in_message(user_text)
    wechat = ""
    if not phone:
        mentioned = (reply.lead.wechat or "").strip()
        if mentioned and mentioned in (user_text or ""):
            wechat = mentioned
        else:
            wechat = lead_flow.wechat_in_message(user_text)
    contact = phone or wechat
    if contact and conversation_id:
        lead_flow.save_lead(
            customer_id=customer_id,
            conversation_id=conversation_id,
            platform=platform,
            nickname=nickname,
            contact=contact,
            contact_type="phone" if phone else "wechat",
            business=lead_flow.business_phrase(user_text, recent) or (reply.lead.note or ""),
        )
