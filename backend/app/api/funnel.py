"""引流漏斗看板：comment → dm → lead → wecom 四级聚合 + 明细 + 企微活码管理。"""
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Agent, FunnelEvent, MatrixAccount, WecomChannelCode
from ..schemas import FunnelEventOut, FunnelOverview
from .deps import get_current_agent, require_admin

router = APIRouter(prefix="/api/funnel", tags=["funnel"])

STAGES = ("comment", "dm", "lead", "wecom")


@router.get("/overview", response_model=FunnelOverview)
def overview(days: int = 7, _: Agent = Depends(get_current_agent),
             db: Session = Depends(get_db)):
    """近 N 天漏斗聚合：四级计数 + 转化率 + 平台/账号下钻。"""
    days = max(1, min(days, 90))
    since = datetime.utcnow() - timedelta(days=days)

    rows = db.query(FunnelEvent.stage, func.count()).filter(
        FunnelEvent.created_at >= since).group_by(FunnelEvent.stage).all()
    stages = {s: 0 for s in STAGES}
    for stage, count in rows:
        if stage in stages:
            stages[stage] = count

    def _rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 3) if denominator else 0.0

    conversion = {
        "dm_rate": _rate(stages["dm"], stages["comment"]),
        "lead_rate": _rate(stages["lead"], stages["dm"]),
        "wecom_rate": _rate(stages["wecom"], stages["lead"]),
    }

    by_platform: dict[str, dict[str, int]] = {}
    for platform, stage, count in db.query(
            FunnelEvent.platform, FunnelEvent.stage, func.count()).filter(
            FunnelEvent.created_at >= since).group_by(
            FunnelEvent.platform, FunnelEvent.stage).all():
        key = platform or "unknown"
        by_platform.setdefault(key, {s: 0 for s in STAGES})
        if stage in STAGES:
            by_platform[key][stage] = count

    # 账号下钻：account_id → 账号名（0/未匹配归为「未归因」）
    account_names = {a.id: a.account_name for a in db.query(MatrixAccount).all()}
    by_account: dict[str, dict[str, int]] = {}
    for account_id, stage, count in db.query(
            FunnelEvent.account_id, FunnelEvent.stage, func.count()).filter(
            FunnelEvent.created_at >= since).group_by(
            FunnelEvent.account_id, FunnelEvent.stage).all():
        name = account_names.get(account_id, "未归因")
        by_account.setdefault(name, {s: 0 for s in STAGES})
        if stage in STAGES:
            by_account[name][stage] += count

    return FunnelOverview(stages=stages, conversion=conversion,
                          by_platform=by_platform, by_account=by_account)


@router.get("/events")
def list_events(stage: str = "", limit: int = 100,
                _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """漏斗事件明细（最新在前）。"""
    limit = max(1, min(limit, 500))
    q = db.query(FunnelEvent)
    if stage:
        q = q.filter(FunnelEvent.stage == stage)
    rows = q.order_by(FunnelEvent.id.desc()).limit(limit).all()
    return {"items": [FunnelEventOut.model_validate(r).model_dump(mode="json") for r in rows]}


# ============ 企微渠道活码管理 ============

class ChannelCodeIn(BaseModel):
    name: str
    bound_content_id: int = 0
    bound_account_id: int = 0
    qr_url: str = ""   # 手工录入时必填；API 创建时由企微返回


def _code_out(c: WecomChannelCode) -> dict:
    return {
        "id": c.id, "name": c.name, "config_id": c.config_id, "qr_url": c.qr_url,
        "state": c.state, "bound_content_id": c.bound_content_id,
        "bound_account_id": c.bound_account_id,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


@router.get("/codes")
def list_codes(_: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    rows = db.query(WecomChannelCode).order_by(WecomChannelCode.id.desc()).all()
    return {"items": [_code_out(c) for c in rows], "wecom_configured": settings.wecom_configured}


@router.post("/codes")
async def create_code(req: ChannelCodeIn, agent: Agent = Depends(require_admin),
                      db: Session = Depends(get_db)):
    """创建活码：企微已配置 → 调 API 创建；未配置 → 手工录入（企微后台建码后填 qr_url）。

    state 由系统生成（全局唯一），企微回调按 state 归因。
    """
    from ..core import audit
    from ..wecom import channel_code

    name = req.name.strip()[:64]
    if not name:
        raise HTTPException(status_code=400, detail="活码名称不能为空")
    state = uuid.uuid4().hex[:16]
    if settings.wecom_configured:
        try:
            code = await channel_code.create_contact_way(
                name, state, req.bound_content_id, req.bound_account_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    else:
        if not req.qr_url.strip():
            raise HTTPException(
                status_code=400,
                detail="企微 API 未配置：请在企微后台手工建活码后，把二维码链接填入 qr_url")
        code = channel_code.save_manual_code(
            name, state, req.qr_url.strip(), req.bound_content_id, req.bound_account_id)
    audit.log(db, agent, "wecom_code_create", target=f"wecom_code:{code.id}", detail=name)
    return _code_out(code)
