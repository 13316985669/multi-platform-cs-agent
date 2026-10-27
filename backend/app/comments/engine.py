"""评论引流引擎：采集入库 → 意图分类 → 规则/LLM 回复 → 频控 → 通道发送 → 漏斗归因。

铁律：评论区零联系方式。所有自动回复过 违禁词 + 引流词 双重扫描，命中即转人工。
自动回复总开关 comment_auto_reply_enabled（默认关）：关闭时评论照常采集与分类，
但不自动回复，全部留在待处理由人工回复。
"""
import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from ..config import settings
from ..core import contentfilter, ratelimit
from ..core.queue import register_handler
from ..creator.contracts import CommentIntent
from ..creator.llm import call_llm_json
from ..database import SessionLocal
from ..models import (ContentItem, ContentVersion, FunnelEvent, MatrixAccount, Post,
                      PostComment, PublishTask)
from .rules import match_rule, render_reply
from .sender import send_comment_reply

logger = logging.getLogger(__name__)

INTENT_LABELS = {
    "consult": "咨询", "price": "问价", "praise": "好评",
    "complaint": "差评", "spam": "广告", "irrelevant": "无关",
}


async def _classify_intent(content: str) -> CommentIntent:
    """LLM 意图分类；未配置/失败降级 irrelevant（不自动回复，转人工判断）。"""
    prompt = (
        "你是评论意图分类器。判断以下社交媒体评论的意图。\n\n"
        f"【评论】\n{content}\n\n"
        "只输出 JSON：{\"intent\": \"consult|price|praise|complaint|spam|irrelevant 六选一\", "
        "\"confidence\": 0.0到1.0}\n"
        "- consult：咨询产品/服务/购买方式\n- price：问价格\n- praise：好评夸赞\n"
        "- complaint：差评/投诉/负面\n- spam：广告/同行/无关推广\n- irrelevant：其他无关内容"
    )
    result = await call_llm_json(prompt, CommentIntent)
    return result or CommentIntent(intent="irrelevant", confidence=0.0)


async def _knowledge_snippets(content: str) -> str:
    """检索失败或未命中时返回空串，调用方继续用短回复。"""
    try:
        from ..rag.pipeline import retrieve
        result = await retrieve(content, top_k=2)
    except Exception:  # noqa: BLE001
        logger.exception("评论回复检索知识库失败，改用短回复")
        return ""
    if not result.passed:
        return ""
    lines = []
    for item in (result.contexts or [])[:2]:
        text = (item.get("content") or "").strip()
        if text:
            lines.append(text[:300])
    return "\n".join(lines)


async def _generate_reply(content: str, intent: str) -> str:
    """无规则命中时的 LLM 兜底生成（仅开关开启时调用）。

    硬约束写进提示词：只引导私信，禁止任何联系方式（评论区零联系方式铁律）。
    知识库只提供事实，检索失败时不中断，仍按短回复生成。
    """
    snippets = await _knowledge_snippets(content)
    knowledge = f"\n【可参考资料】\n{snippets}\n" if snippets else ""
    prompt = (
        "你是品牌账号的运营小编，请回复以下用户评论。\n\n"
        f"【评论】\n{content}\n【意图】{INTENT_LABELS.get(intent, intent)}\n"
        f"{knowledge}\n"
        "要求：\n"
        "1. 口语化、友好，不超过 30 字\n"
        "2. 有咨询/购买意向的，引导用户「私信」进一步沟通\n"
        "3. 严禁出现任何联系方式或站外引导词：微信/加V/薇/vx/二维码/手机号/QQ 等及其变体\n"
        "4. 资料里的联系方式不要抄进评论\n"
        "5. 不夸大、不承诺功效、不用极限词\n\n"
        "只输出 JSON：{\"reply\": \"回复文本\"}"
    )
    from pydantic import BaseModel

    class _Reply(BaseModel):
        reply: str = ""

    result = await call_llm_json(prompt, _Reply)
    return (result.reply if result else "").strip()


def _guard_reply(text: str) -> tuple[bool, str]:
    """回复出口守卫：违禁词安全改写 + 引流词零容忍。返回 (可发送, 处理后文本)。"""
    cleaned, _ = contentfilter.sanitize(text)
    drain_hits = contentfilter.scan_drain(cleaned)
    if drain_hits:
        logger.warning("评论回复命中引流词 %s，转人工: %s", drain_hits, cleaned[:50])
        return False, cleaned
    if not cleaned:
        return False, ""
    return True, cleaned


def _rate_rule_platform(account: MatrixAccount) -> str:
    if account.platform == "douyin":
        return "douyin_comment" if account.auth_type == "api" else "douyin_rpa_comment"
    if account.platform == "xiaohongshu":
        return "xiaohongshu_rpa_comment"
    return "mock_comment"


def _ensure_external_post(db, platform: str, rpa_account: str,
                          platform_post_id: str, post_url: str) -> Post | None:
    """不是本系统发布的作品：按账号登记一条，好让评论能继续回复。

    posts.publish_task_id 不能为空，所以每个账号复用一条 external 任务和一条归档内容。
    """
    account_key = (rpa_account or "").strip()
    if not account_key or not platform:
        return None
    account = (
        db.query(MatrixAccount)
        .filter(MatrixAccount.rpa_account == account_key,
                MatrixAccount.platform == platform,
                MatrixAccount.status == "active")
        .first()
    )
    if account is None:
        logger.warning("外部评论找不到矩阵账号 account=%s platform=%s", account_key, platform)
        return None
    item = (
        db.query(ContentItem)
        .filter(ContentItem.topic == "外部登记", ContentItem.title == f"外部作品@{account.id}")
        .first()
    )
    if item is None:
        item = ContentItem(
            title=f"外部作品@{account.id}", topic="外部登记", status="archived",
            selling_points=[],
        )
        db.add(item)
        db.flush()
    version = (
        db.query(ContentVersion)
        .filter(ContentVersion.content_item_id == item.id)
        .first()
    )
    if version is None:
        version = ContentVersion(
            content_item_id=item.id, platform=platform, title="外部作品", body="",
        )
        db.add(version)
        db.flush()
    task = (
        db.query(PublishTask)
        .filter(PublishTask.account_id == account.id, PublishTask.status == "external")
        .first()
    )
    if task is None:
        task = PublishTask(
            content_version_id=version.id, account_id=account.id,
            scheduled_at=datetime.utcnow(), status="external",
        )
        db.add(task)
        db.flush()
    post = Post(
        publish_task_id=task.id, account_id=account.id, platform=platform,
        platform_post_id=platform_post_id or "", url=post_url or "", title="外部作品",
    )
    db.add(post)
    db.flush()
    logger.info("已登记外部作品 account=%s post_id=%s", account_key, platform_post_id or post_url)
    return post


def _today_reply_count(db, account_id: int) -> int:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(PostComment)
        .join(Post, PostComment.post_id == Post.id)
        .filter(Post.account_id == account_id,
                PostComment.status.in_(["replied", "manual"]),
                PostComment.replied_at >= today_start)
        .count()
    )


async def handle_inbound_comment(payload: dict):
    """队列任务：评论入站处理。payload 字段见 webhooks/rpa 分流处。

    幂等：platform_comment_id 唯一索引 + IntegrityError 捕获，重复投递直接跳过。
    """
    db = SessionLocal()
    try:
        platform = payload.get("platform", "")
        platform_comment_id = payload.get("platform_comment_id", "")
        if not platform or not platform_comment_id:
            logger.warning("评论 payload 缺 platform/platform_comment_id: %s", payload)
            return

        # 关联 Post（platform_post_id 反查；RPA 通道兜底按 URL 匹配）
        post = None
        if payload.get("platform_post_id"):
            post = db.query(Post).filter(
                Post.platform_post_id == payload["platform_post_id"]).first()
        if post is None and payload.get("post_url"):
            post = db.query(Post).filter(Post.url == payload["post_url"]).first()
        if post is None:
            post = _ensure_external_post(
                db, platform, payload.get("account", ""),
                payload.get("platform_post_id", ""), payload.get("post_url", ""),
            )
        if post is None:
            logger.warning("评论关联不到已发布作品（post_id=%s url=%s），丢弃",
                           payload.get("platform_post_id"), payload.get("post_url"))
            return

        comment = PostComment(
            post_id=post.id, platform=platform,
            platform_comment_id=platform_comment_id,
            parent_comment_id=payload.get("parent_comment_id", ""),
            author_id=payload.get("author_id", ""),
            author_nickname=payload.get("author_nickname", ""),
            content=payload.get("content", ""),
        )
        db.add(comment)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return  # 重复投递，幂等跳过
        db.refresh(comment)

        # 漏斗：评论采集
        db.add(FunnelEvent(stage="comment", platform=platform, account_id=post.account_id,
                           post_id=post.id, comment_id=comment.id))
        db.commit()

        # 意图分类
        intent = await _classify_intent(comment.content)
        comment.intent = intent.intent
        db.commit()

        # 差评/广告：不自动回复，WS 提醒人工
        if intent.intent in ("complaint", "spam"):
            comment.status = "skipped"
            db.commit()
            await _broadcast_comment(comment, need_human=True)
            return

        # 自动回复总开关：关闭时仅采集分类，全部留待人工
        if not settings.comment_auto_reply_enabled:
            await _broadcast_comment(comment)
            return

        account = db.get(MatrixAccount, post.account_id)
        if account is None or account.status != "active":
            comment.status = "pending"
            db.commit()
            return

        # 规则匹配 → 模板回复；无规则 → LLM 兜底生成
        reply_text, guide_code = "", ""
        rule = match_rule(db, comment.content, platform, intent.intent)
        if rule is not None:
            reply_text, guide_code = render_reply(rule)
        else:
            reply_text = await _generate_reply(comment.content, intent.intent)
        if not reply_text:
            await _broadcast_comment(comment)
            return

        # 出口守卫：违禁词改写 + 引流词零容忍
        ok, reply_text = _guard_reply(reply_text)
        if not ok:
            await _broadcast_comment(comment, need_human=True)
            return

        # 频控：平台规则 + 账号日上限
        allowed, rule_name = ratelimit.check_and_count(
            _rate_rule_platform(account), account.account_name)
        if not allowed:
            logger.info("评论回复触发频控 %s，留待人工: comment_id=%s", rule_name, comment.id)
            await _broadcast_comment(comment)
            return
        if _today_reply_count(db, account.id) >= account.daily_comment_limit:
            logger.info("账号 %s 当日评论回复已达上限", account.account_name)
            await _broadcast_comment(comment)
            return

        # 通道发送
        try:
            await send_comment_reply(db, account, post, comment, reply_text)
        except Exception as exc:  # noqa: BLE001
            logger.exception("评论回复发送失败 comment_id=%s", comment.id)
            comment.status = "pending"
            comment.reply_content = ""
            db.commit()
            return

        comment.status = "replied"
        comment.reply_content = reply_text
        comment.replied_at = datetime.utcnow()
        db.commit()
        await _broadcast_comment(comment)
        logger.info("评论自动回复成功 comment_id=%s intent=%s", comment.id, intent.intent)
    finally:
        db.close()


async def _broadcast_comment(comment: PostComment, need_human: bool = False):
    try:
        from ..api.ws import manager
        await manager.broadcast("comment_new", {
            "comment_id": comment.id, "post_id": comment.post_id,
            "status": comment.status, "intent": comment.intent,
            "need_human": need_human,
        })
    except Exception:  # noqa: BLE001
        logger.exception("评论广播失败")


register_handler("inbound_comment", handle_inbound_comment)
