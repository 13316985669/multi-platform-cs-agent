"""RPA Worker 桥接 API：incoming / media / outbox / ack / heartbeat / identity。

鉴权：Worker 接口走 X-Rpa-Key 头（比对 RPA_API_KEY）；/workers 管理接口走 JWT。
协议细节见 docs/rpa-workers.md。
"""
import hmac
import json
import logging
import os
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import and_, case, exists, func, literal, or_
from sqlalchemy.orm import Session

from ..config import settings
from ..core import audit, monitor
from ..core.queue import enqueue
from ..database import get_db
from ..bindings import driver_spec, is_enabled, resolve_douyin_account_type
from ..models import (AccountBinding, Agent, Conversation, Customer, MatrixAccount, Message,
                      PostComment, RpaIdentityMap, RpaMedia, RpaOutbox, RpaWorker)
from ..schemas import (RpaAck, RpaHeartbeat, RpaIncoming, RpaIncomingComment,
                       RpaIdentityResolve, RpaRelease, RpaSelfIdentity, RpaWorkerOut)
from .deps import get_current_agent

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/rpa", tags=["rpa"])

MEDIA_MAX_BYTES = 20 * 1024 * 1024  # 20MB
MEDIA_MIME_PREFIXES = ("image/", "audio/")


# ============ 鉴权 ============

async def rpa_key_dep(x_rpa_key: str = Header(default="")) -> None:
    """Worker 接口鉴权：X-Rpa-Key 头比对 RPA_API_KEY（未配置则 RPA 通道整体不可用）。"""
    if not settings.rpa_api_key:
        raise HTTPException(503, "RPA 通道未启用（后端未配置 RPA_API_KEY）")
    if not x_rpa_key or not hmac.compare_digest(x_rpa_key, settings.rpa_api_key):
        raise HTTPException(401, "RPA key 无效")


# ============ 身份映射 ============

def resolve_identity(db: Session, account: str, platform: str, nickname: str,
                     prev_nickname: str = "") -> str:
    """昵称 → 稳定内部 ID。同昵称复用；prev_nickname 命中时迁移映射（改昵称不断会话）。"""
    nickname = nickname or "未知用户"
    row = (
        db.query(RpaIdentityMap)
        .filter(RpaIdentityMap.account == account, RpaIdentityMap.platform == platform,
                RpaIdentityMap.nickname == nickname)
        .first()
    )
    if row is not None:
        row.last_seen_at = datetime.utcnow()
        db.commit()
        return row.stable_user_id

    if prev_nickname:
        old = (
            db.query(RpaIdentityMap)
            .filter(RpaIdentityMap.account == account, RpaIdentityMap.platform == platform,
                    RpaIdentityMap.nickname == prev_nickname)
            .first()
        )
        if old is not None:
            stable = old.stable_user_id
            old.nickname = nickname  # 迁移到新昵称
            old.last_seen_at = datetime.utcnow()
            db.commit()
            logger.info("RPA 身份迁移 account=%s %s → %s (%s)", account, prev_nickname, nickname, stable)
            return stable

    stable = f"rpa_{account}_{uuid.uuid4().hex[:10]}"
    db.add(RpaIdentityMap(account=account, platform=platform, nickname=nickname,
                          stable_user_id=stable))
    db.commit()
    return stable


# ============ 入站 ============

@router.post("/incoming")
def rpa_incoming(req: RpaIncoming, db: Session = Depends(get_db),
                 _: None = Depends(rpa_key_dep)):
    """Worker 上报入站消息。身份解析后入队，复用现有幂等/合规/AI 链路。"""
    stable_user_id = resolve_identity(db, req.account, req.platform, req.nickname,
                                      req.prev_nickname)
    msg_id = req.msg_id or f"rpa_{uuid.uuid4().hex[:16]}"
    conv_id = req.conversation_id or f"conv_{stable_user_id}"
    payload = {
        "channel": "rpa",
        "account": req.account,
        "platform": req.platform,
        "user_id": stable_user_id,
        "nickname": req.nickname,
        "content": req.content,
        "msg_type": req.msg_type,
        "media_id": req.media_id,
        "msg_id": msg_id,
        "conversation_id": conv_id,
        "sender_side": req.sender_side,
    }
    enqueue("inbound_message", payload)
    return {"ok": True, "user_id": stable_user_id}


@router.post("/incoming_comment")
def rpa_incoming_comment(req: RpaIncomingComment, _: None = Depends(rpa_key_dep)):
    """Worker 上报作品评论：入队评论引擎（幂等由 platform_comment_id 唯一索引保证）。"""
    if not req.comment_id.strip():
        raise HTTPException(400, "comment_id 不能为空（幂等键）")
    enqueue("inbound_comment", {
        "account": req.account,
        "platform": req.platform,
        "platform_post_id": req.platform_post_id,
        "post_url": req.post_url,
        "platform_comment_id": req.comment_id.strip(),
        "parent_comment_id": req.parent_comment_id,
        "author_id": req.author_id,
        "author_nickname": req.author_nickname,
        "content": req.content,
    })
    return {"ok": True}


# ============ 媒体上下行 ============

def _save_media(db: Session, file: UploadFile, kind: str) -> RpaMedia:
    mime = file.content_type or "application/octet-stream"
    if not mime.startswith(MEDIA_MIME_PREFIXES):
        raise HTTPException(400, f"不支持的媒体类型: {mime}")
    media_id = uuid.uuid4().hex
    ext = os.path.splitext(file.filename or "")[1][:10] or ".bin"
    dir_path = os.path.join(settings.upload_dir, "rpa")
    os.makedirs(dir_path, exist_ok=True)
    path = os.path.join(dir_path, f"{media_id}{ext}")
    data = file.file.read(MEDIA_MAX_BYTES + 1)
    if len(data) > MEDIA_MAX_BYTES:
        raise HTTPException(413, "媒体文件超过 20MB 限制")
    with open(path, "wb") as f:
        f.write(data)
    row = RpaMedia(id=media_id, kind=kind if kind in ("image", "voice") else "image",
                   path=path, mime=mime, size=len(data))
    db.add(row)
    db.commit()
    return row


@router.post("/media")
def rpa_upload_media(file: UploadFile, kind: str = "image", db: Session = Depends(get_db),
                     _: None = Depends(rpa_key_dep)):
    """Worker 上传媒体（用户发来的图片/语音）。"""
    row = _save_media(db, file, kind)
    return {"media_id": row.id}


@router.get("/media/{media_id}")
def rpa_download_media(media_id: str, db: Session = Depends(get_db),
                       _: None = Depends(rpa_key_dep)):
    """Worker 下载待发媒体（outbox 中的图片/语音）。"""
    row = db.get(RpaMedia, media_id)
    if row is None or not os.path.exists(row.path):
        raise HTTPException(404, "媒体不存在")
    return FileResponse(row.path, media_type=row.mime)


@router.get("/material/{material_id}")
def rpa_download_material(material_id: int, db: Session = Depends(get_db),
                          _: None = Depends(rpa_key_dep)):
    """Worker 下载内容素材（发布任务的图片/视频，对应 materials 表）。"""
    from ..models import Material
    row = db.get(Material, material_id)
    if row is None or not os.path.exists(row.path):
        raise HTTPException(404, "素材不存在")
    return FileResponse(row.path, media_type=row.mime)


# ============ 出站（outbox 拉取 + 回执） ============

def _outbox_is_laike(db: Session, row: RpaOutbox) -> bool:
    """来客出站。平台字段仍是 douyin，按账号形态判断。"""
    if (row.platform or "") != "douyin":
        return False
    account = db.query(MatrixAccount).filter(MatrixAccount.rpa_account == row.account).first()
    return resolve_douyin_account_type(account) == "laike"


def _reap_expired_leases(db: Session):
    """租约超时未 ack 的消息回滚为 pending。来客已经发出过一次就停在 failed，不再自动重发。"""
    cutoff = datetime.utcnow() - timedelta(seconds=settings.rpa_outbox_lease_seconds)
    expired = (
        db.query(RpaOutbox)
        .filter(RpaOutbox.status == "leased", RpaOutbox.leased_at < cutoff)
        .all()
    )
    for row in expired:
        if _outbox_is_laike(db, row) and (row.attempts or 0) > 0:
            row.status = "failed"
            row.error = (row.error or "租约过期，来客不再自动重发")[:500]
        else:
            row.status = "pending"
        row.worker_id = ""
    if expired:
        db.commit()


@router.get("/assignment")
def assignment(worker_id: str, db: Session = Depends(get_db), _: None = Depends(rpa_key_dep)):
    """Worker 领取自己的一条绑定。没有启用绑定时 bound=false，由本机旧配置决定是否继续跑。"""
    rows = (
        db.query(AccountBinding)
        .filter(AccountBinding.worker_id == worker_id)
        .all()
    )
    enabled = [row for row in rows if is_enabled(row.auth_status)]
    if len(enabled) > 1:
        raise HTTPException(409, f"Worker {worker_id} 有多条启用绑定，请先在授权页停用多余的")
    if not enabled:
        return {"bound": False}
    row = enabled[0]
    spec = driver_spec(row.driver) or {}
    account = db.get(MatrixAccount, row.account_id)
    return {
        "bound": True,
        "binding_id": row.id,
        "driver": row.driver,
        "duty": row.duty,
        "provider": row.provider,
        "adspower_profile_id": row.adspower_profile_id or "",
        "platform_url": row.platform_url or "",
        "account": (account.rpa_account if account else "") or "",
        "platform": spec.get("platform", ""),
        "msg_types": spec.get("msg_types", []),
        "auth_status": row.auth_status,
        "reopen": bool(getattr(row, "reopen_requested", 0)),
    }


@router.get("/fleet")
def fleet_assignments(db: Session = Depends(get_db), _: None = Depends(rpa_key_dep)):
    """调度器对账：只返回未停用的绑定。"""
    rows = (
        db.query(AccountBinding)
        .order_by(AccountBinding.id)
        .all()
    )
    cutoff = datetime.utcnow() - timedelta(seconds=settings.rpa_worker_offline_seconds)
    live_ids = {
        worker.worker_id
        for worker in db.query(RpaWorker).filter(RpaWorker.last_heartbeat_at >= cutoff).all()
        if worker.status != "offline"
    }
    return {"bindings": [
        {
            "worker_id": row.worker_id or "",
            "binding_id": row.id,
            "driver": row.driver,
            "adspower_profile_id": row.adspower_profile_id or "",
            "worker_live": bool(row.worker_id) and row.worker_id in live_ids,
        }
        for row in rows
        if is_enabled(row.auth_status)
    ]}


@router.get("/outbox")
def pull_outbox(worker_id: str, account: str, limit: int = 5, db: Session = Depends(get_db),
                _: None = Depends(rpa_key_dep), msg_types: str = ""):
    """Worker 拉取待发消息：按 account 隔离，可按 msg_types 只租本职责的消息。"""
    _reap_expired_leases(db)
    limit = max(1, min(limit, 20))
    query = (
        db.query(RpaOutbox)
        .filter(RpaOutbox.status == "pending", RpaOutbox.account == account)
    )
    types = [item.strip() for item in (msg_types or "").split(",") if item.strip()]
    if types:
        query = query.filter(RpaOutbox.msg_type.in_(types))
    rows = (
        query.order_by(RpaOutbox.created_at, RpaOutbox.id)
        .limit(limit)
        .all()
    )
    now = datetime.utcnow()
    for row in rows:
        row.status = "leased"
        row.worker_id = worker_id
        row.leased_at = now
        row.attempts += 1
    db.commit()
    return {"messages": [
        {
            "outbox_id": r.id,
            "conversation_id": r.platform_conversation_id,
            "user_id": r.platform_user_id,
            "content": r.content,
            "msg_type": r.msg_type,
            "media_id": r.media_id,
        } for r in rows
    ]}


@router.post("/ack")
def ack_outbox(req: RpaAck, db: Session = Depends(get_db), _: None = Depends(rpa_key_dep)):
    """Worker 发送回执。失败累计 3 次置 failed 并告警。"""
    row = db.get(RpaOutbox, req.outbox_id)
    if row is None:
        raise HTTPException(404, "outbox 记录不存在")
    if req.ok:
        row.status = "acked"
        row.acked_at = datetime.utcnow()
        row.error = ""
        row.result = (req.platform_msg_id or "")[:512]  # 发布类任务回传笔记 URL 等
    else:
        row.error = req.error[:500]
        # 来客回执失败立刻停住。页面上可能已经发出去，自动重试会再打一条。
        if _outbox_is_laike(db, row) or row.attempts >= 3:
            row.status = "failed"
            monitor.record("rpa_send_failure",
                           f"outbox_id={row.id} account={row.account} error={row.error[:100]}")
            logger.error("RPA 发送最终失败 outbox_id=%s error=%s", row.id, row.error)
        else:
            row.status = "pending"  # 回滚重试
    db.commit()
    return {"ok": True, "status": row.status}


@router.post("/release")
def release_outbox(req: RpaRelease, db: Session = Depends(get_db), _: None = Depends(rpa_key_dep)):
    """把不属于本职责的租约放回 pending，并撤销本次拉取计入的失败次数。"""
    row = db.get(RpaOutbox, req.outbox_id)
    if row is None:
        raise HTTPException(404, "outbox 记录不存在")
    if row.status != "leased":
        return {"ok": True, "status": row.status}
    if row.worker_id and row.worker_id != req.worker_id:
        raise HTTPException(409, "租约不属于该 Worker")
    if row.attempts > 0:
        row.attempts -= 1
    row.status = "pending"
    row.worker_id = ""
    row.leased_at = None
    db.commit()
    return {"ok": True, "status": row.status}


# ============ outbox 失败处置（工作台侧，JWT 鉴权） ============

_DM_REPLY_TYPES = ("text", "image", "voice")
_COMMENT_REPLY_TYPES = ("comment_reply", "first_comment")
_REPLY_PLATFORMS = ("douyin", "xiaohongshu")
_REPLY_STATUSES = ("pending", "leased", "acked", "failed")
_REPLY_PAGE_SIZE_MAX = 50


def _reply_body(msg_type: str, content: str) -> str:
    """评论和首评的 content 是 JSON，表格只展示回复正文。"""
    text = content or ""
    if msg_type in _COMMENT_REPLY_TYPES:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("text"), str):
            text = payload["text"]
    return text[:500]


def _reply_item(row: RpaOutbox) -> dict:
    return {
        "outbox_id": row.id,
        "account": row.account,
        "platform": row.platform,
        "msg_type": row.msg_type,
        "kind": "comment" if row.msg_type in _COMMENT_REPLY_TYPES else "dm",
        "content": _reply_body(row.msg_type, row.content),
        "status": row.status,
        "error": row.error or "",
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "acked_at": row.acked_at.isoformat() if row.acked_at else None,
    }


def _comment_payload(row: RpaOutbox) -> dict:
    if row.msg_type not in _COMMENT_REPLY_TYPES:
        return {}
    try:
        payload = json.loads(row.content or "")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _profile_fields(account: MatrixAccount | None, account_key: str) -> tuple[str, str, str]:
    profile = account.profile_json if account is not None and isinstance(account.profile_json, dict) else {}
    profile_id = str(profile.get("ads_profile_id") or "")
    if not profile_id and account_key.startswith("ads-"):
        profile_id = account_key[4:]
    profile_name = str(profile.get("ads_profile_name") or "")
    account_name = account.account_name if account is not None else ""
    return account_name or "", profile_id, profile_name


def _conversation_key(row: RpaOutbox) -> str:
    if row.msg_type in _COMMENT_REPLY_TYPES or not row.account or not row.platform_conversation_id:
        return ""
    return f"{row.account}:{row.platform_conversation_id}"


def _enrich_replies(db: Session, rows: list[RpaOutbox]) -> list[dict]:
    """补本方平台账号、环境和提问人。同一页的账号、顾客、评论各查一次。"""
    account_keys = {row.account for row in rows if row.account}
    accounts = {
        item.rpa_account: item
        for item in db.query(MatrixAccount).filter(MatrixAccount.rpa_account.in_(account_keys)).all()
    } if account_keys else {}

    conv_keys = [key for row in rows if (key := _conversation_key(row))]
    conversations = {
        item.platform_conversation_id: item
        for item in db.query(Conversation).filter(Conversation.platform_conversation_id.in_(conv_keys)).all()
    } if conv_keys else {}
    user_ids = {
        row.platform_user_id for row in rows
        if row.msg_type not in _COMMENT_REPLY_TYPES and row.platform_user_id
    }
    customers_by_uid = {
        (item.platform, item.platform_user_id): item
        for item in db.query(Customer).filter(Customer.platform_user_id.in_(user_ids)).all()
    } if user_ids else {}
    customer_ids = {item.customer_id for item in conversations.values() if item.customer_id}
    customers = {
        item.id: item
        for item in db.query(Customer).filter(Customer.id.in_(customer_ids)).all()
    } if customer_ids else {}

    payloads = {row.id: _comment_payload(row) for row in rows}
    comment_ids = []
    for row in rows:
        comment_id = payloads[row.id].get("comment_id")
        if row.msg_type == "comment_reply" and isinstance(comment_id, str) and comment_id:
            comment_ids.append(comment_id)
    comments = {
        item.platform_comment_id: item
        for item in db.query(PostComment).filter(PostComment.platform_comment_id.in_(comment_ids)).all()
    } if comment_ids else {}

    items = []
    for row in rows:
        account_name, profile_id, profile_name = _profile_fields(accounts.get(row.account), row.account or "")
        user_account = ""
        user_name = ""
        question = (row.question or "")[:500]
        payload = payloads[row.id]
        if row.msg_type == "comment_reply":
            comment = comments.get(payload.get("comment_id") or "")
            if comment is not None:
                user_account = comment.author_id or ""
                user_name = comment.author_nickname or ""
            if not user_account:
                user_account = row.platform_user_id or ""
            if not user_name and isinstance(payload.get("author_nickname"), str):
                user_name = payload["author_nickname"]
        elif row.msg_type != "first_comment":
            conv = conversations.get(_conversation_key(row))
            customer = customers.get(conv.customer_id) if conv is not None else None
            if customer is None:
                customer = customers_by_uid.get((row.platform, row.platform_user_id))
            if customer is not None:
                user_account = customer.platform_user_id or ""
                user_name = customer.nickname or ""
            if not user_account:
                user_account = row.platform_user_id or ""
        item = _reply_item(row)
        item.update({
            "account_name": account_name,
            "profile_id": profile_id,
            "profile_name": profile_name,
            "user_account": user_account,
            "user_name": user_name,
            "question": question,
        })
        items.append(item)
    return items


def _json_text(column, path: str):
    """非法 JSON 不当成对象解析，避免私信正文让 json_extract 报错。"""
    safe = case((func.json_valid(column) == 1, column), else_=literal("{}"))
    return func.json_extract(safe, path)


def _reply_keyword_filter(like: str):
    """关键词同时命中本方账号、环境、提问人和问题。"""
    conv_key = func.concat(RpaOutbox.account, literal(":"), RpaOutbox.platform_conversation_id)
    asker = exists().where(and_(
        Conversation.platform_conversation_id == conv_key,
        Customer.id == Conversation.customer_id,
        or_(Customer.platform_user_id.like(like), Customer.nickname.like(like)),
    ))
    question = exists().where(and_(
        Conversation.platform_conversation_id == conv_key,
        Message.conversation_id == Conversation.id,
        Message.sender_type == "user",
        Message.created_at <= RpaOutbox.created_at,
        Message.content.like(like),
    ))
    comment_id = _json_text(RpaOutbox.content, "$.comment_id")
    comment_hit = exists().where(and_(
        PostComment.platform_comment_id == comment_id,
        or_(
            PostComment.content.like(like),
            PostComment.author_id.like(like),
            PostComment.author_nickname.like(like),
        ),
    ))
    profile_id = _json_text(MatrixAccount.profile_json, "$.ads_profile_id")
    profile_name = _json_text(MatrixAccount.profile_json, "$.ads_profile_name")
    account_match = exists().where(and_(
        MatrixAccount.rpa_account == RpaOutbox.account,
        MatrixAccount.rpa_account != "",
        or_(
            MatrixAccount.account_name.like(like),
            profile_id.like(like),
            profile_name.like(like),
        ),
    ))
    return or_(
        RpaOutbox.account.like(like),
        RpaOutbox.content.like(like),
        RpaOutbox.question.like(like),
        RpaOutbox.error.like(like),
        RpaOutbox.platform_user_id.like(like),
        account_match,
        asker,
        question,
        comment_hit,
    )


@router.get("/replies")
def list_replies(kind: str = "", db: Session = Depends(get_db),
                 _: Agent = Depends(get_current_agent),
                 q: str = "", platform: str = "", status: str = "",
                 page: int = 1, page_size: int = 20):
    """RPA 私信回复、评论回复和首评。可按类型、平台、状态和关键词分页。"""
    if kind == "dm":
        types = _DM_REPLY_TYPES
    elif kind == "comment":
        types = _COMMENT_REPLY_TYPES
    elif kind in ("", "all"):
        types = _DM_REPLY_TYPES + _COMMENT_REPLY_TYPES
    else:
        raise HTTPException(400, "kind 只能是 dm、comment 或空")
    if platform and platform not in _REPLY_PLATFORMS:
        raise HTTPException(400, "platform 只能是 douyin、xiaohongshu 或空")
    if status and status not in _REPLY_STATUSES:
        raise HTTPException(400, "status 只能是 pending、leased、acked、failed 或空")
    if page_size < 1 or page_size > _REPLY_PAGE_SIZE_MAX:
        raise HTTPException(400, "page_size 必须在 1 到 50 之间")
    query = db.query(RpaOutbox).filter(RpaOutbox.msg_type.in_(types))
    if platform:
        query = query.filter(RpaOutbox.platform == platform)
    if status:
        query = query.filter(RpaOutbox.status == status)
    keyword = (q or "").strip()
    if keyword:
        query = query.filter(_reply_keyword_filter(f"%{keyword}%"))
    total = query.count()
    rows = (
        query.order_by(RpaOutbox.id.desc())
        .offset((max(page, 1) - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {"total": total, "items": _enrich_replies(db, rows)}


@router.get("/outbox/failed")
def list_failed_outbox(account: str = "", db: Session = Depends(get_db),
                       _: Agent = Depends(get_current_agent)):
    """失败消息列表（设置页 RPA 卡片展示，可按 account 过滤，最近 100 条）。"""
    q = db.query(RpaOutbox).filter(RpaOutbox.status == "failed")
    if account:
        q = q.filter(RpaOutbox.account == account)
    rows = q.order_by(RpaOutbox.id.desc()).limit(100).all()
    return {"items": [
        {
            "outbox_id": r.id,
            "account": r.account,
            "platform": r.platform,
            "conversation_id": r.platform_conversation_id,
            "content": r.content[:100],
            "msg_type": r.msg_type,
            "attempts": r.attempts,
            "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        } for r in rows
    ]}


@router.post("/outbox/{outbox_id}/retry")
def retry_outbox(outbox_id: int, db: Session = Depends(get_db),
                 agent: Agent = Depends(get_current_agent)):
    """失败消息重发：置回 pending、清零重试计数，Worker 下轮即可拉到。"""
    row = db.get(RpaOutbox, outbox_id)
    if row is None:
        raise HTTPException(404, "outbox 记录不存在")
    if row.status != "failed":
        raise HTTPException(400, f"仅 failed 状态可重发（当前 {row.status}）")
    row.status = "pending"
    row.attempts = 0
    row.error = ""
    row.worker_id = ""
    row.leased_at = None
    db.commit()
    logger.info("outbox %s 人工重发（account=%s）", outbox_id, row.account)
    audit.log(db, agent, "rpa_outbox_retry", target=f"outbox:{outbox_id}",
              detail=row.content[:80])
    return {"ok": True, "status": row.status}


@router.post("/outbox/{outbox_id}/discard")
def discard_outbox(outbox_id: int, db: Session = Depends(get_db),
                   agent: Agent = Depends(get_current_agent)):
    """失败消息忽略：置 discarded（不再计入 failed，不再被拉取）。"""
    row = db.get(RpaOutbox, outbox_id)
    if row is None:
        raise HTTPException(404, "outbox 记录不存在")
    if row.status != "failed":
        raise HTTPException(400, f"仅 failed 状态可忽略（当前 {row.status}）")
    row.status = "discarded"
    db.commit()
    logger.info("outbox %s 人工忽略（account=%s）", outbox_id, row.account)
    audit.log(db, agent, "rpa_outbox_discard", target=f"outbox:{outbox_id}",
              detail=row.content[:80])
    return {"ok": True, "status": row.status}


def _sync_binding_status(db: Session, worker_id: str, status: str, meta: dict):
    """心跳写回绑定：在线视为已授权，登录过期单独标记。选择器失效仍看 Worker 状态。"""
    binding = None
    raw_id = (meta or {}).get("binding_id")
    if raw_id:
        try:
            binding = db.get(AccountBinding, int(raw_id))
        except (TypeError, ValueError):
            binding = None
        if binding is not None and binding.worker_id != worker_id:
            binding = None
    if binding is None:
        enabled = [
            row for row in db.query(AccountBinding).filter(AccountBinding.worker_id == worker_id).all()
            if is_enabled(row.auth_status)
        ]
        binding = enabled[0] if len(enabled) == 1 else None
    if binding is None or not is_enabled(binding.auth_status):
        return
    if status == "online":
        binding.auth_status = "authorized"
    elif status == "login_expired":
        binding.auth_status = "login_expired"
    elif status == "selector_mismatch" and binding.auth_status == "login_expired":
        # 选择器对不上时不再把授权留在登录过期。已登录的首页就会出现这种组合。
        binding.auth_status = "pending_login"


# ============ 心跳 ============

@router.post("/heartbeat")
def heartbeat(req: RpaHeartbeat, db: Session = Depends(get_db), _: None = Depends(rpa_key_dep)):
    """Worker 心跳：登记/刷新状态（online | login_expired | selector_mismatch）。

    dry_run=True 时仅校验 key（依赖已完成），不落库不告警 —— 供 doctor 自检使用，
    避免自检把 worker 误标 online 后又被 sweeper 判 offline 产生告警噪音。
    """
    if req.dry_run:
        return {"ok": True, "dry_run": True}
    worker = db.query(RpaWorker).filter(RpaWorker.worker_id == req.worker_id).first()
    if worker is None:
        worker = RpaWorker(worker_id=req.worker_id, account=req.account, platform=req.platform)
        db.add(worker)
    worker.account = req.account
    worker.platform = req.platform
    allowed = ("online", "idle", "login_expired", "selector_mismatch", "browser_unavailable", "fleet")
    worker.status = req.status if req.status in allowed else "online"
    meta = dict(req.meta or {})
    profiles = meta.get("browser_profiles")
    if isinstance(profiles, list):
        meta["browser_profiles"] = profiles[:500]
    worker.meta = meta
    worker.last_heartbeat_at = datetime.utcnow()
    _sync_binding_status(db, req.worker_id, worker.status, meta)
    db.commit()
    if worker.status not in ("online", "idle", "fleet"):
        monitor.record(f"rpa_worker_{worker.status}", f"worker={req.worker_id} account={req.account}")
    reopen = False
    raw_id = meta.get("binding_id")
    if raw_id:
        try:
            binding = db.get(AccountBinding, int(raw_id))
        except (TypeError, ValueError):
            binding = None
        if binding is not None and binding.worker_id == req.worker_id:
            reopen = bool(binding.reopen_requested)
    return {"ok": True, "reopen": reopen}


@router.post("/reopen-ack")
def reopen_ack(worker_id: str, db: Session = Depends(get_db), _: None = Depends(rpa_key_dep)):
    """Worker 开始重新打开浏览器后清掉重开标记，避免反复重启。"""
    rows = db.query(AccountBinding).filter(AccountBinding.worker_id == worker_id).all()
    for row in rows:
        row.reopen_requested = 0
    db.commit()
    return {"ok": True}


# ============ 身份解析（Worker 本地缓存用） ============

@router.post("/identity/self")
def report_self_identity(req: RpaSelfIdentity, db: Session = Depends(get_db),
                         _: None = Depends(rpa_key_dep)):
    """Worker 读到登录账号名后写回矩阵账号。"""
    name = (req.account_name or "").strip()[:64]
    if not name:
        raise HTTPException(400, "账号名不能为空")
    binding = None
    enabled = [
        row for row in db.query(AccountBinding).filter(AccountBinding.worker_id == req.worker_id).all()
        if is_enabled(row.auth_status)
    ]
    if len(enabled) == 1:
        binding = enabled[0]
    if binding is None and req.profile_id:
        matched = [
            row for row in db.query(AccountBinding).filter(
                AccountBinding.adspower_profile_id == req.profile_id).all()
            if is_enabled(row.auth_status)
        ]
        if len(matched) == 1:
            binding = matched[0]
    if binding is None:
        raise HTTPException(404, "找不到对应绑定")
    account = db.get(MatrixAccount, binding.account_id)
    if account is None:
        raise HTTPException(404, "账号不存在")
    previous = (account.account_name or "").strip()
    profile = dict(account.profile_json or {})
    if previous and previous != "待确认" and not str(profile.get("standby_name") or "").strip():
        profile["standby_name"] = previous
    profile["login_name"] = name
    profile["confirmed"] = True
    account.account_name = name
    account.profile_json = profile
    db.commit()
    return {"ok": True, "account_name": name}


@router.post("/identity/resolve")
def identity_resolve(req: RpaIdentityResolve, db: Session = Depends(get_db),
                     _: None = Depends(rpa_key_dep)):
    stable = resolve_identity(db, req.account, req.platform, req.nickname, req.prev_nickname)
    return {"stable_user_id": stable}


# ============ 管理接口（JWT，前端状态卡片） ============

@router.get("/workers", response_model=list[RpaWorkerOut])
def list_workers(agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """RPA Worker 状态列表（按 account 分组展示用）。"""
    workers = db.query(RpaWorker).order_by(RpaWorker.account, RpaWorker.worker_id).all()
    result = []
    for w in workers:
        if w.status == "fleet":
            continue
        pending = db.query(RpaOutbox).filter(
            RpaOutbox.account == w.account, RpaOutbox.status.in_(["pending", "leased"])).count()
        failed = db.query(RpaOutbox).filter(
            RpaOutbox.account == w.account, RpaOutbox.status == "failed").count()
        result.append(RpaWorkerOut(
            worker_id=w.worker_id, account=w.account, platform=w.platform,
            status=w.status, meta=w.meta or {}, last_heartbeat_at=w.last_heartbeat_at,
            pending=pending, failed=failed,
        ))
    return result
