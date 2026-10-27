"""热榜采集 Worker：按关键词搜索平台爆款内容，导入后端灵感库（一次性任务，非常驻）。

与常驻 Worker 不同：本脚本跑完即退出，适合人工触发或计划任务（如每天早 8 点采集一轮）。

用法：
    python hot_collect_worker.py --platform xiaohongshu --keyword 宠物烘干箱 --limit 20
    python hot_collect_worker.py --platform douyin --keyword 宠物烘干箱 --limit 20

登录态：复用 .env.local 中 ACCOUNT 对应的浏览器 profile（先跑对应平台 Worker 完成扫码登录）。
频控：每次运行只采集一个关键词，条目间随机停顿 2-5s（防风控）。

TODO: 确认实际接口地址（搜索结果页 URL 与卡片选择器，首次联调用 doctor 校准）
"""
import argparse
import asyncio
import json
import logging
import random

import httpx
from playwright.async_api import async_playwright

from base_worker import WorkerConfig

logger = logging.getLogger("rpa_worker.hot_collect")

# TODO: 确认实际接口地址（搜索页 URL 模板，联调校准）
SEARCH_URLS = {
    "xiaohongshu": "https://www.xiaohongshu.com/search_result?keyword={keyword}&sort=popularity_descending",
    "douyin": "https://www.douyin.com/search/{keyword}?sort=general",
}

# TODO: 确认实际接口地址（以下选择器为搜索结果页常见结构占位，联调时按实际 DOM 校准）
SELECTORS = {
    "login_form": ".login-form",
    "result_card": ".search-result-card",       # 结果卡片（data-url 属性）
    "card_title": ".card-title",                # 标题
    "card_author": ".card-author",              # 作者
    "card_digg": ".card-digg-count",            # 点赞数
}


def _parse_count(text: str) -> int:
    text = (text or "").strip().replace(",", "")
    if not text:
        return 0
    try:
        if "万" in text:
            return int(float(text.replace("万", "")) * 10000)
        return int(float(text))
    except ValueError:
        return 0


async def collect(cfg: WorkerConfig, platform: str, keyword: str, limit: int) -> list[dict]:
    """打开搜索页，按热度排序采集前 limit 条结果。"""
    url = SEARCH_URLS[platform].format(keyword=keyword)
    items: list[dict] = []
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            f"{cfg.profile_dir}/{cfg.account}", headless=cfg.headless,
            viewport={"width": 1366, "height": 900})
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)
            if await page.locator(SELECTORS["login_form"]).first.is_visible():
                raise RuntimeError("登录态已过期：请先运行对应平台 Worker 完成扫码登录")

            cards = page.locator(SELECTORS["result_card"])
            total = await cards.count()
            if total == 0:
                raise RuntimeError("搜索结果为空或选择器漂移（用 doctor 校准选择器）")

            for i in range(min(total, limit)):
                node = cards.nth(i)
                try:
                    title = (await node.locator(SELECTORS["card_title"]).inner_text()).strip()
                    if not title:
                        continue
                    author = (await node.locator(SELECTORS["card_author"]).inner_text()).strip()
                    digg_text = await node.locator(SELECTORS["card_digg"]).inner_text()
                    items.append({
                        "source_url": await node.get_attribute("data-url") or "",
                        "author": author,
                        "title": title,
                        "content_text": "",
                        "stats_json": {"digg": _parse_count(digg_text)},
                    })
                    await page.wait_for_timeout(random.randint(2000, 5000))  # 拟人停顿
                except Exception:  # noqa: BLE001
                    logger.warning("第 %s 条卡片解析失败，跳过", i + 1)
        finally:
            await context.close()
    return items


async def import_to_backend(cfg: WorkerConfig, platform: str, keyword: str,
                            items: list[dict]) -> dict:
    """POST /api/inspiration/import（X-Rpa-Key 鉴权，服务端按 source_url 去重）。"""
    async with httpx.AsyncClient(base_url=cfg.backend_url, timeout=30) as client:
        resp = await client.post(
            "/api/inspiration/import",
            headers={"X-Rpa-Key": cfg.rpa_key},
            json={"platform": platform, "keyword": keyword, "items": items})
        resp.raise_for_status()
        return resp.json()


async def main():
    parser = argparse.ArgumentParser(description="热榜爆款采集 → 灵感库")
    parser.add_argument("--platform", required=True, choices=["xiaohongshu", "douyin"])
    parser.add_argument("--keyword", required=True, help="搜索关键词（如品类词）")
    parser.add_argument("--limit", type=int, default=20, help="采集条数（默认 20，勿超 50）")
    args = parser.parse_args()

    cfg = WorkerConfig.from_env()
    if not cfg.rpa_key:
        raise SystemExit(".env.local 未配置 RPA_API_KEY")
    if not cfg.account:
        raise SystemExit(".env.local 未配置 ACCOUNT（登录态 profile 目录名）")

    logger.info("开始采集 platform=%s keyword=%s limit=%s", args.platform, args.keyword, args.limit)
    items = await collect(cfg, args.platform, args.keyword, min(args.limit, 50))
    logger.info("采集到 %s 条，导入后端", len(items))
    result = await import_to_backend(cfg, args.platform, args.keyword, items)
    logger.info("导入完成: %s", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
