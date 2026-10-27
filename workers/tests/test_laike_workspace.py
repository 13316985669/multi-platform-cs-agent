"""来客真实会话中心单测：未读数字才进入、只上报对方最后一条、整段粘贴发送。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from base_worker import LocalState  # noqa: E402
from douyin_laike_worker import SELECTORS, LaikeDriver, message_fingerprint  # noqa: E402


class Node:
    def __init__(self, classes, text="", attrs=None, children=None, on_click=None, tag=""):
        self.classes = list(classes) if isinstance(classes, (list, tuple)) else ([classes] if classes else [])
        self.text = text
        self.attrs = attrs or {}
        self.children = children or []
        self.on_click = on_click
        self.tag = tag

    def matches(self, selector: str) -> bool:
        # tag[attr*='...'] 复合（如 textarea[placeholder*='回复内容']）
        if "[" in selector and not selector.startswith("["):
            tag, _, rest = selector.partition("[")
            attr, _, needle = rest.partition("*=")
            return (self.tag == tag
                    and needle.strip("'\"[] ") in str(self.attrs.get(attr.strip(), "")))
        if selector.startswith("[data-"):
            return selector[1:-1] in self.attrs
        if selector.startswith("[class*="):
            needle = selector.split("'", 2)[1]
            return any(needle in name for name in self.classes)
        if selector.startswith("."):
            return selector[1:] in self.classes
        if selector == "img":
            return self.tag == "img"
        return False


def _text_of(node: Node) -> str:
    parts = [node.text] + [_text_of(c) for c in node.children]
    return "".join(p for p in parts if p)


class Locator:
    def __init__(self, nodes):
        self.nodes = nodes

    def locator(self, selector: str):
        found = []
        for part in selector.split(","):
            for node in self.nodes:
                found.extend(_find(node, part.strip()))
        return Locator(found)

    def nth(self, index: int):
        return Locator([self.nodes[index]])

    @property
    def first(self):
        return self.nth(0)

    def filter(self, has_text: str):
        return Locator([n for n in self.nodes if has_text in _text_of(n)])

    async def count(self):
        return len(self.nodes)

    async def inner_text(self):
        return self.nodes[0].text

    async def get_attribute(self, name: str):
        if name == "class" and "class" not in self.nodes[0].attrs:
            return " ".join(self.nodes[0].classes)
        return self.nodes[0].attrs.get(name, "")

    async def click(self):
        if self.nodes[0].on_click:
            self.nodes[0].on_click()

    async def fill(self, text: str):
        self.nodes[0].attrs["value"] = text


def _find(node: Node, selector: str):
    found = [node] if node.matches(selector) else []
    for child in node.children:
        found.extend(_find(child, selector))
    return found


class FakeKeyboard:
    """Enter 触发页面 on_enter 钩子（模拟真实页 Enter 发送生成右侧气泡）。"""

    def __init__(self, page):
        self.presses: list[str] = []
        self._page = page

    async def press(self, key: str, **_kwargs):
        self.presses.append(key)
        if key == "Enter" and self._page.on_enter:
            self._page.on_enter()


class FakePage:
    def __init__(self, roots):
        self.roots = roots
        self.keyboard = FakeKeyboard(self)
        self.on_enter = None

    def locator(self, selector: str):
        found = []
        for part in selector.split(","):
            for node in self.roots:
                found.extend(_find(node, part.strip()))
        return Locator(found)

    async def wait_for_timeout(self, _ms: int):
        return None


def _bubble(side: str, text: str, avatar_src: str, msg_id: str = ""):
    """一个消息气泡：左右侧 + 头像 + 文本 + 可选水印消息 ID。"""
    children = [
        Node(["csUI-Avatar_img"], attrs={"src": avatar_src}, tag="img"),
        Node(["csUI-Text"], text=text),
    ]
    if msg_id:
        children.append(Node([], text=msg_id, attrs={"data-message-watermark": "true"}))
    return Node(["csUI-MessageItem"], children=[
        Node([f"csUI-NormalMessage_{side}"],
             children=[Node(["csUI-NormalMessage_wrapper_content_card"], children=children)]),
    ])


class LaikePage:
    """可编程假页：会话行（昵称/预览/头像/激活态）+ 点击切换面板 + Enter 生成右侧气泡。

    - stuck：str=该会话点击永远失效（面板卡死）；dict {名字: N}=前 N 次点击失效（瞬时故障）
    - swap_on_click：(点击名, 展示名)：点击某行后行激活、但面板渲染的是另一个会话（错行/重名）
    - 点击已激活会话不重绘面板（保留测试直接注入的气泡），与真实页行为一致
    """

    def __init__(self, names, stuck="乙", swap_on_click=None):
        if isinstance(stuck, str):
            self.stuck = {stuck: float("inf")} if stuck else {}
        else:
            self.stuck = dict(stuck or {})
        self.swap_on_click = swap_on_click
        self.click_counts = {n: 0 for n in names}
        self.rows: dict[str, Node] = {}
        self.active = ""
        self.sent_texts: list[str] = []
        self.preview_refs: dict[str, Node] = {}
        self.bubbles: dict[str, list[Node]] = {}
        self.input_node = Node(["textarea"], tag="textarea",
                               attrs={"placeholder": "回复内容", "maxlength": "500"})
        self.bubbles_root = Node(["workspaceBubbles"])
        self.page = FakePage([
            Node(["conversationListContainer"], children=[self._item(n) for n in names]),
            self.bubbles_root,
            self.input_node,
        ])
        self.page.on_enter = self._on_enter

    def _item(self, name: str):
        preview = Node(["lastMsgWrap", "csUI-MessageAbstract"], text=f"{name}的预览")
        self.preview_refs[name] = preview
        row = Node(["conversationItem"], children=[
            Node(["conversationName"], text=name),
            preview,
            Node(["conversationAvatarImg"], attrs={"src": f"avatar-{name}"}, tag="img"),
        ])
        self.rows[name] = row

        def click():
            self.click_counts[name] += 1
            if self.click_counts[name] <= self.stuck.get(name, 0):
                return  # 点击失效：行不激活、面板不切换
            self._set_active(name)
            if self.swap_on_click and name == self.swap_on_click[0]:
                self.active = self.swap_on_click[1]
                self._render_bubbles(self.swap_on_click[1])
                return
            if self.active != name:
                self.active = name
                self._render_bubbles(name)

        row.on_click = click
        return row

    def set_unread(self, name: str, count: int, hidden: bool = False):
        row = self.rows[name]
        row.children = [child for child in row.children
                        if not any("byted-badge" in cls for cls in child.classes)]
        style = "transform: scale(0)" if hidden else ""
        row.children.append(Node(
            ["byted-badge-sup"], text=str(count), attrs={"style": style},
        ))

    def _set_active(self, name: str):
        for n, row in self.rows.items():
            if n == name:
                if "conversationActiveItem" not in row.classes:
                    row.classes.append("conversationActiveItem")
            elif "conversationActiveItem" in row.classes:
                row.classes.remove("conversationActiveItem")

    def _render_bubbles(self, name: str):
        if name not in self.bubbles:
            self.bubbles[name] = [
                _bubble("left", f"{name}的问题", f"avatar-{name}", msg_id=f"msg-{name}-1")]
        self.bubbles_root.children = list(self.bubbles[name])

    def _on_enter(self):
        text = self.input_node.attrs.get("value", "")
        if not text:
            return
        name = self.active
        self.sent_texts.append(text)
        bubble = _bubble("right", text, f"agent-avatar-{name}",
                         msg_id=f"msg-{name}-sent-{len(self.sent_texts)}")
        self.bubbles.setdefault(name, []).append(bubble)
        self.bubbles_root.children.append(bubble)
        self.preview_refs[name].text = text[:40]

    async def read(self, driver):
        return await driver.read_incoming(self.page)


# ---------- 读取 ----------

@pytest.mark.asyncio
async def test_hidden_or_zero_unread_does_not_open():
    page = LaikePage(["甲", "乙", "丙"], stuck=None)
    page.set_unread("甲", 2, hidden=True)
    page.set_unread("乙", 0)
    driver = LaikeDriver()
    items = await page.read(driver)
    assert items == []
    assert page.click_counts == {"甲": 0, "乙": 0, "丙": 0}


@pytest.mark.asyncio
async def test_hidden_sup_ignores_stacked_digits():
    """外层徽标叠着 0–9，未读 sup 被零缩放时不能当成有未读。"""
    page = LaikePage(["甲"], stuck=None)
    page.rows["甲"].children.append(Node(
        ["byted-badge"],
        children=[Node(
            ["byted-badge-sup"],
            text="0\n1\n2\n3\n4\n5\n6\n7\n8\n9",
            attrs={"style": "transform: matrix(0, 0, 0, 0, 0, 0)"},
        )],
    ))
    items = await page.read(LaikeDriver())
    assert items == []
    assert page.click_counts["甲"] == 0
    assert SELECTORS["workspace_unread_count"]


@pytest.mark.asyncio
async def test_unread_opens_only_that_row_and_stuck_panel_is_skipped():
    page = LaikePage(["甲", "乙", "丙", "丁"])  # 乙卡死：点击永远不生效
    page.set_unread("甲", 1)
    page.set_unread("乙", 1)
    page.set_unread("丁", 3)
    driver = LaikeDriver()
    driver.account_id = "shop"
    items = await page.read(driver)
    contents = {item.content for item in items}
    assert contents == {"甲的问题", "丁的问题"}
    assert page.click_counts["丙"] == 0
    assert page.click_counts["乙"] == 1  # 点了，但面板校验失败，不上报


@pytest.mark.asyncio
async def test_no_unread_never_clicks_even_after_many_cycles():
    page = LaikePage(["甲", "乙", "丙", "丁"], stuck=None)
    driver = LaikeDriver()
    for _ in range(6):
        await page.read(driver)
    assert page.click_counts == {"甲": 0, "乙": 0, "丙": 0, "丁": 0}


@pytest.mark.asyncio
async def test_only_last_user_text_and_same_fingerprint(tmp_path):
    page = LaikePage(["甲"], stuck=None)
    page.set_unread("甲", 1)
    page.bubbles["甲"] = [
        _bubble("left", "旧问题", "avatar-甲", msg_id="msg-old"),
        _bubble("right", "已经回过", "avatar-甲", msg_id="msg-agent"),
        _bubble("left", "在吗", "avatar-甲", msg_id="msg-1"),
        _bubble("left", "在吗", "avatar-甲", msg_id="msg-2"),
    ]
    driver = LaikeDriver()
    driver.account_id = "shop"
    items = await page.read(driver)
    assert [item.content for item in items] == ["在吗"]
    assert items[0].msg_key == message_fingerprint("shop", "甲", "在吗")
    state = LocalState(str(tmp_path / "seen.db"))
    assert not state.seen(items[0].msg_key)
    state.mark_seen(items[0].msg_key)
    again = await page.read(driver)
    assert again[0].msg_key == items[0].msg_key
    assert state.seen(again[0].msg_key)


# ---------- 发送 ----------

@pytest.mark.asyncio
async def test_send_paste_fill_and_enter():
    page = LaikePage(["甲", "乙"])
    driver = LaikeDriver()
    await page.read(driver)  # 首轮读过后面板停在甲
    page.active = "甲"
    page._render_bubbles("甲")
    before_sent = len(page.sent_texts)
    await driver.send_message(page.page, "甲", "你好，这是整段回复")
    assert page.sent_texts[before_sent:] == ["你好，这是整段回复"]
    assert page.input_node.attrs["value"] == "你好，这是整段回复"  # 整段填入，非逐字
    assert "Enter" in page.page.keyboard.presses


@pytest.mark.asyncio
async def test_send_truncated_to_500():
    page = LaikePage(["甲"])
    driver = LaikeDriver()
    page.active = "甲"
    page._render_bubbles("甲")
    await driver.send_message(page.page, "甲", "x" * 600)
    assert len(page.sent_texts[-1]) == 500


@pytest.mark.asyncio
async def test_send_panel_verify_failure_raises():
    """点击失效（乙卡死）→ 面板校验失败 → 抛错，绝不在错误面板输入。"""
    page = LaikePage(["甲", "乙"])
    driver = LaikeDriver()
    page.active = "甲"
    page._render_bubbles("甲")
    with pytest.raises(RuntimeError, match="面板校验失败"):
        await driver.send_message(page.page, "乙", "不该发出去的内容")
    assert page.sent_texts == []
    assert page.input_node.attrs.get("value", "") == ""  # 没往输入框写任何东西


@pytest.mark.asyncio
async def test_send_avatar_mismatch_raises():
    """重名/错行：激活了但面板头像对不上 → 抛错。"""
    page = LaikePage(["甲", "丙"], swap_on_click=("丙", "甲"))  # 点丙的行、面板渲染甲
    driver = LaikeDriver()
    with pytest.raises(RuntimeError):
        await driver.send_message(page.page, "丙", "内容")
    assert page.sent_texts == []


@pytest.mark.asyncio
async def test_self_sent_text_is_not_the_reported_message():
    """没有未读不上报。有未读时只报对方最后一条，不报自己刚发出的回复。"""
    page = LaikePage(["甲"], stuck=None)
    driver = LaikeDriver()
    driver.account_id = "shop"
    page.active = "甲"
    page._render_bubbles("甲")
    await driver.send_message(page.page, "甲", "我们的价格是6800元")
    assert await page.read(driver) == []
    page.set_unread("甲", 1)
    items = await page.read(driver)
    contents = {item.content for item in items}
    assert contents == {"甲的问题"}


@pytest.mark.asyncio
async def test_agent_bubble_without_unread_is_not_reported():
    """没有未读时，不进会话同步人工旁路消息。"""
    page = LaikePage(["甲"], stuck=None)
    driver = LaikeDriver()
    page.bubbles["甲"] = [
        _bubble("left", "甲的问题", "avatar-甲", msg_id="msg-a"),
        _bubble("right", "人工客服的回复", "agent-avatar-甲", msg_id="msg-b"),
    ]
    items = await page.read(driver)
    assert items == []
    assert page.click_counts["甲"] == 0
