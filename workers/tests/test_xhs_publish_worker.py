"""小红书发布 Worker fixture 集成测试：发布流程不依赖真实平台即可验证。

运行前提：pip install playwright pytest pytest-asyncio && playwright install chromium
未安装 Playwright 或浏览器时自动跳过。

覆盖：
- 页面自检（online / login_expired）
- publish_note 任务：下载素材 → 填标题/正文/话题 → 上传图片 → 发布 → 成功 toast → ack 带笔记 URL
- 频控：每日上限 / 发布间隔未到时跳过（不 ack，租约自动回 pending）
"""
import json
import os
import sys
import time

import pytest

playwright = pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from base_worker import WorkerConfig  # noqa: E402
from xhs_publish_worker import (DAILY_LIMIT, PublishWorker,  # noqa: E402
                                XhsPublishDriver)

FIXTURE_URL = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "fake_xhs_creator.html")
).replace("\\", "/")


class FakeBackend:
    """内存版后端（含素材下载与 result 回执）。"""

    def __init__(self):
        self.outbox: list[dict] = []
        self.acks: list[tuple] = []
        self._materials: dict[int, bytes] = {}

    async def heartbeat(self, status, meta=None, dry_run=False):
        pass

    async def pull_outbox(self, limit=5):
        msgs = [m for m in self.outbox if m["status"] == "pending"][:limit]
        for m in msgs:
            m["status"] = "leased"
        return msgs

    async def ack(self, outbox_id, ok, error="", result=""):
        self.acks.append((outbox_id, ok, error, result))

    async def download_material(self, material_id, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(self._materials[int(material_id)])
        return dest

    async def health(self):
        return True


class FixturePublishDriver(XhsPublishDriver):
    url = FIXTURE_URL


@pytest.fixture()
def worker(tmp_path):
    cfg = WorkerConfig(
        backend_url="http://fake", rpa_key="k", worker_id="w-pub",
        account="xhs_main", platform="xiaohongshu_publish",
        state_db=str(tmp_path / "state.db"), download_dir=str(tmp_path / "dl"),
    )
    backend = FakeBackend()
    # 1x1 像素 PNG 字节（素材下载用）
    backend._materials[101] = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c626001000000ffff030000060005"
        "57bfabd40000000049454e44ae426082")
    return PublishWorker(cfg, FixturePublishDriver(), backend), backend


async def _open_fixture():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"Chromium 未安装: {exc}")
    page = await browser.new_page()
    await page.goto(FIXTURE_URL)
    return pw, browser, page


def _publish_item(outbox_id: int) -> dict:
    return {
        "outbox_id": outbox_id,
        "conversation_id": "",
        "user_id": "",
        "msg_type": "publish_note",
        "content": json.dumps({
            "kind": "publish_note",
            "title": "便携榨汁杯真的绝",
            "body": "姐妹们这个杯子太方便了",
            "tags": ["好物推荐"],
            "material_ids": [101],
            "task_id": 1,
        }, ensure_ascii=False),
        "media_id": "",
        "status": "pending",
    }


@pytest.mark.asyncio
async def test_check_status_online(worker):
    w, _ = worker
    pw, browser, page = await _open_fixture()
    try:
        assert await w.driver.check_status(page) == "online"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_check_status_login_expired(worker):
    w, _ = worker
    pw, browser, page = await _open_fixture()
    try:
        await page.click("text=切换登录过期")
        assert await w.driver.check_status(page) == "login_expired"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_publish_note_flow(worker):
    """完整发布流程：素材下载 + 填写 + 发布成功 toast + ack 带 URL。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        backend.outbox.append(_publish_item(1))
        messages = await backend.pull_outbox()
        await w._send_one(page, messages[0])

        assert len(backend.acks) == 1
        outbox_id, ok, error, result = backend.acks[0]
        assert outbox_id == 1 and ok is True
        assert result  # 笔记 URL 已回传

        # 页面断言：标题/正文/话题已填
        assert await page.locator(".title-input").input_value() == "便携榨汁杯真的绝"
        assert "姐妹们" in await page.locator(".body-editor").inner_text()
        assert "好物推荐" in await page.locator("#topics").inner_text()
        assert await page.locator(".publish-success-toast").is_visible()

        # 频控计数已记录
        assert w.state.kv_get(w._today_key()) == "1"
        assert float(w.state.kv_get("next_publish_after", "0")) > time.time()
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_rate_limit_skips_without_ack(worker):
    """发布间隔未到 → 跳过且不 ack（租约到期自动回 pending，下轮再试）。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        w.state.kv_set("next_publish_after", str(time.time() + 3600))
        backend.outbox.append(_publish_item(2))
        messages = await backend.pull_outbox()
        await w._send_one(page, messages[0])
        assert backend.acks == []  # 未 ack
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_daily_limit_skips(worker):
    """当日发布达上限 → 跳过。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        w.state.kv_set(w._today_key(), str(DAILY_LIMIT))
        backend.outbox.append(_publish_item(3))
        messages = await backend.pull_outbox()
        await w._send_one(page, messages[0])
        assert backend.acks == []
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_non_publish_message_ignored(worker):
    """非 publish_note 消息不处理（私信由 ark worker 负责）。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        backend.outbox.append({
            "outbox_id": 9, "conversation_id": "conv_a", "user_id": "x",
            "content": "私信回复", "msg_type": "text", "media_id": "", "status": "pending",
        })
        messages = await backend.pull_outbox()
        await w._send_one(page, messages[0])
        assert backend.acks == []
    finally:
        await browser.close()
        await pw.stop()
