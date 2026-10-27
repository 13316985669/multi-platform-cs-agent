"""发布通道注册表：按平台 + 账号接入方式路由。

- mock：全流程联调
- douyin + api：官方开放平台发布（video.create.bind）
- douyin + rpa：预留（创作者中心 RPA 备案通道）
- xiaohongshu + rpa：创作服务平台 RPA 发布（无官方 API）
- xiaohongshu + api：小红书无官方发布 API，不支持
"""
from ...models import MatrixAccount
from .base import PublishChannel
from .mock import MockPublishChannel

_mock = MockPublishChannel()


def get_channel(account: MatrixAccount) -> PublishChannel:
    """按账号的平台与接入方式返回发布通道。不支持的组合抛异常（调度器捕获置 failed）。"""
    if account.platform == "mock":
        return _mock
    if account.platform == "douyin" and account.auth_type == "api":
        from .douyin_api import DouyinApiPublishChannel
        return DouyinApiPublishChannel()
    if account.platform == "xiaohongshu" and account.auth_type == "rpa":
        from .xhs_rpa import XhsRpaPublishChannel
        return XhsRpaPublishChannel()
    raise RuntimeError(f"不支持的发布通道: platform={account.platform} auth_type={account.auth_type}")


__all__ = ["PublishChannel", "get_channel"]
