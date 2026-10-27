"""小红书评论 Worker：创作服务平台评论管理页自动化（采集 + 回复）。

- 采集：轮询评论管理页，新评论经 POST /api/rpa/incoming_comment 入队评论引擎
- 回复：拉取 outbox 中 msg_type=comment_reply 任务 → 定位评论 → 回复 → ack
- 频控由后端引擎负责（xiaohongshu_rpa_comment 规则），Worker 只做页面操作

TODO: 确认实际接口地址（创作服务平台评论管理页 URL 与选择器，首次联调用 doctor 校准）
运行：python xhs_comment_worker.py（.env.local 中 PLATFORM=xiaohongshu_comment，ACCOUNT 与矩阵账号 rpa_account 一致）
"""
import logging

from playwright.async_api import Page

from base_worker import (CommentDriver, CommentItem, IncomingItem, WorkerConfig,
                         run_comment_worker)
from page_markers import is_login_wall, page_text

logger = logging.getLogger("rpa_worker.xhs_comment")

# TODO: 确认实际接口地址（创作服务平台评论管理页 URL，首次联调校准）
COMMENT_MANAGE_URL = "https://creator.xiaohongshu.com/comment/manage"

# ============ 选择器（首次联调用 doctor 校准；页面改版只需改这里） ============
# TODO: 确认实际接口地址（以下选择器为占位结构，联调时按实际 DOM 校准）
SELECTORS = {
    "login_form": ".login-form",                  # 登录表单（可见 = 登录过期）
    "self_name": ".self-account-name",
    "comment_list": ".comment-list",              # 评论列表容器（不存在 = 选择器漂移）
    "comment_item": ".comment-item",              # 单条评论（data-cid / data-post-url 属性）
    "comment_author": ".comment-author",          # 评论作者昵称
    "comment_content": ".comment-content",        # 评论内容
    "reply_button": ".reply-button",              # 回复按钮（定位到该评论）
    "reply_input": ".reply-input",                # 回复输入框
    "reply_submit": ".reply-submit",              # 回复提交按钮
    "reply_success": ".reply-success-toast",      # 回复成功提示
    # 作品页首评（顶层评论）
    "note_comment_input": ".note-comment-input",  # 作品页评论输入框
    "note_comment_submit": ".note-comment-submit",  # 评论发送按钮
}


class XhsCommentDriver(CommentDriver):
    name = "xhs_comment"
    url = COMMENT_MANAGE_URL

    async def check_status(self, page: Page) -> str:
        login = page.locator(SELECTORS["login_form"])
        if await login.count() and await login.first.is_visible():
            return "login_expired"
        if await page.locator(SELECTORS["comment_list"]).count() > 0:
            return "online"
        if is_login_wall(await page_text(page)):
            return "login_expired"
        return "selector_mismatch"

    async def read_incoming(self, page: Page) -> list[IncomingItem]:
        return []  # 评论 Worker 不读私信

    async def send_message(self, page: Page, conv_key: str, content: str,
                           msg_type: str = "text", media_path: str = "") -> None:
        raise RuntimeError("评论 Worker 不支持私信发送")

    async def read_comments(self, page: Page) -> list[CommentItem]:
        items: list[CommentItem] = []
        comments = page.locator(SELECTORS["comment_item"])
        for i in range(await comments.count()):
            node = comments.nth(i)
            comment_id = await node.get_attribute("data-cid") or ""
            if not comment_id:
                continue
            author = await node.locator(SELECTORS["comment_author"]).inner_text()
            content = await node.locator(SELECTORS["comment_content"]).inner_text()
            items.append(CommentItem(
                comment_id=comment_id,
                post_url=await node.get_attribute("data-post-url") or "",
                post_id=await node.get_attribute("data-post-id") or "",
                author_nickname=author.strip(),
                author_id=await node.get_attribute("data-author-id") or "",
                content=content.strip(),
                parent_comment_id=await node.get_attribute("data-parent-cid") or "",
            ))
        return items

    async def reply_comment(self, page: Page, comment_id: str, text: str,
                            post_url: str = "") -> None:
        node = page.locator(f"{SELECTORS['comment_item']}[data-cid='{comment_id}']")
        if await node.count() == 0:
            raise RuntimeError(f"评论不存在或已翻页: {comment_id}")
        await node.first.locator(SELECTORS["reply_button"]).click()
        await page.wait_for_timeout(300)
        reply_input = node.first.locator(SELECTORS["reply_input"])
        await reply_input.click()
        await reply_input.press_sequentially(text, delay=80)
        await page.wait_for_timeout(300)
        await node.first.locator(SELECTORS["reply_submit"]).click()
        # 回复成功确认
        for _ in range(10):
            await page.wait_for_timeout(300)
            if await page.locator(SELECTORS["reply_success"]).count() > 0:
                return
        raise RuntimeError("未检测到回复成功提示")

    async def post_first_comment(self, page: Page, post_url: str, text: str) -> None:
        """打开作品页发顶层评论（首评引流）。

        TODO: 确认实际接口地址（作品页评论区选择器，联调时按实际 DOM 校准）
        """
        if not post_url:
            raise RuntimeError("首评缺少作品链接（post_url）")
        await page.goto(post_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        comment_input = page.locator(SELECTORS["note_comment_input"])
        if await comment_input.count() == 0:
            raise RuntimeError("作品页评论输入框不存在（选择器漂移或未登录）")
        await comment_input.first.click()
        await comment_input.first.press_sequentially(text, delay=80)
        await page.wait_for_timeout(500)
        await page.locator(SELECTORS["note_comment_submit"]).first.click()
        for _ in range(10):
            await page.wait_for_timeout(300)
            if await page.locator(SELECTORS["reply_success"]).count() > 0:
                return
        # 部分页面无成功提示：输入框清空视为已发送
        if (await comment_input.first.inner_text()).strip() == "":
            return
        raise RuntimeError("未检测到首评发布成功提示")


if __name__ == "__main__":
    run_comment_worker(WorkerConfig.from_env(), XhsCommentDriver())
