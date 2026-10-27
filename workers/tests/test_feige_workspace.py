"""真实飞鸽工作台的稳定节点：不依赖哈希 class。"""
import os
import sys

import pytest

playwright = pytest.importorskip("playwright.async_api")
from playwright.async_api import async_playwright  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from douyin_feige_worker import FeigeDriver  # noqa: E402

EMPTY = """
<title>飞鸽客服系统</title>
<div id="imroot">
  <div id="qa-active-chat-tab">当前会话</div>
  <div id="chantListScrollArea"><div>暂无会话中用户</div></div>
  <div>请选择会话，与消费者聊天</div>
</div>
"""

LOGIN = """
<title>飞鸽客服系统</title>
<div id="imroot"><div>请使用扫码登录</div></div>
"""

BOT = """
<title>飞鸽客服系统</title>
<div id="imroot">
  <div id="chantListScrollArea"><div>暂无会话中用户</div></div>
  <div>AI智能客服 - 挽单策略配置</div>
</div>
"""

CHAT = """
<title>飞鸽客服系统</title>
<div id="imroot">
  <div id="chantListScrollArea"><div id="row">买家小王</div></div>
  <div id="pigeon-intelligent-light-app">
    <button class="index_send-btn__drawer">抽屉发送</button>
    <div contenteditable="true" id="drawer-editor">不要点我</div>
  </div>
  <div contenteditable="true" id="editor"></div>
  <button class="index_send-btn__a1dlS" id="send">发送</button>
  <div id="sent"></div>
</div>
<script>
document.getElementById("send").onclick = () => {
  const editor = document.getElementById("editor");
  document.getElementById("sent").textContent = editor.innerText;
  editor.innerText = "";
};
</script>
"""


async def _page(html: str):
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=True)
    except Exception as exc:
        await pw.stop()
        pytest.skip(f"Chromium 未安装: {exc}")
    page = await browser.new_page()
    await page.set_content(html)
    return pw, browser, page


@pytest.mark.asyncio
async def test_empty_workspace_is_online_and_has_no_messages():
    pw, browser, page = await _page(EMPTY)
    driver = FeigeDriver()
    try:
        assert await driver.check_status(page) == "online"
        assert await driver.read_incoming(page) == []
        assert await driver.read_self_name(page) == ""
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_login_wall_and_platform_bot():
    pw, browser, page = await _page(LOGIN)
    driver = FeigeDriver()
    try:
        assert await driver.check_status(page) == "login_expired"
        await page.set_content(BOT)
        assert await driver.check_status(page) == "online"
        assert await driver.check_platform_bot_disabled(page) is False
    finally:
        await browser.close()
        await pw.stop()


@pytest.mark.asyncio
async def test_workspace_send_uses_visible_editor_not_bot_drawer():
    pw, browser, page = await _page(CHAT)
    driver = FeigeDriver()
    try:
        await driver.send_message(page, "买家小王", "你好")
        assert (await page.locator("#sent").inner_text()).strip() == "你好"
        assert (await page.locator("#editor").inner_text()).strip() == ""
        assert (await page.locator("#drawer-editor").inner_text()).strip() == "不要点我"
    finally:
        await browser.close()
        await pw.stop()
