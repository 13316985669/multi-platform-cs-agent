"""运行时设置：AI 全局熔断开关。

开关立即生效，并写入 app_settings。重启后从数据库读回，没有记录时才用 .env。
用途：AI 失控（答非所问/违规输出）时一键全转人工止血。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..bindings import DOUYIN_DM_DRIVERS
from ..config import settings
from ..core import audit
from ..core.runtime_settings import ACCOUNT_TYPES, persist, persist_text
from ..database import get_db
from ..models import AccountBinding, Agent, AuditLog, MatrixAccount
from ..schemas import AutoSwitchesIn, AutoSwitchesOut
from .deps import get_current_agent, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])


class AiSwitchOut(BaseModel):
    enabled: bool


class AiSwitchIn(BaseModel):
    enabled: bool


@router.get("/ai-switch", response_model=AiSwitchOut)
def get_ai_switch(_: Agent = Depends(get_current_agent)):
    return AiSwitchOut(enabled=settings.ai_globally_enabled)


@router.put("/ai-switch", response_model=AiSwitchOut)
def set_ai_switch(req: AiSwitchIn, agent: Agent = Depends(require_admin),
                  db: Session = Depends(get_db)):
    settings.ai_globally_enabled = req.enabled
    persist(db, "ai_globally_enabled", req.enabled)
    logger.warning("AI 全局开关被 %s（@%s）切换为 %s",
                   agent.display_name, agent.username, "开启" if req.enabled else "关闭（全转人工）")
    audit.log(db, agent, "ai_switch", target="settings:ai_globally_enabled",
              detail="开启" if req.enabled else "关闭（全转人工）")
    return AiSwitchOut(enabled=settings.ai_globally_enabled)


@router.get("/auto-switches", response_model=AutoSwitchesOut)
def get_auto_switches(_: Agent = Depends(get_current_agent)):
    """自动化熔断开关：评论自动回复 / 自动发布（关闭后仅人工可操作）。"""
    return AutoSwitchesOut(
        comment_auto_reply_enabled=settings.comment_auto_reply_enabled,
        publish_auto_enabled=settings.publish_auto_enabled)


@router.put("/auto-switches", response_model=AutoSwitchesOut)
def set_auto_switches(req: AutoSwitchesIn, agent: Agent = Depends(require_admin),
                      db: Session = Depends(get_db)):
    changes = []
    if req.comment_auto_reply_enabled is not None:
        settings.comment_auto_reply_enabled = req.comment_auto_reply_enabled
        persist(db, "comment_auto_reply_enabled", req.comment_auto_reply_enabled)
        changes.append(f"评论自动回复={'开' if req.comment_auto_reply_enabled else '关'}")
    if req.publish_auto_enabled is not None:
        settings.publish_auto_enabled = req.publish_auto_enabled
        persist(db, "publish_auto_enabled", req.publish_auto_enabled)
        changes.append(f"自动发布={'开' if req.publish_auto_enabled else '关'}")
    if changes:
        logger.warning("自动化开关被 %s（@%s）切换: %s",
                       agent.display_name, agent.username, "，".join(changes))
        audit.log(db, agent, "auto_switches", target="settings:auto",
                  detail="，".join(changes))
    return AutoSwitchesOut(
        comment_auto_reply_enabled=settings.comment_auto_reply_enabled,
        publish_auto_enabled=settings.publish_auto_enabled)


class DouyinAccountTypeOut(BaseModel):
    account_type: str


class DouyinAccountTypeIn(BaseModel):
    account_type: str


_DM_DRIVER = DOUYIN_DM_DRIVERS


@router.get("/douyin-account-type", response_model=DouyinAccountTypeOut)
def get_douyin_account_type(_: Agent = Depends(get_current_agent)):
    return DouyinAccountTypeOut(account_type=settings.douyin_account_type)


@router.put("/douyin-account-type", response_model=DouyinAccountTypeOut)
def set_douyin_account_type(req: DouyinAccountTypeIn, agent: Agent = Depends(require_admin),
                            db: Session = Depends(get_db)):
    """全站抖音私信的默认形态：账号未单独指定时用这一项，并只改写那些未指定形态的私信绑定。"""
    account_type = (req.account_type or "").strip()
    if account_type not in ACCOUNT_TYPES:
        raise HTTPException(400, "账号类型只能是 feige、enterprise 或 laike")
    settings.douyin_account_type = account_type
    persist_text(db, "douyin_account_type", account_type)
    driver = _DM_DRIVER[account_type]
    rows = (
        db.query(AccountBinding)
        .join(MatrixAccount, AccountBinding.account_id == MatrixAccount.id)
        .filter(AccountBinding.duty == "dm",
                AccountBinding.driver.in_(tuple(_DM_DRIVER.values())),
                or_(MatrixAccount.douyin_account_type == "",
                    MatrixAccount.douyin_account_type.is_(None)))
        .all()
    )
    for row in rows:
        row.driver = driver
    db.commit()
    audit.log(db, agent, "douyin_account_type", target="settings:douyin_account_type",
              detail=f"{account_type}，已更新 {len(rows)} 条私信绑定")
    return DouyinAccountTypeOut(account_type=account_type)


# ============ 品牌语气（Phase 7：创作提示词注入品牌风格指南） ============

class BrandStyleOut(BaseModel):
    brand_style_guide: str


class BrandStyleIn(BaseModel):
    brand_style_guide: str


@router.get("/brand-style", response_model=BrandStyleOut)
def get_brand_style(_: Agent = Depends(get_current_agent)):
    return BrandStyleOut(brand_style_guide=settings.brand_style_guide)


@router.put("/brand-style", response_model=BrandStyleOut)
def set_brand_style(req: BrandStyleIn, agent: Agent = Depends(require_admin),
                    db: Session = Depends(get_db)):
    """品牌语气设置：进程内运行时生效（与 auto-switches 同模式），重启回退 env。"""
    settings.brand_style_guide = req.brand_style_guide.strip()[:2000]
    logger.info("品牌语气被 %s（@%s）更新（%d 字）",
                agent.display_name, agent.username, len(settings.brand_style_guide))
    audit.log(db, agent, "brand_style_update", target="settings:brand_style_guide",
              detail=settings.brand_style_guide[:100])
    return BrandStyleOut(brand_style_guide=settings.brand_style_guide)


@router.get("/audit-logs")
def list_audit_logs(limit: int = 100, _: Agent = Depends(require_admin),
                    db: Session = Depends(get_db)):
    """审计日志只读列表（仅管理员可见，最新在前）。"""
    limit = max(1, min(limit, 500))
    rows = db.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
    return {"items": [
        {
            "id": r.id,
            "agent_name": r.agent_name,
            "action": r.action,
            "target": r.target,
            "detail": r.detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows
    ]}
