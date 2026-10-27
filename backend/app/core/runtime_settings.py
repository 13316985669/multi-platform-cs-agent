"""把 AI 总开关和自动化开关写入 app_settings，启动时读回。"""
import logging

from sqlalchemy.orm import Session

from ..config import settings
from ..models import AppSetting

logger = logging.getLogger(__name__)

PERSISTED_KEYS = (
    "ai_globally_enabled",
    "comment_auto_reply_enabled",
    "publish_auto_enabled",
)
ACCOUNT_TYPES = ("feige", "enterprise", "laike")
PERSISTED_TEXT_KEYS = ("douyin_account_type",)


def load_persisted(db: Session):
    rows = db.query(AppSetting).filter(
        AppSetting.key.in_(PERSISTED_KEYS + PERSISTED_TEXT_KEYS),
    ).all()
    for row in rows:
        if row.key in PERSISTED_KEYS:
            if row.value not in ("true", "false"):
                continue
            setattr(settings, row.key, row.value == "true")
        elif row.key == "douyin_account_type" and row.value in ACCOUNT_TYPES:
            setattr(settings, row.key, row.value)
        else:
            continue
        logger.info("已从数据库读回 %s=%s", row.key, row.value)


def _write(db: Session, key: str, text: str):
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=text))
    else:
        row.value = text
    db.commit()


def persist(db: Session, key: str, value: bool):
    if key not in PERSISTED_KEYS:
        return
    _write(db, key, "true" if value else "false")


def persist_text(db: Session, key: str, value: str):
    if key not in PERSISTED_TEXT_KEYS:
        return
    _write(db, key, value)
