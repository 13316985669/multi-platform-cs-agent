"""发布调度器：扫描到点任务 → 额度校验 → 通道路由 → 状态机流转。

状态机：pending → publishing → success | failed（retries<3 回 pending 重试）；
       cancelled 为人工取消终态。
频控：publish_auto_enabled 总开关 + 每账号每日上限（超限推迟到次日 9 点后）。
"""
import asyncio
import logging
from datetime import datetime, timedelta

from ..config import settings
from ..core import monitor
from ..database import SessionLocal
from ..models import ContentVersion, MatrixAccount, Post, PublishTask
from .channels import get_channel

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
SWEEP_INTERVAL_SECONDS = 30


def _today_success_count(db, account_id: int) -> int:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(PublishTask)
        .filter(PublishTask.account_id == account_id, PublishTask.status == "success",
                PublishTask.published_at >= today_start)
        .count()
    )


def _next_morning() -> datetime:
    """次日 09:00（额度超限推迟用）。"""
    return (datetime.utcnow() + timedelta(days=1)).replace(hour=1, minute=0, second=0,
                                                           microsecond=0)
    # 注：服务器时间为 UTC，UTC+1h ≈ 北京时间 09:00


async def _execute_task(task_id: int):
    """执行单个发布任务（独立会话，异常不外抛）。"""
    db = SessionLocal()
    try:
        task = db.get(PublishTask, task_id)
        if task is None or task.status != "publishing":
            return
        version = db.get(ContentVersion, task.content_version_id)
        account = db.get(MatrixAccount, task.account_id)
        if version is None or account is None:
            task.status = "failed"
            task.error = "内容版本或账号已被删除"
            db.commit()
            return
        try:
            channel = get_channel(account)
            post_id, url = await channel.publish(db, task, version, account)
        except Exception as exc:  # noqa: BLE001
            from .channels.douyin_api import QuotaExceeded
            if isinstance(exc, QuotaExceeded):
                # 平台日配额耗尽：推迟次日，不计重试
                task.status = "pending"
                task.scheduled_at = _next_morning()
                task.error = str(exc)[:500]
                db.commit()
                logger.info("任务 %s 触发平台日配额上限，推迟次日", task_id)
                return
            logger.exception("发布失败 task_id=%s", task_id)
            task.retries += 1
            task.error = str(exc)[:2000]
            if task.retries < MAX_RETRIES:
                task.status = "pending"  # 回滚重试
            else:
                task.status = "failed"
                monitor.record("publish_failure",
                               f"task={task_id} account={account.account_name} {exc}"[:180])
            db.commit()
            return

        # 异步通道（RPA）：入队成功即返回，保持 publishing 等 Worker 回执对账
        if getattr(channel, "is_async", False):
            task.platform_post_id = post_id  # outbox:{id} 临时标识
            task.error = ""
            db.commit()
            logger.info("任务 %s 已入队异步通道（%s），等待 Worker 回执", task_id, post_id)
            return

        _mark_success(db, task, account, version, post_id, url)
        logger.info("发布成功 task_id=%s post_id=%s", task_id, post_id)
        # WS 广播，发布页实时刷新
        try:
            from ..api.ws import manager
            await manager.broadcast("publish_task_updated", {"task_id": task_id, "status": "success"})
        except Exception:  # noqa: BLE001
            logger.exception("发布成功广播失败")
    finally:
        db.close()


def _mark_success(db, task: PublishTask, account: MatrixAccount,
                  version: ContentVersion, post_id: str, url: str):
    task.status = "success"
    task.platform_post_id = post_id
    task.post_url = url
    task.published_at = datetime.utcnow()
    task.error = ""
    post = Post(publish_task_id=task.id, account_id=account.id,
                platform=account.platform, platform_post_id=post_id,
                url=url, title=version.title)
    db.add(post)
    db.commit()
    db.refresh(post)
    # 首评引流钩子：版本配置了首评话术则延迟入队（异步/RPA 通道对账成功同样触发）
    if (version.first_comment or "").strip():
        try:
            from ..comments.first_comment import enqueue_first_comment
            enqueue_first_comment(post.id)
        except Exception:  # noqa: BLE001
            logger.exception("首评入队失败 post=%s", post.id)


ASYNC_TASK_TIMEOUT = timedelta(minutes=30)


async def reconcile_async_tasks():
    """异步通道对账：publishing 且 platform_post_id=outbox:{id} 的任务按 outbox 回执落定。"""
    db = SessionLocal()
    try:
        rows = (
            db.query(PublishTask)
            .filter(PublishTask.status == "publishing",
                    PublishTask.platform_post_id.like("outbox:%"))
            .limit(50)
            .all()
        )
        for task in rows:
            from ..models import RpaOutbox
            try:
                outbox_id = int(task.platform_post_id.split(":", 1)[1])
            except (ValueError, IndexError):
                continue
            outbox = db.get(RpaOutbox, outbox_id)
            if outbox is None:
                task.status = "failed"
                task.error = "outbox 记录丢失"
                continue
            if outbox.status == "acked":
                version = db.get(ContentVersion, task.content_version_id)
                account = db.get(MatrixAccount, task.account_id)
                _mark_success(db, task, account, version,
                              outbox.result or task.platform_post_id, outbox.result or "")
            elif outbox.status == "failed":
                task.status = "failed"
                task.error = f"Worker 发布失败: {outbox.error[:400]}"
                db.commit()
            elif task.updated_at and datetime.utcnow() - task.updated_at > ASYNC_TASK_TIMEOUT:
                task.status = "failed"
                task.error = "Worker 超时未回执（30 分钟），请检查 Worker 在线状态"
                db.commit()
        if rows:
            db.commit()
    finally:
        db.close()


async def run_due_tasks() -> list[int]:
    """扫描并执行到点任务。返回实际执行的任务 ID 列表。"""
    if not settings.publish_auto_enabled:
        return []
    db = SessionLocal()
    try:
        due = (
            db.query(PublishTask)
            .filter(PublishTask.status == "pending",
                    PublishTask.scheduled_at <= datetime.utcnow())
            .order_by(PublishTask.scheduled_at)
            .limit(20)
            .all()
        )
        picked: list[int] = []
        for task in due:
            account = db.get(MatrixAccount, task.account_id)
            if account is None or account.status != "active":
                task.status = "failed"
                task.error = "账号不存在或已停用/授权过期"
                continue
            # 账号日额度硬约束：宁可少发不可封号
            if _today_success_count(db, account.id) >= account.daily_publish_limit:
                task.scheduled_at = _next_morning()
                logger.info("账号 %s 当日发布额度已满，任务 %s 推迟到次日",
                            account.account_name, task.id)
                continue
            task.status = "publishing"
            picked.append(task.id)
        db.commit()
    finally:
        db.close()

    for task_id in picked:
        await _execute_task(task_id)
    return picked


async def publish_scheduler_loop():
    """后台周期任务：每 30s 扫描一次到点发布任务 + 异步通道回执对账（main.py lifespan 注册）。"""
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        try:
            await run_due_tasks()
            await reconcile_async_tasks()
        except Exception:  # noqa: BLE001
            logger.exception("发布调度扫描失败")
