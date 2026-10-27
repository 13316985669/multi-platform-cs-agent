"""账号职责绑定：driver 与平台、职责、出站消息类型的对应关系。"""
from .config import settings
from .core.runtime_settings import ACCOUNT_TYPES

DRIVERS: dict[str, dict] = {
    "douyin_feige": {
        "platform": "douyin", "duty": "dm", "label": "抖音飞鸽私信",
        "msg_types": ["text", "image", "voice"],
    },
    "douyin_enterprise": {
        "platform": "douyin", "duty": "dm", "label": "抖音企业号私信",
        "msg_types": ["text", "image", "voice"],
    },
    "douyin_laike": {
        "platform": "douyin", "duty": "dm", "label": "抖音来客私信",
        "msg_types": ["text", "image", "voice"],
    },
    "xhs_ark": {
        "platform": "xiaohongshu", "duty": "dm", "label": "小红书千帆私信",
        "msg_types": ["text", "image", "voice"],
    },
    "douyin_comment": {
        "platform": "douyin", "duty": "comment", "label": "抖音企业号评论",
        "msg_types": ["comment_reply", "first_comment"],
    },
    "xhs_comment": {
        "platform": "xiaohongshu", "duty": "comment", "label": "小红书评论",
        "msg_types": ["comment_reply", "first_comment"],
    },
    "xhs_publish": {
        "platform": "xiaohongshu", "duty": "publish", "label": "小红书发布",
        "msg_types": ["publish_note", "collect_stats", "collect_account"],
    },
}

DUTIES = ("dm", "comment", "publish")
PROVIDERS = ("adspower", "local")
DISABLED = "disabled"
# 抖音私信形态 → driver。空形态不在这里，由 resolve 回退全局默认。
DOUYIN_DM_DRIVERS = {
    "feige": "douyin_feige",
    "enterprise": "douyin_enterprise",
    "laike": "douyin_laike",
}


def driver_spec(driver: str) -> dict | None:
    return DRIVERS.get(driver)


def resolve_douyin_account_type(account=None) -> str:
    """账号显式形态优先；空或非法值回退全站默认。"""
    raw = ""
    if account is not None:
        raw = (getattr(account, "douyin_account_type", None) or "").strip()
    if raw in ACCOUNT_TYPES:
        return raw
    fallback = (settings.douyin_account_type or "").strip()
    return fallback if fallback in ACCOUNT_TYPES else "feige"


def douyin_dm_driver_for(account=None) -> str:
    return DOUYIN_DM_DRIVERS[resolve_douyin_account_type(account)]


def is_enabled(auth_status: str) -> bool:
    return auth_status != DISABLED
