"""会话接口：列表/详情/模式切换/分配/结束/标签。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..core import audit
from ..database import get_db
from ..models import Agent, Conversation, Customer, HandoffEvent, Message
from ..schemas import (AgentMessageSend, AssignRequest, ConversationModeUpdate,
                       ConversationOut, CustomerOut, MessageOut, TagUpdate)
from ..services import send_outbound, _save_message
from ..api.ws import manager
from .deps import get_current_agent

router = APIRouter(prefix="/api/conversations", tags=["conversations"])

FIRST_RESPONSE_OVERDUE_SECONDS = 20


def _agents_map(db: Session) -> dict[int, Agent]:
    return {a.id: a for a in db.query(Agent).all()}


def _to_out(db: Session, conv: Conversation, agents: dict[int, Agent] | None = None,
            viewer_id: int | None = None) -> ConversationOut:
    agents = agents if agents is not None else _agents_map(db)
    last = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.is_internal == False)  # noqa: E712
        .order_by(Message.id.desc())
        .first()
    )
    last_user = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.sender_type == "user")
        .order_by(Message.id.desc())
        .first()
    )
    last_human = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.sender_type == "agent",
                Message.is_internal == False)  # noqa: E712
        .order_by(Message.id.desc())
        .first()
    )
    # 等待计时认 AI 和人工回复；转入标记仍只看人工，避免 AI 补答把「转入」弄丢。
    last_reply = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id,
                Message.sender_type.in_(("ai", "agent")),
                Message.is_internal == False)  # noqa: E712
        .order_by(Message.id.desc())
        .first()
    )
    now = datetime.utcnow()
    wait_seconds = 0
    awaiting_first = False
    overdue = False
    if conv.status == "open" and conv.mode in ("pending", "human") and last_user:
        no_reply_after = last_reply is None or last_reply.id < last_user.id
        if no_reply_after:
            awaiting_first = True
            wait_seconds = max(0, int((now - last_user.created_at).total_seconds()))
            overdue = wait_seconds >= FIRST_RESPONSE_OVERDUE_SECONDS
    if conv.mode == "pending":
        ho = (
            db.query(HandoffEvent)
            .filter(HandoffEvent.conversation_id == conv.id)
            .order_by(HandoffEvent.id.desc())
            .first()
        )
        if ho:
            wait_seconds = max(0, int((now - ho.created_at).total_seconds()))

    transferred_in = False
    transfer_msg = (
        db.query(Message)
        .filter(Message.conversation_id == conv.id, Message.sender_type == "system")
        .order_by(Message.id.desc())
        .first()
    )
    if transfer_msg and (transfer_msg.extra or {}).get("event") == "transfer":
        if (transfer_msg.extra or {}).get("to_agent_id") == conv.assignee_id:
            if last_human is None or last_human.id < transfer_msg.id:
                transferred_in = True
                if viewer_id is not None and conv.assignee_id != viewer_id:
                    transferred_in = False

    assignee = agents.get(conv.assignee_id) if conv.assignee_id else None
    sibling_count = (
        db.query(func.count(Conversation.id))
        .filter(Conversation.customer_id == conv.customer_id)
        .scalar()
        or 0
    )
    return ConversationOut(
        id=conv.id,
        customer=CustomerOut.model_validate(conv.customer),
        platform=conv.platform,
        mode=conv.mode,
        status=conv.status,
        assignee_id=conv.assignee_id,
        assignee_name=assignee.display_name if assignee else "",
        unread_count=conv.unread_count,
        summary=conv.summary or "",
        last_message_at=conv.last_message_at,
        created_at=conv.created_at,
        last_message=MessageOut.model_validate(last) if last else None,
        wait_seconds=wait_seconds,
        awaiting_first_response=awaiting_first,
        overdue=overdue,
        transferred_in=transferred_in,
        is_returning=sibling_count > 1,
    )


@router.get("", response_model=list[ConversationOut])
def list_conversations(
    tab: str = Query("mine", description="mine(我的) | pending(排队) | active(进行中) | closed(已结束)"),
    platform: str = Query("", description="douyin | xiaohongshu | mock，空=全部"),
    agent: Agent = Depends(get_current_agent),
    db: Session = Depends(get_db),
):
    q = db.query(Conversation).join(Customer)
    if platform:
        q = q.filter(Conversation.platform == platform)
    if tab == "closed":
        q = q.filter(Conversation.status == "closed")
    elif tab == "pending":
        q = q.filter(Conversation.status == "open", Conversation.mode == "pending")
    elif tab == "mine":
        q = q.filter(Conversation.status == "open", Conversation.mode == "human",
                     Conversation.assignee_id == agent.id)
    else:  # active：AI + 人工（含同事接待，便于旁观）
        q = q.filter(Conversation.status == "open", Conversation.mode.in_(["ai", "human"]))
    convs = q.order_by(Conversation.last_message_at.desc()).limit(100).all()
    agents = _agents_map(db)
    return [_to_out(db, c, agents, viewer_id=agent.id) for c in convs]


@router.get("/counts")
def conversation_counts(agent: Agent = Depends(get_current_agent),
                        db: Session = Depends(get_db)):
    """各 tab 会话计数（前端徽标常驻显示，无需切换 tab）。"""
    mine = db.query(func.count(Conversation.id)).filter(
        Conversation.status == "open", Conversation.mode == "human",
        Conversation.assignee_id == agent.id).scalar() or 0
    active = db.query(func.count(Conversation.id)).filter(
        Conversation.status == "open", Conversation.mode.in_(["ai", "human"])).scalar() or 0
    pending = db.query(func.count(Conversation.id)).filter(
        Conversation.status == "open", Conversation.mode == "pending").scalar() or 0
    closed = db.query(func.count(Conversation.id)).filter(
        Conversation.status == "closed").scalar() or 0
    return {"mine": mine, "active": active, "pending": pending, "closed": closed}


@router.get("/customer/{customer_id}", response_model=list[ConversationOut])
def list_by_customer(customer_id: int, agent: Agent = Depends(get_current_agent),
                     db: Session = Depends(get_db)):
    """同客户历史会话（右侧顾客栏）。"""
    convs = (
        db.query(Conversation)
        .filter(Conversation.customer_id == customer_id)
        .order_by(Conversation.last_message_at.desc())
        .limit(20)
        .all()
    )
    agents = _agents_map(db)
    return [_to_out(db, c, agents, viewer_id=agent.id) for c in convs]


@router.get("/{conversation_id}", response_model=ConversationOut)
def get_conversation(conversation_id: int, agent: Agent = Depends(get_current_agent),
                     db: Session = Depends(get_db)):
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    conv.unread_count = 0
    db.commit()
    return _to_out(db, conv, viewer_id=agent.id)


@router.post("/{conversation_id}/mode")
async def set_mode(conversation_id: int, req: ConversationModeUpdate,
                   agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    conv.mode = req.mode
    if req.mode == "human" and conv.assignee_id is None:
        conv.assignee_id = agent.id
    db.commit()
    action = {"human": "takeover", "ai": "release"}.get(req.mode, f"mode:{req.mode}")
    audit.log(db, agent, action, target=f"conversation:{conversation_id}")
    await manager.broadcast("conversation_update", {"conversation_id": conversation_id, "mode": req.mode})
    return {"ok": True}


@router.post("/{conversation_id}/assign")
async def assign(conversation_id: int, req: AssignRequest, agent: Agent = Depends(get_current_agent),
                 db: Session = Depends(get_db)):
    conv = db.get(Conversation, conversation_id)
    target = db.get(Agent, req.agent_id)
    if conv is None or target is None:
        raise HTTPException(404, "会话或客服不存在")
    prev_id = conv.assignee_id
    conv.assignee_id = target.id
    if conv.mode == "pending":
        conv.mode = "human"
    transferred = prev_id is not None and prev_id != target.id
    if transferred:
        prev = db.get(Agent, prev_id)
        from_name = prev.display_name if prev else "同事"
        content = f"会话已从 {from_name} 转接给 {target.display_name}"
        extra = {"event": "transfer", "from_agent_id": prev_id, "to_agent_id": target.id}
    else:
        content = f"{target.display_name} 已接单"
        extra = {"event": "claim", "to_agent_id": target.id}
    sys_msg = _save_message(db, conversation_id, sender_type="system", msg_type="system",
                            content=content, extra=extra)
    note_msg = None
    if req.note.strip():
        note_msg = _save_message(db, conversation_id, sender_type="agent", sender_id=agent.id,
                                 content=req.note.strip(), is_internal=True)
    conv.last_message_at = datetime.utcnow()
    db.commit()
    audit.log(db, agent, "transfer" if transferred else "claim",
              target=f"conversation:{conversation_id}", detail=target.display_name)
    await manager.broadcast("conversation_update", {
        "conversation_id": conversation_id, "assignee_id": target.id, "mode": conv.mode,
    })
    await manager.broadcast("new_message", {
        "conversation_id": conversation_id,
        "message": {
            "id": sys_msg.id, "conversation_id": conversation_id, "sender_type": "system",
            "sender_id": None, "msg_type": "system", "content": sys_msg.content,
            "extra": extra, "is_internal": False,
            "created_at": sys_msg.created_at.isoformat(),
        },
    })
    if note_msg:
        await manager.broadcast("new_message", {
            "conversation_id": conversation_id,
            "message": {
                "id": note_msg.id, "conversation_id": conversation_id, "sender_type": "agent",
                "sender_id": agent.id, "msg_type": "text", "content": note_msg.content,
                "extra": {}, "is_internal": True,
                "created_at": note_msg.created_at.isoformat(),
            },
        })
    return {"ok": True}


@router.post("/{conversation_id}/close")
async def close(conversation_id: int, agent: Agent = Depends(get_current_agent),
                db: Session = Depends(get_db)):
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    conv.status = "closed"
    conv.closed_at = datetime.utcnow()
    _save_message(db, conversation_id, sender_type="system", msg_type="system",
                  content="会话已结束")
    db.commit()
    audit.log(db, agent, "close", target=f"conversation:{conversation_id}")
    await manager.broadcast("conversation_update", {"conversation_id": conversation_id, "status": "closed"})
    return {"ok": True}


@router.post("/{conversation_id}/tags")
def update_tags(conversation_id: int, req: TagUpdate, agent: Agent = Depends(get_current_agent),
                db: Session = Depends(get_db)):
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    conv.customer.tags = req.tags
    db.commit()
    return {"ok": True, "tags": conv.customer.tags}


@router.post("/{conversation_id}/reply", response_model=MessageOut | None)
async def agent_reply(conversation_id: int, req: AgentMessageSend,
                      agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """人工客服发送消息（is_internal=true 时为内部备注，不发给用户）。"""
    conv = db.get(Conversation, conversation_id)
    if conv is None:
        raise HTTPException(404, "会话不存在")
    if req.is_internal:
        msg = _save_message(db, conversation_id, sender_type="agent", sender_id=agent.id,
                            content=req.content, is_internal=True)
        conv.last_message_at = msg.created_at
        db.commit()
        db.refresh(msg)
        await manager.broadcast("new_message", {
            "conversation_id": conversation_id,
            "message": {"id": msg.id, "conversation_id": conversation_id, "sender_type": "agent",
                        "sender_id": agent.id, "msg_type": "text", "content": msg.content,
                        "extra": {}, "is_internal": True,
                        "created_at": msg.created_at.isoformat()},
        })
        return MessageOut.model_validate(msg)
    msg = await send_outbound(conversation_id, req.content, sender_type="agent",
                              sender_id=agent.id, msg_type=req.msg_type, media_id=req.media_id)
    if msg is not None and req.msg_type == "text":
        from ..services import note_agent_correction
        note_agent_correction(conversation_id, req.content)
    if msg is None:
        raise HTTPException(429, "触发平台频控，已自动转人工排队")
    # send_outbound 用的是另一个已关闭的会话，不能 refresh 那个对象
    msg_id = msg.id
    db.rollback()
    saved = db.get(Message, msg_id)
    if saved is None:
        raise HTTPException(500, "消息已发出，但读取回执失败")
    return MessageOut.model_validate(saved)
