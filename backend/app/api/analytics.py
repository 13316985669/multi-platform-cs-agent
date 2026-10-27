"""内容效果分析 API：作品数据排行、单作品趋势、选题 ROI 排行。"""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..analytics.attribution import account_report, content_roi
from ..database import get_db
from ..models import Agent, MatrixAccount, Post, PostStatSnapshot
from ..schemas import (AccountReportOut, AnalyticsPostOut, ContentRoiOut,
                       PostStatPoint)
from .deps import get_current_agent

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/posts", response_model=list[AnalyticsPostOut])
def list_post_stats(platform: str = "", account_id: int = 0,
                    sort: str = "play", days: int = 30, limit: int = 50,
                    _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """作品数据排行：按播放/点赞/评论排序，支持平台/账号/时间窗筛选。"""
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 200))
    since = datetime.utcnow() - timedelta(days=days)
    q = db.query(Post).filter(Post.created_at >= since)
    if platform:
        q = q.filter(Post.platform == platform)
    if account_id:
        q = q.filter(Post.account_id == account_id)
    posts = q.all()

    def _key(p: Post) -> int:
        return int((p.stats_json or {}).get(sort) or 0) if sort in (
            "play", "digg", "comment", "share", "collect") else 0

    posts.sort(key=_key, reverse=True)
    result = []
    for p in posts[:limit]:
        account = db.get(MatrixAccount, p.account_id)
        result.append(AnalyticsPostOut(
            id=p.id, account_id=p.account_id,
            account_name=account.account_name if account else "",
            platform=p.platform, title=p.title or "", url=p.url or "",
            stats=p.stats_json or {}, stats_updated_at=p.stats_updated_at,
            created_at=p.created_at))
    return result


@router.get("/posts/{post_id}/trend", response_model=list[PostStatPoint])
def post_trend(post_id: int, _: Agent = Depends(get_current_agent),
               db: Session = Depends(get_db)):
    """单作品数据趋势（回采快照时间序列）。"""
    if db.get(Post, post_id) is None:
        raise HTTPException(404, "作品不存在")
    rows = db.query(PostStatSnapshot).filter(
        PostStatSnapshot.post_id == post_id).order_by(PostStatSnapshot.captured_at).all()
    return [PostStatPoint(play=r.play, digg=r.digg, comment=r.comment, share=r.share,
                          collect=r.collect, captured_at=r.captured_at) for r in rows]


@router.get("/contents", response_model=list[ContentRoiOut])
def list_content_roi(days: int = 30, limit: int = 50,
                     _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """选题 ROI 排行：播放数据 + 漏斗四级（哪类内容真正带来留资）。"""
    days = max(1, min(days, 90))
    limit = max(1, min(limit, 200))
    return [ContentRoiOut(**row) for row in content_roi(db, days=days, limit=limit)]


@router.get("/accounts", response_model=list[AccountReportOut])
def list_account_report(days: int = 30, _: Agent = Depends(get_current_agent),
                        db: Session = Depends(get_db)):
    """账号效果报表（Phase 7）：每账号发布/互动/漏斗/成功率/健康度/画像。"""
    days = max(1, min(days, 90))
    return [AccountReportOut(**row) for row in account_report(db, days=days)]
