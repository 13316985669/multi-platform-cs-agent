"""小红书发布 Worker：创作服务平台（creator.xiaohongshu.com）图文笔记发布自动化。

与私信 Worker（xhs_ark_worker）职责不同：本 Worker 只做「发布」，不读私信。
- 拉取 outbox 中 msg_type=publish_note 的任务 → 下载素材 → 填标题/正文/标签 → 上传图片 → 发布
- 频控（防风控，Worker 侧硬约束）：每账号每日 ≤3 条；两次发布间隔随机 30~90 分钟
- 登录过期/选择器漂移 → 上报状态并暂停（框架统一处理）

TODO: 确认实际接口地址（创作服务平台发布页 URL 与选择器，首次联调用 doctor 校准）
运行：python xhs_publish_worker.py（.env.local 中 PLATFORM=xiaohongshu_publish，ACCOUNT 与后端矩阵账号 rpa_account 一致）
"""
import json
import logging
import os
import random
import time

from playwright.async_api import Page

from base_worker import (BaseWorker, IncomingItem, PlatformDriver, WorkerConfig)

logger = logging.getLogger("rpa_worker.xhs_publish")

# TODO: 确认实际接口地址（创作服务平台发布页 URL，首次联调校准）
CREATOR_PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"

# ============ 选择器（首次联调用 doctor 校准；页面改版只需改这里） ============
# TODO: 确认实际接口地址（以下选择器为创作服务平台常见结构占位，联调时按实际 DOM 校准）
SELECTORS = {
    "login_form": ".login-form",                    # 登录表单（可见 = 登录过期）
    "self_name": ".self-account-name",
    "publish_entry": ".publish-entry",              # 发布页容器（不存在 = 选择器漂移）
    "tab_image_text": ".tab-image-text",            # 「图文」Tab
    "title_input": ".title-input",                  # 标题输入框
    "body_editor": ".body-editor",                  # 正文编辑器（contenteditable）
    "topic_input": ".topic-input",                  # 话题输入框
    "topic_suggestion": ".topic-suggestion-item",   # 话题下拉候选
    "image_upload": "input[type='file'].image-upload",  # 图片上传 input
    "submit_button": ".submit-button",              # 发布按钮
    "success_toast": ".publish-success-toast",      # 发布成功提示
    # 笔记管理页（数据回采用）
    "note_manage_entry": ".note-manage-entry",      # 笔记管理列表容器
    "note_stat_play": ".note-stat-play",            # 阅读数
    "note_stat_digg": ".note-stat-digg",            # 点赞数
    "note_stat_comment": ".note-stat-comment",      # 评论数
    "note_stat_collect": ".note-stat-collect",      # 收藏数
    "note_stat_share": ".note-stat-share",          # 分享数
    # 创作中心主页（账号画像回采用）
    "profile_followers": ".profile-followers",      # 粉丝数
    "profile_works": ".profile-works",              # 作品数
    "profile_liked": ".profile-liked",              # 获赞数
}

# TODO: 确认实际接口地址（笔记管理页 URL，首次联调校准）
CREATOR_NOTES_URL = "https://creator.xiaohongshu.com/creator/notemanager"
# TODO: 确认实际接口地址（创作中心主页 URL，首次联调校准）
CREATOR_HOME_URL = "https://creator.xiaohongshu.com/creator/home"

# 频控参数（防风控硬约束，勿调大）
DAILY_LIMIT = 3
INTERVAL_MIN_SECONDS = 30 * 60
INTERVAL_MAX_SECONDS = 90 * 60


class XhsPublishDriver(PlatformDriver):
    name = "xhs_publish"
    url = CREATOR_PUBLISH_URL

    async def check_status(self, page: Page) -> str:
        if await page.locator(SELECTORS["login_form"]).first.is_visible():
            return "login_expired"
        if await page.locator(SELECTORS["publish_entry"]).count() == 0:
            return "selector_mismatch"
        return "online"

    async def read_incoming(self, page: Page) -> list[IncomingItem]:
        return []  # 纯发布 Worker，不读入站

    async def send_message(self, page: Page, conv_key: str, content: str,
                           msg_type: str = "text", media_path: str = "") -> None:
        raise RuntimeError("发布 Worker 不支持私信发送（请使用 xhs_ark_worker）")

    async def publish_note(self, page: Page, payload: dict,
                           image_paths: list[str]) -> str:
        """执行图文笔记发布，返回笔记 URL（失败抛异常由框架 ack failed）。"""
        await page.goto(self.url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)

        # 切到「图文」Tab
        tab = page.locator(SELECTORS["tab_image_text"])
        if await tab.count() > 0:
            await tab.first.click()
            await page.wait_for_timeout(500)

        # 上传图片
        if image_paths:
            upload = page.locator(SELECTORS["image_upload"])
            if await upload.count() == 0:
                raise RuntimeError("图片上传入口不存在（选择器漂移）")
            await upload.first.set_input_files(image_paths)
            await page.wait_for_timeout(2000 + 1000 * len(image_paths))  # 等上传

        # 标题
        await BaseWorker.type_humanlike(page, SELECTORS["title_input"],
                                        (payload.get("title") or "")[:20])
        # 正文
        body = payload.get("body") or ""
        if body:
            editor = page.locator(SELECTORS["body_editor"])
            await editor.first.click()
            await BaseWorker.type_humanlike(page, SELECTORS["body_editor"], body)

        # 话题标签：逐个输入并选候选
        for tag in (payload.get("tags") or [])[:10]:
            try:
                topic_input = page.locator(SELECTORS["topic_input"])
                await topic_input.first.click()
                await BaseWorker.type_humanlike(page, SELECTORS["topic_input"], f"#{tag}")
                await page.wait_for_timeout(800)
                suggestion = page.locator(SELECTORS["topic_suggestion"])
                if await suggestion.count() > 0:
                    await suggestion.first.click()
            except Exception:  # noqa: BLE001
                logger.warning("话题 %s 添加失败，跳过", tag)

        # 发布 + 成功确认
        await page.locator(SELECTORS["submit_button"]).first.click()
        for _ in range(30):  # 1s × 30 = 30s 超时
            await page.wait_for_timeout(1000)
            if await page.locator(SELECTORS["success_toast"]).count() > 0:
                # 发布后跳转到笔记管理页可拿真实 URL；此处先记录当前页 URL
                return page.url
        raise RuntimeError("未检测到发布成功提示（可能进入审核或被拦截）")

    @staticmethod
    def _parse_count(text: str) -> int:
        """解析「1.2万」「3456」等计数文本。"""
        text = (text or "").strip().replace(",", "")
        if not text:
            return 0
        try:
            if "万" in text:
                return int(float(text.replace("万", "")) * 10000)
            return int(float(text))
        except ValueError:
            return 0

    async def collect_stats(self, page: Page, note_url: str) -> dict:
        """打开笔记页读取数据（阅读/点赞/评论/收藏/分享），返回标准 stats dict。

        TODO: 确认实际接口地址（笔记详情页/管理页数据区选择器，联调校准）
        """
        await page.goto(note_url or CREATOR_NOTES_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        async def _read(key: str) -> int:
            node = page.locator(SELECTORS[key])
            if await node.count() == 0:
                return 0
            return self._parse_count(await node.first.inner_text())

        return {
            "play": await _read("note_stat_play"),
            "digg": await _read("note_stat_digg"),
            "comment": await _read("note_stat_comment"),
            "collect": await _read("note_stat_collect"),
            "share": await _read("note_stat_share"),
        }

    async def collect_account(self, page: Page) -> dict:
        """打开创作中心主页读取账号画像（粉丝/作品/获赞），返回标准 profile dict。

        TODO: 确认实际接口地址（创作中心主页数据区选择器，联调校准）
        """
        await page.goto(CREATOR_HOME_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)

        async def _read(key: str) -> int:
            node = page.locator(SELECTORS[key])
            if await node.count() == 0:
                return 0
            return self._parse_count(await node.first.inner_text())

        return {
            "followers": await _read("profile_followers"),
            "works": await _read("profile_works"),
            "liked": await _read("profile_liked"),
        }


class PublishWorker(BaseWorker):
    """发布专用 Worker：处理 outbox 中 msg_type=publish_note 的任务（含频控）。"""

    def _today_key(self) -> str:
        return f"publish_count_{time.strftime('%Y-%m-%d')}"

    def _rate_limited(self) -> str:
        """返回空串表示可发布；否则返回原因（跳过本轮，不 ack，租约到期自动回 pending）。"""
        count = int(self.state.kv_get(self._today_key(), "0") or 0)
        if count >= DAILY_LIMIT:
            return f"今日已发 {count} 条（上限 {DAILY_LIMIT}）"
        next_after = float(self.state.kv_get("next_publish_after", "0") or 0)
        if time.time() < next_after:
            return f"发布间隔未到（{int(next_after - time.time())}s 后可发）"
        return ""

    def _record_publish(self):
        count = int(self.state.kv_get(self._today_key(), "0") or 0)
        self.state.kv_set(self._today_key(), str(count + 1))
        interval = random.randint(INTERVAL_MIN_SECONDS, INTERVAL_MAX_SECONDS)
        self.state.kv_set("next_publish_after", str(time.time() + interval))
        logger.info("发布计数 %d/%d，下次发布间隔 %d 分钟",
                    count + 1, DAILY_LIMIT, interval // 60)

    async def _send_one(self, page: Page, item: dict):
        msg_type = item.get("msg_type")
        if msg_type == "collect_stats":
            await self._collect_one(page, item)
            return
        if msg_type == "collect_account":
            await self._collect_account_one(page, item)
            return
        if msg_type != "publish_note":
            await self._release_foreign(item)
            return
        outbox_id = item["outbox_id"]

        limited = self._rate_limited()
        if limited:
            logger.info("频控跳过 outbox_id=%s：%s（不 ack，租约到期自动重排）", outbox_id, limited)
            return

        try:
            payload = json.loads(item.get("content") or "{}")
        except json.JSONDecodeError as exc:
            await self.backend.ack(outbox_id, False, error=f"任务内容 JSON 解析失败: {exc}")
            return

        # 下载素材（图片）
        image_paths: list[str] = []
        try:
            for mid in payload.get("material_ids") or []:
                dest = os.path.join(self.cfg.download_dir, f"material_{mid}")
                await self.backend.download_material(int(mid), dest)
                image_paths.append(dest)
        except Exception as exc:  # noqa: BLE001
            await self.backend.ack(outbox_id, False, error=f"素材下载失败: {exc}")
            return

        try:
            note_url = await self.driver.publish_note(page, payload, image_paths)
            self._record_publish()
            await self.backend.ack(outbox_id, True, result=note_url)
            logger.info("笔记发布成功 outbox_id=%s url=%s", outbox_id, note_url)
        except Exception as exc:  # noqa: BLE001
            logger.exception("笔记发布失败 outbox_id=%s", outbox_id)
            await self.backend.ack(outbox_id, False, error=str(exc))
            await self._self_check(page)

    async def _collect_one(self, page: Page, item: dict):
        """数据回采任务：不占发布频控额度，读取笔记数据后 ack 回传 JSON。"""
        outbox_id = item["outbox_id"]
        try:
            payload = json.loads(item.get("content") or "{}")
        except json.JSONDecodeError as exc:
            await self.backend.ack(outbox_id, False, error=f"任务内容 JSON 解析失败: {exc}")
            return
        try:
            stats = await self.driver.collect_stats(page, payload.get("url") or "")
            await self.backend.ack(outbox_id, True,
                                   result=json.dumps(stats, ensure_ascii=False))
            logger.info("数据回采成功 outbox_id=%s stats=%s", outbox_id, stats)
        except Exception as exc:  # noqa: BLE001
            logger.exception("数据回采失败 outbox_id=%s", outbox_id)
            await self.backend.ack(outbox_id, False, error=str(exc))

    async def _collect_account_one(self, page: Page, item: dict):
        """账号画像回采任务：不占发布频控额度，读取创作中心主页数据后 ack 回传 JSON。"""
        outbox_id = item["outbox_id"]
        try:
            profile = await self.driver.collect_account(page)
            await self.backend.ack(outbox_id, True,
                                   result=json.dumps(profile, ensure_ascii=False))
            logger.info("画像回采成功 outbox_id=%s profile=%s", outbox_id, profile)
        except Exception as exc:  # noqa: BLE001
            logger.exception("画像回采失败 outbox_id=%s", outbox_id)
            await self.backend.ack(outbox_id, False, error=str(exc))


if __name__ == "__main__":
    import asyncio

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker = PublishWorker(WorkerConfig.from_env(), XhsPublishDriver())
    try:
        asyncio.run(worker.run())
    except KeyboardInterrupt:
        logger.info("Worker 已停止")
