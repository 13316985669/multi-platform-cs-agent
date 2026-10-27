"""消息接口：列表 / bad case 标记 / 工作台媒体上传与读取。"""
import os

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..core.modules import agent_can
from ..core.security import decode_access_token
from ..database import get_db
from ..models import Agent, Conversation, Message, RpaMedia, RpaOutbox
from ..schemas import BadCaseMark, MessageOut
from ..services import record_missed_question
from .deps import get_current_agent
from .rpa import _save_media

router = APIRouter(prefix="/api/messages", tags=["messages"])

# 工作台媒体接口（/api/media）：<img>/<audio> 标签无法带 Authorization 头，读取走 ?token= 鉴权
media_router = APIRouter(tags=["media"])


@router.get("/conversation/{conversation_id}", response_model=list[MessageOut])
def list_messages(conversation_id: int, agent: Agent = Depends(get_current_agent),
                  db: Session = Depends(get_db)):
    messages = (
        db.query(Message)
        .filter(Message.conversation_id == conversation_id)
        .order_by(Message.id)
        .limit(500)
        .all()
    )
    # RPA 通道送达状态回填：出站消息的 platform_msg_id 形如 "rpa_{outbox_id}"
    outbox_ids = [
        int(m.platform_msg_id[4:]) for m in messages
        if (m.platform_msg_id or "").startswith("rpa_") and m.platform_msg_id[4:].isdigit()
    ]
    status_map: dict[int, str] = {}
    if outbox_ids:
        status_map = {
            r.id: r.status
            for r in db.query(RpaOutbox.id, RpaOutbox.status)
            .filter(RpaOutbox.id.in_(outbox_ids)).all()
        }
    out = []
    for m in messages:
        mo = MessageOut.model_validate(m)
        pmid = m.platform_msg_id or ""
        if pmid.startswith("rpa_") and pmid[4:].isdigit():
            st = status_map.get(int(pmid[4:]))
            if st:
                mo.extra = {**(mo.extra or {}), "outbox_status": st}
        out.append(mo)
    return out


@router.post("/{message_id}/badcase")
def mark_bad_case(message_id: int, req: BadCaseMark,
                  agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """客服标记 AI 回复「回答不佳」（可附备注），沉淀 bad case 供 evals 回流。"""
    msg = db.get(Message, message_id)
    if msg is None:
        raise HTTPException(404, "消息不存在")
    msg.bad_case = req.bad_case
    extra = dict(msg.extra or {})
    if req.bad_case and req.note:
        extra["badcase_note"] = req.note[:200]
    else:
        extra.pop("badcase_note", None)
    msg.extra = extra
    if req.bad_case:
        _record_bad_case_question(db, msg)
    db.commit()
    return {"ok": True}


def _record_bad_case_question(db: Session, msg: Message) -> None:
    """答得差时，把这条回复之前的用户原话放进待补问题。已有 pending 只加次数。"""
    previous = (
        db.query(Message)
        .filter(
            Message.conversation_id == msg.conversation_id,
            Message.sender_type == "user",
            Message.id < msg.id,
        )
        .order_by(Message.id.desc())
        .first()
    )
    if previous is None or not (previous.content or "").strip():
        return
    conversation = db.get(Conversation, msg.conversation_id)
    record_missed_question(
        previous.content.strip(),
        conversation.platform if conversation else "",
        msg.conversation_id,
    )


@media_router.post("/api/media")
def upload_media(file: UploadFile, kind: str = "image",
                 agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """客服上传图片（发图给用户用）。复用 RPA 媒体存储。"""
    row = _save_media(db, file, kind)
    return {"media_id": row.id}


@media_router.get("/api/media/{media_id}")
def get_media(media_id: str, token: str = "", db: Session = Depends(get_db)):
    """媒体读取。<img>/<audio> 标签无法带 Authorization 头，JWT 走 ?token= 查询参数。

    注意：token 会进访问日志/浏览器历史，生产环境建议短时效 token + HTTPS。
    """
    payload = decode_access_token(token) if token else None
    if payload is None or "sub" not in payload:
        raise HTTPException(401, "未授权")
    try:
        agent = db.get(Agent, int(payload["sub"]))
    except (TypeError, ValueError):
        agent = None
    if agent is None:
        raise HTTPException(401, "未授权")
    if not agent_can(agent, "workbench"):
        raise HTTPException(403, "无权访问该模块")
    row = db.get(RpaMedia, media_id)
    if row is None or not os.path.exists(row.path):
        raise HTTPException(404, "媒体不存在")
    return FileResponse(row.path, media_type=row.mime)
