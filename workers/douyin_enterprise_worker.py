"""抖音企业号私信后台 Worker：e.douyin.com（企业服务中心 → 消息管理）自动化。

适用场景：蓝V 企业号、无抖店店铺（飞鸽需抖店）、未投线索广告（官方 API 准入不达标）。
企业私信 OpenAPI 已「内测结束暂不开放」，企业号场景 RPA 是当前唯一可行通道。

前置条件：
- 企业号蓝V 认证（600 元/年）
- e.douyin.com 开通「客服管理」权限（1-3 个工作日审核，需营业执照 + 法人身份证）
- 关闭企业号「自动回复/智能客服」（消息管理页内设置），否则用户会收到双份回复

平台归属：上报后端时 platform 归 "douyin"（base_worker 的 backend_platform 映射），
会话/统计/通道路由与抖店一致；仅频控规则不同（见下）。

频控差异（后端 douyin_enterprise_rpa 规则已内置对齐，DOUYIN_ACCOUNT_TYPE=enterprise 时生效）：
- 用户回复后 48h 内可发 6 条（抖店为 24h/6 条）
- 主动触达 1 小时 ≤40 人、1 天 ≤100 人（本系统只做被动回复，不触发）

选择器说明（重要）：
- 选择器集中在 SELECTORS，页面改版时只需改这里 + fixtures/fake_enterprise.html
- 当前选择器与伪平台 fixture 契约一致（可端到端测试）；
  真实企业号后台 DOM 需在真账号冒烟时按实际调整，并用 doctor.py 验证
- 冒烟确认真实私信页地址后，可在 .env.local 用 PLATFORM_URL 固化

运行：python douyin_enterprise_worker.py（.env.local 中 PLATFORM=douyin_enterprise）
"""
import hashlib
import logging

from playwright.async_api import Page

from base_worker import (BaseWorker, IncomingItem, PlatformDriver, WorkerConfig,
                         run_worker)
from page_markers import is_login_wall, page_text

logger = logging.getLogger("rpa_worker.douyin_enterprise")

# 企业号后台地址（企业服务中心；私信管理页确切路径待真账号冒烟确认，可用 PLATFORM_URL 覆盖）
ENTERPRISE_URL = "https://e.douyin.com/"

# ============ 选择器（与 fixtures/fake_enterprise.html 契约一致；真实页面需按实际 DOM 调整） ============
SELECTORS = {
    "login_form": ".login-form",                    # 出现即登录过期
    "conversation_list": ".conversation-list",      # 会话列表容器（缺失即选择器漂移）
    "conversation_item": ".conversation-item",      # 单个会话（data-conv 属性为会话标识）
    "unread_badge": ".unread-badge",                # 未读红点
    "nickname": ".conv-nickname",                   # 会话上的用户昵称
    "message_item": ".message-item",                # 消息气泡（data-mid 唯一，data-side=user|self）
    "message_text": ".message-text",                # 文本内容节点
    "message_image": "img.message-image",           # 图片消息
    "message_audio": "audio.message-audio",         # 语音消息
    "chat_input": ".chat-input",                    # 输入框
    "send_button": ".send-button",                  # 发送按钮
    "file_input": "input[type='file']",             # 附件上传 input
    "bot_enabled_flag": ".platform-bot-enabled",    # 企业号自动回复/智能客服开启标记（存在即未关闭）
    "self_name": ".self-account-name",              # 当前登录的企业号名称
}

# 每轮额外扫描会话列表顶部 N 个会话（不依赖未读红点），用于捕捉人工客服在平台后台
# 直接发送的旁路消息；靠 msg_key 去重，不会重复上报
SCAN_RECENT = 3


class EnterpriseDriver(PlatformDriver):
    name = "douyin_enterprise"
    url = ENTERPRISE_URL

    async def check_status(self, page: Page) -> str:
        login = page.locator(SELECTORS["login_form"])
        if await login.count() and await login.first.is_visible():
            return "login_expired"
        if await page.locator(SELECTORS["conversation_list"]).count() > 0:
            return "online"
        if is_login_wall(await page_text(page)):
            return "login_expired"
        return "selector_mismatch"

    async def check_platform_bot_disabled(self, page: Page) -> bool:
        """企业号「自动回复/智能客服」开启时页面会有对应标记（fixture 契约）。"""
        return await page.locator(SELECTORS["bot_enabled_flag"]).count() == 0

    async def read_incoming(self, page: Page) -> list[IncomingItem]:
        """扫描有未读红点的会话 + 列表顶部最近活跃会话，提取新消息（含我方旁路消息）。

        旁路场景：人工客服在企业号后台直接回复时，该会话通常没有未读红点
        （未读针对的是用户消息），只扫未读会漏掉旁路同步；
        因此每轮额外扫描顶部 SCAN_RECENT 个会话，靠 msg_key 去重保证不重复上报。
        """
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

            # 消息类型：图片 / 语音 / 文本
            img = bubble.locator(SELECTORS["message_image"])
            audio = bubble.locator(SELECTORS["message_audio"])
            text_node = bubble.locator(SELECTORS["message_text"])
            msg_type, content, media_url = "text", "", ""
            if await img.count() > 0:
                msg_type = "image"
                media_url = await img.first.get_attribute("src") or ""
            elif await audio.count() > 0:
                msg_type = "voice"
                media_url = await audio.first.get_attribute("src") or ""
            elif await text_node.count() > 0:
                content = (await text_node.first.inner_text()).strip()
            else:
                continue

            msg_key = await bubble.get_attribute("data-mid") or ""
            if not msg_key:
                # 无稳定消息 ID 时用确定性哈希兜底（随机值会导致每轮轮询重复上报）；
                # 代价：同一会话内内容完全相同的重复消息只会上报一次
                fingerprint = f"{conv_key}|{side}|{msg_type}|{content or media_url}"
                msg_key = f"{conv_key}_{hashlib.md5(fingerprint.encode()).hexdigest()[:12]}"

            items.append(IncomingItem(
                conv_key=conv_key, msg_key=msg_key, nickname=nickname,
                sender_side=sender_side, msg_type=msg_type,
                content=content, media_url=media_url))
        return items

    async def send_message(self, page: Page, conv_key: str, content: str,
                           msg_type: str = "text", media_path: str = "") -> None:
        # 定位会话
        conv = page.locator(f"{SELECTORS['conversation_item']}[data-conv='{conv_key}']")
        if await conv.count() == 0:
            raise RuntimeError(f"会话不存在: {conv_key}")
        await conv.first.click()
        await page.wait_for_timeout(300)

        # 媒体先发附件，再发文本（如有）
        if msg_type in ("image", "voice") and media_path:
            file_input = page.locator(SELECTORS["file_input"])
            if await file_input.count() == 0:
                raise RuntimeError("页面无附件上传入口（选择器漂移或平台不支持）")
            await file_input.first.set_input_files(media_path)
            await page.wait_for_timeout(800)  # 等待附件上传完成

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
    run_worker(WorkerConfig.from_env(), EnterpriseDriver())
