"""Worker ↔ 伪平台 fixture 集成测试：选择器逻辑不依赖真实店铺即可验证。

运行前提：pip install playwright pytest pytest-asyncio && playwright install chromium
未安装 Playwright 或浏览器时自动跳过（不影响后端测试）。

覆盖：
- 页面自检（online / login_expired / selector_mismatch）
- 用户文本/图片消息 → read_incoming → 上报 FakeBackend
- 旁路消息（我方气泡）识别为 sender_side=agent
- outbox 消息 → 页面发送 → 我方气泡断言
"""
import os
import sys
import uuid

import pytest

playwright = pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from base_worker import BaseWorker, WorkerConfig  # noqa: E402
from douyin_enterprise_worker import EnterpriseDriver  # noqa: E402
from douyin_feige_worker import FeigeDriver  # noqa: E402
from douyin_laike_worker import LaikeDriver  # noqa: E402

FIXTURE_URL = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "fake_feige.html")
).replace("\\", "/")

ENTERPRISE_FIXTURE_URL = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "fake_enterprise.html")
).replace("\\", "/")

LAIKE_FIXTURE_URL = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "fixtures", "fake_laike.html")
).replace("\\", "/")


class FakeBackend:
    """内存版后端：实现 BackendClient 协议，断言用。"""

    def __init__(self):
        self.incoming: list[dict] = []
        self.outbox: list[dict] = []
        self.acks: list[tuple[int, bool, str]] = []
        self.heartbeats: list[str] = []
        self._media: dict[str, bytes] = {}
        self._seq = 0

    async def heartbeat(self, status, meta=None):
        self.heartbeats.append(status)

    async def pull_outbox(self, limit=5):
        msgs = [m for m in self.outbox if m["status"] == "pending"][:limit]
        for m in msgs:
            m["status"] = "leased"
        return msgs

    async def ack(self, outbox_id, ok, error=""):
        self.acks.append((outbox_id, ok, error))

    async def report_incoming(self, item):
        self._seq += 1
        self.incoming.append(item)
        return f"rpa_test_{self._seq:04d}"

    async def upload_media(self, data, filename, mime, kind):
        media_id = f"media_{uuid.uuid4().hex[:8]}"
        self._media[media_id] = data
        return media_id

    async def download_media(self, media_id, dest):
        with open(dest, "wb") as f:
            f.write(self._media[media_id])
        return dest

    async def health(self):
        return True


class FixtureFeigeDriver(FeigeDriver):
    url = FIXTURE_URL


@pytest.fixture()
def worker(tmp_path):
    cfg = WorkerConfig(
        backend_url="http://fake", rpa_key="k", worker_id="w-test",
        account="shopA", platform="douyin",
        state_db=str(tmp_path / "state.db"), download_dir=str(tmp_path / "dl"),
    )
    backend = FakeBackend()
    return BaseWorker(cfg, FixtureFeigeDriver(), backend), backend


async def _open_fixture():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:  # 浏览器未安装（playwright install chromium 未跑）
        await pw.stop()
        pytest.skip(f"Chromium 未安装: {exc}")
    page = await browser.new_page()
    await page.goto(FIXTURE_URL)
    return pw, browser, page


@pytest.mark.asyncio
async def test_check_status_online(worker):
    w, _ = worker
    pw, browser, page = await _open_fixture()
    try:
        assert await w.driver.check_status(page) == "online"
        assert await w.driver.check_platform_bot_disabled(page) is True
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
async def test_incoming_text_message_reported(worker):
    """注入用户文本消息 → read_incoming → 上报后端。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        await page.fill("#simText", "这个多少钱")
        await page.click("text=注入用户消息")

        items = await w.driver.read_incoming(page)
        assert len(items) == 1
        assert items[0].content == "这个多少钱"
        assert items[0].nickname == "买家小王"
        assert items[0].sender_side == "user"

        await w._report_one(items[0])
        assert len(backend.incoming) == 1
        assert backend.incoming[0]["content"] == "这个多少钱"

        # 去重：同一 msg_key 不重复上报
        await w._report_one(items[0])
        assert len(backend.incoming) == 1
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_incoming_image_message_uploaded(worker):
    """注入用户图片消息 → 媒体字节上传后端，media_id 透传。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        await page.select_option("#simType", "image")
        await page.click("text=注入用户消息")

        items = await w.driver.read_incoming(page)
        assert len(items) == 1
        assert items[0].msg_type == "image"
        assert items[0].media_url.startswith("data:image/png")

        await w._report_one(items[0])
        assert backend.incoming[0]["media_id"] != ""
        assert len(backend._media) == 1  # 字节已上传
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_outbound_message_sent_to_page(worker):
    """outbox 消息 → 页面发送 → 我方气泡出现 → ack 成功。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        backend.outbox.append({
            "outbox_id": 1, "conversation_id": "conv_a",
            "user_id": "rpa_shopA_x", "content": "亲，马上为您查询",
            "msg_type": "text", "media_id": "", "status": "pending",
        })
        messages = await backend.pull_outbox()
        assert len(messages) == 1

        await w._send_one(page, messages[0])

        assert backend.acks == [(1, True, "")]
        bubble = page.locator(".message-item[data-side='self'] .message-text")
        assert await bubble.count() == 1
        assert await bubble.first.inner_text() == "亲，马上为您查询"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_outbound_unknown_conversation_acks_failure(worker):
    """会话不存在 → ack 失败（后端将重试/置 failed）。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        backend.outbox.append({
            "outbox_id": 2, "conversation_id": "conv_not_exist",
            "user_id": "x", "content": "测试", "msg_type": "text",
            "media_id": "", "status": "pending",
        })
        messages = await backend.pull_outbox()
        await w._send_one(page, messages[0])
        assert backend.acks[0][0] == 2
        assert backend.acks[0][1] is False
        assert "会话不存在" in backend.acks[0][2]
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_bypass_message_scanned_without_unread(worker):
    """人工客服在平台后台直接回复（会话无未读红点）→ SCAN_RECENT 扫描仍能捕获旁路消息。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        # 模拟人工客服在平台后台直接发消息：self 气泡，且不产生未读红点
        await page.evaluate("""
            () => {
                const conv = conversations.find(c => c.key === 'conv_b');
                conv.messages.push({ mid: 'm_bypass_1', side: 'self', type: 'text',
                                     text: '亲，我直接在后台回复你' });
            }
        """)
        items = await w.driver.read_incoming(page)
        bypass = [it for it in items if it.msg_key == "m_bypass_1"]
        assert len(bypass) == 1
        assert bypass[0].sender_side == "agent"
        assert bypass[0].content == "亲，我直接在后台回复你"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_send_confirmation_counts_new_bubble(worker):
    """会话已有历史我方气泡时，发送确认仍能识别本次发送（计数 +1，而非 selector 立即命中）。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        # 预置一条历史我方气泡（旧逻辑下 selector 会立即命中造成假确认）
        await page.evaluate("""
            () => {
                const conv = conversations.find(c => c.key === 'conv_a');
                conv.messages.push({ mid: 'm_hist_1', side: 'self', type: 'text', text: '历史回复' });
            }
        """)
        backend.outbox.append({
            "outbox_id": 3, "conversation_id": "conv_a",
            "user_id": "rpa_shopA_x", "content": "新的回复",
            "msg_type": "text", "media_id": "", "status": "pending",
        })
        messages = await backend.pull_outbox()
        await w._send_one(page, messages[0])
        assert backend.acks == [(3, True, "")]
        assert await page.locator(".message-item[data-side='self']").count() == 2
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_send_confirmation_timeout_acks_failure(worker):
    """发送后无新气泡（页面未真正发出）→ 超时 → ack 失败（后端将重试/置 failed）。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        # content 为空且无附件：fixture 的发送按钮不会产生新气泡，模拟"点了没发出去"
        backend.outbox.append({
            "outbox_id": 4, "conversation_id": "conv_a",
            "user_id": "x", "content": "", "msg_type": "text",
            "media_id": "", "status": "pending",
        })
        messages = await backend.pull_outbox()
        await w._send_one(page, messages[0])
        assert backend.acks[0][0] == 4
        assert backend.acks[0][1] is False
        assert "发送可能失败" in backend.acks[0][2]
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_deterministic_msg_key_no_duplicate_report(worker):
    """气泡无 data-mid 时用内容哈希兜底：两轮扫描 msg_key 稳定，去重后不重复上报。"""
    w, backend = worker
    pw, browser, page = await _open_fixture()
    try:
        # mid 为空字符串 → get_attribute 返回 "" → 走确定性哈希兜底
        await page.evaluate("""
            () => {
                const conv = conversations.find(c => c.key === 'conv_a');
                conv.messages.push({ mid: '', side: 'user', type: 'text', text: '没有ID的消息' });
                renderConvs();
            }
        """)
        items1 = await w.driver.read_incoming(page)
        target1 = [it for it in items1 if it.content == "没有ID的消息"]
        assert len(target1) == 1 and target1[0].msg_key
        for it in items1:
            await w._report_one(it)
        reported = len(backend.incoming)
        assert reported == 1

        # 第二轮扫描：同一消息产生相同 msg_key，去重后不再上报
        items2 = await w.driver.read_incoming(page)
        target2 = [it for it in items2 if it.content == "没有ID的消息"]
        assert len(target2) == 1
        assert target2[0].msg_key == target1[0].msg_key
        for it in items2:
            await w._report_one(it)
        assert len(backend.incoming) == reported
    finally:
        await browser.close()
        await pw.stop()


# ============ 企业号 driver（fake_enterprise.html 契约） ============

class FixtureEnterpriseDriver(EnterpriseDriver):
    url = ENTERPRISE_FIXTURE_URL


@pytest.fixture()
def ent_worker(tmp_path):
    cfg = WorkerConfig(
        backend_url="http://fake", rpa_key="k", worker_id="w-ent",
        account="shopEnt", platform="douyin_enterprise",
        state_db=str(tmp_path / "state.db"), download_dir=str(tmp_path / "dl"),
    )
    backend = FakeBackend()
    return BaseWorker(cfg, FixtureEnterpriseDriver(), backend), backend


async def _open_enterprise_fixture():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"Chromium 未安装: {exc}")
    page = await browser.new_page()
    await page.goto(ENTERPRISE_FIXTURE_URL)
    return pw, browser, page


def test_enterprise_platform_maps_to_douyin(ent_worker):
    """企业号 driver 上报后端时平台归 douyin（会话/统计/通道路由一致）。"""
    w, _ = ent_worker
    assert w.cfg.platform == "douyin_enterprise"
    assert w.cfg.backend_platform == "douyin"


@pytest.mark.asyncio
async def test_enterprise_check_status_online(ent_worker):
    w, _ = ent_worker
    pw, browser, page = await _open_enterprise_fixture()
    try:
        assert await w.driver.check_status(page) == "online"
        assert await w.driver.check_platform_bot_disabled(page) is True
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_enterprise_incoming_and_outbound(ent_worker):
    """企业号契约：用户消息入站上报 + outbox 出站发送 + 气泡确认。"""
    w, backend = ent_worker
    pw, browser, page = await _open_enterprise_fixture()
    try:
        # 入站：注入用户文本消息
        await page.fill("#simText", "你们企业服务怎么收费")
        await page.click("text=注入用户消息")
        items = await w.driver.read_incoming(page)
        assert len(items) == 1
        assert items[0].content == "你们企业服务怎么收费"
        assert items[0].nickname == "咨询客户甲"
        await w._report_one(items[0])
        assert len(backend.incoming) == 1

        # 出站：发送回复并确认气泡
        backend.outbox.append({
            "outbox_id": 10, "conversation_id": "conv_a",
            "user_id": "rpa_shopEnt_x", "content": "亲，按服务模块计费，我给您详细介绍",
            "msg_type": "text", "media_id": "", "status": "pending",
        })
        messages = await backend.pull_outbox()
        await w._send_one(page, messages[0])
        assert backend.acks == [(10, True, "")]
        bubble = page.locator(".message-item[data-side='self'] .message-text")
        assert await bubble.first.inner_text() == "亲，按服务模块计费，我给您详细介绍"
    finally:
        await browser.close()
        await pw.stop()


# ============ 来客 driver（fake_laike.html 契约） ============

class FixtureLaikeDriver(LaikeDriver):
    url = LAIKE_FIXTURE_URL


@pytest.fixture()
def laike_worker(tmp_path):
    cfg = WorkerConfig(
        backend_url="http://fake", rpa_key="k", worker_id="w-laike",
        account="shopLaike", platform="douyin_laike",
        state_db=str(tmp_path / "state.db"), download_dir=str(tmp_path / "dl"),
    )
    backend = FakeBackend()
    return BaseWorker(cfg, FixtureLaikeDriver(), backend), backend


async def _open_laike_fixture():
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"Chromium 未安装: {exc}")
    page = await browser.new_page()
    await page.goto(LAIKE_FIXTURE_URL)
    return pw, browser, page


def test_laike_platform_maps_to_douyin(laike_worker):
    """来客 driver 上报后端时平台归 douyin（会话/统计/通道路由一致）。"""
    w, _ = laike_worker
    assert w.cfg.platform == "douyin_laike"
    assert w.cfg.backend_platform == "douyin"


@pytest.mark.asyncio
async def test_laike_check_status_online(laike_worker):
    w, _ = laike_worker
    pw, browser, page = await _open_laike_fixture()
    try:
        assert await w.driver.check_status(page) == "online"
        assert await w.driver.check_platform_bot_disabled(page) is True
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_laike_incoming_and_outbound(laike_worker):
    """来客契约：用户消息入站上报 + outbox 出站发送 + 气泡确认。"""
    w, backend = laike_worker
    pw, browser, page = await _open_laike_fixture()
    try:
        # 入站：注入用户文本消息
        await page.fill("#simText", "你们门店的团购怎么用")
        await page.click("text=注入用户消息")
        items = await w.driver.read_incoming(page)
        assert len(items) == 1
        assert items[0].content == "你们门店的团购怎么用"
        assert items[0].nickname == "团购咨询客甲"
        await w._report_one(items[0])
        assert len(backend.incoming) == 1

        # 出站：发送回复并确认气泡
        backend.outbox.append({
            "outbox_id": 11, "conversation_id": "conv_a",
            "user_id": "rpa_shopLaike_x", "content": "亲，购买后到店出示券码即可核销",
            "msg_type": "text", "media_id": "", "status": "pending",
        })
        messages = await backend.pull_outbox()
        await w._send_one(page, messages[0])
        assert backend.acks == [(11, True, "")]
        bubble = page.locator(".message-item[data-side='self'] .message-text")
        assert await bubble.first.inner_text() == "亲，购买后到店出示券码即可核销"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_laike_upgrade_modal_only_on_open(laike_worker):
    """升级弹窗只在打开时看一次：没有就马上返回，有则点 ×，读消息不再关。"""
    w, _ = laike_worker
    pw, browser, page = await _open_laike_fixture()
    try:
        blank = await browser.new_page()
        await blank.set_content("<html><body><p>no-modal</p></body></html>")

        async def fail_wait(*_args, **_kwargs):
            raise AssertionError("没有弹窗不应等待")

        blank.wait_for_timeout = fail_wait
        driver = LaikeDriver()
        await driver.dismiss_blockers(blank)
        await driver.dismiss_blockers(blank)

        opened = LaikeDriver()
        original_wait = page.wait_for_timeout
        page.wait_for_timeout = fail_wait
        await opened.dismiss_blockers(page)
        await page.evaluate("showUpgradeModal()")
        await opened.dismiss_blockers(page)
        assert await page.locator("#upgradeModal").is_visible()

        fresh = LaikeDriver()
        await fresh.dismiss_blockers(page)
        page.wait_for_timeout = original_wait
        assert not await page.locator(".life-core-modal-body").is_visible()
        assert await page.locator("#upgradeTry").get_attribute("data-clicked") == "0"

        await page.fill("#simText", "门店今天还营业吗")
        await page.click("text=注入用户消息")
        items = await w.driver.read_incoming(page)
        assert len(items) == 1
        assert items[0].content == "门店今天还营业吗"
        assert await page.locator("#upgradeModal").evaluate("el => el.style.display") == "none"
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_laike_workspace_read_send_skip_modal(laike_worker):
    """真实会话中心的读、发不再查升级弹窗。"""
    w, _ = laike_worker
    pw, browser, page = await _open_laike_fixture()
    calls = {"n": 0}
    original = w.driver.dismiss_blockers

    async def counting(target):
        calls["n"] += 1
        await original(target)

    w.driver.dismiss_blockers = counting
    try:
        await page.set_content(
            "<html><body>"
            "<div class='conversationListContainer-x'></div>"
            "<textarea placeholder='回复内容'></textarea>"
            "</body></html>"
        )
        assert await w.driver.read_incoming(page) == []
        with pytest.raises(RuntimeError, match="会话不存在"):
            await w.driver.send_message(page, "不存在的顾客", "你好")
        assert calls["n"] == 0
    finally:
        await browser.close()
        await pw.stop()
