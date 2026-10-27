"""评论回复发送通道：按账号接入方式路由（抖音 API / RPA outbox / Mock）。"""
import json
import logging

from sqlalchemy.orm import Session

from ..models import MatrixAccount, Post, PostComment, RpaOutbox

logger = logging.getLogger(__name__)


async def send_comment_reply(db: Session, account: MatrixAccount, post: Post,
                             comment: PostComment, text: str):
    """发送评论回复。失败抛异常（引擎捕获并保持 pending 可重试）。"""
    if account.platform == "douyin" and account.auth_type == "api":
        from .douyin_api import reply_comment
        await reply_comment(account, post.platform_post_id,
                            comment.platform_comment_id, text)
        return
    if account.auth_type == "rpa":
        # RPA 通道：写 outbox，由评论 Worker 定位评论完成回复
        payload = {
            "kind": "comment_reply",
            "comment_id": comment.platform_comment_id,
            "post_url": post.url or "",
            "author_nickname": comment.author_nickname,
            "text": text,
        }
        db.add(RpaOutbox(
            account=account.rpa_account, platform=account.platform,
            platform_conversation_id="", platform_user_id=comment.author_id,
            msg_type="comment_reply",
            content=json.dumps(payload, ensure_ascii=False),
            question=(comment.content or "")[:500],
        ))
        db.commit()
        logger.info("评论回复入队 RPA outbox account=%s comment=%s",
                    account.rpa_account, comment.platform_comment_id)
        return
    if account.platform == "mock":
        logger.info("[Mock评论回复] post=%s comment=%s text=%s",
                    post.platform_post_id, comment.platform_comment_id, text)
        return
    raise RuntimeError(f"不支持的评论回复通道: platform={account.platform} "
                       f"auth_type={account.auth_type}")


async def send_first_comment(db: Session, account: MatrixAccount, post: Post, text: str):
    """发布成功后发首评（顶层评论，非回复）：暗号引导话术的标准落地位置。"""
    if account.platform == "douyin" and account.auth_type == "api":
        from .douyin_api import publish_comment
        await publish_comment(account, post.platform_post_id, text)
        return
    if account.auth_type == "rpa":
        payload = {
            "kind": "first_comment",
            "post_id": post.id,
            "post_url": post.url or "",
            "text": text,
        }
        db.add(RpaOutbox(
            account=account.rpa_account, platform=account.platform,
            platform_conversation_id="", platform_user_id="",
            msg_type="first_comment",
            content=json.dumps(payload, ensure_ascii=False),
        ))
        db.commit()
        logger.info("首评入队 RPA outbox account=%s post=%s", account.rpa_account, post.id)
        return
    if account.platform == "mock":
        logger.info("[Mock首评] post=%s text=%s", post.platform_post_id, text)
        return
    raise RuntimeError(f"不支持的首评通道: platform={account.platform} "
                       f"auth_type={account.auth_type}")
