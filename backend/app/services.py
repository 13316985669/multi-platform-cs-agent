"""会话服务中枢：入站消息处理、出站发送（守卫+频控）、转人工、广播。

这是连接 适配器 / 队列 / Agent 引擎 / WebSocket 的业务核心。
"""
import logging
import re
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError

from .adapters import get_adapter, get_rpa_fallback_adapter, get_send_adapter, send_channel
from .adapters.rpa import split_account
from .bindings import resolve_douyin_account_type
from .api.ws import manager
from .config import settings
from .core import contentfilter, idempotency, monitor, ratelimit
from .core.convlock import conversation_lock
from .core.queue import enqueue, register_handler
from .database import SessionLocal
from .models import Conversation, Customer, HandoffEvent, MatrixAccount, Message, MissedQuestion
from .schemas import InboundMessage

logger = logging.getLogger(__name__)


# ============ 入站 ============

async def handle_inbound(payload: dict[str, Any]):
    """队列 worker 消费入口：normalize → 幂等 → 入库 → 广播 → AI 接待。"""
    # 按通道选适配器：mock 通道（模拟面板）强制走 MockAdapter，
    # 其 normalize 会读取 payload["platform"] 作为会话的展示平台标识；
    # 真实平台 webhook 不带 channel 字段，按 platform 正常路由
    channel = payload.get("channel") or payload.get("platform", "mock")
    adapter = get_adapter(channel)
    msg = await adapter.normalize(payload)
    if msg is None:
        logger.info(
            "入站事件被适配器忽略 channel=%s platform=%s payload_keys=%s",
            channel, payload.get("platform"), sorted(payload.keys()),
        )
        return

    # 幂等键在用户消息入库成功后才写入。键在、消息不在，说明进程在这两步之间被杀掉，放行重试。
    event_key = f"{msg.platform}:{msg.platform_msg_id}" if msg.platform_msg_id else ""
    if event_key and idempotency.exists(event_key) and not _platform_message_exists(msg.platform_msg_id):
        idempotency.release(event_key)

    db = SessionLocal()
    try:
        try:
            await _handle_inbound_locked(db, msg)
        except Exception:
            db.rollback()
            idempotency.release(event_key)
            raise
    finally:
        db.close()


def _platform_message_exists(platform_msg_id: str) -> bool:
    if not platform_msg_id:
        return False
    db = SessionLocal()
    try:
        row = db.query(Message.id).filter(Message.platform_msg_id == platform_msg_id).first()
        return row is not None
    finally:
        db.close()


def _mark_inbound(msg: InboundMessage) -> None:
    if msg.platform_msg_id:
        idempotency.mark(f"{msg.platform}:{msg.platform_msg_id}")


async def _handle_inbound_locked(db, msg: InboundMessage):
    """幂等键已占住之后的入库与接待。调用方在异常时释放键。"""
    if msg.platform_msg_id:
        existing = (
            db.query(Message).filter(Message.platform_msg_id == msg.platform_msg_id).first()
        )
        if existing is not None:
            # 上次已入库但中途失败：不插第二条、不加未读；还没有回复才补跑
            if msg.sender_side != "agent":
                await _resume_if_unanswered(existing.conversation_id, existing.id)
            _mark_inbound(msg)
            return

    customer = _upsert_customer(db, msg)
    conversation = _get_or_create_conversation(db, customer, msg)

    # 暗号钩子：用户私信命中评论规则的暗号 → 打标 + 漏斗归因（不阻断后续 AI 流程）
    if msg.sender_side != "agent" and msg.content:
        _apply_guide_code_hook(db, customer, msg)

    # 人工旁路消息（客服在平台后台直接发送，RPA Worker 同步）：入库保持消息流完整，
    # 不触发 AI、不计未读、不做内容过滤
    if msg.sender_side == "agent":
        message = _save_message(
            db, conversation.id, sender_type="agent", msg_type=msg.msg_type,
            content=msg.content, platform_msg_id=msg.platform_msg_id or None,
            extra={"bypass": True, **({"media_id": msg.media_id} if msg.media_id else {})},
        )
        conversation.last_message_at = message.created_at
        db.commit()
        _mark_inbound(msg)
        await _broadcast_message(conversation, message)
        return

    # 媒体消息 → 文本替身：语音走 ASR 转写，图片走视觉描述；未配置/失败用占位文本
    media_extra: dict = {}
    if msg.msg_type == "voice" and msg.media_id:
        from .core import asr
        text = await asr.transcribe_media(msg.media_id)
        msg.content = text or "[语音消息]"
        media_extra = {"media_id": msg.media_id, "asr": bool(text)}
    elif msg.msg_type == "image" and msg.media_id:
        from .core import asr
        desc = await asr.describe_media(msg.media_id)
        msg.content = f"[图片] {desc}" if desc else "[图片消息]"
        media_extra = {"media_id": msg.media_id, "vision": bool(desc)}

    message = _save_message(
        db, conversation.id, sender_type="user", msg_type=msg.msg_type,
        content=msg.content, platform_msg_id=msg.platform_msg_id or None,
        extra=media_extra,
    )

    # 入口内容安全分级：block 打标 + 转人工（AI 不回复）；warn 仅打标
    # 媒体消息的替身文本（转写/描述）同样参与过滤
    risk = contentfilter.check_inbound(msg.content) if msg.content else {"level": "ok", "hit": []}
    if risk["level"] != "ok":
        message.extra = {**(message.extra or {}), "risk": risk["level"], "risk_hits": risk["hit"]}
        logger.warning("入口风险消息 level=%s hits=%s content=%s",
                       risk["level"], risk["hit"], contentfilter.mask_lead(msg.content))

    conversation.last_message_at = message.created_at
    conversation.unread_count = (conversation.unread_count or 0) + 1
    ratelimit.reset_reply_window(
        conversation.platform,
        conversation.platform_conversation_id or str(conversation.id),
    )
    db.commit()
    _mark_inbound(msg)

    await _broadcast_message(conversation, message)

    await _route_after_user_message(
        conversation.id,
        risk_level=risk["level"],
        mode=conversation.mode,
        status=conversation.status,
    )


async def _route_after_user_message(conversation_id: int, *, risk_level: str, mode: str, status: str):
    """用户消息已入库后的分流：风险/熔断转人工，AI 接待才进引擎。"""
    if risk_level == "block":
        # 严重违规：AI 不回复，直接转人工（系统提示语由 handoff 生成）
        await handoff(conversation_id, reason="risk_content")
        return

    # AI 全局熔断：开关关闭时所有 AI 会话直接转人工，不调用 LLM
    if not settings.ai_globally_enabled and mode == "ai":
        logger.warning("AI 全局开关已关闭，会话 %s 直接转人工", conversation_id)
        await handoff(conversation_id, reason="ai_globally_disabled")
        return

    # AI 接待直接进引擎。已经排队或人工接待、但这条后面还没人回时，也补答一次。
    # 会话锁：fast lane 并发消费下，同一会话的两条消息必须按序答题。
    if status == "open" and mode == "ai":
        await manager.broadcast("ai_typing", {"conversation_id": conversation_id})
        from .agent.engine import process_ai_reply  # 延迟导入避免循环依赖
        lock = await conversation_lock(conversation_id)
        async with lock:
            await process_ai_reply(conversation_id)
        return
    if status == "open" and mode in ("pending", "human") and settings.ai_globally_enabled:
        from .agent.engine import process_ai_reply
        lock = await conversation_lock(conversation_id)
        async with lock:
            await process_ai_reply(conversation_id, allow_owned=True)


async def _resume_if_unanswered(conversation_id: int, user_message_id: int):
    """重试时消息已在库：紧跟着的已是回复则结束，否则按当前规则补跑。

    后面先插入更新的用户消息时（回复答的是下一问），本条仍算没答上，需要补跑。
    """
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        user_message = db.get(Message, user_message_id)
        if conversation is None or user_message is None:
            return
        first_after = (
            db.query(Message.sender_type)
            .filter(
                Message.conversation_id == conversation_id,
                Message.id > user_message_id,
                Message.is_internal.is_(False),
            )
            .order_by(Message.id)
            .first()
        )
        # 紧跟本条出现的是回复才算已答；先出现新用户消息说明本条被跳过了
        if first_after is not None and first_after[0] != "user":
            return
        content = user_message.content or ""
        risk_level = contentfilter.check_inbound(content)["level"] if content else "ok"
        mode, status = conversation.mode, conversation.status
    finally:
        db.close()
    await _route_after_user_message(
        conversation_id, risk_level=risk_level, mode=mode, status=status,
    )


async def note_unmatched(conversation_id: int) -> None:
    """排队或人工会话没对上资料：写系统说明，不再重复转人工。"""
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.status != "open":
            return
        message = _save_message(
            db, conversation_id, sender_type="system", msg_type="system",
            content="这条没对上资料，需要人工回复",
        )
        conversation.last_message_at = message.created_at
        db.commit()
        await _broadcast_message(conversation, message)
    except Exception:
        db.rollback()
        logger.exception("写入未命中说明失败 conversation=%s", conversation_id)
    finally:
        db.close()


async def replay_unanswered_phrase(phrase: str) -> int:
    """只补跑仍 open、最后一条正好是这句话、且后面没有回复的会话。

    知你快回会话跳过：插件当时已经结束等待，事后生成也送不到网页。
    """
    target = (phrase or "").strip()
    if not target:
        return 0
    db = SessionLocal()
    jobs: list[tuple[int, int]] = []
    try:
        conversations = db.query(Conversation).filter(Conversation.status == "open").all()
        for conversation in conversations:
            if is_zhini_conversation(conversation):
                continue
            last = (
                db.query(Message)
                .filter(
                    Message.conversation_id == conversation.id,
                    Message.is_internal.is_(False),
                )
                .order_by(Message.id.desc())
                .first()
            )
            if last is None or last.sender_type != "user":
                continue
            if (last.content or "").strip() != target:
                continue
            jobs.append((conversation.id, last.id))
    finally:
        db.close()
    done = 0
    for conversation_id, message_id in jobs:
        try:
            await _resume_if_unanswered(conversation_id, message_id)
            done += 1
        except Exception:
            logger.exception("补跑未回复消息失败 conversation=%s", conversation_id)
    if done:
        logger.info("已补跑未回复的「%s」%d 条", target, done)
    return done


def _upsert_customer(db, msg: InboundMessage) -> Customer:
    customer = (
        db.query(Customer)
        .filter(Customer.platform == msg.platform, Customer.platform_user_id == msg.platform_user_id)
        .first()
    )
    if customer is None:
        customer = Customer(
            platform=msg.platform,
            platform_user_id=msg.platform_user_id,
            nickname=msg.nickname or f"用户{msg.platform_user_id[-4:]}",
            avatar=msg.avatar,
            tags=[],
        )
        db.add(customer)
        db.flush()
    else:
        # 更新资料（平台侧昵称头像可能变化）
        if msg.nickname:
            customer.nickname = msg.nickname
        if msg.avatar:
            customer.avatar = msg.avatar
        customer.updated_at = datetime.utcnow()
    return customer


def _get_or_create_conversation(db, customer: Customer, msg: InboundMessage) -> Conversation:
    conversation = (
        db.query(Conversation)
        .filter(Conversation.customer_id == customer.id, Conversation.status == "open")
        .order_by(Conversation.id.desc())
        .first()
    )
    if conversation is None:
        conversation = Conversation(
            customer_id=customer.id,
            platform=msg.platform,
            platform_conversation_id=msg.platform_conversation_id,
            mode="ai",
            status="open",
        )
        db.add(conversation)
        db.flush()
    elif msg.platform_conversation_id and not conversation.platform_conversation_id:
        conversation.platform_conversation_id = msg.platform_conversation_id
    return conversation


def _save_message(db, conversation_id: int, *, sender_type: str, content: str,
                  msg_type: str = "text", sender_id: int | None = None,
                  platform_msg_id: str | None = None, extra: dict | None = None,
                  is_internal: bool = False) -> Message:
    message = Message(
        conversation_id=conversation_id,
        sender_type=sender_type,
        sender_id=sender_id,
        msg_type=msg_type,
        content=content,
        platform_msg_id=platform_msg_id,
        extra=extra or {},
        is_internal=is_internal,
    )
    db.add(message)
    try:
        db.flush()
    except IntegrityError:
        # platform_msg_id 唯一冲突 = 重复消息，回滚后查已有
        db.rollback()
        existing = db.query(Message).filter(Message.platform_msg_id == platform_msg_id).first()
        if existing:
            return existing
        raise
    return message


# ============ 出站（守卫 + 频控 + 发送 + 入库 + 广播） ============

def _extra_at_ai_save(sender_type: str, extra: dict | None) -> dict:
    """AI 气泡入库前复制 extra，并写下从本轮开始到此刻的总耗时。"""
    if sender_type != "ai":
        return dict(extra or {})
    from .agent.engine import refresh_reply_timing
    return refresh_reply_timing(extra)


def conversation_is_laike(db, conversation: Conversation | None) -> bool:
    """来客账号形态。平台字段仍是 douyin，不能按平台名判断。"""
    if conversation is None or conversation.platform != "douyin":
        return False
    if send_channel(conversation.platform) != "rpa":
        return False
    account_key, _ = split_account(conversation.platform_conversation_id or "")
    account = None
    if account_key:
        account = db.query(MatrixAccount).filter(MatrixAccount.rpa_account == account_key).first()
    return resolve_douyin_account_type(account) == "laike"


def is_zhini_conversation(conversation: Conversation | None) -> bool:
    """知你快回入库时把会话号收成 zn: 短键，不能拿去调开放平台或写 RPA。"""
    if conversation is None:
        return False
    return str(conversation.platform_conversation_id or "").startswith("zn:")


async def send_outbound(conversation_id: int, content: str, *, sender_type: str,
                        sender_id: int | None = None, extra: dict | None = None,
                        skip_filter: bool = False, msg_type: str = "text",
                        media_id: str = "", allow_owned: bool = False) -> Message | None:
    """统一出站入口。返回 None 表示被守卫/频控拦截。

    msg_type/media_id：图片/语音消息（RPA 通道经 outbox 由 Worker 发到平台）。
    """
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            return None
        # AI 气泡发出前再看一次：人工已接管、会话已结束或全局开关已关，就不再打到平台。
        # 人工消息和转人工系统消息不走这里。
        if sender_type == "ai" and not _ai_may_send(conversation, allow_owned=allow_owned):
            logger.info(
                "跳过 AI 出站 conversation=%s mode=%s status=%s ai_enabled=%s",
                conversation_id, conversation.mode, conversation.status,
                settings.ai_globally_enabled,
            )
            return None
        customer = db.get(Customer, conversation.customer_id)

        # 1. 出口违禁词过滤。删空后不发给平台。
        filtered = content
        if not skip_filter:
            filtered, hits = contentfilter.sanitize(content)
            filtered = (filtered or "").strip()
            if hits:
                logger.warning("违禁词命中 %s，已改写: %s", hits, contentfilter.mask_lead(content))
                extra = {**(extra or {}), "filtered_words": hits}
            if not filtered:
                note = _save_message(
                    db, conversation_id, sender_type="system", msg_type="system",
                    is_internal=True, content="这条回复被违禁词删空了，没有发给顾客",
                )
                conversation.last_message_at = note.created_at
                db.commit()
                await _broadcast_message(conversation, note)
                await handoff(conversation_id, reason="low_confidence")
                return None

        # 知你快回没有回写网页的通道。事后出站只留在工作台，不占平台频控，也不打开放平台。
        if is_zhini_conversation(conversation):
            return await _persist_zhini_local(
                db, conversation, content=filtered, sender_type=sender_type,
                sender_id=sender_id, msg_type=msg_type, media_id=media_id,
                extra=_extra_at_ai_save(sender_type, extra),
            )

        # 2. 平台频控（RPA 通道适用更严的专属规则，键为 <platform>_rpa；
        #    抖音按会话所属账号的形态选规则，账号未指定时回退全站默认）
        conv_key = conversation.platform_conversation_id or str(conversation.id)
        rate_platform = conversation.platform
        if send_channel(conversation.platform) == "rpa":
            rate_platform = f"{conversation.platform}_rpa"
            if conversation.platform == "douyin":
                account_key, _ = split_account(conversation.platform_conversation_id or "")
                account = None
                if account_key:
                    account = db.query(MatrixAccount).filter(
                        MatrixAccount.rpa_account == account_key,
                    ).first()
                kind = resolve_douyin_account_type(account)
                if kind == "enterprise":
                    rate_platform = "douyin_enterprise_rpa"
                elif kind == "laike":
                    rate_platform = "douyin_laike_rpa"
        account_hour_key = ""
        if rate_platform == "douyin_laike_rpa":
            account_hour_key = account_key or "laike"
            allowed, rule = ratelimit.check_and_count(
                "douyin_laike_account", account_hour_key, commit=False,
            )
            if not allowed:
                logger.warning("来客账号熔断 account=%s rule=%s，转人工", account_hour_key, rule)
                note = _save_message(
                    db, conversation_id, sender_type="system", msg_type="system",
                    is_internal=True,
                    content=f"来客这个账号 1 小时内已经发出 10 条，这条没有再发：{(filtered or '')[:200]}",
                )
                conversation.last_message_at = note.created_at
                db.commit()
                await _broadcast_message(conversation, note)
                await handoff(conversation_id, reason=f"rate_limit:{rule}")
                return None
        allowed, rule = ratelimit.check_and_count(rate_platform, conv_key)
        if allowed and account_hour_key:
            ratelimit.check_and_count("douyin_laike_account", account_hour_key)
        if not allowed:
            logger.warning("频控超限 platform=%s rule=%s，转人工", rate_platform, rule)
            note = _save_message(
                db, conversation_id, sender_type="system", msg_type="system",
                is_internal=True,
                content=f"这条回复被频控拦住了（{rule}），未发给顾客：{(filtered or '')[:200]}",
            )
            conversation.last_message_at = note.created_at
            db.commit()
            await _broadcast_message(conversation, note)
            await handoff(conversation_id, reason=f"rate_limit:{rule}")
            return None

        # 3. 平台发送（官方 API 或 RPA outbox，按通道配置路由）；
        #    API 通道失败且 RPA 已配置时自动降级 RPA 兜底（消息不丢，运维告警）
        adapter = get_send_adapter(conversation.platform)
        try:
            platform_msg_id = await adapter.send(
                conversation.platform_conversation_id, customer.platform_user_id, filtered,
                msg_type=msg_type, media_id=media_id,
            )
        except Exception as exc:  # noqa: BLE001
            fallback = get_rpa_fallback_adapter(conversation.platform)
            if fallback is not None:
                logger.exception("[API 发送失败→RPA 降级] platform=%s conv=%s err=%s",
                                 conversation.platform, conversation.platform_conversation_id, exc)
                monitor.record("api_send_fallback",
                               f"platform={conversation.platform} err={str(exc)[:150]}")
                try:
                    platform_msg_id = await fallback.send(
                        conversation.platform_conversation_id, customer.platform_user_id, filtered,
                        msg_type=msg_type, media_id=media_id,
                    )
                except Exception as fallback_exc:  # noqa: BLE001
                    return await _persist_send_failure(
                        db, conversation, content=filtered, sender_type=sender_type,
                        sender_id=sender_id, msg_type=msg_type,
                        extra=_extra_at_ai_save(sender_type, extra),
                        error=str(fallback_exc),
                    )
                extra = {**(extra or {}), "send_fallback": "rpa", "api_error": str(exc)[:200]}
            else:
                return await _persist_send_failure(
                    db, conversation, content=filtered, sender_type=sender_type,
                    sender_id=sender_id, msg_type=msg_type,
                    extra=_extra_at_ai_save(sender_type, extra), error=str(exc),
                )

        # 4. 入库 + 广播。总耗时在平台发送返回后写入，每条气泡用一份新的 extra。
        if media_id:
            extra = {**(extra or {}), "media_id": media_id}
        extra = _extra_at_ai_save(sender_type, extra)
        message = _save_message(
            db, conversation_id, sender_type=sender_type, content=filtered,
            msg_type=msg_type, sender_id=sender_id, platform_msg_id=platform_msg_id,
            extra=extra,
        )
        conversation.last_message_at = message.created_at
        db.commit()
        await _broadcast_message(conversation, message)
        return message
    finally:
        db.close()


async def _persist_zhini_local(db, conversation: Conversation, *, content: str,
                               sender_type: str, sender_id: int | None, msg_type: str,
                               media_id: str, extra: dict | None) -> Message:
    """知你快回事后出站：入库并广播，标明这条没有发到网页。"""
    payload = _extra_at_ai_save(sender_type, extra)
    payload["channel"] = "zhinikuaihui"
    payload["not_delivered_to_page"] = True
    if media_id:
        payload["media_id"] = media_id
    message = _save_message(
        db, conversation.id, sender_type=sender_type, content=content,
        msg_type=msg_type, sender_id=sender_id, extra=payload,
    )
    conversation.last_message_at = message.created_at
    db.commit()
    await _broadcast_message(conversation, message)
    return message


def _ai_may_send(conversation: Conversation, *, allow_owned: bool = False) -> bool:
    """AI 出站默认只在 AI 接待时放行。补答和招呼可在排队/人工会话里发出，模式不变。"""
    if conversation.status != "open" or not settings.ai_globally_enabled:
        return False
    if conversation.mode == "ai":
        return True
    return allow_owned and conversation.mode in ("pending", "human")


async def record_local_ai_message(conversation_id: int, content: str,
                                  extra: dict | None = None,
                                  allow_owned: bool = False) -> Message | None:
    """知你快回通道：违禁词过滤后只写入本系统并广播，不调用平台适配器，不计频控。

    platform_msg_id 留空，避免和触发消息的短键或平台消息 ID 冲突。
    """
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            logger.info("知你快回本地回复未入库 conversation=%s reason=会话不存在", conversation_id)
            return None
        if not _ai_may_send(conversation, allow_owned=allow_owned):
            if conversation.status != "open":
                reason = f"会话已结束 status={conversation.status}"
            elif not settings.ai_globally_enabled:
                reason = "AI 总开关已关"
            else:
                reason = "模式不允许代发"
            logger.info(
                "知你快回本地回复未入库 conversation=%s mode=%s reason=%s",
                conversation_id, conversation.mode, reason,
            )
            return None
        filtered, hits = contentfilter.sanitize(content)
        filtered = (filtered or "").strip()[:4000]
        if not filtered:
            reason = "违禁词删空" if hits else "正文为空"
            logger.info(
                "知你快回本地回复未入库 conversation=%s mode=%s reason=%s hits=%s",
                conversation_id, conversation.mode, reason, hits,
            )
            return None
        payload = _extra_at_ai_save("ai", extra)
        payload["channel"] = "zhinikuaihui"
        if hits:
            payload["filtered_words"] = hits
        message = Message(
            conversation_id=conversation_id,
            sender_type="ai",
            msg_type="text",
            content=filtered,
            platform_msg_id=None,
            extra=payload,
        )
        db.add(message)
        db.flush()
        conversation.last_message_at = message.created_at
        db.commit()
        db.refresh(message)
        await _broadcast_message(conversation, message)
        return message
    finally:
        db.close()


# ============ 转人工 / 模式切换 ============

def handoff_public_line(reason: str) -> str:
    """工作台系统行用口语。原始 reason 只留在 HandoffEvent。"""
    if (reason or "").startswith("rate_limit:"):
        detail = "发送有点频繁，先请同事来回复"
    else:
        detail = {
            "low_confidence": "我先请同事过来确认一下",
            "llm_parse_failed": "我这边卡住了，先请同事过来",
            "tool_rounds_exhausted": "我查到一半卡住了，先请同事过来",
            "llm_not_configured": "自动回复这会儿不可用，先请同事过来",
            "ai_globally_disabled": "自动回复已暂停，先请同事过来",
            "risk_content": "这条需要同事来处理",
        }.get(reason or "", "先请同事过来")
    return f"已为您转接人工客服，请稍等哈~（{detail}）"


async def handoff(conversation_id: int, reason: str, db=None):
    """会话切到 pending（排队待人工），记录事件，自动分配客服，广播提醒。"""
    own_db = db is None
    db = db or SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None or conversation.mode != "ai":
            return
        conversation.mode = "pending"
        db.add(HandoffEvent(conversation_id=conversation_id, reason=reason, from_mode="ai"))

        sys_msg = _save_message(
            db, conversation_id, sender_type="system", msg_type="system",
            content=handoff_public_line(reason),
        )
        conversation.last_message_at = sys_msg.created_at
        db.commit()

        # 自动分配（按空闲度轮转）
        from .agent.router import auto_assign
        auto_assign(db, conversation_id)
        db.commit()

        await _broadcast_message(conversation, sys_msg)
        await manager.broadcast("handoff", {"conversation_id": conversation_id, "reason": reason})
    finally:
        if own_db:
            db.close()


# ============ 未命中问题沉淀 ============

def _normalize_missed_question(question: str) -> str:
    text = re.sub(r"\s+", "", question or "").strip()
    return text.rstrip("？?。！!")[:500]


def record_missed_question(question: str, platform: str, conversation_id: int, suggested_answer: str = ""):
    db = SessionLocal()
    try:
        normalized = _normalize_missed_question(question)
        pending = (
            db.query(MissedQuestion)
            .filter(MissedQuestion.status == "pending")
            .all()
        )
        existing = next(
            (row for row in pending if _normalize_missed_question(row.question) == normalized),
            None,
        )
        if existing:
            existing.count += 1
            existing.updated_at = datetime.utcnow()
            if suggested_answer:
                existing.suggested_answer = suggested_answer[:2000]
        else:
            db.add(MissedQuestion(
                question=normalized or question[:500], platform=platform, conversation_id=conversation_id,
                suggested_answer=(suggested_answer or "")[:2000],
            ))
        db.commit()
    finally:
        db.close()


def note_agent_correction(conversation_id: int, answer: str) -> None:
    """人工接管后的回复先记成待审答案，确认后才写入知识库。"""
    text = (answer or "").strip()
    if not text:
        return
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            return
        previous = (
            db.query(Message)
            .filter(Message.conversation_id == conversation_id, Message.sender_type == "user")
            .order_by(Message.id.desc())
            .first()
        )
        if previous is None or not (previous.content or "").strip():
            return
        question = previous.content.strip()[:500]
        existing = (
            db.query(MissedQuestion)
            .filter(MissedQuestion.question == question, MissedQuestion.status == "pending")
            .first()
        )
        if existing:
            existing.suggested_answer = text[:2000]
            existing.updated_at = datetime.utcnow()
        else:
            db.add(MissedQuestion(
                question=question, platform=conversation.platform, conversation_id=conversation_id,
                suggested_answer=text[:2000],
            ))
        db.commit()
    finally:
        db.close()


# ============ 广播辅助 ============

async def _broadcast_message(conversation: Conversation, message: Message):
    await manager.broadcast("new_message", {
        "conversation_id": conversation.id,
        "platform": conversation.platform,
        "message": {
            "id": message.id,
            "conversation_id": message.conversation_id,
            "sender_type": message.sender_type,
            "sender_id": message.sender_id,
            "msg_type": message.msg_type,
            "content": message.content,
            "extra": message.extra or {},
            "is_internal": message.is_internal,
            "created_at": message.created_at.isoformat() if message.created_at else None,
        },
    })


def _apply_guide_code_hook(db, customer: Customer, msg: InboundMessage):
    """暗号钩子：私信内容命中启用中评论规则的 guide_code → 客户打标 + FunnelEvent(dm)。

    用于评论引流归因：评论自动回复引导用户私信暗号，此处识别暗号完成
    「评论 → 私信」链路的归因串联。不阻断后续 AI 接待流程。
    """
    from .models import CommentRule, FunnelEvent  # 延迟导入避免顶部膨胀
    rules = (db.query(CommentRule).filter(CommentRule.enabled.is_(True))
             .order_by(CommentRule.priority.desc(), CommentRule.id).all())
    for rule in rules:
        code = (rule.guide_code or "").strip()
        if not code or code not in msg.content:
            continue
        tags = list(customer.tags or [])
        tag = f"暗号:{code}"
        if tag not in tags:
            tags.append(tag)
            customer.tags = tags
        if not customer.source_guide_code:
            customer.source_guide_code = code
        db.add(FunnelEvent(stage="dm", platform=msg.platform, customer_id=customer.id,
                           guide_code=code))
        db.commit()
        logger.info("暗号命中 customer_id=%s code=%s", customer.id, code)
        return  # 一条消息只归因一个暗号（优先级最高的先命中）


# ============ 内容矩阵：队列任务处理器 ============

async def handle_content_generate(payload: dict[str, Any]):
    """队列任务：AI 生成平台版本（创作图：生成→合规→改写循环）。"""
    from .creator.graph import run_creator_graph  # 延迟导入避免循环依赖
    version = await run_creator_graph(
        item_id=int(payload["item_id"]),
        platform=payload["platform"],
        content_type=payload.get("content_type", "note"),
        variant_index=int(payload.get("variant_index", 0)),  # 一稿多版（Phase 7）
    )
    try:
        from .api.ws import manager
        await manager.broadcast("content_version_updated", {
            "item_id": int(payload["item_id"]),
            "version_id": version.id if version else 0,
            "platform": payload["platform"],
            "ok": version is not None,
        })
    except Exception:  # noqa: BLE001
        logger.exception("创作完成广播失败")


async def handle_compliance_check(payload: dict[str, Any]):
    """队列任务：版本合规手动复检。"""
    from .creator.compliance import check_version  # 延迟导入避免循环依赖
    version = await check_version(int(payload["version_id"]))
    try:
        from .api.ws import manager
        await manager.broadcast("content_version_updated", {
            "item_id": version.content_item_id if version else 0,
            "version_id": int(payload["version_id"]),
            "platform": version.platform if version else "",
            "ok": version is not None,
        })
    except Exception:  # noqa: BLE001
        logger.exception("复检完成广播失败")


# 注册队列处理器
async def _persist_send_failure(db, conversation: Conversation, *, content: str, sender_type: str,
                                sender_id: int | None, msg_type: str, extra: dict | None,
                                error: str) -> Message:
    """平台没发出去：记下正文和 send_failed，稍后再发这一条，不重跑整段接待。"""
    payload = _extra_at_ai_save(sender_type, extra)
    payload["send_failed"] = True
    payload["send_error"] = (error or "")[:200]
    message = _save_message(
        db, conversation.id, sender_type=sender_type, content=content,
        msg_type=msg_type, sender_id=sender_id, extra=payload,
    )
    conversation.last_message_at = message.created_at
    db.commit()
    db.refresh(message)
    await _broadcast_message(conversation, message)
    monitor.record("outbound_send_failed", f"conversation={conversation.id} {(error or '')[:150]}")
    enqueue("outbound_retry", {"message_id": message.id, "attempt": 1}, delay_seconds=30)
    return message


async def save_unsent_outbound(conversation_id: int, content: str, *, extra: dict | None = None) -> Message | None:
    """拟人多条里还没发出的后几条，记成未送达，交给 outbound_retry。"""
    text = (content or "").strip()
    if not text:
        return None
    db = SessionLocal()
    try:
        conversation = db.get(Conversation, conversation_id)
        if conversation is None:
            return None
        return await _persist_send_failure(
            db, conversation, content=text, sender_type="ai", sender_id=None,
            msg_type="text", extra=extra, error="前一条发送失败，本条未发出",
        )
    finally:
        db.close()


async def retry_outbound_send(payload: dict[str, Any]):
    """只重发标记了 send_failed 的那一条。"""
    message_id = int(payload.get("message_id") or 0)
    attempt = int(payload.get("attempt") or 1)
    db = SessionLocal()
    try:
        message = db.get(Message, message_id)
        if message is None or not (message.extra or {}).get("send_failed"):
            return
        conversation = db.get(Conversation, message.conversation_id)
        customer = db.get(Customer, conversation.customer_id) if conversation is not None else None
        if conversation is None or customer is None or conversation.status != "open":
            return
        adapter = get_send_adapter(conversation.platform)
        try:
            platform_msg_id = await adapter.send(
                conversation.platform_conversation_id, customer.platform_user_id, message.content,
                msg_type=message.msg_type or "text",
                media_id=str((message.extra or {}).get("media_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            fallback = get_rpa_fallback_adapter(conversation.platform)
            platform_msg_id = ""
            if fallback is not None:
                try:
                    platform_msg_id = await fallback.send(
                        conversation.platform_conversation_id, customer.platform_user_id, message.content,
                        msg_type=message.msg_type or "text",
                        media_id=str((message.extra or {}).get("media_id") or ""),
                    )
                except Exception as fallback_exc:  # noqa: BLE001
                    exc = fallback_exc
                    platform_msg_id = ""
            if not platform_msg_id:
                if attempt >= 3:
                    monitor.record("outbound_send_failed", f"retry_exhausted message={message_id} {str(exc)[:120]}")
                    return
                enqueue(
                    "outbound_retry",
                    {"message_id": message_id, "attempt": attempt + 1},
                    delay_seconds=30 * attempt,
                )
                return
        extra = dict(message.extra or {})
        extra.pop("send_failed", None)
        extra["send_retried"] = True
        message.extra = extra
        message.platform_msg_id = platform_msg_id
        db.commit()
        await _broadcast_message(conversation, message)
    finally:
        db.close()


async def sweep_lead_followups() -> int:
    """顾客沉默超过 5 分钟，用跟进话术再留资。整段会话最多两次。"""
    from .agent import lead_flow

    cutoff = datetime.utcnow() - timedelta(minutes=5)
    db = SessionLocal()
    due: list[int] = []
    try:
        conversations = (
            db.query(Conversation)
            .filter(
                Conversation.mode == "ai",
                Conversation.status == "open",
                Conversation.lead_followups < 2,
                Conversation.last_message_at < cutoff,
            )
            .all()
        )
        for conversation in conversations:
            customer = db.get(Customer, conversation.customer_id)
            if customer is not None and ((customer.lead_phone or "").strip() or (customer.lead_wechat or "").strip()):
                continue
            last = (
                db.query(Message)
                .filter(Message.conversation_id == conversation.id, Message.is_internal.is_(False))
                .order_by(Message.id.desc())
                .first()
            )
            if last is None or last.sender_type != "ai":
                continue
            if conversation_is_laike(db, conversation):
                continue
            due.append(conversation.id)
    finally:
        db.close()

    sent = 0
    script = lead_flow.library_script("跟进话术") or lead_flow.library_script("留资话术") or lead_flow.ASK_FULL
    for conversation_id in due:
        message = await send_outbound(
            conversation_id,
            script,
            sender_type="ai",
            extra={"lead_followup": True},
        )
        if message is None:
            continue
        mark = SessionLocal()
        try:
            conversation = mark.get(Conversation, conversation_id)
            if conversation is None:
                continue
            conversation.lead_followups = (conversation.lead_followups or 0) + 1
            mark.commit()
            sent += 1
        finally:
            mark.close()
    return sent


register_handler("inbound_message", handle_inbound)
register_handler("outbound_retry", retry_outbound_send)
register_handler("content_generate", handle_content_generate)
register_handler("compliance_check", handle_compliance_check)
