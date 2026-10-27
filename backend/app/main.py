"""FastAPI 入口：路由注册、CORS、启动初始化（DB/索引/默认数据/worker/会话清扫/备份）。"""
import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .api import account_bindings, accounts, agents, analytics, auth, comments, contents, conversations, evals, funnel, inspiration, knowledge, leads, messages, publish, queue_ops, quick, rpa, stats, tickets, webhooks, ws, zhinikuaihui
from .api import settings as settings_api
from .api.deps import agent_for_module_check, require_admin
from .core.modules import agent_can, allowed_modules
from .wecom import callback as wecom_callback
from .api.messages import media_router
from .api.ws import manager
from .config import settings
from .core import contentfilter
from .core.backup import start_backup, stop_backup
from .core.queue import start_worker, stop_worker
from .core.security import hash_password, verify_password
from .database import SessionLocal, init_db
from .models import Agent, BannedWord, Conversation, QuickReply, RpaMedia, RpaOutbox, RpaWorker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


# ============ 会话超时自动关闭 ============

async def _sweep_once() -> list[int]:
    """扫描超时未活跃的 AI 接待会话：发结束语 → 关闭 → 广播。返回关闭的会话 ID 列表。

    只处理 mode="ai" 的会话：pending 在等人工、human 由客服负责，不应被自动关闭。
    """
    from .services import send_outbound  # 延迟导入避免循环依赖

    cutoff = datetime.utcnow() - timedelta(minutes=settings.session_timeout_minutes)
    db = SessionLocal()
    try:
        convs = (
            db.query(Conversation)
            .filter(Conversation.status == "open", Conversation.mode == "ai",
                    Conversation.last_message_at < cutoff)
            .limit(50)
            .all()
        )
        ids = [c.id for c in convs]
    finally:
        db.close()

    closed = []
    for cid in ids:
        try:
            if not _still_ai_open(cid):
                continue
            # 知你快回会话的结束语由 send_outbound 只写入本系统，不打开放平台。
            await send_outbound(cid, settings.session_close_message, sender_type="ai")
            db = SessionLocal()
            try:
                conv = db.get(Conversation, cid)
                # 发送期间可能已被接管或关掉，再确认一次才写入 closed
                if conv is not None and conv.mode == "ai" and conv.status == "open":
                    conv.status = "closed"
                    conv.closed_at = datetime.utcnow()
                    db.commit()
                    closed.append(cid)
            finally:
                db.close()
            if cid in closed:
                await manager.broadcast("conversation_closed", {"conversation_id": cid})
        except Exception:  # noqa: BLE001
            logger.exception("会话超时关闭失败 conversation_id=%s", cid)
    if closed:
        logger.info("会话超时自动关闭: %s", closed)
    return closed


def _still_ai_open(conversation_id: int) -> bool:
    """发结束语前再读一次：已经进人工队列或已关闭的会话不再动。"""
    db = SessionLocal()
    try:
        conv = db.get(Conversation, conversation_id)
        return conv is not None and conv.mode == "ai" and conv.status == "open"
    finally:
        db.close()


async def session_sweeper():
    """后台周期任务：每 session_sweep_interval_seconds 扫描一次超时会话 + RPA 运维。"""
    while True:
        await asyncio.sleep(settings.session_sweep_interval_seconds)
        try:
            await _sweep_once()
        except Exception:  # noqa: BLE001
            logger.exception("会话超时扫描失败")
        try:
            from .services import sweep_lead_followups
            await sweep_lead_followups()
        except Exception:  # noqa: BLE001
            logger.exception("沉默留资跟进失败")
        try:
            _rpa_housekeep_once()
        except Exception:  # noqa: BLE001
            logger.exception("RPA 运维清扫失败")


# ============ 小红书 token 自动刷新 ============

async def xhs_token_refresher():
    """后台周期任务：小红书 accessToken 临期自动刷新（每 10 分钟检查一次）。

    官方规则：剩余有效期 >30 分钟时刷新为 no-op，故适配器内默认 35 分钟余量；
    refreshToken 过期需人工重新授权（scripts/xhs_oauth.py exchange），此处刷新失败会告警。
    """
    from .adapters import get_adapter  # 延迟导入避免循环依赖
    from .core import monitor

    adapter = get_adapter("xiaohongshu")
    while True:
        await asyncio.sleep(600)
        try:
            if await adapter.maybe_refresh():
                logger.info("小红书 accessToken 已自动刷新")
        except Exception as exc:  # noqa: BLE001
            logger.exception("小红书 token 自动刷新失败")
            monitor.record("xhs_token_refresh_failure", str(exc)[:200])


# ============ 账号健康度预警（Phase 7） ============

async def account_health_loop():
    """后台周期任务：每小时扫描全账号健康度，warn/bad 账号走 monitor 告警（按天去重）。"""
    from .core.account_health import check_all_and_alert  # 延迟导入避免循环依赖

    while True:
        await asyncio.sleep(3600)
        try:
            check_all_and_alert()
        except Exception:  # noqa: BLE001
            logger.exception("账号健康度扫描失败")


# ============ RPA 运维清扫（Worker 掉线告警 + outbox/媒体清理） ============

def _rpa_housekeep_once():
    """1) 心跳超时 Worker 置 offline 并告警；2) 清理 7 天前已完结 outbox；3) 清理过期媒体。"""
    from .core import monitor

    now = datetime.utcnow()
    db = SessionLocal()
    try:
        # 1. Worker 掉线检测
        offline_cutoff = now - timedelta(seconds=settings.rpa_worker_offline_seconds)
        stale = (
            db.query(RpaWorker)
            .filter(RpaWorker.last_heartbeat_at < offline_cutoff,
                    RpaWorker.status != "offline")
            .all()
        )
        for w in stale:
            w.status = "offline"
            monitor.record("rpa_worker_offline", f"worker={w.worker_id} account={w.account}")
            logger.warning("RPA Worker 掉线: %s (account=%s)", w.worker_id, w.account)
        if stale:
            db.commit()

        # 2. outbox 清理：acked/failed 超 7 天
        outbox_cutoff = now - timedelta(days=7)
        deleted = (
            db.query(RpaOutbox)
            .filter(RpaOutbox.status.in_(["acked", "failed"]),
                    RpaOutbox.created_at < outbox_cutoff)
            .delete(synchronize_session=False)
        )
        if deleted:
            db.commit()

        # 3. 媒体清理：超保留期的文件与记录
        media_cutoff = now - timedelta(days=settings.rpa_media_retention_days)
        old_media = db.query(RpaMedia).filter(RpaMedia.created_at < media_cutoff).all()
        for m in old_media:
            try:
                if os.path.exists(m.path):
                    os.remove(m.path)
            except OSError:
                logger.warning("媒体文件删除失败: %s", m.path)
            db.delete(m)
        if old_media:
            db.commit()
            logger.info("清理过期 RPA 媒体 %d 个", len(old_media))
    finally:
        db.close()


def _ensure_admin(db) -> None:
    """保证有 admin。非生产环境密码不是 admin123 时重置；生产环境只补建缺失账号。"""
    admin = db.query(Agent).filter(Agent.username == "admin").first()
    if admin is None:
        db.add(Agent(username="admin", password_hash=hash_password("admin123"),
                     display_name="管理员", role="admin", status="active"))
        logger.info("已创建默认管理员 admin / admin123，请尽快修改密码")
        return
    if settings.app_env == "production":
        return
    if verify_password("admin123", admin.password_hash):
        return
    admin.password_hash = hash_password("admin123")
    admin.role = "admin"
    logger.warning("非生产环境已将管理员 admin 的密码重置为 admin123")


def _warn_insecure_defaults(db) -> None:
    """启动安全自检：弱配置显著告警（不阻断启动，处置见 docs/deployment-checklist.md）。"""
    if settings.secret_key == "change-me-to-a-random-string":
        logger.warning("【安全】SECRET_KEY 仍为默认值，JWT 可被伪造！生产环境必须在 .env 中修改")
    admin = db.query(Agent).filter(Agent.username == "admin").first()
    if admin is not None and verify_password("admin123", admin.password_hash):
        logger.warning("【安全】默认管理员 admin 仍使用初始密码 admin123，请立即登录后台修改！")
    if settings.mock_enabled:
        logger.info("Mock 通道已启用（/api/mock/incoming）；生产环境请设置 MOCK_ENABLED=false")
    if not settings.cors_origins.strip():
        logger.info("CORS 白名单为空：仅放行同源请求（前后端分离部署时配置 CORS_ORIGINS）")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ---- 启动初始化 ----
    init_db()
    db = SessionLocal()
    try:
        from .core.runtime_settings import load_persisted
        load_persisted(db)
        _ensure_admin(db)
        # 默认口语化快捷回复
        if db.query(QuickReply).count() == 0:
            db.add_all([
                QuickReply(title="打招呼", content="您好老板，这边可以提供注册，开户，年审，地址挂靠等服务，您是想咨询什么呢"),
                QuickReply(title="稍等", content="稍等哈，我帮您查一下"),
                QuickReply(title="转人工", content="这个我让同事来帮您处理哈，马上来"),
                QuickReply(title="感谢", content="客气啦，有问题随时喊我"),
                QuickReply(title="留资引导", content="您留个手机号，稍后同事联系您，给您安排优惠"),
            ])
        else:
            stale = (
                db.query(QuickReply)
                .filter(
                    QuickReply.title == "打招呼",
                    QuickReply.content.in_([
                        "来啦，想问啥呀",
                        "在的，我是小赢。香港公司注册、开户都可以问我",
                    ]),
                )
                .all()
            )
            for item in stale:
                item.content = "您好老板，这边可以提供注册，开户，年审，地址挂靠等服务，您是想咨询什么呢"
        db.commit()
        # 违禁词库加载（含入口/出口方向）
        contentfilter.load_db_words(
            [(w.word, w.category, w.direction or "out") for w in db.query(BannedWord).all()])
        # 弱安全配置启动自检（仅告警）
        _warn_insecure_defaults(db)
    finally:
        db.close()

    # BM25 先用现有切片可用；切块版本变化时后台重嵌入，避免旧切片继续被搜到
    from .rag import ingest
    ingest.rebuild_bm25_from_db()
    reindex_task = None
    if ingest.index_version_stale():
        logger.info("知识库索引版本不一致，后台重建")
        reindex_task = asyncio.create_task(ingest.reindex_all())

    async def _replay_stuck_phrase():
        if reindex_task is not None:
            try:
                await reindex_task
            except Exception:
                logger.exception("索引重建失败，仍尝试补跑未回复消息")
        from .services import replay_unanswered_phrase
        await replay_unanswered_phrase("面签资料清单")

    replay_task = asyncio.create_task(_replay_stuck_phrase())

    # 启动任务队列 worker / 会话超时清扫 / 小红书 token 刷新 / 发布调度 / 自动备份
    start_worker()
    sweeper_task = asyncio.create_task(session_sweeper())
    xhs_token_task = asyncio.create_task(xhs_token_refresher())
    from .publisher.scheduler import publish_scheduler_loop
    publish_task_ = asyncio.create_task(publish_scheduler_loop())
    from .comments.douyin_api import comment_polling_loop
    comment_poll_task = asyncio.create_task(comment_polling_loop())
    from .analytics.collector import stats_collection_loop
    stats_task = asyncio.create_task(stats_collection_loop())
    health_task = asyncio.create_task(account_health_loop())
    # 评论默认回复规则（首次启动落库）
    from .comments import engine as _comments_engine  # noqa: F401 注册 inbound_comment 队列处理器
    from .comments import first_comment as _first_comment  # noqa: F401 注册 first_comment 队列处理器
    from .comments.rules import init_default_rules
    db2 = SessionLocal()
    try:
        init_default_rules(db2)
    finally:
        db2.close()
    start_backup()
    logger.info("%s 启动完成", settings.app_name)
    yield
    sweeper_task.cancel()
    xhs_token_task.cancel()
    publish_task_.cancel()
    comment_poll_task.cancel()
    stats_task.cancel()
    health_task.cancel()
    if reindex_task is not None:
        reindex_task.cancel()
    replay_task.cancel()
    await stop_backup()
    await stop_worker()


app = FastAPI(title=settings.app_name, lifespan=lifespan)

# CORS 白名单：空 = 不放行任何跨域来源（同源部署经 nginx/vite 代理，无需 CORS）；
# 前后端分离部署时在 .env 配置 CORS_ORIGINS=https://ops.example.com
_cors_allow = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow,
    allow_credentials=bool(_cors_allow),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def guard_modules(request: Request, call_next):
    """按路径拦截未授权模块。登录、webhook、RPA worker、签名素材不在此列。"""
    if request.method != "OPTIONS":
        needed = allowed_modules(request.method, request.url.path)
        if needed:
            try:
                agent = agent_for_module_check(request)
            except HTTPException as exc:
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
            if not any(agent_can(agent, key) for key in needed):
                return JSONResponse({"detail": "无权访问该模块"}, status_code=403)
    return await call_next(request)

for r in [auth.router, webhooks.router, conversations.router, messages.router,
          account_bindings.router,
          knowledge.router, agents.router, stats.router, quick.router, tickets.router, leads.router,
          rpa.router, settings_api.router, evals.router, media_router, ws.router,
          accounts.router, contents.router, publish.router, comments.router,
          funnel.router, wecom_callback.router, analytics.router, inspiration.router,
          queue_ops.router, zhinikuaihui.router]:
    app.include_router(r)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "llm": settings.llm_configured,
        "embedding": settings.embedding_configured,
        "rerank": settings.rerank_configured,
        "douyin": settings.douyin_configured,
        "xiaohongshu": settings.xhs_configured,
        "wecom": settings.wecom_configured,
        "wecom_callback": settings.wecom_callback_configured,
        "zhini": settings.zhini_configured,
    }


@app.get("/api/health/detail")
def health_detail(_: Agent = Depends(require_admin)):
    """健康详情（仅管理员）：队列深度、各组件配置状态、近 1h 错误计数。"""
    from .core import monitor
    from .models import QueueTask

    db = SessionLocal()
    try:
        queue_pending = db.query(QueueTask).filter(QueueTask.status == "pending").count()
        queue_failed = db.query(QueueTask).filter(QueueTask.status == "failed").count()
    finally:
        db.close()
    return {
        "status": "ok",
        "queue": {"pending": queue_pending, "failed": queue_failed},
        "components": {
            "llm": settings.llm_configured,
            "llm_fallback": settings.llm_fallback_configured,
            "embedding": settings.embedding_configured,
            "rerank": settings.rerank_configured,
            "douyin": settings.douyin_configured,
            "xiaohongshu": settings.xhs_configured,
            "zhini": settings.zhini_configured,
            "alert_webhook": bool(settings.alert_webhook_url),
        },
        "errors_last_hour": monitor.snapshot(),
    }
