"""接待模式路由 + 会话分配：回头客优先，再按「在线且未满上限、接待数最少」分配。"""
import logging

from sqlalchemy import func

from ..models import Agent, Conversation

logger = logging.getLogger(__name__)


def _workload(db) -> dict[int, int]:
    return dict(
        db.query(Conversation.assignee_id, func.count(Conversation.id))
        .filter(Conversation.status == "open", Conversation.mode.in_(["human", "pending"]),
                Conversation.assignee_id.isnot(None))
        .group_by(Conversation.assignee_id)
        .all()
    )


def _under_capacity(agent: Agent, load: int) -> bool:
    cap = agent.max_concurrent if agent.max_concurrent is not None else 20
    if cap == 0:
        return False  # 不参与自动分配，仍可被转接
    return load < cap


def auto_assign(db, conversation_id: int) -> int | None:
    """把 pending 会话分配给回头客跟进人，否则分配给当前最空闲且未满上限的在线客服。"""
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.assignee_id:
        return conversation.assignee_id if conversation else None

    agents = db.query(Agent).filter(Agent.status == "active").all()
    if not agents:
        logger.info("无在线客服，会话 %s 留在排队队列", conversation_id)
        return None

    workload = _workload(db)

    # 回头客：上一通已关闭会话的接待人优先（在线且未满上限）
    prev = (
        db.query(Conversation)
        .filter(Conversation.customer_id == conversation.customer_id,
                Conversation.id != conversation.id,
                Conversation.status == "closed",
                Conversation.assignee_id.isnot(None))
        .order_by(Conversation.last_message_at.desc())
        .first()
    )
    if prev and prev.assignee_id:
        last_agent = next((a for a in agents if a.id == prev.assignee_id), None)
        if last_agent and _under_capacity(last_agent, workload.get(last_agent.id, 0)):
            conversation.assignee_id = last_agent.id
            conversation.mode = "human"
            logger.info("会话 %s 回头客分配给客服 %s", conversation_id, last_agent.username)
            return last_agent.id

    eligible = [a for a in agents if _under_capacity(a, workload.get(a.id, 0))]
    if not eligible:
        logger.info("在线客服均已满接待上限，会话 %s 留在排队队列", conversation_id)
        return None

    best = min(eligible, key=lambda a: workload.get(a.id, 0))
    conversation.assignee_id = best.id
    conversation.mode = "human"
    logger.info("会话 %s 已分配给客服 %s（当前接待 %d 个）",
                conversation_id, best.username, workload.get(best.id, 0))
    return best.id
