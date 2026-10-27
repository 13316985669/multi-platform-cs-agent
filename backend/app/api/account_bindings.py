"""账号授权：一个矩阵账号的私信 / 评论 / 发布各绑一个 Worker 与浏览器环境。"""
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..bindings import (DISABLED, DRIVERS, DUTIES, PROVIDERS, douyin_dm_driver_for,
                       driver_spec, is_enabled, resolve_douyin_account_type)
from ..config import settings
from ..core import audit
from ..core.runtime_settings import ACCOUNT_TYPES
from ..database import get_db
from ..models import AccountBinding, Agent, MatrixAccount, RpaWorker
from .deps import get_current_agent, require_admin

router = APIRouter(prefix="/api/account-bindings", tags=["account-bindings"])


class BindingIn(BaseModel):
    account_id: int
    duty: str
    driver: str
    provider: str = "adspower"
    adspower_profile_id: str = ""
    worker_id: str
    platform_url: str = ""


class BindingUpdate(BaseModel):
    driver: str | None = None
    provider: str | None = None
    adspower_profile_id: str | None = None
    worker_id: str | None = None
    platform_url: str | None = None
    auth_status: str | None = None


class BindingOut(BaseModel):
    id: int
    account_id: int
    duty: str
    driver: str
    provider: str
    adspower_profile_id: str
    worker_id: str
    platform_url: str
    auth_status: str
    rpa_account: str = ""
    account_type_mismatch: bool = False
    worker_status: str = ""
    debug_file: str = ""

    class Config:
        from_attributes = True


def _mismatch(driver: str, account: MatrixAccount | None = None) -> bool:
    expected = {
        "douyin_enterprise": "enterprise",
        "douyin_feige": "feige",
        "douyin_laike": "laike",
    }.get(driver)
    if expected is None:
        return False
    return resolve_douyin_account_type(account) != expected


def _worker_view(db: Session, worker_id: str) -> tuple[str, str]:
    if not worker_id:
        return "", ""
    worker = db.query(RpaWorker).filter(RpaWorker.worker_id == worker_id).first()
    if worker is None:
        return "", ""
    meta = worker.meta or {}
    return worker.status or "", str(meta.get("debug_file") or "")


def _out(db: Session, row: AccountBinding, account: MatrixAccount | None = None) -> BindingOut:
    if account is None:
        account = db.get(MatrixAccount, row.account_id)
    status, debug_file = _worker_view(db, row.worker_id)
    return BindingOut(
        id=row.id, account_id=row.account_id, duty=row.duty, driver=row.driver,
        provider=row.provider, adspower_profile_id=row.adspower_profile_id or "",
        worker_id=row.worker_id or "", platform_url=row.platform_url or "",
        auth_status=row.auth_status, rpa_account=(account.rpa_account if account else "") or "",
        account_type_mismatch=_mismatch(row.driver, account),
        worker_status=status, debug_file=debug_file,
    )


def _check_conflicts(db: Session, *, worker_id: str, profile_id: str, provider: str,
                     auth_status: str, exclude_id: int | None):
    if not is_enabled(auth_status):
        return
    q = db.query(AccountBinding).filter(AccountBinding.auth_status != DISABLED)
    if exclude_id:
        q = q.filter(AccountBinding.id != exclude_id)
    if worker_id:
        taken = q.filter(AccountBinding.worker_id == worker_id).first()
        if taken:
            raise HTTPException(409, f"Worker {worker_id} 已绑定其他职责（绑定 #{taken.id}）")
    if provider == "adspower" and profile_id:
        taken = q.filter(AccountBinding.adspower_profile_id == profile_id).first()
        if taken:
            raise HTTPException(
                409,
                f"AdsPower 环境 {profile_id} 已被绑定 #{taken.id} 占用。"
                "同一个环境同一时间只能做一个职责，私信和评论要各用一个环境",
            )


def _validate(db: Session, *, account_id: int, duty: str, driver: str, provider: str,
              profile_id: str, worker_id: str) -> MatrixAccount:
    account = db.get(MatrixAccount, account_id)
    if account is None:
        raise HTTPException(404, "账号不存在")
    spec = driver_spec(driver)
    if spec is None:
        raise HTTPException(400, "不支持的 driver")
    if duty not in DUTIES:
        raise HTTPException(400, "职责只能是 dm、comment、publish")
    if spec["duty"] != duty:
        raise HTTPException(400, f"{driver} 不属于{duty}职责")
    if spec["platform"] != account.platform:
        raise HTTPException(400, "driver 与账号平台不一致")
    if provider not in PROVIDERS:
        raise HTTPException(400, "接入方式只能是 adspower 或 local")
    if not worker_id.strip():
        raise HTTPException(400, "请填写 Worker 标识")
    if provider == "adspower" and not profile_id.strip():
        raise HTTPException(400, "AdsPower 接入需要环境 ID")
    if not (account.rpa_account or "").strip():
        raise HTTPException(400, "请先在账号上填写 Worker 账号标识 rpa_account")
    return account


@router.get("", response_model=list[BindingOut])
def list_bindings(account_id: int = 0, _: Agent = Depends(get_current_agent),
                  db: Session = Depends(get_db)):
    q = db.query(AccountBinding)
    if account_id:
        q = q.filter(AccountBinding.account_id == account_id)
    return [_out(db, row) for row in q.order_by(AccountBinding.id).all()]


@router.get("/overview")
def overview(_: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    accounts = db.query(MatrixAccount).order_by(MatrixAccount.platform, MatrixAccount.id).all()
    bindings = db.query(AccountBinding).all()
    by_account: dict[int, dict] = {}
    for row in bindings:
        by_account.setdefault(row.account_id, {})[row.duty] = _out(db, row).model_dump()
    workers = db.query(RpaWorker).order_by(RpaWorker.worker_id).all()
    return {
        "douyin_account_type": settings.douyin_account_type,
        "drivers": [
            {"id": key, **{k: v for k, v in spec.items() if k != "msg_types"},
             "msg_types": spec["msg_types"]}
            for key, spec in DRIVERS.items()
        ],
        "accounts": [
            {
                "id": acc.id, "platform": acc.platform, "account_name": acc.account_name,
                "group_name": acc.group_name or "", "rpa_account": acc.rpa_account or "",
                "status": acc.status,
                "confirmed": bool((acc.profile_json or {}).get("confirmed")),
                "login_name": (acc.profile_json or {}).get("login_name") or "",
                "standby_name": (acc.profile_json or {}).get("standby_name") or "",
                "profile_name": (acc.profile_json or {}).get("ads_profile_name") or "",
                "bindings": {
                    duty: by_account.get(acc.id, {}).get(duty)
                    for duty in DUTIES
                },
            }
            for acc in accounts
        ],
        "workers": [
            {
                "worker_id": w.worker_id,
                "status": w.status,
                "account": w.account,
                "platform": w.platform,
                "browser_profiles": ((w.meta or {}).get("browser_profiles") or [])[:500],
                "ads_error": (w.meta or {}).get("ads_error") or "",
            }
            for w in workers
        ],
    }


def _driver_for(platform: str, duty: str, account: MatrixAccount | None = None) -> str | None:
    if platform == "douyin" and duty == "dm":
        return douyin_dm_driver_for(account)
    if platform == "douyin" and duty == "comment":
        return "douyin_comment"
    if platform == "xiaohongshu" and duty == "dm":
        return "xhs_ark"
    if platform == "xiaohongshu" and duty == "comment":
        return "xhs_comment"
    if platform == "xiaohongshu" and duty == "publish":
        return "xhs_publish"
    return None


def _idle_workers(db: Session) -> list[RpaWorker]:
    cutoff = datetime.utcnow() - timedelta(seconds=settings.rpa_worker_offline_seconds)
    taken = {
        row.worker_id for row in db.query(AccountBinding).all()
        if is_enabled(row.auth_status) and row.worker_id
    }
    return [
        worker for worker in db.query(RpaWorker).filter(
            RpaWorker.status == "idle", RpaWorker.last_heartbeat_at >= cutoff,
        ).all()
        if worker.worker_id not in taken
    ]


class FromProfileIn(BaseModel):
    platform: str
    duty: str
    adspower_profile_id: str
    profile_name: str = ""
    worker_id: str = ""
    douyin_account_type: str = ""  # 空=跟随全局默认；仅抖音私信生效


@router.post("/from-profile", response_model=BindingOut)
def create_from_profile(req: FromProfileIn, agent: Agent = Depends(require_admin),
                        db: Session = Depends(get_db)):
    """用已登录的 AdsPower 环境建账号。账号名先标待确认，打开页面后再改成登录名。"""
    requested = (req.douyin_account_type or "").strip()
    if requested not in ("", *ACCOUNT_TYPES):
        raise HTTPException(400, "账号形态只能是空、feige、enterprise 或 laike")
    if req.platform != "douyin":
        requested = ""
    profile_id = req.adspower_profile_id.strip()
    if not profile_id:
        raise HTTPException(400, "请选择 AdsPower 环境")
    worker_id = req.worker_id.strip()
    if not worker_id:
        worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    elif worker_id not in {item.worker_id for item in _idle_workers(db)}:
        raise HTTPException(400, "所选进程不在空闲状态")
    rpa_account = f"ads-{profile_id}"[:64]
    account = db.query(MatrixAccount).filter(MatrixAccount.rpa_account == rpa_account).first()
    if account is None:
        account = MatrixAccount(
            platform=req.platform, account_name="待确认", auth_type="rpa",
            open_id=f"pending_{uuid.uuid4().hex[:12]}", rpa_account=rpa_account, status="active",
            douyin_account_type=requested,
            profile_json={"ads_profile_id": profile_id, "ads_profile_name": req.profile_name.strip(),
                          "confirmed": False},
        )
        db.add(account)
        db.flush()
    elif account.platform != req.platform:
        raise HTTPException(409, "这个环境已经绑在另一个平台上")
    elif requested:
        account.douyin_account_type = requested
    driver = _driver_for(req.platform, req.duty, account)
    if driver is None:
        raise HTTPException(400, "这个平台和职责没有对应的 RPA 脚本")
    _check_conflicts(db, worker_id=worker_id, profile_id=profile_id, provider="adspower",
                     auth_status="pending_login", exclude_id=None)
    exists = db.query(AccountBinding).filter(
        AccountBinding.account_id == account.id, AccountBinding.duty == req.duty,
    ).first()
    if exists and is_enabled(exists.auth_status):
        raise HTTPException(409, "该账号的这个职责已经有绑定")
    if exists:
        exists.driver = driver
        exists.provider = "adspower"
        exists.adspower_profile_id = profile_id
        exists.worker_id = worker_id
        exists.auth_status = "pending_login"
        row = exists
    else:
        row = AccountBinding(
            account_id=account.id, duty=req.duty, driver=driver, provider="adspower",
            adspower_profile_id=profile_id, worker_id=worker_id, auth_status="pending_login",
        )
        db.add(row)
    db.commit()
    db.refresh(row)
    audit.log(db, agent, "binding_create", target=f"binding:{row.id}",
              detail=f"profile={profile_id} {req.duty} {driver} worker={worker_id}")
    return _out(db, row, account)


@router.post("", response_model=BindingOut)
def create_binding(req: BindingIn, agent: Agent = Depends(require_admin),
                   db: Session = Depends(get_db)):
    profile_id = req.adspower_profile_id.strip()
    worker_id = req.worker_id.strip()
    account = _validate(
        db, account_id=req.account_id, duty=req.duty, driver=req.driver,
        provider=req.provider, profile_id=profile_id, worker_id=worker_id,
    )
    exists = db.query(AccountBinding).filter(
        AccountBinding.account_id == req.account_id, AccountBinding.duty == req.duty,
    ).first()
    if exists:
        raise HTTPException(409, "该账号的这个职责已经有绑定，请直接修改")
    _check_conflicts(db, worker_id=worker_id, profile_id=profile_id, provider=req.provider,
                     auth_status="pending_login", exclude_id=None)
    row = AccountBinding(
        account_id=req.account_id, duty=req.duty, driver=req.driver, provider=req.provider,
        adspower_profile_id=profile_id, worker_id=worker_id,
        platform_url=req.platform_url.strip()[:256], auth_status="pending_login",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    audit.log(db, agent, "binding_create", target=f"binding:{row.id}",
              detail=f"{account.account_name} {req.duty} {req.driver} worker={worker_id}")
    return _out(db, row, account)


@router.put("/{binding_id}", response_model=BindingOut)
def update_binding(binding_id: int, req: BindingUpdate, agent: Agent = Depends(require_admin),
                   db: Session = Depends(get_db)):
    row = db.get(AccountBinding, binding_id)
    if row is None:
        raise HTTPException(404, "绑定不存在")
    driver = req.driver or row.driver
    provider = req.provider or row.provider
    profile_id = row.adspower_profile_id if req.adspower_profile_id is None else req.adspower_profile_id.strip()
    worker_id = row.worker_id if req.worker_id is None else req.worker_id.strip()
    auth_status = row.auth_status
    if req.auth_status is not None:
        if req.auth_status not in (DISABLED, "pending_login"):
            raise HTTPException(400, "只能改为停用或待登录")
        auth_status = req.auth_status
    account = _validate(
        db, account_id=row.account_id, duty=row.duty, driver=driver, provider=provider,
        profile_id=profile_id, worker_id=worker_id,
    )
    _check_conflicts(db, worker_id=worker_id, profile_id=profile_id, provider=provider,
                     auth_status=auth_status, exclude_id=row.id)
    changed_runtime = (
        driver != row.driver or provider != row.provider or profile_id != (row.adspower_profile_id or "")
        or worker_id != (row.worker_id or "") or auth_status == DISABLED
    )
    row.driver = driver
    row.provider = provider
    row.adspower_profile_id = profile_id
    row.worker_id = worker_id
    if req.platform_url is not None:
        row.platform_url = req.platform_url.strip()[:256]
    row.auth_status = "pending_login" if changed_runtime and auth_status != DISABLED else auth_status
    db.commit()
    db.refresh(row)
    audit.log(db, agent, "binding_update", target=f"binding:{row.id}",
              detail=f"{account.account_name} {row.duty} {row.driver} status={row.auth_status}")
    return _out(db, row, account)


@router.post("/{binding_id}/reopen")
def request_reopen(binding_id: int, agent: Agent = Depends(require_admin),
                   db: Session = Depends(get_db)):
    """让正在跑的 Worker 关掉当前窗口并用有界面方式重新打开，方便再次登录。"""
    row = db.get(AccountBinding, binding_id)
    if row is None:
        raise HTTPException(404, "绑定不存在")
    if not is_enabled(row.auth_status) and row.auth_status != "login_expired":
        raise HTTPException(400, "停用的绑定不能重新打开")
    row.reopen_requested = 1
    if row.auth_status == "authorized":
        row.auth_status = "pending_login"
    db.commit()
    audit.log(db, agent, "binding_reopen", target=f"binding:{row.id}",
              detail=row.worker_id or "")
    return {"ok": True}


@router.delete("/{binding_id}")
def delete_binding(binding_id: int, agent: Agent = Depends(require_admin),
                   db: Session = Depends(get_db)):
    row = db.get(AccountBinding, binding_id)
    if row is None:
        raise HTTPException(404, "绑定不存在")
    detail = f"account={row.account_id} {row.duty} {row.driver}"
    db.delete(row)
    db.commit()
    audit.log(db, agent, "binding_delete", target=f"binding:{binding_id}", detail=detail)
    return {"ok": True}
