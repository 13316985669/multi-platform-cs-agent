"""抖音来客私信后台 Worker：life.douyin.com（来客商家后台 → 消息/客服管理）自动化。

适用场景：抖音本地生活商家（团购/门店/预约类咨询），非蓝V 企业号、非抖店商家。
与飞鸽/企业号的区别：来客面向本地生活商家，私信以团购、核销、门店、预约咨询为主。
来客无开放的私信 IM API，RPA 与飞鸽/企业号通道同理。

前置条件：
- 来客商家账号（营业执照开通抖音来客）
- life.douyin.com 后台开通「消息/客服管理」相关权限
- 关闭来客后台自带的自动回复/智能客服（如有，见 bot_enabled_flag 检查），否则用户会收到双份回复
  （2026-10-22 实测发现商家侧接入了「美洽客服系统」三方应用在自动回复，上线前务必在后台关闭）

平台归属：上报后端时 platform 归 "douyin"（base_worker 的 backend_platform 映射），
会话/统计/通道路由与抖店/企业号一致；仅频控规则不同（DOUYIN_ACCOUNT_TYPE=laike 时生效 douyin_laike_rpa）。

频控说明（后端 douyin_laike_rpa 规则已内置）：
- 来客本地生活 IM 的官方回复窗口规则未经真账号核实，初期保守对齐企业号：
  用户回复后 48h 内可发 6 条 + 每分钟 ≤6 条；冒烟核实后校准
- 本系统只做被动回复，不做主动触达

真实页校准要点（2026-10-22 CDP 只读转储，改动选择器前先读）：
1. 真实会话列表只认行内 ``byted-badge-sup``。数量为 0 时这个 sup 的 transform 是零缩放，
   外层 ``byted-badge`` 仍在且内部叠着 0–9，不能用外层文本判断。sup 未隐藏时，
   用动画位的位移读出当前数字，大于 0 才点进去。进去后只上报对方最后一条文本。
   指纹是账号 + 会话 + 正文，相同正文不重复上报。
2. 会话行 ``lastMsgWrap`` 仍是最后一条预览，但不再用来决定点不点。
   没有未读就不进会话，也不再每 5 轮点开顶部会话。
3. 气泡文本用 ``.csUI-Text``：旧 ``wrapper_content`` 选择器会混入"已读/未读"角标与
   "此消息由三方应用回复"尾注，污染内容。
4. 面板头部无 ``[data-panel-name]``（真实页不存在该节点）。面板身份校验两步：
   点击行后等该行出现 ``conversationActiveItem`` 类（点击生效），再对比
   "面板可见用户气泡头像 URL == 列表行头像 URL"（重名昵称兜底）。
5. 回复框 ``textarea[placeholder*='回复内容']`` maxlength=500；页面无"发送"按钮，
   官方交互是 Enter 发送（placeholder 明示）。整段 fill + Enter，不再逐字输入。
6. 入站/出站两个协程共用同一页面，用实例级 asyncio.Lock 串行化（防止粘贴到一半
   被另一协程点开别的会话，把回复发错人）。
7. 没有未读就不回读面板，因此自己发出的回复不会被当成新的用户消息再上报。

运行：python douyin_laike_worker.py（.env.local 中 PLATFORM=douyin_laike）
"""
import asyncio
import hashlib
import logging
import time

from playwright.async_api import Page

from base_worker import (BaseWorker, IncomingItem, PlatformDriver, WorkerConfig,
                         run_worker)
from page_markers import is_login_wall, page_text

logger = logging.getLogger("rpa_worker.douyin_laike")

# 来客会话中心。首页 /p/home 没有会话列表；侧栏「会话中心」的 data-path 会进这个地址。
LAIKE_URL = "https://life.douyin.com/p/liteapp/leads_cs/chat/session"

# ============ 选择器（fixture 用稳定 class；真实页面用前缀，哈希后缀会变） ============
SELECTORS = {
    "login_form": ".login-form",                    # 出现即登录过期
    "conversation_list": ".conversation-list",      # 单个会话（fixture）
    "conversation_item": ".conversation-item",      # 单个会话（data-conv 属性为会话标识）
    "unread_badge": ".unread-badge",                # 未读红点
    "nickname": ".conv-nickname",                   # 会话上的用户昵称
    "message_item": ".message-item",                # 消息气泡（data-mid 唯一，data-side=user|self）
    "message_text": ".message-text",                # 文本内容节点
    "message_image": "img.message-image",          # 图片消息
    "message_audio": "audio.message-audio",        # 语音消息
    "chat_input": ".chat-input",                    # 输入框
    "send_button": ".send-button",                  # 发送按钮
    "file_input": "input[type='file']",             # 附件上传 input
    "bot_enabled_flag": ".platform-bot-enabled",    # 来客自动回复/智能客服开启标记（存在即未关闭）
    "self_name": ".self-account-name",              # 当前登录的来客商家名称
    # 真实来客会话中心（2026-10-22 CDP 转储校准；class 前缀带哈希后缀，用 [class*=] 前缀匹配）
    "workspace_list": "[class*='conversationListContainer']",   # 会话列表滚动容器
    "workspace_item": "[class*='conversationItem']",           # 会话行（置顶 conversationStickItem-*/激活 conversationActiveItem-*）
    "workspace_item_active": "[class*='conversationActiveItem']",  # 当前激活行：点击生效校验
    "workspace_name": "[class*='conversationName']",            # 会话昵称
    "workspace_preview": "[class*='lastMsgWrap']",             # 最后一条消息预览（.csUI-MessageAbstract）
    # 未读是行内 sup.byted-badge-sup。数量为 0 时该节点 matrix 缩成 0。
    # 外层 byted-badge 的 transform 仍是 none，内部还叠着 0–9，不能拿它的文本当未读数。
    "workspace_unread_count": "[class*='byted-badge-sup']",
    "workspace_avatar": "[class*='conversationAvatarImg']",     # 会话行头像（身份校验用）
    "workspace_upgrade_modal": "[class*='upgradeModal']",
    "workspace_upgrade_modal_close": "button.close",
    "workspace_input": "textarea[placeholder*='回复内容']",     # maxlength=500，Enter 发送
    "workspace_message": ".csUI-MessageItem",                   # 消息气泡（无后缀稳定类）
    "workspace_message_id": "[data-message-watermark]",        # 平台消息 ID：msg7699051396724966961
    "workspace_message_text": ".csUI-Text",                     # 气泡文本（不含已读/三方尾注）
    "workspace_message_left": ".csUI-NormalMessage_left",       # 用户侧气泡
    "workspace_message_right": ".csUI-NormalMessage_right",      # 我方气泡
    "workspace_message_avatar": ".csUI-Avatar_img",              # 气泡内头像（面板身份校验用）
    "workspace_message_card": "[class*='wrapper_content_card']",  # 消息内容卡片（图片识别用）
}

# fixture 伪页面仍扫顶部会话补旁路。真实会话中心不靠这个点开会话。
SCAN_RECENT = 3


def message_fingerprint(account_id: str, conv_id: str, msg: str) -> str:
    """去重键：账号 + 会话 + 对方最后一条正文。相同正文只上报一次。"""
    raw = f"{account_id}|{conv_id}|{msg}"
    return hashlib.md5(raw.encode()).hexdigest()


def _transform_hides(transform: str) -> bool:
    """字节徽标在数量为 0 时用 transform 藏起来。scale(0) 或矩阵缩放为 0 都算隐藏。"""
    text = (transform or "").replace(" ", "").lower()
    if "scale(0)" in text or "scale(0," in text:
        return True
    for token in text.split("matrix(")[1:]:
        nums = token.split(")", 1)[0].split(",")
        if len(nums) < 4:
            continue
        try:
            scale_x = float(nums[0])
            scale_y = float(nums[3])
        except ValueError:
            continue
        if abs(scale_x) < 0.01 and abs(scale_y) < 0.01:
            return True
    return False


def _badge_number(text: str) -> int:
    raw = (text or "").strip()
    if not raw or "\n" in raw:
        return 0
    digits = raw.rstrip("+")
    if not digits.isdigit():
        return 0
    return int(digits)


# 徽标里每个数字是一格。位移除以格高才是当前显示的数字，不能把 0–9 拼成一个整数。
_SHOWN_BADGE_JS = """
el => {
  const bit = el.querySelector("[class*='animated-number-bit']");
  const cells = bit ? [...bit.querySelectorAll("p")] : [];
  if (!bit || !cells.length) {
    const raw = (el.innerText || "").trim();
    if (!raw || raw.includes("\\n")) return 0;
    const digits = raw.replace(/\\+$/, "");
    return /^\\d+$/.test(digits) ? parseInt(digits, 10) : 0;
  }
  const matrix = new DOMMatrix(getComputedStyle(bit).transform);
  const height = cells[0].getBoundingClientRect().height;
  if (!height) return 0;
  let index = Math.round(-matrix.m42 / height);
  if (index < 0) index = 0;
  if (index >= cells.length) index = cells.length - 1;
  const digits = (cells[index].innerText || "").trim();
  return /^\\d+$/.test(digits) ? parseInt(digits, 10) : 0;
}
"""


# 真实回复框 maxlength（textarea 属性实测）
INPUT_MAX_LEN = 500

# 自己发送的回复在面板上会被下一轮扫描读到；TTL 内内容指纹命中则不上报，
# 避免与 outbox 落库形成双份历史（人工旁路消息不受影响——人发的内容不在指纹表里）
SENT_FP_TTL_SECONDS = 300
SENT_FP_MAX = 200

# 升级弹窗从"每页查一次"改为周期重查：长跑中途弹窗不再挡住点击导致读写连环失败
UPGRADE_MODAL_RECHECK_SECONDS = 300

# 打开页面时执行一次。用元素自身的 click()，蒙层挡住坐标点击时仍能关掉。
# 只点弹窗内的关闭控件，不点「立即体验」。
_CLOSE_UPGRADE_MODAL_JS = """
() => {
  function shown(el) {
    if (!el) return false;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    return el.getClientRects().length > 0;
  }
  const body = [...document.querySelectorAll('.life-core-modal-body')].find(shown);
  let root = null;
  if (body) {
    root = body.closest('.life-core-modal, [role="dialog"]') || body.parentElement;
  } else {
    const dialogs = document.querySelectorAll('.life-core-modal, [role="dialog"]');
    for (const node of dialogs) {
      if (shown(node) && (node.innerText || '').includes('全新升级')) {
        root = node;
        break;
      }
    }
  }
  if (!root || !shown(root)) return 'absent';
  const close = root.querySelector(
    '.life-core-modal-close-icon, .life-core-modal-close, [aria-label="关闭"], [aria-label="Close"]'
  );
  if (!close) return 'no-close';
  const label = (close.getAttribute('aria-label') || close.innerText || '').trim();
  if (label.includes('立即体验')) return 'no-close';
  close.click();
  return 'closed';
}
"""


class LaikeDriver(PlatformDriver):
    name = "douyin_laike"
    url = LAIKE_URL

    def __init__(self):
        # 会话预览状态：conv_key -> 最近一次读到的预览文本（预览变化=有新消息，才点开）
        self._conv_state: dict[str, str] = {}
        # W9：自己发送内容的指纹表 fp -> monotonic 时间戳
        self._sent_fingerprints: dict[str, float] = {}
        # 入站/出站协程共页互斥锁（惰性创建：__init__ 在事件循环外执行，
        # asyncio.Lock 必须在首次异步使用时建，避免绑错 loop）
        self._page_op_lock: asyncio.Lock | None = None
        self.account_id = ""
        self._upgrade_modal_page = None
        self._upgrade_modal_checked = 0.0

    def _page_op(self) -> asyncio.Lock:
        if self._page_op_lock is None:
            self._page_op_lock = asyncio.Lock()
        return self._page_op_lock

    def _remember_sent(self, conv_key: str, content: str) -> None:
        fp = hashlib.md5(f"{conv_key}|{content}".encode()).hexdigest()
        self._sent_fingerprints[fp] = time.monotonic()
        if len(self._sent_fingerprints) > SENT_FP_MAX:
            cutoff = time.monotonic() - SENT_FP_TTL_SECONDS
            stale = [k for k, ts in self._sent_fingerprints.items() if ts < cutoff]
            for k in stale:
                self._sent_fingerprints.pop(k, None)

    def _is_self_sent(self, conv_key: str, content: str) -> bool:
        fp = hashlib.md5(f"{conv_key}|{content}".encode()).hexdigest()
        ts = self._sent_fingerprints.get(fp)
        if ts is None:
            return False
        if time.monotonic() - ts > SENT_FP_TTL_SECONDS:
            self._sent_fingerprints.pop(fp, None)
            return False
        return True

    async def check_status(self, page: Page) -> str:
        login = page.locator(SELECTORS["login_form"])
        if await login.count() and await login.first.is_visible():
            return "login_expired"
        if await page.locator(SELECTORS["conversation_list"]).count() > 0:
            return "online"
        # 真实会话中心：列表容器或回复框在，就说明已经登录并进入私信。
        if (await page.locator(SELECTORS["workspace_list"]).count() > 0
                or await page.locator(SELECTORS["workspace_input"]).count() > 0):
            return "online"
        if is_login_wall(await page_text(page)):
            return "login_expired"
        return "selector_mismatch"

    async def dismiss_blockers(self, page: Page):
        """打开页面时查一次升级弹窗；同一页每 UPGRADE_MODAL_RECHECK_SECONDS 秒重查一次。

        弹窗不在就马上返回，不等待、不重试。重新打开浏览器会换新的 page，再看一次。
        只点弹窗内的 ×，不点「立即体验」。
        周期重查的原因：升级弹窗可能在长跑中途弹出，挡住会话点击导致读写连环失败。
        """
        now = time.monotonic()
        if (self._upgrade_modal_page is page
                and now - self._upgrade_modal_checked < UPGRADE_MODAL_RECHECK_SECONDS):
            return
        self._upgrade_modal_page = page
        self._upgrade_modal_checked = now
        if not await self._upgrade_modal_visible(page):
            return
        try:
            result = await page.evaluate(_CLOSE_UPGRADE_MODAL_JS)
        except Exception:  # noqa: BLE001
            logger.warning("关闭来客升级弹窗失败", exc_info=True)
            return
        if result == "no-close":
            logger.warning("来客升级弹窗没有关闭按钮，未点立即体验")
        elif result == "closed":
            logger.info("已关闭来客升级弹窗")

    async def _upgrade_modal_visible(self, page: Page) -> bool:
        body = page.locator(".life-core-modal-body")
        if await body.count() > 0 and await body.first.is_visible():
            return True
        dialog = page.locator(".life-core-modal, [role='dialog']").filter(has_text="全新升级")
        if await dialog.count() == 0:
            return False
        return await dialog.first.is_visible()

    async def check_platform_bot_disabled(self, page: Page) -> bool:
        """来客「自动回复/智能客服」开启时页面会有对应标记（fixture 契约）。"""
        return await page.locator(SELECTORS["bot_enabled_flag"]).count() == 0

    async def read_incoming(self, page: Page) -> list[IncomingItem]:
        # 入站/出站共用一个页面：串行化点击/输入，防止发送到一半被切会话
        async with self._page_op():
            if await page.locator(SELECTORS["conversation_item"]).count() > 0:
                return await self._read_fixture(page)
            if await page.locator(SELECTORS["workspace_list"]).count() > 0:
                return await self._read_workspace(page)
            return []

    async def _read_fixture(self, page: Page) -> list[IncomingItem]:
        """扫描有未读红点的会话 + 列表顶部最近活跃会话，提取新消息（含我方旁路消息）。

        旁路场景：人工客服在来客后台直接回复时，该会话通常没有未读红点
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

    async def _badge_transform(self, badge) -> str:
        style = (await badge.get_attribute("style")) or ""
        evaluate = getattr(badge, "evaluate", None)
        if evaluate is None:
            return style
        try:
            computed = await evaluate("el => getComputedStyle(el).transform || ''")
        except Exception:  # noqa: BLE001
            return style
        return f"{style} {computed or ''}"

    async def _shown_badge_number(self, badge) -> int:
        evaluate = getattr(badge, "evaluate", None)
        if evaluate is not None:
            try:
                value = await evaluate(_SHOWN_BADGE_JS)
            except Exception:  # noqa: BLE001
                value = None
            if isinstance(value, int):
                return value
        return _badge_number(await badge.inner_text())

    async def _unread_count(self, row) -> int:
        """这一行的未读数字。sup 被零缩放、数字为 0、或找不到徽标时返回 0。"""
        badges = row.locator(SELECTORS["workspace_unread_count"])
        total = await badges.count()
        best = 0
        for index in range(total):
            badge = badges.nth(index)
            if _transform_hides(await self._badge_transform(badge)):
                continue
            value = await self._shown_badge_number(badge)
            if value > best:
                best = value
        return best

    async def _conversation_id(self, row, nickname: str) -> str:
        for attr in ("data-conv-id", "data-id", "data-conv", "id"):
            value = (await row.get_attribute(attr) or "").strip()
            if value:
                return value[:128]
        return nickname

    async def _last_user_text(self, page: Page) -> str:
        """只取对方最后一条文本。更早的气泡和我方气泡都不上报。"""
        bubbles = page.locator(SELECTORS["workspace_message"])
        total = await bubbles.count()
        for index in range(total - 1, -1, -1):
            bubble = bubbles.nth(index)
            if await bubble.locator(SELECTORS["workspace_message_left"]).count() == 0:
                continue
            text_node = bubble.locator(SELECTORS["workspace_message_text"])
            if await text_node.count() == 0:
                continue
            content = (await text_node.first.inner_text()).strip()
            if content:
                return content
        return ""

    async def _read_workspace(self, page: Page) -> list[IncomingItem]:
        """真实会话中心：没有未读数字就不点开，点开后只上报对方最后一条。"""
        items: list[IncomingItem] = []
        rows = page.locator(SELECTORS["workspace_item"])
        total = await rows.count()
        for index in range(total):
            row = rows.nth(index)
            try:
                unread = await self._unread_count(row)
            except Exception:  # noqa: BLE001
                logger.debug("未读标记捕获失败，跳过该会话", exc_info=True)
                continue
            if unread <= 0:
                continue
            name = row.locator(SELECTORS["workspace_name"])
            if await name.count() == 0:
                continue
            nickname = (await name.first.inner_text()).strip()[:64]
            if not nickname:
                continue
            await row.click()
            if not await self._workspace_panel_ready(page, row, nickname):
                continue
            content = await self._last_user_text(page)
            if not content:
                continue
            conv_id = await self._conversation_id(row, nickname)
            items.append(IncomingItem(
                conv_key=nickname, msg_key=message_fingerprint(self.account_id, conv_id, content),
                nickname=nickname, sender_side="user", msg_type="text", content=content))
        return items

    async def _workspace_panel_ready(self, page: Page, row, nickname: str) -> bool:
        """点击后两步校验面板确实是 nickname 的会话。

        1) 点击生效：等该行 class 出现 conversationActiveItem（真实页激活态标记）
        2) 身份校验：面板可见用户气泡的头像 URL == 会话行头像 URL（重名昵称兜底）
           面板无用户气泡（空会话/虚拟化未加载）时跳过头像校验。
        """
        for _ in range(10):
            cls = (await row.get_attribute("class")) or ""
            if "conversationActiveItem" in cls:
                break
            await page.wait_for_timeout(100)
        else:
            return False
        bubbles = page.locator(SELECTORS["workspace_message"])
        for _ in range(5):
            if await bubbles.count() > 0:
                break
            await page.wait_for_timeout(200)
        else:
            return True  # 空面板视为就绪（新会话无历史气泡）
        left = bubbles.locator(SELECTORS["workspace_message_left"])
        if await left.count() == 0:
            return True  # 面板内暂无用户气泡（只有我方消息），跳过头像校验
        row_av_loc = row.locator(SELECTORS["workspace_avatar"])
        panel_av_loc = left.first.locator(SELECTORS["workspace_message_avatar"])
        if await row_av_loc.count() > 0 and await panel_av_loc.count() > 0:
            row_av = (await row_av_loc.first.get_attribute("src")) or ""
            panel_av = (await panel_av_loc.first.get_attribute("src")) or ""
            if row_av and panel_av and row_av != panel_av:
                return False  # 重名昵称点错行 / 面板未切换
        return True

    async def send_message(self, page: Page, conv_key: str, content: str,
                           msg_type: str = "text", media_path: str = "") -> None:
        # 入站/出站共用一个页面：串行化点击/输入，防止粘贴到一半被切会话发错人
        async with self._page_op():
            if (await page.locator(SELECTORS["conversation_item"]).count() == 0
                    and await page.locator(SELECTORS["workspace_list"]).count() > 0):
                await self._send_workspace(page, conv_key, content, msg_type, media_path)
                return
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

    async def _send_workspace(self, page: Page, conv_key: str, content: str,
                              msg_type: str, media_path: str) -> None:
        """真实会话中心：精确昵称定位 → 面板校验 → 整段 fill → Enter 发送。

        - 定位：先精确匹配昵称（has_text 子串匹配在"张"与"张三"并存时会点错行），
          找不到再退回子串匹配，最终靠头像校验兜底
        - 面板校验失败直接抛错（不静默跳过）：发错人是正确性事故，宁可进失败列表人工处理
        - 整段 fill 替代逐字输入（fill 自动清空原值并触发 input 事件），
          超过 maxlength(500) 截断；页面无发送按钮，官方交互是 Enter
        - 发送成功登记内容指纹（W9）并同步预览状态（防下一轮因预览变化误点开）
        """
        if msg_type != "text" or media_path:
            raise RuntimeError("来客会话中心暂只自动发送文字")
        rows = page.locator(SELECTORS["workspace_item"])
        total = await rows.count()
        target = None
        for i in range(total):
            row = rows.nth(i)
            name = row.locator(SELECTORS["workspace_name"])
            if await name.count() == 0:
                continue
            if (await name.first.inner_text()).strip()[:64] == conv_key:
                target = row
                break
        if target is None:
            fallback = rows.filter(has_text=conv_key)
            if await fallback.count() == 0:
                raise RuntimeError(f"会话不存在: {conv_key}")
            target = fallback.first
        await target.click()
        if not await self._workspace_panel_ready(page, target, conv_key):
            raise RuntimeError(
                f"点击会话后面板校验失败（可能重名或页面未就绪）: {conv_key}")
        box = page.locator(SELECTORS["workspace_input"])
        if await box.count() == 0:
            raise RuntimeError("页面无回复输入框")
        before = await page.locator(SELECTORS["workspace_message_right"]).count()
        if content:
            await box.first.click()
            await box.first.fill(content[:INPUT_MAX_LEN])
            await page.keyboard.press("Enter")
        for _ in range(25):
            await page.wait_for_timeout(200)
            if await page.locator(SELECTORS["workspace_message_right"]).count() > before:
                self._remember_sent(conv_key, content)
                self._conv_state[conv_key] = content[:40]
                return
        raise RuntimeError("发送后未出现新的我方气泡，发送可能失败")


if __name__ == "__main__":
    run_worker(WorkerConfig.from_env(), LaikeDriver())