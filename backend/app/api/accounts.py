"""账号矩阵 API：多平台多账号管理 + 抖音 OAuth 授权闭环。

凭证安全：access_token/refresh_token 经 core/crypto.py Fernet 加密后落库，
任何接口不出明文凭证（只返回 has_credentials 布尔）。
"""
import logging
import re
import uuid
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..bindings import DOUYIN_DM_DRIVERS, douyin_dm_driver_for
from ..config import settings
from ..core import audit, crypto
from ..core.runtime_settings import ACCOUNT_TYPES
from ..core.http import get_json
from ..database import get_db
from ..models import AccountBinding, Agent, MatrixAccount, PublishTask
from ..schemas import (AccountImportIn, MatrixAccountIn, MatrixAccountOut,
                       MatrixAccountUpdate)
from .deps import get_current_agent, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

# 抖音开放平台 OAuth 端点
DOUYIN_OAUTH_AUTHORIZE = "https://open.douyin.com/platform/oauth/connect/"
DOUYIN_OAUTH_ACCESS_TOKEN = "https://open.douyin.com/oauth/access_token/"
DOUYIN_OAUTH_REFRESH_TOKEN = "https://open.douyin.com/oauth/refresh_token/"
# 申请的能力：内容发布 + 评论管理（见 docs/platform-integration.md）
DOUYIN_OAUTH_SCOPE = "video.create.bind,item.comment"


def _account_out(db: Session, acc: MatrixAccount) -> MatrixAccountOut:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_published = (
        db.query(PublishTask)
        .filter(PublishTask.account_id == acc.id, PublishTask.status == "success",
                PublishTask.published_at >= today_start)
        .count()
    )
    return MatrixAccountOut(
        id=acc.id, platform=acc.platform, account_name=acc.account_name,
        auth_type=acc.auth_type,
        # pending_ 前缀为授权前占位（绕开 platform+open_id 唯一索引），不出参
        open_id="" if (acc.open_id or "").startswith("pending_") else (acc.open_id or ""),
        rpa_account=acc.rpa_account or "",
        status=acc.status, has_credentials=bool(acc.credentials_enc),
        daily_publish_limit=acc.daily_publish_limit,
        daily_comment_limit=acc.daily_comment_limit,
        group_name=acc.group_name or "",
        douyin_account_type=acc.douyin_account_type or "",
        queue_enabled=bool(acc.queue_enabled), queue_slots=acc.queue_slots or [],
        today_published=today_published,
        profile=acc.profile_json or {},
        created_at=acc.created_at,
    )


# ============ CRUD ============

def _normalize_douyin_type(platform: str, raw: str) -> str:
    value = (raw or "").strip()
    if value not in ("", *ACCOUNT_TYPES):
        raise HTTPException(400, "账号形态只能是空、feige、enterprise 或 laike")
    return value if platform == "douyin" else ""


def _rewrite_dm_driver(db: Session, acc: MatrixAccount):
    """账号形态变化后，只改写这个账号的私信绑定。"""
    if acc.platform != "douyin":
        return
    driver = douyin_dm_driver_for(acc)
    row = db.query(AccountBinding).filter(
        AccountBinding.account_id == acc.id, AccountBinding.duty == "dm",
    ).first()
    if row and row.driver in DOUYIN_DM_DRIVERS.values() and row.driver != driver:
        row.driver = driver


@router.get("", response_model=list[MatrixAccountOut])
def list_accounts(platform: str = "", _: Agent = Depends(get_current_agent),
                  db: Session = Depends(get_db)):
    q = db.query(MatrixAccount)
    if platform:
        q = q.filter(MatrixAccount.platform == platform)
    return [_account_out(db, a) for a in q.order_by(MatrixAccount.platform, MatrixAccount.id).all()]


@router.get("/health")
def accounts_health(_: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """全账号健康度聚合（列表级批量计算，避免 N+1）。

    返回 {account_id: {score, level, issues, token_expires_at, worker_status}}。
    """
    from ..core.account_health import compute_all
    return {str(k): v for k, v in compute_all(db).items()}


@router.post("", response_model=MatrixAccountOut)
def create_account(req: MatrixAccountIn, agent: Agent = Depends(require_admin),
                   db: Session = Depends(get_db)):
    if req.auth_type == "rpa" and not req.rpa_account.strip():
        raise HTTPException(400, "RPA 账号必须填写 rpa_account（与 Worker 配置的 ACCOUNT 一致）")
    acc = MatrixAccount(
        platform=req.platform, account_name=req.account_name.strip(),
        auth_type=req.auth_type, rpa_account=req.rpa_account.strip(),
        douyin_account_type=_normalize_douyin_type(req.platform, req.douyin_account_type),
        # open_id 占位：(platform, open_id) 有唯一索引，空串会导致同平台第二个账号创建失败；
        # OAuth 回调 _save_credentials 会用真实 open_id 覆盖
        open_id=f"pending_{uuid.uuid4().hex[:12]}",
        daily_publish_limit=req.daily_publish_limit,
        daily_comment_limit=req.daily_comment_limit, group_name=req.group_name.strip(),
        queue_enabled=req.queue_enabled, queue_slots=req.queue_slots,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    audit.log(db, agent, "account_create", target=f"account:{acc.id}",
              detail=f"{acc.platform}/{acc.account_name} ({acc.auth_type})")
    return _account_out(db, acc)


@router.put("/{account_id}", response_model=MatrixAccountOut)
def update_account(account_id: int, req: MatrixAccountUpdate,
                   agent: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    acc = db.get(MatrixAccount, account_id)
    if acc is None:
        raise HTTPException(404, "账号不存在")
    payload = req.model_dump(exclude_none=True)
    new_type = payload.pop("douyin_account_type", None)
    for field, value in payload.items():
        setattr(acc, field, value.strip() if isinstance(value, str) else value)
    if new_type is not None:
        acc.douyin_account_type = _normalize_douyin_type(acc.platform, new_type)
        _rewrite_dm_driver(db, acc)
    db.commit()
    db.refresh(acc)
    audit.log(db, agent, "account_update", target=f"account:{acc.id}",
              detail=str(req.model_dump(exclude_none=True))[:200])
    return _account_out(db, acc)


@router.post("/import")
def import_accounts(req: AccountImportIn, agent: Agent = Depends(require_admin),
                    db: Session = Depends(get_db)):
    """批量导入账号（Phase 7）：每行 `平台,名称,auth_type,rpa_account,分组`。

    逐行校验（平台/名称/接入方式/Worker 标识/重名），失败行不阻断其他行；
    返回成功数与失败明细（行号 + 原因）。授权凭证仍需逐账号走 OAuth/Worker 接入。
    """
    imported = 0
    failed: list[dict] = []
    for i, raw in enumerate(req.lines, 1):
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in re.split(r"[,，]", line)]
        platform = parts[0] if parts else ""
        name = parts[1] if len(parts) > 1 else ""
        auth_type = parts[2] if len(parts) > 2 and parts[2] else "api"
        rpa_account = parts[3] if len(parts) > 3 else ""
        group_name = parts[4] if len(parts) > 4 else ""

        def _fail(reason: str):
            failed.append({"line": i, "content": line[:80], "error": reason})

        if platform not in ("douyin", "xiaohongshu"):
            _fail("平台应为 douyin/xiaohongshu")
            continue
        if not name:
            _fail("缺少账号名称")
            continue
        if auth_type not in ("api", "rpa"):
            _fail("auth_type 应为 api/rpa")
            continue
        if auth_type == "rpa" and not rpa_account:
            _fail("RPA 账号必须提供 Worker 账号标识（第 4 列）")
            continue
        if db.query(MatrixAccount).filter(
                MatrixAccount.platform == platform,
                MatrixAccount.account_name == name).count():
            _fail("同平台同名账号已存在")
            continue
        if rpa_account and db.query(MatrixAccount).filter(
                MatrixAccount.rpa_account == rpa_account).count():
            _fail(f"Worker 账号标识 {rpa_account} 已被占用")
            continue
        db.add(MatrixAccount(platform=platform, account_name=name, auth_type=auth_type,
                             rpa_account=rpa_account, group_name=group_name,
                             open_id=f"pending_{uuid.uuid4().hex[:12]}"))  # 占位，授权后覆盖
        db.flush()  # autoflush=False：让后续行的重名/占用查重能看到本行
        imported += 1
    db.commit()
    audit.log(db, agent, "account_import", target="accounts",
              detail=f"成功 {imported} 行，失败 {len(failed)} 行")
    return {"imported": imported, "failed": failed}


@router.delete("/{account_id}")
def delete_account(account_id: int, agent: Agent = Depends(require_admin),
                   db: Session = Depends(get_db)):
    acc = db.get(MatrixAccount, account_id)
    if acc is None:
        raise HTTPException(404, "账号不存在")
    running = db.query(PublishTask).filter(
        PublishTask.account_id == account_id,
        PublishTask.status.in_(["pending", "publishing"])).count()
    if running:
        raise HTTPException(400, f"该账号还有 {running} 个未完结发布任务，请先取消")
    db.query(AccountBinding).filter(AccountBinding.account_id == account_id).delete()
    db.delete(acc)
    db.commit()
    audit.log(db, agent, "account_delete", target=f"account:{account_id}",
              detail=f"{acc.platform}/{acc.account_name}")
    return {"ok": True}


# ============ 抖音 OAuth 闭环 ============

@router.get("/{account_id}/oauth-url")
def douyin_oauth_url(account_id: int, _: Agent = Depends(get_current_agent),
                     db: Session = Depends(get_db)):
    """生成抖音授权链接（运营在浏览器打开并扫码授权）。"""
    acc = db.get(MatrixAccount, account_id)
    if acc is None:
        raise HTTPException(404, "账号不存在")
    if acc.platform != "douyin" or acc.auth_type != "api":
        raise HTTPException(400, "仅抖音 API 账号需要 OAuth 授权")
    if not settings.douyin_client_key or not settings.douyin_oauth_redirect_uri:
        raise HTTPException(503, "未配置 DOUYIN_CLIENT_KEY / DOUYIN_OAUTH_REDIRECT_URI")
    url = (
        f"{DOUYIN_OAUTH_AUTHORIZE}?client_key={settings.douyin_client_key}"
        f"&response_type=code&scope={quote(DOUYIN_OAUTH_SCOPE)}"
        f"&redirect_uri={quote(settings.douyin_oauth_redirect_uri, safe='')}"
        f"&state={acc.id}"
    )
    return {"url": url}


async def _exchange_code(code: str) -> dict:
    """授权 code 换 token。错误码非 0 抛异常；网络异常由 get_json 重试。"""
    data = await get_json(DOUYIN_OAUTH_ACCESS_TOKEN, params={
        "client_key": settings.douyin_client_key,
        "client_secret": settings.douyin_client_secret,
        "code": code, "grant_type": "authorization_code",
    })
    # 抖音返回包一层 data：{"data": {"error_code": 0, "access_token": ..., "open_id": ...}}
    inner = data.get("data") or {}
    err = inner.get("error_code", data.get("error_code", 0))
    if err not in (0, None):
        raise RuntimeError(f"抖音 code 换 token 失败: {data}")
    return inner


async def _refresh_token(refresh_token: str) -> dict:
    data = await get_json(DOUYIN_OAUTH_REFRESH_TOKEN, params={
        "client_key": settings.douyin_client_key,
        "refresh_token": refresh_token, "grant_type": "refresh_token",
    })
    inner = data.get("data") or {}
    err = inner.get("error_code", data.get("error_code", 0))
    if err not in (0, None):
        raise RuntimeError(f"抖音 token 刷新失败: {data}")
    return inner


def _save_credentials(acc: MatrixAccount, token_data: dict):
    acc.credentials_enc = crypto.encrypt_json({
        "access_token": token_data.get("access_token", ""),
        "refresh_token": token_data.get("refresh_token", ""),
        "expires_in": token_data.get("expires_in", 0),
        "open_id": token_data.get("open_id", ""),
        "saved_at": datetime.utcnow().isoformat(),
    })
    if token_data.get("open_id"):
        acc.open_id = token_data["open_id"]
    acc.status = "active"


@router.get("/oauth/callback", response_class=HTMLResponse)
async def douyin_oauth_callback(code: str = "", state: str = "",
                                db: Session = Depends(get_db)):
    """抖音授权回调（免 JWT：平台重定向，浏览器直达）。code 换 token 并加密落库。"""
    def _page(msg: str) -> str:
        return f"<html><body style='font-family:sans-serif;text-align:center;padding-top:80px'><h2>{msg}</h2></body></html>"

    if not code or not state:
        return HTMLResponse(_page("授权失败：缺少 code/state 参数"), status_code=400)
    acc = db.get(MatrixAccount, int(state)) if state.isdigit() else None
    if acc is None:
        return HTMLResponse(_page("授权失败：账号不存在"), status_code=404)
    try:
        token_data = await _exchange_code(code)
    except Exception as exc:  # noqa: BLE001
        logger.exception("抖音 OAuth code 换 token 失败 account_id=%s", acc.id)
        return HTMLResponse(_page(f"授权失败：{exc}"), status_code=502)
    _save_credentials(acc, token_data)
    db.commit()
    audit.log(db, None, "account_oauth", target=f"account:{acc.id}",
              detail=f"抖音授权成功 open_id={acc.open_id}")
    logger.info("抖音账号授权成功 account_id=%s open_id=%s", acc.id, acc.open_id)
    return HTMLResponse(_page(f"账号「{acc.account_name}」授权成功，可关闭本页面"))


@router.post("/{account_id}/refresh", response_model=MatrixAccountOut)
async def refresh_account_token(account_id: int, agent: Agent = Depends(require_admin),
                                db: Session = Depends(get_db)):
    """手动刷新抖音 token（自动刷新由后台任务负责，此为运维入口）。"""
    acc = db.get(MatrixAccount, account_id)
    if acc is None:
        raise HTTPException(404, "账号不存在")
    creds = crypto.decrypt_json(acc.credentials_enc)
    if not creds.get("refresh_token"):
        raise HTTPException(400, "该账号无可刷新的 refresh_token，请重新授权")
    try:
        token_data = await _refresh_token(creds["refresh_token"])
    except Exception as exc:  # noqa: BLE001
        logger.exception("抖音 token 刷新失败 account_id=%s", acc.id)
        raise HTTPException(502, f"刷新失败：{exc}")
    _save_credentials(acc, token_data)
    db.commit()
    db.refresh(acc)
    audit.log(db, agent, "account_token_refresh", target=f"account:{acc.id}", detail="手动刷新")
    return _account_out(db, acc)
