"""操作审计：敏感操作留痕（谁、何时、对什么、做了什么）。

写失败只记日志不影响主流程。agent 传 None 表示系统操作。
"""
import logging

from sqlalchemy.orm import Session

from ..models import Agent, AuditLog

logger = logging.getLogger(__name__)


def log(db: Session, agent: Agent | None, action: str, target: str = "", detail: str = ""):
    try:
        db.add(AuditLog(
            agent_id=agent.id if agent else 0,
            agent_name=agent.display_name if agent else "系统",
            action=action,
            target=str(target)[:128],
            detail=str(detail)[:500],
        ))
        db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("审计日志写入失败 action=%s", action, exc_info=True)
