"""抖音评论客户端：item.comment 权限（仅能回复授权账号自己视频的评论；图集不支持评论）。

轮询兜底：webhook 事件不可靠/未开通时，每 5 分钟遍历 active 账号近期 Post 拉新评论入队。
# TODO: 确认实际接口地址（评论事件 webhook 的事件名与字段，联调用真实推送校准 normalize）
"""
import asyncio
import logging
from datetime import datetime, timedelta

from ..core import crypto
from ..core.http import get_json, post_json
from ..core.queue import enqueue
from ..database import SessionLocal
from ..models import MatrixAccount, Post

logger = logging.getLogger(__name__)

# TODO: 确认实际接口地址（评论列表/回复接口路径联调时对照官方文档核对）
COMMENT_LIST_URL = "https://open.douyin.com/item/comment/list/"
COMMENT_REPLY_URL = "https://open.douyin.com/item/comment/reply/"
# TODO: 确认实际接口地址（顶层评论发布接口，联调核对；部分账号体系仅支持回复不支持主动评论）
COMMENT_PUBLISH_URL = "https://open.douyin.com/item/comment/publish/"

POLL_INTERVAL_SECONDS = 300  # 5 分钟
POLL_POST_DAYS = 7           # 只拉最近 7 天发布的作品评论


def _access_token(account: MatrixAccount) -> str:
    creds = crypto.decrypt_json(account.credentials_enc)
    token = creds.get("access_token", "")
    if not token:
        raise RuntimeError(f"账号 {account.account_name} 未授权或凭证缺失")
    return token


async def list_comments(account: MatrixAccount, item_id: str,
                        cursor: int = 0, count: int = 20) -> dict:
    """拉取作品评论列表。返回平台原始 data（含 comments/list、cursor、has_more）。"""
    data = await get_json(COMMENT_LIST_URL, params={
        "open_id": account.open_id, "item_id": item_id,
        "cursor": cursor, "count": count,
    }, headers={"access-token": _access_token(account)})
    inner = data.get("data") or {}
    err = inner.get("error_code", data.get("error_code", 0))
    if err not in (0, None):
        raise RuntimeError(f"拉取评论失败: {data}")
    return inner


async def reply_comment(account: MatrixAccount, item_id: str, comment_id: str,
                        content: str) -> dict:
    """回复评论。错误码非 0 抛异常。"""
    data = await post_json(COMMENT_REPLY_URL,
                           json_body={"open_id": account.open_id, "item_id": item_id,
                                      "comment_id": comment_id, "content": content},
                           headers={"access-token": _access_token(account)})
    inner = data.get("data") or {}
    err = inner.get("error_code", data.get("error_code", 0))
    if err not in (0, None):
        raise RuntimeError(f"评论回复失败: {data}")
    return inner


async def publish_comment(account: MatrixAccount, item_id: str, content: str) -> dict:
    """发布顶层评论（首评引流用）。错误码非 0 抛异常。"""
    data = await post_json(COMMENT_PUBLISH_URL,
                           json_body={"open_id": account.open_id, "item_id": item_id,
                                      "content": content},
                           headers={"access-token": _access_token(account)})
    inner = data.get("data") or {}
    err = inner.get("error_code", data.get("error_code", 0))
    if err not in (0, None):
        raise RuntimeError(f"首评发布失败: {data}")
    return inner


async def poll_account_comments(db, account: MatrixAccount):
    """拉取一个账号近期作品的新评论并入队（幂等由引擎 platform_comment_id 保证）。"""
    since = datetime.utcnow() - timedelta(days=POLL_POST_DAYS)
    posts = (
        db.query(Post)
        .filter(Post.account_id == account.id, Post.created_at >= since,
                Post.platform_post_id != "", ~Post.platform_post_id.like("outbox:%"),
                ~Post.platform_post_id.like("mock_%"))
        .limit(20)
        .all()
    )
    for post in posts:
        try:
            data = await list_comments(account, post.platform_post_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("拉取评论失败 account=%s post=%s: %s",
                           account.account_name, post.platform_post_id, exc)
            continue
        # TODO: 确认实际接口地址（评论列表字段名联调校准：comments/list）
        for c in (data.get("comments") or data.get("list") or []):
            enqueue("inbound_comment", {
                "platform": "douyin",
                "platform_post_id": post.platform_post_id,
                "platform_comment_id": str(c.get("comment_id", "")),
                "parent_comment_id": str(c.get("reply_to_comment_id", "") or ""),
                "author_id": str(c.get("user_id", "") or c.get("open_id", "")),
                "author_nickname": c.get("nickname", ""),
                "content": c.get("content", ""),
            })


async def comment_polling_loop():
    """评论轮询兜底：每 5 分钟遍历 active 抖音 API 账号（main.py lifespan 注册）。"""
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        try:
            db = SessionLocal()
            try:
                accounts = (
                    db.query(MatrixAccount)
                    .filter(MatrixAccount.platform == "douyin",
                            MatrixAccount.auth_type == "api",
                            MatrixAccount.status == "active")
                    .all()
                )
                for account in accounts:
                    await poll_account_comments(db, account)
            finally:
                db.close()
        except Exception:  # noqa: BLE001
            logger.exception("评论轮询失败")
