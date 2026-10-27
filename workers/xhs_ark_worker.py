"""小红书千帆 Worker：千帆 Web 后台私信页自动化（文本 + 图片）。

目标页面：小红书千帆（商家经营平台）私信/客服页。

选择器说明（重要）：
- 选择器集中在 SELECTORS，页面改版时只需改这里
- 当前选择器与伪平台 fixture 契约一致；对接真实千帆时请按实际 DOM 调整并用 doctor.py 验证
- 若千帆 Web 私信能力受限，降级路径：Android 无障碍 Worker（见 docs/rpa-workers.md Phase 3）

运行：python xhs_ark_worker.py（.env.local 中 PLATFORM=xiaohongshu）
"""
import hashlib
import logging

from playwright.async_api import Page

from base_worker import (BaseWorker, IncomingItem, PlatformDriver, WorkerConfig,
                         run_worker)
from page_markers import is_login_wall, page_text

logger = logging.getLogger("rpa_worker.xhs")

# 千帆商家后台私信页地址（直达客服私信页；平台改地址时可用 PLATFORM_URL 覆盖，无需改代码）
ARK_URL = "https://ark.xiaohongshu.com/ark/message/platform/msg"

# ============ 选择器（与 fixture 契约一致；真实页面需按实际 DOM 调整） ============
SELECTORS = {
    "login_form": ".login-form",
    "conversation_list": ".conversation-list",
    "conversation_item": ".conversation-item",
    "unread_badge": ".unread-badge",
    "nickname": ".conv-nickname",
    "message_item": ".message-item",
    "message_text": ".message-text",
    "message_image": "img.message-image",
    "message_audio": "audio.message-audio",
    "chat_input": ".chat-input",
    "send_button": ".send-button",
    "file_input": "input[type='file']",
    "bot_enabled_flag": ".platform-bot-enabled",
    "self_name": ".self-account-name",              # 当前登录的专业号名称
}

# 每轮额外扫描会话列表顶部 N 个会话（不依赖未读红点），用于捕捉人工客服在平台后台
# 直接发送的旁路消息；靠 msg_key 去重，不会重复上报
SCAN_RECENT = 3


class XhsArkDriver(PlatformDriver):
    name = "xhs_ark"
    url = ARK_URL

    async def check_status(self, page: Page) -> str:
        # 登录表单常驻 DOM、通过显隐切换，必须按「可见性」判断（count 会把隐藏元素也算进去）
        login = page.locator(SELECTORS["login_form"])
        if await login.count() and await login.first.is_visible():
            return "login_expired"
        if await page.locator(SELECTORS["conversation_list"]).count() > 0:
            return "online"
        # 千帆已登录页还没有会话节点快照。先把登录墙认成登录过期，避免一直显示选择器失效。
        if is_login_wall(await page_text(page)):
            return "login_expired"
        return "selector_mismatch"

    async def check_platform_bot_disabled(self, page: Page) -> bool:
        """千帆「自动回复」开启时页面有对应标记（fixture 契约）。"""
        return await page.locator(SELECTORS["bot_enabled_flag"]).count() == 0

    async def read_incoming(self, page: Page) -> list[IncomingItem]:
        """扫描有未读红点的会话 + 列表顶部最近活跃会话（旁路消息场景见飞鸽 driver 注释）。"""
        items: list[IncomingItem] = []
        convs = page.locator(SELECTORS["conversation_item"])
        for i in range(await convs.count()):
            conv = convs.nth(i)
            conv_key = await conv.get_attribute("data-conv") or ""
            if not conv_key:
                continue
            has_unread = await conv.locator(SELECTORS["unread_badge"]).count() > 0
            if not has_unread and i >= SCAN_RECENT:
                continue
            nickname = await conv.locator(SELECTORS["nickname"]).inner_text()
            await conv.click()
            await page.wait_for_timeout(300)
            items.extend(await self._read_messages(page, conv_key, nickname.strip()))
        return items

    async def _read_messages(self, page: Page, conv_key: str, nickname: str) -> list[IncomingItem]:
        items: list[IncomingItem] = []
        bubbles = page.locator(SELECTORS["message_item"])
        for j in range(await bubbles.count()):
            bubble = bubbles.nth(j)
            side = await bubble.get_attribute("data-side") or "user"
            sender_side = "agent" if side == "self" else "user"

            img = bubble.locator(SELECTORS["message_image"])
            text_node = bubble.locator(SELECTORS["message_text"])
            msg_type, content, media_url = "text", "", ""
            if await img.count() > 0:
                msg_type = "image"
                media_url = await img.first.get_attribute("src") or ""
            elif await text_node.count() > 0:
                content = (await text_node.first.inner_text()).strip()
            else:
                continue

            msg_key = await bubble.get_attribute("data-mid") or ""
            if not msg_key:
                # 无稳定消息 ID 时用确定性哈希兜底（随机 uuid 会导致每轮轮询重复上报）
                fingerprint = f"{conv_key}|{side}|{msg_type}|{content or media_url}"
                msg_key = f"{conv_key}_{hashlib.md5(fingerprint.encode()).hexdigest()[:12]}"

            items.append(IncomingItem(
                conv_key=conv_key, msg_key=msg_key, nickname=nickname,
                sender_side=sender_side, msg_type=msg_type,
                content=content, media_url=media_url))
        return items

    async def send_message(self, page: Page, conv_key: str, content: str,
                           msg_type: str = "text", media_path: str = "") -> None:
        conv = page.locator(f"{SELECTORS['conversation_item']}[data-conv='{conv_key}']")
        if await conv.count() == 0:
            raise RuntimeError(f"会话不存在: {conv_key}")
        await conv.first.click()
        await page.wait_for_timeout(300)

        if msg_type == "image" and media_path:
            file_input = page.locator(SELECTORS["file_input"])
            if await file_input.count() == 0:
                raise RuntimeError("页面无附件上传入口（选择器漂移或平台不支持）")
            await file_input.first.set_input_files(media_path)
            await page.wait_for_timeout(800)

        if content:
            await BaseWorker.type_humanlike(page, SELECTORS["chat_input"], content)

        # 发送确认：发送前计数我方气泡，发送后等待数量 +1
        # （直接等 selector 会在已有历史我方气泡时立即命中，无法确认本次发送）
        self_bubbles = f"{SELECTORS['message_item']}[data-side='self']"
        before = await page.locator(self_bubbles).count()
        await page.locator(SELECTORS["send_button"]).first.click()
        for _ in range(25):  # 200ms × 25 = 5s 超时
            await page.wait_for_timeout(200)
            if await page.locator(self_bubbles).count() > before:
                return
        raise RuntimeError("发送后未出现新的我方气泡，发送可能失败")


if __name__ == "__main__":
    run_worker(WorkerConfig.from_env(), XhsArkDriver())
