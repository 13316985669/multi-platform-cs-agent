"""留资列表：按平台、联系方式、业务搜索。"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import Agent, LeadCapture
from ..schemas import LeadCaptureOut
from .deps import get_current_agent

router = APIRouter(prefix="/api/leads", tags=["leads"])


@router.get("", response_model=dict)
def list_leads(q: str = "", platform: str = "", page: int = 1, page_size: int = 20,
               agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    query = db.query(LeadCapture)
    if platform:
        query = query.filter(LeadCapture.platform == platform)
    keyword = (q or "").strip()
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            (LeadCapture.contact.like(like))
            | (LeadCapture.nickname.like(like))
            | (LeadCapture.business.like(like))
        )
    total = query.count()
    rows = (
        query.order_by(LeadCapture.created_at.desc())
        .offset(max(page - 1, 0) * page_size)
        .limit(page_size)
        .all()
    )
    return {"total": total, "items": [LeadCaptureOut.model_validate(row) for row in rows]}
