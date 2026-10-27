"""发布队列与智能排期。

- 队列模式：账号配置每日时段位（queue_slots，北京时间 HH:MM），内容入队自动占下一个坑
- 占坑规则：同一账号同一时段 ±30 分钟内只允许一个未完结任务；当日任务数不超日限额
- 最佳时段推荐：历史数据足够（≥5 条有播放数据的作品）时按发布小时的平均播放取 top3，
  否则回退平台黄金时段默认值
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import MatrixAccount, Post, PublishTask

logger = logging.getLogger(__name__)

BEIJING_OFFSET = timedelta(hours=8)   # 服务器时间为 UTC，时段位按北京时间配置
SLOT_WINDOW_MINUTES = 30              # 时段位占用窗口
LOOKAHEAD_DAYS = 14                   # 队列最多向后找 14 天
MIN_HISTORY_FOR_RECOMMEND = 5         # 历史推荐所需的最少数据点

# 平台黄金时段默认值（北京时间）
DEFAULT_SLOTS = {
    "douyin": ["12:00", "18:00", "21:00"],
    "xiaohongshu": ["10:00", "20:00"],
}


def _beijing_day_start(day_utc: datetime) -> datetime:
    """取 day_utc 所在北京日的 00:00（UTC 表示）。"""
    beijing = day_utc + BEIJING_OFFSET
    return beijing.replace(hour=0, minute=0, second=0, microsecond=0) - BEIJING_OFFSET


def _slot_utc(day_utc: datetime, slot: str) -> datetime:
    """slot 为北京时间 HH:MM，返回该北京日对应时刻的 UTC 时间。

    day_start 是「北京日 00:00」的 UTC 表示，直接加时段偏移即为所求。
    """
    hour, minute = slot.split(":")
    day_start = _beijing_day_start(day_utc)
    return day_start + timedelta(hours=int(hour), minutes=int(minute))


def _day_task_count(db: Session, account_id: int, day_utc: datetime) -> int:
    start = _beijing_day_start(day_utc)
    end = start + timedelta(days=1)
    return db.query(PublishTask).filter(
        PublishTask.account_id == account_id,
        PublishTask.status.in_(["pending", "publishing", "success"]),
        PublishTask.scheduled_at >= start, PublishTask.scheduled_at < end).count()


def _slot_occupied(db: Session, account_id: int, at: datetime) -> bool:
    window = timedelta(minutes=SLOT_WINDOW_MINUTES)
    return db.query(PublishTask).filter(
        PublishTask.account_id == account_id,
        PublishTask.status.in_(["pending", "publishing"]),
        PublishTask.scheduled_at >= at - window,
        PublishTask.scheduled_at <= at + window).count() > 0


def next_free_slot(db: Session, account: MatrixAccount,
                   after: datetime | None = None) -> datetime:
    """找该账号下一个可用时段位（UTC）。无可用位抛 ValueError。"""
    slots = sorted(s for s in (account.queue_slots or []) if ":" in s)
    if not slots:
        raise ValueError(f"账号「{account.account_name}」未配置发布时段位（queue_slots）")
    after = after or datetime.utcnow()
    base_day = _beijing_day_start(after)
    for d in range(LOOKAHEAD_DAYS):
        day = base_day + timedelta(days=d)
        if _day_task_count(db, account.id, day) >= account.daily_publish_limit:
            continue
        for slot in slots:
            at = _slot_utc(day, slot)
            if at <= after:
                continue
            if _slot_occupied(db, account.id, at):
                continue
            return at
    raise ValueError(f"账号「{account.account_name}」未来 {LOOKAHEAD_DAYS} 天内无可用时段位")


def best_slots(db: Session, account: MatrixAccount) -> tuple[str, list[str]]:
    """最佳发布时段推荐：返回 (source, slots)。source=history 历史数据 | default 平台默认。"""
    rows = db.query(Post).filter(Post.account_id == account.id).all()
    by_hour: dict[int, list[int]] = {}
    for post in rows:
        play = int((post.stats_json or {}).get("play") or 0)
        if play <= 0:
            continue
        task = db.get(PublishTask, post.publish_task_id)
        if task is None or task.published_at is None:
            continue
        hour_bj = (task.published_at + BEIJING_OFFSET).hour
        by_hour.setdefault(hour_bj, []).append(play)
    total_points = sum(len(v) for v in by_hour.values())
    if total_points >= MIN_HISTORY_FOR_RECOMMEND:
        ranked = sorted(by_hour.items(),
                        key=lambda kv: sum(kv[1]) / len(kv[1]), reverse=True)
        return "history", [f"{hour:02d}:00" for hour, _ in ranked[:3]]
    return "default", DEFAULT_SLOTS.get(account.platform, ["12:00"])
