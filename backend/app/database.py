"""数据库连接：SQLite 默认，通过 DATABASE_URL 可换 PostgreSQL。"""
import os
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, declarative_base, Session

from .config import settings

# 确保数据目录存在
if settings.database_url.startswith("sqlite"):
    db_path = settings.database_url.split("///")[-1]
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)

engine = create_engine(
    settings.database_url,
    connect_args={"check_same_thread": False} if settings.database_url.startswith("sqlite") else {},
    pool_pre_ping=True,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_conn, connection_record):
    """SQLite 开启 WAL，提升并发读写能力。"""
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_column(table: str, column: str, ddl: str):
    """轻量迁移：老库缺列时 ALTER TABLE 补上（仅 SQLite；PostgreSQL 新库由 create_all 建全量，
    存量变更请用 Alembic）。"""
    if not settings.database_url.startswith("sqlite"):
        return
    with engine.connect() as conn:
        cols = [r[1] for r in conn.exec_driver_sql(f"PRAGMA table_info({table})")]
        if cols and column not in cols:
            conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db():
    from . import models  # noqa: F401 确保模型已注册
    Base.metadata.create_all(bind=engine)
    _ensure_column("banned_words", "direction", "direction VARCHAR(8) DEFAULT 'out'")
    # 内容矩阵平台：Customer 引流归因列
    _ensure_column("customers", "wecom_added_at", "wecom_added_at DATETIME")
    _ensure_column("customers", "source_guide_code", "source_guide_code VARCHAR(32) DEFAULT ''")
    # RPA 出站回执结果列（发布类任务回传笔记 URL 等）
    _ensure_column("rpa_outbox", "result", "result VARCHAR(512) DEFAULT ''")
    _ensure_column("rpa_outbox", "question", "question TEXT DEFAULT ''")
    from .adapters.rpa import backfill_reply_questions, resnapshot_dm_questions
    resnapshot_dm_questions()
    backfill_reply_questions()
    # Phase 6：发布队列 / 首评 / 查重 / 审批 / 灵感来源
    _ensure_column("matrix_accounts", "queue_enabled", "queue_enabled BOOLEAN DEFAULT 0")
    _ensure_column("matrix_accounts", "queue_slots", "queue_slots TEXT DEFAULT '[]'")
    _ensure_column("content_versions", "first_comment", "first_comment VARCHAR(512) DEFAULT ''")
    _ensure_column("content_versions", "dup_report", "dup_report TEXT DEFAULT '{}'")
    _ensure_column("content_items", "review_note", "review_note VARCHAR(512) DEFAULT ''")
    _ensure_column("content_items", "reviewed_by", "reviewed_by INTEGER DEFAULT 0")
    _ensure_column("content_items", "reviewed_at", "reviewed_at DATETIME")
    _ensure_column("content_items", "inspiration_id", "inspiration_id INTEGER DEFAULT 0")
    # Phase 7：一稿多版 / 账号画像
    _ensure_column("content_versions", "variant_no", "variant_no INTEGER DEFAULT 1")
    _ensure_column("matrix_accounts", "profile_json", "profile_json TEXT DEFAULT '{}'")
    # 按账号指定抖音形态（空 = 跟随全局默认 DOUYIN_ACCOUNT_TYPE）
    _ensure_column("matrix_accounts", "douyin_account_type", "douyin_account_type VARCHAR(16) DEFAULT ''")
    # 队列延迟任务（首评等）
    _ensure_column("queue_tasks", "not_before", "not_before DATETIME")
    _ensure_column("agents", "max_concurrent", "max_concurrent INTEGER DEFAULT 20")
    _ensure_column("agents", "modules", "modules TEXT")
    _ensure_column("quick_replies", "agent_id", "agent_id INTEGER")
    _ensure_column("knowledge_docs", "category", "category VARCHAR(64) DEFAULT '未分类'")
    _ensure_column("knowledge_docs", "similar_questions", "similar_questions TEXT DEFAULT '[]'")
    _ensure_column("knowledge_docs", "hit_count", "hit_count INTEGER DEFAULT 0")
    _ensure_column("account_bindings", "reopen_requested", "reopen_requested INTEGER DEFAULT 0")
    _ensure_column("missed_questions", "suggested_answer", "suggested_answer TEXT DEFAULT ''")
    _ensure_column("conversations", "lead_followups", "lead_followups INTEGER DEFAULT 0")
