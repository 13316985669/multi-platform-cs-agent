"""作品数据回采：发布后的播放/点赞/评论/分享/收藏定期采集。

通道路由：
- 抖音 API 账号：调视频数据接口（token 自动刷新复用发布通道实现）
- RPA 账号（小红书/抖音企业号）：写 rpa_outbox msg_type=collect_stats，Worker 回执 JSON 对账
- Mock 通道：合成确定性增长数据（演示/测试全链路用）

每次回采写 PostStatSnapshot（时间序列）并刷新 Post.stats_json（最新快照）。
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta

from ..core import http, monitor
from ..database import SessionLocal
from ..models import MatrixAccount, Post, PostStatSnapshot, RpaOutbox

logger = logging.getLogger(__name__)

COLLECT_INTERVAL_SECONDS = 4 * 3600   # 每 4 小时一轮
POST_MAX_AGE_DAYS = 7                  # 发布超过 7 天停止回采（数据已稳定）
SWEEP_BATCH = 30                       # 每轮最多处理作品数（防爆量）

# TODO: 确认实际接口地址（抖音「视频数据」能力，需单独申请 data.external.item 权限）
DOUYIN_ITEM_DATA_URL = "https://open.douyin.com/api/douyin/v1/video/data/"
# TODO: 确认实际接口地址（抖音「用户数据」能力，需 data.external.user 权限）
DOUYIN_USER_DATA_URL = "https://open.douyin.com/api/douyin/v1/user/data/"


def _save_stats(db, post: Post, stats: dict) -> None:
    """统一落库：最新快照写 posts.stats_json，同时追加时间序列快照。"""
    normalized = {
        "play": int(stats.get("play") or 0),
        "digg": int(stats.get("digg") or 0),
        "comment": int(stats.get("comment") or 0),
        "share": int(stats.get("share") or 0),
        "collect": int(stats.get("collect") or 0),
    }
    post.stats_json = normalized
    post.stats_updated_at = datetime.utcnow()
    db.add(PostStatSnapshot(post_id=post.id, captured_at=post.stats_updated_at,
                            **normalized))


async def _collect_douyin(db, post: Post, account: MatrixAccount) -> None:
    """抖音 API 回采：token 失效自动刷新一次后重试。"""
    from ..publisher.channels.douyin_api import DouyinApiPublishChannel

    channel = DouyinApiPublishChannel()
    token = await channel._valid_token(db, account)
    data = await http.post_json(DOUYIN_ITEM_DATA_URL,
                                params={"access_token": token, "open_id": account.open_id},
                                json_body={"item_id": post.platform_post_id})
    inner = data.get("data") or {}
    if inner.get("error_code", 0) not in (0, None):
        raise RuntimeError(f"视频数据接口错误: {inner.get('error_code')} "
                           f"{inner.get('description')}")
    # TODO: 确认实际接口地址（返回字段名以联调为准，以下为常见命名）
    stat = inner.get("statistics") or inner.get("stats") or {}
    _save_stats(db, post, {
        "play": stat.get("play_count") or stat.get("play") or 0,
        "digg": stat.get("digg_count") or stat.get("digg") or 0,
        "comment": stat.get("comment_count") or stat.get("comment") or 0,
        "share": stat.get("share_count") or stat.get("share") or 0,
        "collect": stat.get("collect_count") or stat.get("collect") or 0,
    })


def _mock_stats(post: Post) -> dict:
    """Mock 通道合成数据：按作品年龄确定性增长（同一作品多次回采单调递增）。"""
    age_hours = max(1, int((datetime.utcnow() - post.created_at).total_seconds() // 3600))
    seed = sum(ord(c) for c in post.platform_post_id) % 50 + 10  # 每作品不同基数
    play = seed * age_hours
    return {
        "play": play,
        "digg": play // 20,
        "comment": play // 80,
        "share": play // 50,
        "collect": play // 35,
    }


def _enqueue_rpa_collect(db, post: Post, account: MatrixAccount) -> None:
    """RPA 回采：写 outbox，Worker 页面读取数据后 ack 回传 JSON。"""
    payload = json.dumps({"post_id": post.id, "url": post.url,
                          "platform_post_id": post.platform_post_id},
                         ensure_ascii=False)
    # 同一作品已有未完结采集单则跳过（防重复堆积）
    existing = db.query(RpaOutbox).filter(
        RpaOutbox.msg_type == "collect_stats",
        RpaOutbox.account == account.rpa_account,
        RpaOutbox.content == payload,
        RpaOutbox.status.in_(["pending", "leased"])).count()
    if existing:
        return
    db.add(RpaOutbox(account=account.rpa_account, platform=account.platform,
                     content=payload, msg_type="collect_stats"))


async def collect_due_stats() -> int:
    """扫描一轮到期作品并回采。返回处理数（含入队 RPA 的）。"""
    db = SessionLocal()
    processed = 0
    try:
        since = datetime.utcnow() - timedelta(days=POST_MAX_AGE_DAYS)
        posts = (
            db.query(Post)
            .filter(Post.created_at >= since)
            .order_by(Post.stats_updated_at.is_(None).desc(), Post.stats_updated_at)
            .limit(SWEEP_BATCH)
            .all()
        )
        for post in posts:
            account = db.get(MatrixAccount, post.account_id)
            if account is None or account.status != "active":
                continue
            try:
                if post.platform == "mock":
                    _save_stats(db, post, _mock_stats(post))
                elif account.auth_type == "api" and post.platform == "douyin":
                    await _collect_douyin(db, post, account)
                elif account.auth_type == "rpa" and account.rpa_account:
                    _enqueue_rpa_collect(db, post, account)
                else:
                    continue
                processed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("作品 %s 数据回采失败: %s", post.id, exc)
                monitor.record("stats_collect_failure",
                               f"post={post.id} {exc}"[:180])
        db.commit()
    finally:
        db.close()
    return processed


async def reconcile_stats_outbox() -> int:
    """RPA 回采对账：acked 的 collect_stats / collect_account 单解析 result JSON 落库，置 consumed。"""
    db = SessionLocal()
    consumed = 0
    try:
        rows = db.query(RpaOutbox).filter(
            RpaOutbox.msg_type.in_(["collect_stats", "collect_account"]),
            RpaOutbox.status == "acked").limit(50).all()
        for row in rows:
            try:
                payload = json.loads(row.content or "{}")
                result = json.loads(row.result or "{}")
                if row.msg_type == "collect_account":
                    account = db.get(MatrixAccount, int(payload.get("account_id") or 0))
                    if account is not None and result:
                        _save_profile(db, account, result)
                        consumed += 1
                else:
                    post = db.get(Post, int(payload.get("post_id") or 0))
                    if post is not None and result:
                        _save_stats(db, post, result)
                        consumed += 1
            except (ValueError, TypeError) as exc:
                logger.warning("%s 回执解析失败 outbox=%s: %s", row.msg_type, row.id, exc)
            row.status = "consumed"
        if rows:
            db.commit()
    finally:
        db.close()
    return consumed


# ============ 账号画像回采（Phase 7：粉丝数/作品数/获赞数） ============

def _save_profile(db, account: MatrixAccount, profile: dict) -> None:
    """账号画像落库（profile_json），供账号卡片与效果报表展示。"""
    account.profile_json = {
        "followers": int(profile.get("followers") or 0),
        "works": int(profile.get("works") or 0),
        "liked": int(profile.get("liked") or 0),
        "updated_at": datetime.utcnow().isoformat(),
    }


async def _collect_profile_douyin(db, account: MatrixAccount) -> None:
    """抖音 API 画像回采：token 失效自动刷新一次后重试（复用发布通道实现）。"""
    from ..publisher.channels.douyin_api import DouyinApiPublishChannel

    channel = DouyinApiPublishChannel()
    token = await channel._valid_token(db, account)
    data = await http.post_json(DOUYIN_USER_DATA_URL,
                                params={"access_token": token, "open_id": account.open_id},
                                json_body={})
    inner = data.get("data") or {}
    if inner.get("error_code", 0) not in (0, None):
        raise RuntimeError(f"用户数据接口错误: {inner.get('error_code')} "
                           f"{inner.get('description')}")
    # TODO: 确认实际接口地址（返回字段名以联调为准，以下为常见命名）
    _save_profile(db, account, {
        "followers": inner.get("fans_count") or inner.get("follower_count") or 0,
        "works": inner.get("aweme_count") or inner.get("works_count") or 0,
        "liked": inner.get("total_favorited") or inner.get("favoriting_count") or 0,
    })


def _mock_profile(account: MatrixAccount) -> dict:
    """Mock 通道合成画像：按账号名确定性生成（演示/测试全链路用）。"""
    seed = sum(ord(c) for c in account.account_name) % 500 + 100
    return {"followers": seed * 7, "works": seed // 3 + 5, "liked": seed * 21}


def _enqueue_rpa_profile_collect(db, account: MatrixAccount) -> None:
    """RPA 画像回采：写 outbox（msg_type=collect_account），Worker 打开主页抓取后回执。"""
    payload = json.dumps({"account_id": account.id}, ensure_ascii=False)
    existing = db.query(RpaOutbox).filter(
        RpaOutbox.msg_type == "collect_account",
        RpaOutbox.account == account.rpa_account,
        RpaOutbox.status.in_(["pending", "leased"])).count()
    if existing:
        return
    db.add(RpaOutbox(account=account.rpa_account, platform=account.platform,
                     content=payload, msg_type="collect_account"))


async def refresh_account_profiles() -> int:
    """账号画像回采：active 账号逐路由（Mock 合成 / 抖音 API / RPA outbox）。"""
    db = SessionLocal()
    processed = 0
    try:
        accounts = db.query(MatrixAccount).filter(MatrixAccount.status == "active").all()
        for account in accounts:
            try:
                if account.platform == "mock":
                    _save_profile(db, account, _mock_profile(account))
                elif (account.auth_type == "api" and account.platform == "douyin"
                      and account.credentials_enc):
                    await _collect_profile_douyin(db, account)
                elif account.auth_type == "rpa" and account.rpa_account:
                    _enqueue_rpa_profile_collect(db, account)
                else:
                    continue
                processed += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("账号 %s 画像回采失败: %s", account.id, exc)
                monitor.record("stats_collect_failure", f"profile account={account.id}"[:180])
        db.commit()
    finally:
        db.close()
    return processed


async def stats_collection_loop():
    """后台周期任务：每 4h 回采 + 每轮顺带对账与账号画像刷新（main.py lifespan 注册）。"""
    while True:
        try:
            await reconcile_stats_outbox()
            await collect_due_stats()
            await refresh_account_profiles()
        except Exception:  # noqa: BLE001
            logger.exception("数据回采轮次失败")
        await asyncio.sleep(COLLECT_INTERVAL_SECONDS)
