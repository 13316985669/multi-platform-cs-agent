"""首评引流：发布成功后延迟 1-3 分钟自动发顶层评论（暗号话术）。

- 触发：publisher/scheduler._mark_success 在版本带 first_comment 时入队（延迟随机 60-180s）
- 守卫：与评论回复同一套出口守卫（违禁词改写 + 引流词零容忍），暗号 {code} 放行
- 频控：计入账号评论日限额 + 平台小时频控（与回复共用额度，防风控）
- 落库：写入 PostComment（author=本账号，status=replied）便于评论页留痕
"""
import logging
import random
from datetime import datetime

from ..core import ratelimit
from ..core.queue import register_handler
from ..database import SessionLocal
from ..models import (CommentRule, MatrixAccount, Post, PostComment, PublishTask,
                      ContentVersion)
from .engine import _guard_reply, _rate_rule_platform, _today_reply_count
from .sender import send_first_comment

logger = logging.getLogger(__name__)


def _pick_guide_code(db, platform: str) -> str:
    """取最高优先级的启用规则暗号作为首评 {code} 替换值。"""
    rule = (
        db.query(CommentRule)
        .filter(CommentRule.enabled.is_(True), CommentRule.guide_code != "",
                CommentRule.platform.in_(["", platform]))
        .order_by(CommentRule.priority.desc(), CommentRule.id)
        .first()
    )
    return rule.guide_code if rule else ""


async def handle_first_comment(payload: dict):
    """队列任务：发首评。payload = {"post_id": int}。"""
    post_id = int(payload.get("post_id") or 0)
    db = SessionLocal()
    try:
        post = db.get(Post, post_id)
        if post is None:
            logger.warning("首评任务：作品 %s 不存在", post_id)
            return
        task = db.get(PublishTask, post.publish_task_id)
        version = db.get(ContentVersion, task.content_version_id) if task else None
        account = db.get(MatrixAccount, post.account_id)
        if version is None or account is None:
            return
        template = (version.first_comment or "").strip()
        if not template:
            return  # 未配置首评话术

        code = _pick_guide_code(db, post.platform)
        if "{code}" in template and not code:
            logger.warning("首评话术含 {code} 但无可用暗号规则，跳过 post=%s", post_id)
            return
        text = template.replace("{code}", code)

        ok, cleaned = _guard_reply(text)
        if not ok:
            logger.warning("首评被出口守卫拦截 post=%s text=%s", post_id, text[:50])
            return

        # 频控：日限额 + 小时窗口（与评论回复共用额度）
        if _today_reply_count(db, account.id) >= account.daily_comment_limit:
            logger.warning("账号 %s 评论日限额已满，首评跳过", account.account_name)
            return
        allowed, _rule = ratelimit.check_and_count(_rate_rule_platform(account),
                                                   account.account_name)
        if not allowed:
            logger.warning("账号 %s 评论小时频控超限，首评跳过", account.account_name)
            return

        await send_first_comment(db, account, post, cleaned)

        db.add(PostComment(
            post_id=post.id, platform=post.platform,
            platform_comment_id=f"self_fc_{post.id}_{int(datetime.utcnow().timestamp())}",
            author_id="self", author_nickname="（本账号首评）",
            content=cleaned, intent="", status="replied",
            reply_content="", replied_at=datetime.utcnow()))
        db.commit()
        logger.info("首评已发送 post=%s", post_id)
    finally:
        db.close()


def enqueue_first_comment(post_id: int) -> None:
    """发布成功钩子：延迟 60-180s 入队（模拟真人行为间隔）。"""
    from ..core.queue import enqueue
    enqueue("first_comment", {"post_id": post_id},
            delay_seconds=random.randint(60, 180))


register_handler("first_comment", handle_first_comment)
