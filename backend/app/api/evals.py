"""evals 回流：把在线标注的 badcase 导出为 evals/cases.json 格式。

另有 /drafts：从未命中和不佳起草评测 JSON，不写入 cases.json。

工作流：客服在工作台标记 badcase（可附备注）→ 管理员从此接口导出 →
人工审阅补全 expect（期望行为）→ 合入 backend/evals/cases.json → 跑 run_evals.py 验证修复。
"""
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Agent, Conversation, Message
from .deps import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/evals", tags=["evals"])


@router.get("/export")
def export_badcases(_: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    """导出全部 badcase 消息为 cases.json 草稿（expect 为占位，需人工审阅补全）。

    返回内容可直接另存为 backend/evals/cases.json 的增量（id 用消息 ID，合入时注意去重）。
    """
    rows = (
        db.query(Message)
        .filter(Message.bad_case.is_(True))
        .order_by(Message.id)
        .limit(500)
        .all()
    )
    cases = []
    for m in rows:
        # 触发该 AI 回复的用户消息（同会话内该消息之前最近一条用户消息）
        user_msg = (
            db.query(Message)
            .filter(Message.conversation_id == m.conversation_id,
                    Message.sender_type == "user", Message.id < m.id)
            .order_by(Message.id.desc())
            .first()
        )
        conv = db.get(Conversation, m.conversation_id)
        cases.append({
            "id": m.id,
            "name": f"badcase-{m.id}",
            "user": user_msg.content if user_msg else "",
            "knowledge": "",
            "expect": {"handoff": False},  # 占位：人工审阅时补全期望行为
            "review": {
                "conversation_id": m.conversation_id,
                "platform": conv.platform if conv else "",
                "bad_reply": m.content,
                "note": (m.extra or {}).get("badcase_note", ""),
                "marked_at": m.created_at.isoformat() if m.created_at else None,
            },
        })
    logger.info("导出 badcase %s 条", len(cases))
    return JSONResponse(cases)


@router.post("/drafts")
async def draft_cases(_: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    """起草评测案例。调用方自行下载，服务端不改 cases.json。"""
    from ..evals_draft import build_eval_drafts

    drafts = await build_eval_drafts(db)
    logger.info("生成评测草稿 %s 条", len(drafts))
    return JSONResponse(drafts)
