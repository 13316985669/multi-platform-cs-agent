"""工单接口：列表/详情/创建/状态流转/指派。工单可由 AI 工具自动创建，也可由客服手动创建。"""
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Agent, Conversation, Ticket
from ..schemas import AssignRequest, TicketCreate, TicketOut, TicketStatusUpdate
from .deps import get_current_agent

router = APIRouter(prefix="/api/tickets", tags=["tickets"])


def _ticket_out(db: Session, ticket: Ticket) -> TicketOut:
    out = TicketOut.model_validate(ticket)
    if ticket.assignee_id:
        agent = db.get(Agent, ticket.assignee_id)
        if agent:
            out.assignee_name = agent.display_name
    return out


@router.get("", response_model=dict)
def list_tickets(status: str = "", page: int = 1, page_size: int = 20,
                 agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """工单列表：状态筛选 + 分页，按更新时间倒序。"""
    query = db.query(Ticket)
    if status:
        query = query.filter(Ticket.status == status)
    total = query.count()
    rows = (
        query.order_by(Ticket.updated_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
        .all()
    )
    return {"total": total, "items": [_ticket_out(db, t) for t in rows]}


@router.get("/by-conversation/{conversation_id}", response_model=list[TicketOut])
def tickets_by_conversation(conversation_id: int, agent: Agent = Depends(get_current_agent),
                            db: Session = Depends(get_db)):
    """会话关联工单（客户面板展示用）。"""
    rows = (
        db.query(Ticket)
        .filter(Ticket.conversation_id == conversation_id)
        .order_by(Ticket.created_at.desc())
        .all()
    )
    return [_ticket_out(db, t) for t in rows]


@router.get("/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: int, agent: Agent = Depends(get_current_agent),
               db: Session = Depends(get_db)):
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "工单不存在")
    return _ticket_out(db, ticket)


@router.post("", response_model=TicketOut)
async def create_ticket(req: TicketCreate, agent: Agent = Depends(get_current_agent),
                        db: Session = Depends(get_db)):
    """客服手动创建工单。"""
    conversation = db.get(Conversation, req.conversation_id)
    if conversation is None:
        raise HTTPException(404, "会话不存在")
    ticket = Ticket(
        ticket_no=f"T{datetime.now():%Y%m%d}-{uuid.uuid4().hex[:6].upper()}",
        conversation_id=req.conversation_id,
        customer_id=conversation.customer_id,
        type=req.type, title=req.title[:120], content=req.content[:2000],
        status="open", assignee_id=agent.id,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    from .ws import manager
    await manager.broadcast("ticket_created", {
        "ticket_no": ticket.ticket_no, "conversation_id": ticket.conversation_id,
        "title": ticket.title,
    })
    return _ticket_out(db, ticket)


@router.post("/{ticket_id}/status", response_model=TicketOut)
def update_status(ticket_id: int, req: TicketStatusUpdate,
                  agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "工单不存在")
    ticket.status = req.status
    ticket.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ticket)
    return _ticket_out(db, ticket)


@router.post("/{ticket_id}/assign", response_model=TicketOut)
def assign_ticket(ticket_id: int, req: AssignRequest,
                  agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    ticket = db.get(Ticket, ticket_id)
    if ticket is None:
        raise HTTPException(404, "工单不存在")
    if db.get(Agent, req.agent_id) is None:
        raise HTTPException(404, "客服不存在")
    ticket.assignee_id = req.agent_id
    ticket.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(ticket)
    return _ticket_out(db, ticket)
