"""知你快回「自己的回复接口」。

插件把网页私信 POST 过来，本模块入库并在同一次请求里生成回复。
发送由插件完成，这里不调用平台适配器，也不写 RPA outbox。
"""
import asyncio
import hashlib
import hmac
import logging
import re
import time
from typing import Any

from sqlalchemy.exc import IntegrityError

from ..agent.engine import process_ai_reply
from ..config import settings
from ..core import contentfilter, ratelimit
from ..core.convlock import conversation_lock
from ..database import SessionLocal
from ..models import Conversation, Customer, Message
from ..schemas import InboundMessage
from ..services import _apply_guide_code_hook, _broadcast_message, handoff

logger = logging.getLogger(__name__)


async def _conversation_lock(conversation_id: int) -> asyncio.Lock:
    """会话锁统一走 core.convlock：与队列并发消费路径共享，防跨入口同会话并发答题。"""
    return await conversation_lock(conversation_id)


async def _note_skip(conversation_id: int, content: str) -> None:
    """知你快回没有回复、也没有转人工系统行时，给工作台留一条内部说明。"""
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            return
        message = Message(
            conversation_id=conversation_id,
            sender_type="system",
            msg_type="system",
            content=content,
            is_internal=True,
            extra={"zhini_skip": True},
        )
        db.add(message)
        db.commit()
        db.refresh(message)
        await _broadcast_message(conversation, message)
    finally:
        db.close()

REPLY_PATH = "/api/integrations/zhinikuaihui/reply"
CONNECTION_REPLY = "连接成功，可以开始回复。"
REPLY_CHAR_LIMIT = 500
_LIST_LEAD = "您要的我都列下面啦"
_SHORTEN_TIMEOUT = 8.0
# 插件对多行回复的输入框粘贴会重复拼接、且停在生成阶段不自动发送；
# 发给插件的正文一律压成单行：换行折叠为空格，条目正文保持原样。
_SENT_SPLIT_RE = re.compile(r"([。！？!?；;])")
_SENT_PUNCT_RE = re.compile(r"[\s。；，、,;.:：()（）·…！!?？\"'“”‘’]+")
_SENT_DEDUPE_MIN = 10
PLATFORM_MAP = {
    "douyin": "douyin",
    "xiaohongshu": "xiaohongshu",
    "xiaohongshu-sxt": "xiaohongshu",
}


def short_key(platform: str, kind: str, raw: str) -> str:
    """稳定 63 字符短键，放进 String(64) 唯一列。原文不直接入库，避免和开放平台消息 ID 撞车。"""
    digest = hashlib.sha256(f"{platform}\n{kind}\n{raw}".encode()).hexdigest()[:60]
    return f"zn:{digest}"


def check_api_key(authorization: str, x_api_key: str) -> dict | None:
    """密钥未配置返回 503 说明；不匹配返回 401。通过时返回 None。

    插件只读 JSON 的 message 字段，不能用 FastAPI 默认的 detail。
    """
    if not settings.zhini_reply_api_key:
        return {"status": 503, "body": {"message": "知你快回回复接口未启用（未配置 ZHINI_REPLY_API_KEY）"}}
    tokens = []
    header = (authorization or "").strip()
    if header.lower().startswith("bearer "):
        tokens.append(header[7:].strip())
    if x_api_key:
        tokens.append(x_api_key.strip())
    expected = settings.zhini_reply_api_key
    if any(token and hmac.compare_digest(token, expected) for token in tokens):
        return None
    return {"status": 401, "body": {"message": "接口密钥无效"}}


def is_connection_test(body: dict) -> bool:
    request_id = str(body.get("request_id") or "")
    return body.get("conversation_id") == "connection-test" or request_id.startswith("connection-test-")


async def handle_reply(body: dict) -> dict:
    """返回插件要求的 reply / skip 对象。生成失败也是 HTTP 200 的 skip，避免插件暂停自动回复。"""
    started = time.monotonic()
    try:
        return await _handle_reply(body, started)
    except Exception:
        logger.exception("知你快回回复失败")
        return _log_outcome(started, _skip("回复生成失败，请稍后在工作台处理"))


async def _handle_reply(body: dict, started: float) -> dict:
    if is_connection_test(body):
        return _log_outcome(started, {"action": "reply", "reply": CONNECTION_REPLY})

    raw_platform = str(body.get("platform") or "").strip()
    platform = PLATFORM_MAP.get(raw_platform)
    if platform is None:
        return _log_outcome(started, _skip("该平台暂不支持"))

    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    text = str(message.get("text") or "").strip()
    direction = message.get("direction") or "incoming"
    if direction != "incoming" or not text:
        return _log_outcome(started, _skip("没有需要回复的客户消息"))
    if not _last_message_needs_reply(body, message):
        return _log_outcome(started, _skip("没有新的客户消息"))

    request_id = str(body.get("request_id") or message.get("id") or "").strip()
    if not request_id:
        return _log_outcome(started, _skip("缺少 request_id"))
    request_key = short_key(raw_platform, "request", request_id)

    cached = _cached_reply(request_key)
    if cached:
        reply = await prepare_plugin_reply(cached[:4000])
        return _log_outcome(started, {"action": "reply", "reply": reply})

    conversation_id, mode, status, trigger_id = _persist(body, platform, raw_platform, request_key, text)
    if trigger_id is None:
        cached = _cached_reply(request_key)
        if cached:
            reply = await prepare_plugin_reply(cached[:4000])
            return _log_outcome(started, {"action": "reply", "reply": reply},
                                conversation_id=conversation_id, mode=mode)
        return _log_outcome(started, _skip("消息入库冲突，请稍后重试"),
                            conversation_id=conversation_id, mode=mode)

    await _broadcast_saved(conversation_id, trigger_id)

    if status != "open":
        await _note_skip(conversation_id, "会话已结束，这条没有自动回复")
        return _log_outcome(started, _skip("会话已结束"), conversation_id=conversation_id, mode=mode)

    risk = contentfilter.check_inbound(text)
    if risk["level"] == "block":
        _mark_risk(trigger_id, risk)
        if mode == "ai":
            await handoff(conversation_id, reason="risk_content")
        return _log_outcome(started, _skip("消息触发风险拦截，请人工处理"),
                            conversation_id=conversation_id, mode=mode)
    if risk["level"] == "warn":
        _mark_risk(trigger_id, risk)

    if not settings.ai_globally_enabled:
        if mode == "ai":
            await handoff(conversation_id, reason="ai_globally_disabled")
        return _log_outcome(started, _skip("AI 已关闭，请人工回复"),
                            conversation_id=conversation_id, mode=mode)

    if mode not in ("ai", "pending", "human"):
        await _note_skip(conversation_id, "会话已由人工接待，这条没有自动回复")
        return _log_outcome(started, _skip("会话已由人工接待"),
                            conversation_id=conversation_id, mode=mode)

    lock = await _conversation_lock(conversation_id)
    async with lock:
        reply_text = await process_ai_reply(conversation_id, local=True, allow_owned=mode != "ai")
    reply_text = await prepare_plugin_reply((reply_text or "").strip()[:4000])
    if not reply_text:
        await _note_skip(conversation_id, "没有可发送的回复")
        return _log_outcome(started, _skip("没有可发送的回复"),
                            conversation_id=conversation_id, mode=mode)
    return _log_outcome(started, {"action": "reply", "reply": reply_text},
                        conversation_id=conversation_id, mode=mode)


def _log_outcome(started: float, result: dict, *, conversation_id: int | None = None,
                 mode: str = "") -> dict:
    """每个返回都留下 action。HTTP 200 分不出回复和跳过。"""
    elapsed_ms = int((time.monotonic() - started) * 1000)
    conv = conversation_id if conversation_id is not None else "-"
    mode_label = mode or "-"
    if result.get("action") == "reply":
        preview = (result.get("reply") or "").replace("\n", " ")[:80]
        logger.info(
            "知你快回回复 action=reply conversation=%s mode=%s elapsed_ms=%s reply=%s",
            conv, mode_label, elapsed_ms, preview,
        )
    else:
        logger.info(
            "知你快回回复 action=skip conversation=%s mode=%s elapsed_ms=%s reason=%s",
            conv, mode_label, elapsed_ms, (result.get("reason") or "")[:200],
        )
    return result


def _skip(reason: str) -> dict:
    return {"action": "skip", "reason": (reason or "不回复")[:500]}


def _collapse_leading_leads(text: str) -> str:
    """开头连续的「您要的我都列下面啦」只留一句，中间可以没有换行。"""
    body = (text or "").lstrip()
    lead = _LIST_LEAD
    while body.startswith(lead):
        rest = body[len(lead):].lstrip("\n")
        if rest.startswith(lead):
            body = rest
            continue
        break
    return body


def _same_notice(head: str, tail: str) -> bool:
    left = re.sub(r"\s+", "", head or "")
    right = re.sub(r"\s+", "", tail or "")
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    return len(shorter) >= 20 and longer.startswith(shorter)


def _drop_repeated_notice(text: str) -> str:
    """同一段清单再出现一次时，从第二句导语处截掉。不同清单保留。"""
    lead = _LIST_LEAD
    first = text.find(lead)
    if first < 0:
        return text
    second = text.find(lead, first + len(lead))
    if second < 0:
        return text
    head = text[first + len(lead):second]
    tail = text[second + len(lead):]
    if not _same_notice(head, tail):
        return text
    return text[:second].rstrip()


def dedupe_plugin_reply(text: str) -> str:
    return _drop_repeated_notice(_collapse_leading_leads(text)).strip()


def _flatten_for_plugin(text: str) -> str:
    """压成单行：换行折叠为空格，条目正文保持原样。"""
    parts = [line.strip() for line in (text or "").splitlines()]
    return " ".join(part for part in parts if part)


def _dedupe_sentences(text: str) -> str:
    """单行文本里归一化后相同的句子只留第一份（防缩短模型复述同段）。

    极短句不参与判重，避免误删正常重复的短句。
    """
    if not text:
        return text
    sentences: list[str] = []
    buf = ""
    for chunk in _SENT_SPLIT_RE.split(text):
        buf += chunk
        if _SENT_SPLIT_RE.match(chunk):
            sentences.append(buf)
            buf = ""
    if buf:
        sentences.append(buf)
    seen: set[str] = set()
    kept: list[str] = []
    for sentence in sentences:
        key = _SENT_PUNCT_RE.sub("", sentence)
        if len(key) >= _SENT_DEDUPE_MIN:
            if key in seen:
                continue
            seen.add(key)
        kept.append(sentence)
    return "".join(kept).strip()


def _finalize_plugin_reply(text: str) -> str:
    """发给插件前的统一收口：去掉重复份，再压成单行并做句级去重。"""
    return _dedupe_sentences(_flatten_for_plugin(dedupe_plugin_reply(text)))


def _cut_complete(text: str, limit: int = REPLY_CHAR_LIMIT) -> str:
    """收到最后一个还能放进上限的完整条目，不把半条编号切进去。

    多行按行切；压平后的单行按空格分出的条目（即压平前的行）切。都不行才按字符截。
    """
    if len(text) <= limit:
        return text.strip()
    units: list[str] = []
    for line in (text or "").splitlines():
        units.extend(piece for piece in line.split() if piece)
    kept: list[str] = []
    total = 0
    for unit in units:
        extra = len(unit) + (1 if kept else 0)
        if total + extra > limit:
            break
        kept.append(unit)
        total += extra
    if not kept:
        return text[:limit].rstrip()
    return " ".join(kept)


async def _shorten_written_reply(text: str) -> str:
    """只缩短已经写好的这段，不检索，不补充原文没有的事实。"""
    if not settings.llm_configured:
        return ""
    from ..llm import get_llm_client, record_usage

    prompt = (
        "把下面这段已经写好的客服回复缩成不超过500字。"
        "只保留原文里已有的事实，不要补充原文没有的内容，不要把同一段再说一遍。"
        "编号条目能留下就留下，放不下就整条省去。"
        "只输出缩短后的正文，不要解释。\n\n"
        + text
    )
    client = get_llm_client(settings.llm_base_url, settings.llm_api_key)
    try:
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            timeout=_SHORTEN_TIMEOUT,
        )
        record_usage(0, settings.llm_model, getattr(resp, "usage", None))
        return (resp.choices[0].message.content or "").strip()
    except Exception:
        logger.warning("知你快回长回复缩短失败", exc_info=True)
        return ""


async def prepare_plugin_reply(text: str) -> str:
    """交给插件前去掉重复份并压成单行。仍超过 500 字，再把这段长回复缩短。"""
    cleaned = _finalize_plugin_reply(text)
    if len(cleaned) <= REPLY_CHAR_LIMIT:
        return cleaned
    brief = await _shorten_written_reply(cleaned)
    brief = _finalize_plugin_reply(brief)
    if brief and len(brief) <= REPLY_CHAR_LIMIT:
        logger.info("知你快回长回复已缩短 before=%s after=%s", len(cleaned), len(brief))
        return brief
    if brief and len(brief) > REPLY_CHAR_LIMIT:
        cut = _cut_complete(brief)
        if cut:
            logger.info("知你快回缩短结果仍超长，改按条目截断 after=%s", len(cut))
            return cut
    cut = _cut_complete(cleaned)
    logger.info("知你快回长回复缩短失败，改按条目截断 after=%s", len(cut))
    return cut


def _cached_reply(request_key: str) -> str:
    db = SessionLocal()
    try:
        trigger = db.query(Message).filter(Message.platform_msg_id == request_key).first()
        if trigger is None:
            return ""
        reply = (
            db.query(Message)
            .filter(
                Message.conversation_id == trigger.conversation_id,
                Message.id > trigger.id,
                Message.sender_type == "ai",
                Message.is_internal.is_(False),
            )
            .order_by(Message.id.asc())
            .first()
        )
        return (reply.content or "").strip() if reply is not None else ""
    finally:
        db.close()


def _persist(body: dict, platform: str, raw_platform: str, request_key: str, text: str) -> tuple[int, str, str, int | None]:
    """写入客户、会话和消息。返回 (conversation_id, mode, status, trigger_message_id)。

    客户和会话先提交。消息单独提交，唯一冲突只回滚消息事务。
    """
    message = body.get("message") if isinstance(body.get("message"), dict) else {}
    customer_raw = str(body.get("customer_id") or message.get("sender_id") or body.get("conversation_id") or "unknown")
    customer_key = short_key(raw_platform, "customer", customer_raw)
    conversation_raw = str(body.get("conversation_id") or customer_raw)
    conversation_key = short_key(raw_platform, "conversation", conversation_raw)
    nickname = str(body.get("customer_nickname") or message.get("sender_name") or body.get("conversation_name") or "")[:64]
    request_id = str(body.get("request_id") or "")
    is_group = body.get("is_group") is True

    db = SessionLocal()
    try:
        customer = (
            db.query(Customer)
            .filter(Customer.platform == platform, Customer.platform_user_id == customer_key)
            .first()
        )
        if customer is None:
            customer = Customer(
                platform=platform,
                platform_user_id=customer_key,
                nickname=nickname or f"用户{customer_key[-4:]}",
                tags=[],
            )
            db.add(customer)
            db.flush()
        elif nickname:
            customer.nickname = nickname

        conversation = (
            db.query(Conversation)
            .filter(
                Conversation.customer_id == customer.id,
                Conversation.platform_conversation_id == conversation_key,
                Conversation.status == "open",
            )
            .order_by(Conversation.id.desc())
            .first()
        )
        if conversation is None:
            conversation = Conversation(
                customer_id=customer.id,
                platform=platform,
                platform_conversation_id=conversation_key,
                mode="ai",
                status="open",
            )
            db.add(conversation)
            db.flush()

        _apply_guide_code_hook(db, customer, InboundMessage(
            platform=platform,
            platform_user_id=customer_key,
            platform_conversation_id=conversation_key,
            content=text,
            nickname=nickname,
        ))
        db.commit()
        conversation_id = conversation.id
        mode = conversation.mode
        status = conversation.status
    finally:
        db.close()

    history = body.get("messages") if isinstance(body.get("messages"), list) else []
    for item in history:
        if not isinstance(item, dict) or _same_trigger(item, message):
            continue
        item_text = str(item.get("text") or "").strip()
        if not item_text:
            continue
        item_direction = item.get("direction") or "incoming"
        item_id = str(item.get("id") or "").strip()
        if item_id:
            item_key = short_key(raw_platform, "message", item_id)
        else:
            item_key = short_key(
                raw_platform, "history",
                f"{conversation_key}\n{item_direction}\n{int(item.get('timestamp') or 0)}\n{item_text}",
            )
        _insert_message(
            conversation_id,
            sender_type="user" if item_direction == "incoming" else "agent",
            content=item_text[:4000],
            platform_msg_id=item_key,
            extra=_message_extra(body, raw_platform, item, request_id=""),
            unread=False,
        )

    trigger = _insert_message(
        conversation_id,
        sender_type="user",
        content=text[:4000],
        platform_msg_id=request_key,
        extra=_message_extra(body, raw_platform, message, request_id=request_id),
        unread=True,
    )
    if trigger is None:
        return conversation_id, mode, status, None

    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        mode = conversation.mode if conversation is not None else mode
        status = conversation.status if conversation is not None else status
        if conversation is not None:
            ratelimit.reset_reply_window(
                conversation.platform,
                conversation.platform_conversation_id or str(conversation.id),
            )
    finally:
        db.close()
    return conversation_id, mode, status, trigger


_OUR_DIRECTIONS = {"outgoing", "out", "sent"}


def _message_timestamp(item: dict) -> int:
    try:
        return int(item.get("timestamp") or 0)
    except (TypeError, ValueError):
        return 0


def _is_customer_message(item: dict, customer_id: str) -> bool:
    """最后一条要有正文，并且是客户发的，才值得回复。"""
    if not str(item.get("text") or "").strip():
        return False
    direction = str(item.get("direction") or "").strip().lower()
    if direction in _OUR_DIRECTIONS:
        return False
    if direction == "incoming":
        return True
    if direction:
        return False
    sender = str(item.get("sender_id") or "").strip()
    customer = (customer_id or "").strip()
    if sender and customer and sender != customer:
        return False
    return True


def _ordered_thread(body: dict, trigger: dict) -> list[dict]:
    """历史加触发消息。有时间戳就按时间排，否则保持插件顺序。"""
    history = body.get("messages") if isinstance(body.get("messages"), list) else []
    items = [item for item in history if isinstance(item, dict)]
    if trigger and not any(_same_trigger(item, trigger) for item in items):
        items.append(trigger)
    if any(_message_timestamp(item) for item in items):
        items = sorted(items, key=_message_timestamp)
    return items


def _last_message_needs_reply(body: dict, trigger: dict) -> bool:
    items = _ordered_thread(body, trigger)
    if not items:
        return False
    customer_id = str(body.get("customer_id") or trigger.get("sender_id") or "")
    return _is_customer_message(items[-1], customer_id)


def _same_trigger(item: dict, trigger: dict) -> bool:
    item_id = str(item.get("id") or "").strip()
    trigger_id = str(trigger.get("id") or "").strip()
    if item_id and trigger_id and item_id == trigger_id:
        return True
    return (
        str(item.get("text") or "").strip() == str(trigger.get("text") or "").strip()
        and (item.get("direction") or "incoming") == (trigger.get("direction") or "incoming")
        and int(item.get("timestamp") or 0) == int(trigger.get("timestamp") or 0)
    )


def _message_extra(body: dict, raw_platform: str, message: dict, *, request_id: str) -> dict:
    extra: dict[str, Any] = {
        "channel": "zhinikuaihui",
        "plugin_platform": raw_platform,
        "zhini_conversation_id": str(body.get("conversation_id") or ""),
        "zhini_customer_id": str(body.get("customer_id") or ""),
        "zhini_message_id": str(message.get("id") or ""),
        "is_group": body.get("is_group") is True,
    }
    if request_id:
        extra["zhini_request_id"] = request_id
    return extra


def _insert_message(conversation_id: int, *, sender_type: str, content: str,
                    platform_msg_id: str, extra: dict, unread: bool) -> int | None:
    """插入一条消息。唯一冲突时不回滚其它已提交数据，返回已有或新建消息的 id。"""
    db = SessionLocal()
    try:
        existing = db.query(Message).filter(Message.platform_msg_id == platform_msg_id).first()
        if existing is not None:
            return existing.id
        message = Message(
            conversation_id=conversation_id,
            sender_type=sender_type,
            msg_type="text",
            content=content,
            platform_msg_id=platform_msg_id,
            extra=extra,
        )
        db.add(message)
        db.flush()
        if unread and sender_type == "user":
            conversation = db.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.unread_count = (conversation.unread_count or 0) + 1
                conversation.last_message_at = message.created_at
        db.commit()
        return message.id
    except IntegrityError:
        db.rollback()
        existing = db.query(Message).filter(Message.platform_msg_id == platform_msg_id).first()
        return existing.id if existing is not None else None
    finally:
        db.close()


def _mark_risk(message_id: int, risk: dict) -> None:
    db = SessionLocal()
    try:
        message = db.get(Message, message_id)
        if message is None:
            return
        message.extra = {**(message.extra or {}), "risk": risk["level"], "risk_hits": risk["hit"]}
        db.commit()
    finally:
        db.close()


async def _broadcast_saved(conversation_id: int, message_id: int) -> None:
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        message = db.get(Message, message_id)
        if conversation is None or message is None:
            return
        await _broadcast_message(conversation, message)
    finally:
        db.close()
