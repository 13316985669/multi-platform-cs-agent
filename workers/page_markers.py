"""各平台后台共用的页面文案判断。只认可见文字，不猜 class。"""
from __future__ import annotations

LOGIN_MARKERS = ("扫码登录", "密码登录", "手机号登录", "短信登录")


async def page_text(page) -> str:
    try:
        return await page.locator("body").inner_text(timeout=3000)
    except Exception:  # noqa: BLE001
        return ""


def is_login_wall(text: str) -> bool:
    return any(marker in (text or "") for marker in LOGIN_MARKERS)
