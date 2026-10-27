"""评论管理 API：评论列表/人工回复/跳过 + 回复规则 CRUD + 自动回复开关。"""
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..core import audit, ratelimit
from ..database import get_db
from ..models import Agent, CommentRule, MatrixAccount, Post, PostComment
from ..schemas import (CommentReplyIn, CommentRuleIn, CommentRuleOut,
                       PostCommentOut)
from .deps import get_current_agent, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/comments", tags=["comments"])


def _comment_out(db: Session, c: PostComment) -> PostCommentOut:
    post = db.get(Post, c.post_id)
    return PostCommentOut(
        id=c.id, post_id=c.post_id, post_title=(post.title if post else "") or "",
        platform=c.platform, platform_comment_id=c.platform_comment_id,
        author_nickname=c.author_nickname or "", content=c.content or "",
        intent=c.intent or "", status=c.status, reply_content=c.reply_content or "",
        replied_at=c.replied_at, created_at=c.created_at,
    )


# ============ 评论列表与人工处理 ============

@router.get("", response_model=list[PostCommentOut])
def list_comments(status: str = "", intent: str = "", account_id: int = 0,
                  _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    q = db.query(PostComment)
    if status:
        q = q.filter(PostComment.status == status)
    if intent:
        q = q.filter(PostComment.intent == intent)
    if account_id:
        q = q.join(Post, PostComment.post_id == Post.id).filter(Post.account_id == account_id)
    rows = q.order_by(PostComment.id.desc()).limit(200).all()
    return [_comment_out(db, c) for c in rows]


@router.post("/{comment_id}/reply", response_model=PostCommentOut)
async def reply_comment(comment_id: int, req: CommentReplyIn,
                        agent: Agent = Depends(get_current_agent),
                        db: Session = Depends(get_db)):
    """人工回复：与自动回复走同一套出口守卫 + 频控 + 通道（评论区零联系方式铁律）。"""
    from ..comments.engine import _guard_reply, _rate_rule_platform, _today_reply_count
    from ..comments.sender import send_comment_reply

    comment = db.get(PostComment, comment_id)
    if comment is None:
        raise HTTPException(404, "评论不存在")
    if comment.status in ("replied", "manual"):
        raise HTTPException(400, "该评论已回复过")
    text = req.content.strip()
    if not text:
        raise HTTPException(400, "回复内容不能为空")

    ok, text = _guard_reply(text)
    if not ok:
        raise HTTPException(400, "回复内容含违禁词或联系方式（评论区禁止引流词），请修改")

    post = db.get(Post, comment.post_id)
    account = db.get(MatrixAccount, post.account_id) if post else None
    if account is None:
        raise HTTPException(404, "关联账号不存在")

    allowed, rule_name = ratelimit.check_and_count(
        _rate_rule_platform(account), account.account_name)
    if not allowed:
        raise HTTPException(429, f"触发评论频控规则 {rule_name}，请稍后再发")
    if _today_reply_count(db, account.id) >= account.daily_comment_limit:
        raise HTTPException(429, "该账号今日评论回复已达上限")

    try:
        await send_comment_reply(db, account, post, comment, text)
    except Exception as exc:  # noqa: BLE001
        logger.exception("人工评论回复发送失败 comment_id=%s", comment_id)
        raise HTTPException(502, f"发送失败：{exc}")

    comment.status = "manual"
    comment.reply_content = text
    comment.replied_at = datetime.utcnow()
    db.commit()
    db.refresh(comment)
    audit.log(db, agent, "comment_reply", target=f"comment:{comment_id}", detail=text[:80])
    return _comment_out(db, comment)


@router.post("/{comment_id}/skip", response_model=PostCommentOut)
def skip_comment(comment_id: int, agent: Agent = Depends(get_current_agent),
                 db: Session = Depends(get_db)):
    comment = db.get(PostComment, comment_id)
    if comment is None:
        raise HTTPException(404, "评论不存在")
    comment.status = "skipped"
    db.commit()
    db.refresh(comment)
    audit.log(db, agent, "comment_skip", target=f"comment:{comment_id}")
    return _comment_out(db, comment)


# ============ 回复规则 CRUD ============

@router.get("/rules", response_model=list[CommentRuleOut])
def list_rules(_: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    return (db.query(CommentRule)
            .order_by(CommentRule.priority.desc(), CommentRule.id).all())


@router.post("/rules", response_model=CommentRuleOut)
def create_rule(req: CommentRuleIn, agent: Agent = Depends(require_admin),
                db: Session = Depends(get_db)):
    if not req.reply_templates:
        raise HTTPException(400, "至少配置一条回复模板")
    rule = CommentRule(**req.model_dump())
    db.add(rule)
    db.commit()
    db.refresh(rule)
    audit.log(db, agent, "comment_rule_create", target=f"rule:{rule.id}",
              detail=f"intent={rule.intent} code={rule.guide_code}")
    return rule


@router.put("/rules/{rule_id}", response_model=CommentRuleOut)
def update_rule(rule_id: int, req: CommentRuleIn, agent: Agent = Depends(require_admin),
                db: Session = Depends(get_db)):
    rule = db.get(CommentRule, rule_id)
    if rule is None:
        raise HTTPException(404, "规则不存在")
    for field, value in req.model_dump().items():
        setattr(rule, field, value)
    db.commit()
    db.refresh(rule)
    audit.log(db, agent, "comment_rule_update", target=f"rule:{rule_id}")
    return rule


@router.delete("/rules/{rule_id}")
def delete_rule(rule_id: int, agent: Agent = Depends(require_admin),
                db: Session = Depends(get_db)):
    rule = db.get(CommentRule, rule_id)
    if rule is None:
        raise HTTPException(404, "规则不存在")
    db.delete(rule)
    db.commit()
    audit.log(db, agent, "comment_rule_delete", target=f"rule:{rule_id}")
    return {"ok": True}
