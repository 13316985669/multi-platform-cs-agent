"""平台适配器注册表 + 出站通道路由（官方 API / RPA 降级）。"""
from ..config import settings
from .base import PlatformAdapter
from .mock import MockAdapter
from .douyin import DouyinAdapter
from .xiaohongshu import XiaohongshuAdapter
from .rpa import RpaAdapter

_ADAPTERS: dict[str, PlatformAdapter] = {
    "mock": MockAdapter(),
    "douyin": DouyinAdapter(),
    "xiaohongshu": XiaohongshuAdapter(),
    "rpa": RpaAdapter(),  # 入站解析用（platform 字段透传真实平台）
}

# 出站 RPA 适配器按平台实例化，outbox 记录携带正确平台标识
_RPA_SEND_ADAPTERS: dict[str, RpaAdapter] = {
    "douyin": RpaAdapter("douyin"),
    "xiaohongshu": RpaAdapter("xiaohongshu"),
}


def get_adapter(platform: str) -> PlatformAdapter:
    adapter = _ADAPTERS.get(platform)
    if adapter is None:
        # 未识别的平台走 Mock 兜底（仅记录日志，不对外发送）
        return _ADAPTERS["mock"]
    return adapter


def send_channel(platform: str) -> str:
    """平台出站通道：api（官方 API） | rpa（RPA 降级）。"""
    return {
        "douyin": settings.douyin_channel,
        "xiaohongshu": settings.xhs_channel,
    }.get(platform, "api")


def get_send_adapter(platform: str) -> PlatformAdapter:
    """出站适配器：按平台通道配置在官方 API 与 RPA 间路由。"""
    if send_channel(platform) == "rpa":
        adapter = _RPA_SEND_ADAPTERS.get(platform)
        if adapter is not None:
            return adapter
    return get_adapter(platform)


def get_rpa_fallback_adapter(platform: str) -> PlatformAdapter | None:
    """API 通道发送失败时的 RPA 降级适配器。

    仅在主通道为 api 且 RPA 已配置（RPA_API_KEY 非空）时提供；
    主通道已是 rpa 或不支持 RPA 的平台返回 None。
    """
    if send_channel(platform) != "api" or not settings.rpa_configured:
        return None
    return _RPA_SEND_ADAPTERS.get(platform)


__all__ = ["PlatformAdapter", "get_adapter", "get_send_adapter", "get_rpa_fallback_adapter",
           "send_channel"]
