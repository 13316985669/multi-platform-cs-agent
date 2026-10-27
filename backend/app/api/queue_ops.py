"""队列运维：失败任务可见性与手动重试（仅管理员）。

失败任务此前只能查库发现（Phase 8 盘点 B8）。本模块提供：
- GET  /api/queue/failed      失败任务列表 + 队列深度计数
- POST /api/queue/{id}/retry  手动重试（重置 retries/退避，重回 pending）
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Agent, QueueTask
from .deps import require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/queue", tags=["queue"])


@router.get("/failed")
def list_failed_tasks(limit: int = 50, _: Agent = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """失败任务列表（重试用尽的最终失败）+ pending/failed 计数。"""
    rows = (db.query(QueueTask).filter(QueueTask.status == "failed")
            .order_by(QueueTask.updated_at.desc()).limit(min(limit, 200)).all())
    return {
        "pending": db.query(QueueTask).filter(QueueTask.status == "pending").count(),
        "failed": db.query(QueueTask).filter(QueueTask.status == "failed").count(),
        "items": [{
            "id": t.id,
            "task_type": t.task_type,
            "error": t.error or "",
            "retries": t.retries,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        } for t in rows],
    }


@router.post("/{task_id}/retry")
def retry_failed_task(task_id: int, _: Agent = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """手动重试：重置 retries 与退避延迟，重回 pending（worker 轮询自动拾取）。"""
    task = db.get(QueueTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status != "failed":
        raise HTTPException(400, f"仅失败任务可重试（当前状态 {task.status}）")
    task.status = "pending"
    task.retries = 0
    task.not_before = None
    task.error = ""
    db.commit()
    logger.info("队列任务手动重试 task_id=%s type=%s", task.id, task.task_type)
    return {"ok": True}
