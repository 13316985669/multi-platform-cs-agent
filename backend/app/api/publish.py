"""发布中心 API：发布任务创建/取消/重试/改期/导出发布包、日历聚合、队列占坑、最佳时段、已发布作品列表。"""
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core import audit
from ..database import get_db
from ..models import (Agent, ContentVersion, Material, MatrixAccount, Post,
                      PublishTask)
from ..publisher.queue import best_slots, next_free_slot
from ..schemas import (BestSlotsOut, CalendarDayOut, EnqueueIn, PostOut,
                       PublishTaskIn, PublishTaskOut, RescheduleIn)
from .deps import get_current_agent

BEIJING_OFFSET = timedelta(hours=8)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["publish"])


def _task_out(db: Session, task: PublishTask) -> PublishTaskOut:
    account = db.get(MatrixAccount, task.account_id)
    return PublishTaskOut(
        id=task.id, content_version_id=task.content_version_id, account_id=task.account_id,
        account_name=account.account_name if account else "",
        platform=account.platform if account else "",
        scheduled_at=task.scheduled_at, status=task.status,
        platform_post_id=task.platform_post_id or "", post_url=task.post_url or "",
        error=task.error or "", retries=task.retries, published_at=task.published_at,
        created_at=task.created_at,
    )


def _check_dup_soft_block(version: ContentVersion, force: bool):
    """查重软拦截：同平台高相似版本存在时需显式 force 确认（409 带相似度信息）。"""
    from ..creator.dedup import SOFT_BLOCK_THRESHOLD
    report = version.dup_report or {}
    max_sim = float(report.get("max_similarity") or 0)
    if max_sim > SOFT_BLOCK_THRESHOLD and not force:
        raise HTTPException(
            409, f"与同平台版本 #{report.get('similar_version_id')} 相似度 "
                 f"{max_sim:.0%}（矩阵同质化易被限流），确认仍要发布请带 force=true")


def _check_item_approved(db: Session, version: ContentVersion):
    """审批硬约束：内容主体必须审批通过（approved）才能发布/入队。"""
    from ..models import ContentItem
    item = db.get(ContentItem, version.content_item_id)
    if item is None or item.status != "approved":
        raise HTTPException(400, "内容未审批通过，请先在创作台提交审核并由管理员通过")


@router.post("/publish/tasks", response_model=list[PublishTaskOut])
def create_publish_task(req: PublishTaskIn, agent: Agent = Depends(get_current_agent),
                        db: Session = Depends(get_db)):
    """创建发布任务：一个版本 × N 个账号。校验版本合规通过 + 账号平台匹配 + 审批通过。"""
    version = db.get(ContentVersion, req.content_version_id)
    if version is None:
        raise HTTPException(404, "内容版本不存在")
    if version.compliance_status != "passed":
        raise HTTPException(400, "该版本未通过合规检测，不能发布")
    _check_item_approved(db, version)
    _check_dup_soft_block(version, req.force)
    if not req.account_ids:
        raise HTTPException(400, "至少选择一个账号")

    scheduled_at = req.scheduled_at or datetime.utcnow()
    tasks = []
    for account_id in dict.fromkeys(req.account_ids):  # 去重保序
        account = db.get(MatrixAccount, account_id)
        if account is None:
            raise HTTPException(404, f"账号 {account_id} 不存在")
        if account.platform != version.platform:
            raise HTTPException(
                400, f"账号「{account.account_name}」平台（{account.platform}）"
                     f"与内容版本平台（{version.platform}）不匹配")
        if account.status != "active":
            raise HTTPException(400, f"账号「{account.account_name}」状态为 {account.status}，不可发布")
        task = PublishTask(content_version_id=version.id, account_id=account.id,
                           scheduled_at=scheduled_at)
        db.add(task)
        tasks.append(task)
    db.commit()
    for t in tasks:
        db.refresh(t)
    audit.log(db, agent, "publish_task_create", target=f"version:{version.id}",
              detail=f"accounts={req.account_ids} scheduled_at={scheduled_at}")
    return [_task_out(db, t) for t in tasks]


@router.get("/publish/tasks", response_model=list[PublishTaskOut])
def list_publish_tasks(status: str = "", _: Agent = Depends(get_current_agent),
                       db: Session = Depends(get_db)):
    q = db.query(PublishTask)
    if status:
        q = q.filter(PublishTask.status == status)
    else:
        q = q.filter(PublishTask.status != "external")
    return [_task_out(db, t) for t in q.order_by(PublishTask.id.desc()).limit(200).all()]


@router.post("/publish/tasks/{task_id}/cancel", response_model=PublishTaskOut)
def cancel_publish_task(task_id: int, agent: Agent = Depends(get_current_agent),
                        db: Session = Depends(get_db)):
    task = db.get(PublishTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status not in ("pending", "failed"):
        raise HTTPException(400, f"当前状态 {task.status} 不可取消")
    task.status = "cancelled"
    db.commit()
    db.refresh(task)
    audit.log(db, agent, "publish_task_cancel", target=f"task:{task_id}")
    return _task_out(db, task)


@router.post("/publish/tasks/{task_id}/retry", response_model=PublishTaskOut)
def retry_publish_task(task_id: int, agent: Agent = Depends(get_current_agent),
                       db: Session = Depends(get_db)):
    """失败任务重试：清零重试计数，立即到点。"""
    task = db.get(PublishTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status != "failed":
        raise HTTPException(400, f"仅 failed 状态可重试（当前 {task.status}）")
    task.status = "pending"
    task.retries = 0
    task.error = ""
    task.scheduled_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    audit.log(db, agent, "publish_task_retry", target=f"task:{task_id}")
    return _task_out(db, task)


@router.post("/publish/tasks/{task_id}/reschedule", response_model=PublishTaskOut)
def reschedule_task(task_id: int, req: RescheduleIn,
                    agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """改期：仅 pending 任务可改（日历拖拽用）。"""
    task = db.get(PublishTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    if task.status != "pending":
        raise HTTPException(400, f"仅待发布任务可改期（当前 {task.status}）")
    if req.scheduled_at <= datetime.utcnow() - timedelta(minutes=5):
        raise HTTPException(400, "计划时间不能早于当前时间")
    old = task.scheduled_at
    task.scheduled_at = req.scheduled_at
    db.commit()
    db.refresh(task)
    audit.log(db, agent, "publish_task_reschedule", target=f"task:{task_id}",
              detail=f"{old} -> {req.scheduled_at}")
    return _task_out(db, task)


@router.get("/publish/calendar", response_model=list[CalendarDayOut])
def publish_calendar(month: str = "", _: Agent = Depends(get_current_agent),
                     db: Session = Depends(get_db)):
    """月历聚合：month=YYYY-MM（默认当月），按北京日分组返回任务。"""
    if month:
        try:
            month_start = datetime.strptime(month, "%Y-%m")
        except ValueError as exc:
            raise HTTPException(400, "month 格式应为 YYYY-MM") from exc
    else:
        month_start = datetime.utcnow().replace(day=1)
    # 北京日界换算：查询范围按 UTC 偏移 8h 放宽
    start_utc = month_start - BEIJING_OFFSET
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    end_utc = next_month - BEIJING_OFFSET

    tasks = db.query(PublishTask).filter(
        PublishTask.scheduled_at >= start_utc,
        PublishTask.scheduled_at < end_utc,
    ).order_by(PublishTask.scheduled_at).all()

    days: dict[str, CalendarDayOut] = {}
    for task in tasks:
        day_key = (task.scheduled_at + BEIJING_OFFSET).strftime("%Y-%m-%d")
        days.setdefault(day_key, CalendarDayOut(date=day_key, tasks=[]))
        days[day_key].tasks.append(_task_out(db, task))
    return list(days.values())


@router.post("/publish/enqueue", response_model=list[PublishTaskOut])
def enqueue_publish(req: EnqueueIn, agent: Agent = Depends(get_current_agent),
                    db: Session = Depends(get_db)):
    """队列发布：按各账号时段位自动占坑（账号需开启队列模式并配置时段位）。"""
    version = db.get(ContentVersion, req.content_version_id)
    if version is None:
        raise HTTPException(404, "内容版本不存在")
    if version.compliance_status != "passed":
        raise HTTPException(400, "该版本未通过合规检测，不能发布")
    _check_item_approved(db, version)
    _check_dup_soft_block(version, req.force)
    if not req.account_ids:
        raise HTTPException(400, "至少选择一个账号")

    tasks = []
    for account_id in dict.fromkeys(req.account_ids):
        account = db.get(MatrixAccount, account_id)
        if account is None:
            raise HTTPException(404, f"账号 {account_id} 不存在")
        if account.platform != version.platform:
            raise HTTPException(400, f"账号「{account.account_name}」平台与内容版本不匹配")
        if account.status != "active":
            raise HTTPException(400, f"账号「{account.account_name}」状态为 {account.status}，不可发布")
        if not account.queue_enabled:
            raise HTTPException(400, f"账号「{account.account_name}」未开启队列模式")
        try:
            slot = next_free_slot(db, account)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        task = PublishTask(content_version_id=version.id, account_id=account.id,
                           scheduled_at=slot)
        db.add(task)
        tasks.append(task)
    db.commit()
    for t in tasks:
        db.refresh(t)
    audit.log(db, agent, "publish_enqueue", target=f"version:{version.id}",
              detail=f"accounts={req.account_ids}")
    return [_task_out(db, t) for t in tasks]


@router.get("/publish/best-slots", response_model=BestSlotsOut)
def get_best_slots(account_id: int, _: Agent = Depends(get_current_agent),
                   db: Session = Depends(get_db)):
    """最佳发布时段推荐：历史数据足够时按平均播放推荐，否则平台黄金时段默认值。"""
    account = db.get(MatrixAccount, account_id)
    if account is None:
        raise HTTPException(404, "账号不存在")
    source, slots = best_slots(db, account)
    return BestSlotsOut(account_id=account_id, source=source, slots=slots)


@router.get("/publish/tasks/{task_id}/export")
def export_publish_task(task_id: int, _: Agent = Depends(get_current_agent),
                        db: Session = Depends(get_db)):
    """导出发布包（人工降级发布用）：标题/正文/标签/脚本/素材下载链接。"""
    task = db.get(PublishTask, task_id)
    if task is None:
        raise HTTPException(404, "任务不存在")
    version = db.get(ContentVersion, task.content_version_id)
    account = db.get(MatrixAccount, task.account_id)
    if version is None or account is None:
        raise HTTPException(404, "关联数据缺失")
    materials = []
    for mid in version.material_ids or []:
        m = db.get(Material, mid)
        if m is not None:
            materials.append({"id": m.id, "kind": m.kind, "mime": m.mime,
                              "url": f"/api/materials/{m.id}/file"})
    return {
        "platform": account.platform,
        "account_name": account.account_name,
        "title": version.title,
        "body": version.body,
        "tags": version.tags or [],
        "script": version.script,
        "cover_text": version.cover_text,
        "materials": materials,
        "scheduled_at": task.scheduled_at.isoformat() if task.scheduled_at else None,
    }


@router.get("/posts", response_model=list[PostOut])
def list_posts(platform: str = "", _: Agent = Depends(get_current_agent),
               db: Session = Depends(get_db)):
    q = db.query(Post)
    if platform:
        q = q.filter(Post.platform == platform)
    posts = q.order_by(Post.id.desc()).limit(200).all()
    result = []
    for p in posts:
        account = db.get(MatrixAccount, p.account_id)
        result.append(PostOut(
            id=p.id, publish_task_id=p.publish_task_id, account_id=p.account_id,
            account_name=account.account_name if account else "",
            platform=p.platform, platform_post_id=p.platform_post_id or "",
            url=p.url or "", title=p.title or "", stats_json=p.stats_json or {},
            created_at=p.created_at,
        ))
    return result
