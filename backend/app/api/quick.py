"""快捷回复（口语化模板库）CRUD。agent_id 为空=团队；有值=仅本人。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Agent, QuickReply
from ..schemas import QuickReplyIn, QuickReplyOut
from .deps import get_current_agent

router = APIRouter(prefix="/api/quick-replies", tags=["quick-replies"])


@router.get("", response_model=list[QuickReplyOut])
def list_all(agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    rows = (
        db.query(QuickReply)
        .filter(or_(QuickReply.agent_id.is_(None), QuickReply.agent_id == agent.id))
        .order_by(QuickReply.id)
        .all()
    )
    return [QuickReplyOut.model_validate(q) for q in rows]


@router.post("", response_model=QuickReplyOut)
def create(req: QuickReplyIn, agent: Agent = Depends(get_current_agent),
           db: Session = Depends(get_db)):
    item = QuickReply(
        title=req.title,
        content=req.content,
        agent_id=agent.id if req.personal else None,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@router.put("/{item_id}", response_model=QuickReplyOut)
def update_item(item_id: int, req: QuickReplyIn, agent: Agent = Depends(get_current_agent),
                db: Session = Depends(get_db)):
    item = db.get(QuickReply, item_id)
    if item is None:
        raise HTTPException(404, "不存在")
    if item.agent_id is not None and item.agent_id != agent.id and agent.role != "admin":
        raise HTTPException(403, "只能修改自己的个人快捷回复")
    title = (req.title or "").strip()
    content = (req.content or "").strip()
    if not title or not content:
        raise HTTPException(400, "标题和内容不能为空")
    item.title = title
    item.content = content
    if req.personal:
        if item.agent_id is None:
            item.agent_id = agent.id
    else:
        item.agent_id = None
    db.commit()
    db.refresh(item)
    return item


@router.delete("/{item_id}")
def delete(item_id: int, agent: Agent = Depends(get_current_agent),
           db: Session = Depends(get_db)):
    item = db.get(QuickReply, item_id)
    if item is None:
        raise HTTPException(404, "不存在")
    if item.agent_id is not None and item.agent_id != agent.id and agent.role != "admin":
        raise HTTPException(403, "只能删除自己的个人快捷回复")
    db.delete(item)
    db.commit()
    return {"ok": True}
