"""打开执行用的浏览器页面。

local：Playwright 自己的持久化目录，供测试和未装 AdsPower 的机器。
adspower：指纹浏览器负责隔离，Playwright 只通过 CDP 操作已经打开的窗口。
"""
from __future__ import annotations

import asyncio
import logging
import os
import random

import httpx
from playwright.async_api import Page

logger = logging.getLogger("rpa_worker")


class BrowserSession:
    def __init__(self, page: Page, closer):
        self.page = page
        self._closer = closer

    async def close(self):
        await self._closer()


class AdsPowerError(RuntimeError):
    pass


class AdsPowerClient:
    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = (base_url or "http://127.0.0.1:50325").rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def start(self, profile_id: str, *, headless: bool = False) -> str:
        if not profile_id:
            raise AdsPowerError("未配置 AdsPower 环境 ID")
        payload = {
            "profile_id": profile_id,
            "last_opened_tabs": "0",
            "proxy_detection": "0",
            "headless": "1" if headless else "0",
            # 默认会挡住 Playwright 的调试连接
            "cdp_mask": "0",
        }
        data = await self._request("POST", "/api/v2/browser-profile/start", json=payload)
        ws = ((data.get("data") or {}).get("ws") or {}).get("puppeteer") or ""
        if not ws:
            raise AdsPowerError("AdsPower 未返回 CDP 地址")
        return ws

    async def stop(self, profile_id: str):
        if not profile_id:
            return
        try:
            await self._request("POST", "/api/v2/browser-profile/stop", json={"profile_id": profile_id})
        except Exception:  # noqa: BLE001
            logger.warning("关闭 AdsPower 环境失败 profile=%s", profile_id, exc_info=True)

    async def list_profiles(self, limit: int = 500) -> list[dict]:
        """本机环境列表，只保留 id 和名称。按页拉全，接口失败时抛出，由心跳忽略。"""
        profiles: list[dict] = []
        page = 1
        while len(profiles) < limit:
            page_size = min(100, limit - len(profiles))
            data = await self._request(
                "GET", "/api/v1/user/list",
                params={"page": page, "page_size": page_size},
            )
            items = ((data.get("data") or {}).get("list") or [])
            if not items:
                break
            for item in items:
                profile_id = str(item.get("user_id") or item.get("profile_id") or "")
                if not profile_id:
                    continue
                profiles.append({
                    "id": profile_id,
                    "name": str(item.get("name") or item.get("remark") or profile_id),
                })
                if len(profiles) >= limit:
                    break
            if len(items) < page_size:
                break
            page += 1
        return profiles

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        async with httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=60) as client:
            resp = await client.request(method, path, **kwargs)
            resp.raise_for_status()
            data = resp.json()
        if data.get("code") not in (0, "0"):
            raise AdsPowerError(str(data.get("msg") or "AdsPower 接口失败"))
        return data


async def open_session(pw, cfg) -> BrowserSession:
    """按配置打开页面。AdsPower 多进程同时启动时先错开几秒。"""
    if (cfg.browser_provider or "local") == "adspower":
        await _stagger()
        client = AdsPowerClient(cfg.adspower_api_base, cfg.adspower_api_key)
        ws = await client.start(cfg.adspower_profile_id, headless=cfg.headless)
        browser = await _connect_when_ready(pw, ws)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        async def closer():
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                logger.warning("断开 CDP 失败", exc_info=True)
            await client.stop(cfg.adspower_profile_id)

        return BrowserSession(page, closer)

    context = await pw.chromium.launch_persistent_context(
        user_data_dir=os.path.join(cfg.profile_dir, cfg.account or "default"),
        headless=cfg.headless,
        viewport={"width": 1440, "height": 900},
    )
    page = context.pages[0] if context.pages else await context.new_page()

    async def closer():
        await context.close()

    return BrowserSession(page, closer)


async def _connect_when_ready(pw, ws: str, attempts: int = 8):
    """AdsPower 先返回调试地址，Chrome 端口可能还没监听。"""
    delay = 0.6
    last = None
    for i in range(1, attempts + 1):
        try:
            return await pw.chromium.connect_over_cdp(ws)
        except Exception as exc:  # noqa: BLE001
            last = exc
            text = str(exc)
            if "ECONNREFUSED" not in text and "WebSocket error" not in text:
                raise
            logger.warning("调试端口未就绪（第 %d/%d 次）: %s", i, attempts, text.splitlines()[0][:160])
            await asyncio.sleep(delay)
            delay = min(delay * 1.6, 3)
    raise last


async def _stagger():
    await asyncio.sleep(random.uniform(0, 3))
