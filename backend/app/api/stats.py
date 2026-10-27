"""数据看板：AI 拦截率、转人工率、平均首次响应时长、3 分钟回复率、会话趋势、token 成本。"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import (Agent, Conversation, FunnelEvent, HandoffEvent, LlmUsage,
                      Message, MissedQuestion, PostComment)
from ..schemas import StatsOverview
from .deps import get_current_agent

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/overview", response_model=StatsOverview)
def overview(agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    today_convs = db.query(func.count(Conversation.id)).filter(Conversation.created_at >= today_start).scalar() or 0
    today_msgs = db.query(func.count(Message.id)).filter(Message.created_at >= today_start).scalar() or 0

    total_convs = db.query(func.count(Conversation.id)).scalar() or 0
    handoff_total = db.query(func.count(func.distinct(HandoffEvent.conversation_id))).scalar() or 0
    handoff_rate = handoff_total / total_convs if total_convs else 0.0

    ai_msgs = db.query(func.count(Message.id)).filter(Message.sender_type == "ai").scalar() or 0
    user_msgs = db.query(func.count(Message.id)).filter(Message.sender_type == "user").scalar() or 0
    ai_reply_rate = ai_msgs / user_msgs if user_msgs else 0.0

    active_agents = db.query(func.count(Agent.id)).filter(Agent.status == "active").scalar() or 0
    pending = db.query(func.count(Conversation.id)).filter(
        Conversation.status == "open", Conversation.mode == "pending").scalar() or 0
    total_unread = db.query(func.coalesce(func.sum(Conversation.unread_count), 0)).filter(
        Conversation.status == "open").scalar() or 0
    platform_breakdown = {
        p: c for p, c in db.query(Conversation.platform, func.count())
        .filter(Conversation.created_at >= today_start)
        .group_by(Conversation.platform).all()
    }

    today_comments = db.query(func.count(PostComment.id)).filter(
        PostComment.created_at >= today_start).scalar() or 0
    today_leads = db.query(func.count(FunnelEvent.id)).filter(
        FunnelEvent.stage == "lead", FunnelEvent.created_at >= today_start).scalar() or 0
    today_wecom_adds = db.query(func.count(FunnelEvent.id)).filter(
        FunnelEvent.stage == "wecom", FunnelEvent.created_at >= today_start).scalar() or 0

    today_prompt, today_completion = _usage_sum(db, today_start)
    since_7d = today_start - timedelta(days=6)
    handoff_reasons = {
        reason or "": count
        for reason, count in db.query(HandoffEvent.reason, func.count())
        .filter(HandoffEvent.created_at >= since_7d)
        .group_by(HandoffEvent.reason)
        .all()
    }
    bad_cases = db.query(func.count(Message.id)).filter(
        Message.bad_case.is_(True), Message.created_at >= since_7d,
    ).scalar() or 0
    recent_extra = db.query(Message.sender_type, Message.extra).filter(Message.created_at >= since_7d).all()
    retrieval_total = 0
    retrieval_miss = 0
    send_failed = 0
    for sender_type, extra in recent_extra:
        payload = extra or {}
        if sender_type == "user" and isinstance(payload.get("retrieval"), dict):
            retrieval_total += 1
            if payload["retrieval"].get("reason") not in (None, "passed"):
                retrieval_miss += 1
        if payload.get("send_failed") or payload.get("send_retried"):
            send_failed += 1
    miss_rate = retrieval_miss / retrieval_total if retrieval_total else 0.0
    return StatsOverview(
        today_conversations=today_convs,
        today_messages=today_msgs,
        ai_reply_rate=round(ai_reply_rate, 3),
        handoff_rate=round(handoff_rate, 3),
        avg_first_response_seconds=_avg_first_response(db),
        reply_within_3min_rate=_reply_within_3min_rate(db, today_start),
        today_tokens=today_prompt + today_completion,
        today_token_cost_yuan=_estimate_cost(today_prompt, today_completion),
        active_agents=active_agents,
        pending_conversations=pending,
        total_unread=int(total_unread),
        platform_breakdown=platform_breakdown,
        today_comments=today_comments,
        today_leads=today_leads,
        today_wecom_adds=today_wecom_adds,
        handoff_reasons_7d=handoff_reasons,
        retrieval_miss_rate_7d=round(miss_rate, 3),
        bad_case_count_7d=int(bad_cases),
        send_failed_count_7d=send_failed,
    )


@router.get("/token-usage")
def token_usage(days: int = 7, agent: Agent = Depends(get_current_agent),
                db: Session = Depends(get_db)):
    """近 N 天 token 用量与估算成本（按 LLM_PRICE_INPUT/OUTPUT_PER_1K 单价，单位：元）。"""
    days = max(1, min(days, 90))
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    for i in range(days - 1, -1, -1):
        day = today_start - timedelta(days=i)
        prompt, completion = _usage_sum(db, day, day + timedelta(days=1))
        result.append({
            "date": day.strftime("%m-%d"),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            "cost_yuan": _estimate_cost(prompt, completion),
        })
    return result


def _usage_sum(db: Session, start: datetime, end: datetime | None = None) -> tuple[int, int]:
    """统计时间窗内 prompt/completion token 总量。"""
    q = db.query(
        func.coalesce(func.sum(LlmUsage.prompt_tokens), 0),
        func.coalesce(func.sum(LlmUsage.completion_tokens), 0),
    ).filter(LlmUsage.created_at >= start)
    if end is not None:
        q = q.filter(LlmUsage.created_at < end)
    prompt, completion = q.one()
    return int(prompt), int(completion)


def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    """按配置单价估算成本（元）；单价为 0 时返回 0（未配置）。"""
    cost = (prompt_tokens / 1000 * settings.llm_price_input_per_1k
            + completion_tokens / 1000 * settings.llm_price_output_per_1k)
    return round(cost, 4)


@router.get("/trend")
def trend(days: int = 7, agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """近 N 天会话量趋势。"""
    result = []
    for i in range(days - 1, -1, -1):
        day = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=i)
        next_day = day + timedelta(days=1)
        count = db.query(func.count(Conversation.id)).filter(
            Conversation.created_at >= day, Conversation.created_at < next_day).scalar() or 0
        result.append({"date": day.strftime("%m-%d"), "count": count})
    return result


@router.get("/missed-top")
def missed_top(agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """未命中问题 Top 榜。"""
    rows = (
        db.query(MissedQuestion)
        .filter(MissedQuestion.status == "pending")
        .order_by(MissedQuestion.count.desc())
        .limit(10)
        .all()
    )
    return [{"question": r.question, "count": r.count} for r in rows]


def _avg_first_response(db: Session) -> float:
    """平均首次响应时长（秒）：每个会话第一条用户消息到第一条 AI/客服回复。"""
    convs = db.query(Conversation.id).filter(Conversation.status == "open").limit(200).all()
    durations = []
    for (cid,) in convs:
        first_user = db.query(Message).filter(
            Message.conversation_id == cid, Message.sender_type == "user").order_by(Message.id).first()
        first_reply = db.query(Message).filter(
            Message.conversation_id == cid, Message.sender_type.in_(["ai", "agent"])
        ).order_by(Message.id).first()
        if first_user and first_reply and first_reply.created_at > first_user.created_at:
            durations.append((first_reply.created_at - first_user.created_at).total_seconds())
    return round(sum(durations) / len(durations), 1) if durations else 0.0


def _reply_within_3min_rate(db: Session, day_start: datetime) -> float:
    """三分钟回复率：今日会话中，首条用户消息到首次 AI/客服回复 ≤180s 的占比（对齐飞鸽考核）。"""
    convs = db.query(Conversation.id).filter(Conversation.created_at >= day_start).limit(500).all()
    total, within = 0, 0
    for (cid,) in convs:
        first_user = db.query(Message).filter(
            Message.conversation_id == cid, Message.sender_type == "user").order_by(Message.id).first()
        if first_user is None:
            continue
        first_reply = db.query(Message).filter(
            Message.conversation_id == cid, Message.sender_type.in_(["ai", "agent"]),
            Message.created_at > first_user.created_at,
        ).order_by(Message.id).first()
        if first_reply is None:
            continue
        total += 1
        if (first_reply.created_at - first_user.created_at).total_seconds() <= 180:
            within += 1
    return round(within / total, 3) if total else 0.0
