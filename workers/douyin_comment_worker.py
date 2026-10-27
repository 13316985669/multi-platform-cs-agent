"""抖音评论 Worker：e.douyin.com 企业号后台评论管理页自动化（item.comment 的 RPA 兜底）。

适用场景：企业号不适用 item.comment 权限（已暂停申请）时，用本 Worker 采集与回复评论。
登录态复用企业号 driver 的 profile（PLATFORM=douyin_enterprise 同一 ACCOUNT 目录）。

TODO: 确认实际接口地址（企业号后台评论管理页 URL 与选择器，首次联调用 doctor 校准）
运行：python douyin_comment_worker.py（.env.local 中 PLATFORM=douyin_enterprise_comment，ACCOUNT 与矩阵账号 rpa_account 一致）
"""
import logging

from playwright.async_api import Page

from base_worker import (CommentDriver, CommentItem, IncomingItem, WorkerConfig,
                         run_comment_worker)
from page_markers import is_login_wall, page_text

logger = logging.getLogger("rpa_worker.douyin_comment")

# TODO: 确认实际接口地址（企业号评论管理页 URL，首次联调校准）
ENTERPRISE_COMMENT_URL = "https://e.douyin.com/site/comment/manage"

# ============ 选择器（首次联调用 doctor 校准；页面改版只需改这里） ============
# TODO: 确认实际接口地址（以下选择器为占位结构，联调时按实际 DOM 校准）
SELECTORS = {
    "login_form": ".login-form",
    "self_name": ".self-account-name",
    "comment_list": ".comment-list",
    "comment_item": ".comment-item",
    "comment_author": ".comment-author",
    "comment_content": ".comment-content",
    "reply_button": ".reply-button",
    "reply_input": ".reply-input",
    "reply_submit": ".reply-submit",
    "reply_success": ".reply-success-toast",
    # 视频页首评（顶层评论）
    "video_comment_input": ".video-comment-input",  # 视频页评论输入框
    "video_comment_submit": ".video-comment-submit",  # 评论发送按钮
}


class DouyinCommentDriver(CommentDriver):
    name = "douyin_comment"
    url = ENTERPRISE_COMMENT_URL

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
        for _ in range(10):
            await page.wait_for_timeout(300)
            if await page.locator(SELECTORS["reply_success"]).count() > 0:
                return
        raise RuntimeError("未检测到回复成功提示")

    async def post_first_comment(self, page: Page, post_url: str, text: str) -> None:
        """打开视频页发顶层评论（首评引流）。

        TODO: 确认实际接口地址（视频页评论区选择器，联调时按实际 DOM 校准）
        """
        if not post_url:
            raise RuntimeError("首评缺少作品链接（post_url）")
        await page.goto(post_url, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        comment_input = page.locator(SELECTORS["video_comment_input"])
        if await comment_input.count() == 0:
            raise RuntimeError("视频页评论输入框不存在（选择器漂移或未登录）")
        await comment_input.first.click()
        await comment_input.first.press_sequentially(text, delay=80)
        await page.wait_for_timeout(500)
        await page.locator(SELECTORS["video_comment_submit"]).first.click()
        for _ in range(10):
            await page.wait_for_timeout(300)
            if await page.locator(SELECTORS["reply_success"]).count() > 0:
                return
        if (await comment_input.first.inner_text()).strip() == "":
            return  # 输入框清空视为已发送
        raise RuntimeError("未检测到首评发布成功提示")


if __name__ == "__main__":
    run_comment_worker(WorkerConfig.from_env(), DouyinCommentDriver())
