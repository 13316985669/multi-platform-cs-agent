"""内容效果归因：选题 → 版本 → 任务 → 作品 聚合播放数据，并关联漏斗事件算内容 ROI。

归因链路：
- comment 层：FunnelEvent.post_id → Post → PublishTask → ContentVersion → ContentItem
- dm/lead/wecom 层：FunnelEvent.account_id + 时间窗近似归因到该账号发布的内容
  （精准归因依赖暗号链路，见 services._apply_guide_code_hook）
"""
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import (ContentItem, ContentVersion, FunnelEvent, MatrixAccount,
                      Post, PublishTask)

FUNNEL_STAGES = ("comment", "dm", "lead", "wecom")


def content_roi(db: Session, days: int = 30, limit: int = 50) -> list[dict]:
    """选题 ROI 排行：每个 ContentItem 聚合发布数/播放/点赞/评论 + 漏斗四级计数。"""
    since = datetime.utcnow() - timedelta(days=days)

    # 1. 作品 → 内容映射 + 播放数据聚合
    posts = db.query(Post).filter(Post.created_at >= since).all()
    task_ids = {p.publish_task_id for p in posts}
    tasks = {t.id: t for t in db.query(PublishTask).filter(PublishTask.id.in_(task_ids))} \
        if task_ids else {}
    version_ids = {t.content_version_id for t in tasks.values()}
    versions = {v.id: v for v in db.query(ContentVersion)
                .filter(ContentVersion.id.in_(version_ids))} if version_ids else {}

    agg: dict[int, dict] = defaultdict(lambda: {
        "posts": 0, "play": 0, "digg": 0, "comment": 0,
        "funnel": {s: 0 for s in FUNNEL_STAGES}, "_post_ids": set(), "_account_ids": set()})

    for post in posts:
        task = tasks.get(post.publish_task_id)
        version = versions.get(task.content_version_id) if task else None
        if version is None:
            continue
        item_id = version.content_item_id
        slot = agg[item_id]
        slot["posts"] += 1
        stats = post.stats_json or {}
        slot["play"] += int(stats.get("play") or 0)
        slot["digg"] += int(stats.get("digg") or 0)
        slot["comment"] += int(stats.get("comment") or 0)
        slot["_post_ids"].add(post.id)
        slot["_account_ids"].add(post.account_id)

    # 2. 漏斗事件归因：comment 按 post_id 精准归因；dm/lead/wecom 按账号近似归因
    events = db.query(FunnelEvent).filter(FunnelEvent.created_at >= since).all()
    for ev in events:
        if ev.stage == "comment" and ev.post_id:
            for item_id, slot in agg.items():
                if ev.post_id in slot["_post_ids"]:
                    slot["funnel"]["comment"] += 1
                    break
        elif ev.stage in ("dm", "lead", "wecom") and ev.account_id:
            # 同账号下时间窗内可能有多个内容：归因到该账号最新有数据的内容
            candidates = [(item_id, slot) for item_id, slot in agg.items()
                          if ev.account_id in slot["_account_ids"]]
            if candidates:
                candidates[0][1]["funnel"][ev.stage] += 1

    # 3. 组装输出
    item_ids = list(agg.keys())
    items = {i.id: i for i in db.query(ContentItem).filter(ContentItem.id.in_(item_ids))} \
        if item_ids else {}
    result = []
    for item_id, slot in agg.items():
        item = items.get(item_id)
        result.append({
            "content_item_id": item_id,
            "title": item.title if item else f"内容#{item_id}",
            "posts": slot["posts"],
            "play": slot["play"],
            "digg": slot["digg"],
            "comment": slot["comment"],
            "funnel": slot["funnel"],
        })
    result.sort(key=lambda r: (r["funnel"]["lead"] + r["funnel"]["wecom"], r["play"]),
                reverse=True)
    return result[:limit]


def account_report(db: Session, days: int = 30) -> list[dict]:
    """账号维度效果报表（Phase 7）：发布数/播放/点赞/评论 + 漏斗 lead/wecom +
    发布成功率 + 健康度 + 画像（粉丝数等），供账号对比 Tab 与分组视图使用。"""
    from ..core.account_health import compute_all  # 延迟导入避免循环依赖

    since = datetime.utcnow() - timedelta(days=days)
    accounts = db.query(MatrixAccount).order_by(MatrixAccount.id).all()
    health_map = compute_all(db)

    # 1. 作品数据按账号聚合
    posts = db.query(Post).filter(Post.created_at >= since).all()
    post_agg: dict[int, dict] = defaultdict(
        lambda: {"posts": 0, "play": 0, "digg": 0, "comment": 0})
    for post in posts:
        slot = post_agg[post.account_id]
        slot["posts"] += 1
        stats = post.stats_json or {}
        slot["play"] += int(stats.get("play") or 0)
        slot["digg"] += int(stats.get("digg") or 0)
        slot["comment"] += int(stats.get("comment") or 0)

    # 2. 发布成功率（窗口内 success/failed）
    task_rows = (
        db.query(PublishTask.account_id, PublishTask.status)
        .filter(PublishTask.updated_at >= since,
                PublishTask.status.in_(["success", "failed"]))
        .all()
    )
    rate_agg: dict[int, list[int]] = defaultdict(lambda: [0, 0])
    for account_id, status in task_rows:
        rate_agg[account_id][0 if status == "success" else 1] += 1

    # 3. 漏斗事件按账号聚合（lead/wecom 两层，账号维度本就精准归因）
    events = db.query(FunnelEvent).filter(
        FunnelEvent.created_at >= since,
        FunnelEvent.stage.in_(["lead", "wecom"])).all()
    funnel_agg: dict[int, dict] = defaultdict(lambda: {"lead": 0, "wecom": 0})
    for ev in events:
        if ev.account_id:
            funnel_agg[ev.account_id][ev.stage] += 1

    result = []
    for acc in accounts:
        pa = post_agg.get(acc.id, {"posts": 0, "play": 0, "digg": 0, "comment": 0})
        success, failed = rate_agg.get(acc.id, [0, 0])
        total = success + failed
        health = health_map.get(acc.id) or {}
        result.append({
            "account_id": acc.id,
            "account_name": acc.account_name,
            "platform": acc.platform,
            "group_name": acc.group_name or "",
            "posts": pa["posts"],
            "play": pa["play"],
            "digg": pa["digg"],
            "comment": pa["comment"],
            "publish_success_rate": round(success / total, 4) if total else None,
            "funnel": funnel_agg.get(acc.id, {"lead": 0, "wecom": 0}),
            "health_level": health.get("level", "good"),
            "health_score": health.get("score", 100),
            "profile": acc.profile_json or {},
        })
    result.sort(key=lambda r: (r["funnel"]["lead"] + r["funnel"]["wecom"], r["play"]),
                reverse=True)
    return result
