"""异步任务队列：webhook 秒级 ACK 的关键。

设计：
- enqueue() 只做持久化（SQLite 队列表）+ 内存事件通知，接口立即返回
- 双通道 worker 并发消费（Phase 8）：fast（inbound_message/inbound_comment，秒级响应要求）
  与 slow（内容生成/发布/首评等分钟级慢任务）各自独立循环，慢任务不再阻塞私信/评论回复
- fast lane 并发消费：_claim_next_pending CAS 认领防双取，信号量
  （QUEUE_FAST_CONCURRENCY，默认 20）限在途数；同会话不并发答题由
  core.convlock 保证（见 services._route_after_user_message）
- 失败重试指数退避：复用 not_before 列，第 n 次重试延迟 30s/60s/120s；
  重试用尽置 failed 并 monitor 告警（queue_task_failed）
- 任务持久化在 queue_tasks 表，进程崩溃重启后自动恢复 pending/processing 任务
"""
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import update

from ..config import settings
from ..database import SessionLocal
from ..models import QueueTask

logger = logging.getLogger(__name__)

# 任务类型 -> 处理函数（在 services 中注册，避免循环依赖）
_HANDLERS: dict[str, Callable[[dict[str, Any]], Awaitable[None]]] = {}

# 双通道划分：fast = 实时性敏感（私信/评论入站）；slow = 其余全部（LLM 生成/发布/首评/回采）
FAST_LANE_TYPES = frozenset({"inbound_message", "inbound_comment"})
LANES = ("fast", "slow")

MAX_RETRIES = 3
RETRY_BASE_SECONDS = 30  # 退避序列：30s / 60s / 120s

# 每通道一个唤醒事件（asyncio.Event.set 会唤醒全部等待者，不能多循环共用同一个）
_wakeup: dict[str, asyncio.Event] = {}
_worker_tasks: list[asyncio.Task] = []
# fast lane 并发在途任务（stop_worker 收尾用；done 回调自动移除）
_inflight: set[asyncio.Task] = set()


def register_handler(task_type: str, handler: Callable[[dict[str, Any]], Awaitable[None]]):
    _HANDLERS[task_type] = handler


def _lane_of(task_type: str) -> str:
    return "fast" if task_type in FAST_LANE_TYPES else "slow"


def enqueue(task_type: str, payload: dict[str, Any], delay_seconds: int = 0) -> int:
    """持久化任务并唤醒对应通道的 worker。返回任务 ID。供同步上下文（FastAPI 路由）调用。

    delay_seconds > 0 时任务到点才可被消费（worker 每 5s 轮询兜底，无需精确唤醒）。
    """
    db = SessionLocal()
    try:
        task = QueueTask(task_type=task_type, payload=payload, status="pending")
        if delay_seconds > 0:
            task.not_before = datetime.utcnow() + timedelta(seconds=delay_seconds)
        db.add(task)
        db.commit()
        db.refresh(task)
        task_id = task.id
    finally:
        db.close()
    # 唤醒对应通道的 worker（若事件循环在跑）
    try:
        loop = asyncio.get_running_loop()
        ev = _wakeup.get(_lane_of(task_type))
        if ev is not None:
            loop.call_soon_threadsafe(ev.set)
    except RuntimeError:
        pass
    return task_id


async def _process_one(task_id: int, *, claimed: bool = False):
    """执行一条任务。claimed=True 表示调用方已 CAS 认领（status=processing），跳过状态迁移。"""
    db = SessionLocal()
    task = db.get(QueueTask, task_id)
    if task is None or (not claimed and task.status not in ("pending",)):
        db.close()
        return
    if not claimed:
        task.status = "processing"
        db.commit()
    task_type, payload = task.task_type, task.payload
    db.close()

    handler = _HANDLERS.get(task_type)
    try:
        if handler is None:
            raise RuntimeError(f"未注册的任务类型: {task_type}")
        await handler(payload)
        status, error = "done", ""
    except Exception as exc:  # noqa: BLE001
        logger.exception("任务处理失败 task_id=%s type=%s", task_id, task_type)
        status, error = "failed", str(exc)[:2000]

    db = SessionLocal()
    task = db.get(QueueTask, task_id)
    if task is not None:
        if status == "failed" and task.retries < MAX_RETRIES:
            # 失败重试：指数退避（30s/60s/120s），避免依赖方故障时瞬间打满重试
            task.retries += 1
            task.status = "pending"
            task.not_before = datetime.utcnow() + timedelta(
                seconds=RETRY_BASE_SECONDS * (2 ** (task.retries - 1)))
            task.error = error
        else:
            task.status = status
            # done 且此前失败过时保留最后一次失败原因，便于排查「重试后才成功」的任务
            if status == "failed" or not task.error:
                task.error = error
            if status == "failed":
                # 重试用尽的最终失败：全类型告警（此前仅 inbound_message 有埋点）
                from . import monitor
                monitor.record("queue_task_failed",
                               f"type={task_type} task_id={task_id} {error[:150]}")
                if task_type == "inbound_message":
                    # 兼容既有告警通道（阈值见 monitor.THRESHOLDS）
                    monitor.record("webhook_failure", f"task_id={task_id} {error[:150]}")
        task.updated_at = datetime.utcnow()
        db.commit()
    db.close()


def _fetch_next_pending(lane: str | None = None) -> int | None:
    """取下一条可消费任务。lane=None 不区分通道（兼容旧调用/测试）。"""
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        q = (
            db.query(QueueTask)
            .filter(QueueTask.status == "pending")
            .filter((QueueTask.not_before.is_(None)) | (QueueTask.not_before <= now))
        )
        if lane == "fast":
            q = q.filter(QueueTask.task_type.in_(FAST_LANE_TYPES))
        elif lane == "slow":
            q = q.filter(QueueTask.task_type.notin_(FAST_LANE_TYPES))
        task = q.order_by(QueueTask.id).first()
        return task.id if task else None
    finally:
        db.close()


def _claim_next_pending(lane: str | None = None) -> int | None:
    """认领下一条可消费任务：查询 + CAS 原子置 processing，并发消费防双取。

    候选已被其他协程抢走（rowcount=0）时返回 None，调用方继续取下一条。
    lane 语义与 _fetch_next_pending 一致。
    """
    db = SessionLocal()
    try:
        now = datetime.utcnow()
        q = (
            db.query(QueueTask.id)
            .filter(QueueTask.status == "pending")
            .filter((QueueTask.not_before.is_(None)) | (QueueTask.not_before <= now))
        )
        if lane == "fast":
            q = q.filter(QueueTask.task_type.in_(FAST_LANE_TYPES))
        elif lane == "slow":
            q = q.filter(QueueTask.task_type.notin_(FAST_LANE_TYPES))
        row = q.order_by(QueueTask.id).first()
        if row is None:
            return None
        task_id = row[0]
        claimed = db.execute(
            update(QueueTask)
            .where(QueueTask.id == task_id, QueueTask.status == "pending")
            .values(status="processing", updated_at=datetime.utcnow())
        )
        db.commit()
        return task_id if claimed.rowcount == 1 else None
    finally:
        db.close()


async def _run_one_fast(task_id: int, sem: asyncio.Semaphore) -> None:
    """并发执行一条 fast 任务；finally 释放信号量槽位。"""
    try:
        await _process_one(task_id, claimed=True)
    finally:
        sem.release()


async def _lane_worker_loop(lane: str):
    """通道 worker 循环：fast 并发派发（信号量限在途数），slow 串行消费。

    fast 满载时在 sem.acquire 背压，不无限堆积在途任务；空闲时挂起等待
    唤醒（5s 轮询兜底延迟任务）。认领后派发前被 stop 取消的任务停留在
    processing，由下次 start_worker 的启动恢复重回 pending。
    """
    ev = asyncio.Event()
    _wakeup[lane] = ev
    logger.info("任务队列 worker 已启动 lane=%s", lane)
    sem = asyncio.Semaphore(max(1, settings.queue_fast_concurrency)) if lane == "fast" else None
    while True:
        task_id = _claim_next_pending(lane)
        if task_id is None:
            ev.clear()
            try:
                await asyncio.wait_for(ev.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            continue
        if sem is None:
            await _process_one(task_id, claimed=True)
            continue
        await sem.acquire()  # 满载背压：等一个空槽位再派发
        t = asyncio.create_task(_run_one_fast(task_id, sem),
                                 name=f"queue-task-{task_id}")
        _inflight.add(t)
        t.add_done_callback(_inflight.discard)


def start_worker():
    """启动 fast + slow 双通道 worker（幂等）。"""
    # 启动恢复：上次进程崩溃遗留的 processing 任务重回 pending
    db = SessionLocal()
    db.query(QueueTask).filter(QueueTask.status == "processing").update({"status": "pending"})
    db.commit()
    db.close()
    for lane in LANES:
        running = [t for t in _worker_tasks if not t.done() and t.get_name() == f"queue-worker-{lane}"]
        if not running:
            _worker_tasks.append(asyncio.create_task(_lane_worker_loop(lane),
                                                     name=f"queue-worker-{lane}"))


async def stop_worker():
    global _worker_tasks
    # 先停通道循环：不再认领/派发新任务
    for t in _worker_tasks:
        t.cancel()
    for t in _worker_tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
    # fast lane 在途任务收尾：取消并等待退出。
    # 被取消的任务停留在 processing，由下次 start_worker 的启动恢复重回 pending。
    inflight = [t for t in _inflight if not t.done()]
    for t in inflight:
        t.cancel()
    if inflight:
        await asyncio.gather(*inflight, return_exceptions=True)
    _worker_tasks = []
    _inflight.clear()
    _wakeup.clear()
