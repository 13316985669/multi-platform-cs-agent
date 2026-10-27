"""账号健康度：聚合状态/Token/Worker/发布成功率四类信号 → 分数 + 等级 + 问题清单。

等级：bad > warn > good；score 0-100 仅供排序与色块展示。
Token 口径：抖音 access_token 有效期短且发布通道会自动 refresh，真正需人工介入的是
refresh_token 过期（约 30 天）——用 credentials.saved_at 近似 refresh_token 年龄，
≥23 天 warn（剩余 ≤7 天）、≥30 天 bad。

预警：check_all_and_alert 由 main.py 的 account_health_loop 每小时调用，
按「账号 + 问题 + 日期」内存去重（每问题每天只告一次），汇总后走 monitor 告警通道。
"""
import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import MatrixAccount, PublishTask, RpaWorker
from . import crypto, monitor

logger = logging.getLogger(__name__)

# TODO: 确认 refresh_token 实际有效期（抖音开放平台文档为 30 天，以联调为准）
REFRESH_TOKEN_VALID_DAYS = 30
TOKEN_WARN_DAYS = 23           # refresh_token 年龄 ≥23 天（剩余 ≤7 天）预警
SUCCESS_RATE_MIN_SAMPLE = 3    # 发布成功率统计最小样本量（低于此不评价）
SUCCESS_RATE_WARN = 0.5        # 近 7 天发布成功率低于 50% 预警
PUBLISH_WINDOW_DAYS = 7

# 各问题码的扣分（bad 类重扣，warn 类轻扣）
_DEDUCT = {
    "status_expired": 40, "status_disabled": 40,
    "token_expired": 40, "token_expiring": 20, "token_missing": 20, "token_unknown": 10,
    "worker_missing": 30, "worker_offline": 30,
    "worker_login_expired": 20, "worker_selector_mismatch": 20,
    "publish_rate_low": 20,
}

_WORKER_STATUS_MSG = {
    "offline": "Worker 掉线（心跳超时）",
    "login_expired": "Worker 登录过期，需重新扫码登录",
    "selector_mismatch": "Worker 页面结构变更（选择器失效），需升级 Worker",
}


def _status_issue(acc: MatrixAccount) -> dict | None:
    if acc.status == "expired":
        return {"code": "status_expired", "level": "bad",
                "message": "账号状态为授权过期，请重新授权"}
    if acc.status == "disabled":
        return {"code": "status_disabled", "level": "bad", "message": "账号已停用"}
    return None


def _token_issue(acc: MatrixAccount, now: datetime) -> tuple[dict | None, datetime | None]:
    """API 账号 token 信号。返回 (issue, token_expires_at)。"""
    if acc.auth_type != "api":
        return None, None
    creds = crypto.decrypt_json(acc.credentials_enc)
    if not creds:
        return {"code": "token_missing", "level": "warn",
                "message": "未授权或凭证缺失，请完成 OAuth 授权"}, None
    try:
        saved_at = datetime.fromisoformat(creds.get("saved_at") or "")
    except ValueError:
        return {"code": "token_unknown", "level": "warn",
                "message": "凭证时间信息不完整，建议重新授权"}, None
    expires_at = saved_at + timedelta(days=REFRESH_TOKEN_VALID_DAYS)
    age_days = (now - saved_at).days
    if age_days >= REFRESH_TOKEN_VALID_DAYS:
        return {"code": "token_expired", "level": "bad",
                "message": "授权已过期（refresh_token 失效），需重新授权"}, expires_at
    if age_days >= TOKEN_WARN_DAYS:
        left = REFRESH_TOKEN_VALID_DAYS - age_days
        return {"code": "token_expiring", "level": "warn",
                "message": f"授权约 {left} 天后过期，请提前重新授权"}, expires_at
    return None, expires_at


def _worker_issue(acc: MatrixAccount,
                  workers_by_account: dict[str, list[RpaWorker]]) -> tuple[dict | None, str]:
    """RPA 账号 Worker 信号。返回 (issue, worker_status)。"""
    if acc.auth_type != "rpa":
        return None, ""
    workers = workers_by_account.get(acc.rpa_account or "", [])
    if not workers:
        return {"code": "worker_missing", "level": "bad",
                "message": "无 Worker 注册心跳，请启动 Worker"}, "none"
    latest = max(workers, key=lambda w: w.last_heartbeat_at or datetime.min)
    if latest.status == "online":
        return None, "online"
    level = "bad" if latest.status == "offline" else "warn"
    return {"code": f"worker_{latest.status}", "level": level,
            "message": _WORKER_STATUS_MSG.get(latest.status,
                                              f"Worker 状态异常：{latest.status}")}, latest.status


def _publish_issue(success: int, failed: int) -> dict | None:
    total = success + failed
    if total < SUCCESS_RATE_MIN_SAMPLE:
        return None
    rate = success / total
    if rate < SUCCESS_RATE_WARN:
        return {"code": "publish_rate_low", "level": "warn",
                "message": f"近 {PUBLISH_WINDOW_DAYS} 天发布成功率 {rate:.0%}"
                           f"（{success}/{total}），请检查账号内容与状态"}
    return None


def _finalize(issues: list[dict], token_expires_at: datetime | None,
              worker_status: str) -> dict:
    score = max(0, 100 - sum(_DEDUCT.get(i["code"], 10) for i in issues))
    level = ("bad" if any(i["level"] == "bad" for i in issues)
             else "warn" if issues else "good")
    return {"score": score, "level": level, "issues": issues,
            "token_expires_at": token_expires_at.isoformat() if token_expires_at else None,
            "worker_status": worker_status}


def compute_health(db: Session, acc: MatrixAccount) -> dict:
    """单账号健康度（详情场景用；列表场景请用 compute_all 避免 N+1）。"""
    workers_by_account: dict[str, list[RpaWorker]] = {}
    if acc.auth_type == "rpa" and acc.rpa_account:
        workers_by_account[acc.rpa_account] = (
            db.query(RpaWorker).filter(RpaWorker.account == acc.rpa_account).all())
    success, failed = _publish_stats(db, [acc.id]).get(acc.id, (0, 0))
    return _compute_one(acc, workers_by_account, {acc.id: (success, failed)})


def compute_all(db: Session) -> dict[int, dict]:
    """全账号健康度聚合：三次批量查询（账号/Worker/发布统计），避免 N+1。"""
    accounts = db.query(MatrixAccount).order_by(MatrixAccount.id).all()
    workers_by_account: dict[str, list[RpaWorker]] = {}
    for w in db.query(RpaWorker).all():
        workers_by_account.setdefault(w.account, []).append(w)
    publish_stats = _publish_stats(db, [a.id for a in accounts])
    return {a.id: _compute_one(a, workers_by_account, publish_stats) for a in accounts}


def _compute_one(acc: MatrixAccount, workers_by_account: dict[str, list[RpaWorker]],
                 publish_stats: dict[int, tuple[int, int]]) -> dict:
    now = datetime.utcnow()
    issues: list[dict] = []
    status_issue = _status_issue(acc)
    if status_issue:
        issues.append(status_issue)
    token_issue, token_expires_at = _token_issue(acc, now)
    if token_issue:
        issues.append(token_issue)
    worker_issue, worker_status = _worker_issue(acc, workers_by_account)
    if worker_issue:
        issues.append(worker_issue)
    success, failed = publish_stats.get(acc.id, (0, 0))
    publish_issue = _publish_issue(success, failed)
    if publish_issue:
        issues.append(publish_issue)
    return _finalize(issues, token_expires_at, worker_status)


def _publish_stats(db: Session, account_ids: list[int]) -> dict[int, tuple[int, int]]:
    """近 7 天各账号发布 success/failed 计数（一次 GROUP BY 查询）。"""
    if not account_ids:
        return {}
    since = datetime.utcnow() - timedelta(days=PUBLISH_WINDOW_DAYS)
    rows = (
        db.query(PublishTask.account_id, PublishTask.status)
        .filter(PublishTask.account_id.in_(account_ids),
                PublishTask.status.in_(["success", "failed"]),
                PublishTask.updated_at >= since)
        .all()
    )
    stats: dict[int, list[int]] = {}
    for account_id, status in rows:
        pair = stats.setdefault(account_id, [0, 0])
        pair[0 if status == "success" else 1] += 1
    return {aid: (pair[0], pair[1]) for aid, pair in stats.items()}


# ============ 预警（内存去重：账号+问题+日期，每天每问题只告一次） ============

_alerted: set[tuple[int, str, str]] = set()


def check_all_and_alert() -> int:
    """全量扫描并对 warn/bad 账号发监控告警。返回本次新告警条数。"""
    from ..database import SessionLocal  # 延迟导入避免循环依赖

    db = SessionLocal()
    try:
        health_map = compute_all(db)
        accounts = {a.id: a for a in db.query(MatrixAccount).all()}
    finally:
        db.close()

    today = datetime.utcnow().strftime("%Y-%m-%d")
    new_alerts: list[str] = []
    for account_id, health in health_map.items():
        if health["level"] == "good":
            continue
        acc = accounts.get(account_id)
        name = f"{acc.platform}/{acc.account_name}" if acc else f"account:{account_id}"
        for issue in health["issues"]:
            key = (account_id, issue["code"], today)
            if key in _alerted:
                continue
            _alerted.add(key)
            new_alerts.append(f"{name}：{issue['message']}")
    if new_alerts:
        monitor.record("account_health", "；".join(new_alerts)[:400])
        logger.warning("账号健康度预警: %s", new_alerts)
    return len(new_alerts)
