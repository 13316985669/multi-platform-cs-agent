"""RPA Worker 自检医生：上线前跑一遍，全绿才可用。

八项检查：
  1. 后端连通（GET /api/health）
  2. RPA key 有效（POST /api/rpa/heartbeat dry_run 试心跳，只验 key 不落库）
  3. 浏览器可启动（Playwright Chromium）
  4. 平台后台可访问（入口地址/网络/代理，地址变更可用 PLATFORM_URL 覆盖）
  5. 登录态有效（打开平台后台不出现登录页）
  6. 关键选择器在位（会话列表等核心 DOM 存在）
  7. 媒体上下行回路（上传 1x1 图片 → 下载 → 字节一致）
  8. 平台自带机器人已关闭（防抢答双回复）

用法：python doctor.py [--platform douyin|douyin_enterprise|douyin_laike|xiaohongshu]
退出码：全部通过 0；任一失败 1（可接入 CI / 开机脚本）
"""
import argparse
import asyncio
import base64
import logging
import sys

from playwright.async_api import async_playwright

from base_worker import BackendClient, WorkerConfig
from browser_session import open_session
from douyin_comment_worker import DouyinCommentDriver
from douyin_enterprise_worker import EnterpriseDriver
from douyin_feige_worker import FeigeDriver
from douyin_laike_worker import LaikeDriver
from xhs_ark_worker import XhsArkDriver
from xhs_comment_worker import XhsCommentDriver
from xhs_publish_worker import XhsPublishDriver

logging.basicConfig(level=logging.WARNING)  # 自检输出保持干净

DRIVERS = {
    "douyin": FeigeDriver,
    "douyin_feige": FeigeDriver,
    "douyin_enterprise": EnterpriseDriver,
    "douyin_laike": LaikeDriver,
    "xiaohongshu": XhsArkDriver,
    "xhs_ark": XhsArkDriver,
    "douyin_comment": DouyinCommentDriver,
    "douyin_enterprise_comment": DouyinCommentDriver,
    "xhs_comment": XhsCommentDriver,
    "xiaohongshu_comment": XhsCommentDriver,
    "xhs_publish": XhsPublishDriver,
    "xiaohongshu_publish": XhsPublishDriver,
}

# 1x1 透明 PNG
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

GREEN, RED, RESET = "", "", ""
if sys.stdout.isatty():
    GREEN, RED, RESET = "\033[92m", "\033[91m", "\033[0m"


def _report(results: list[tuple[str, bool, str]]):
    print("\n===== RPA Worker 自检报告 =====")
    for name, ok, detail in results:
        mark = f"{GREEN}✔{RESET}" if ok else f"{RED}✘{RESET}"
        line = f" {mark} {name}"
        if detail:
            line += f" — {detail}"
        print(line)
    failed = [n for n, ok, _ in results if not ok]
    print("==============================")
    if failed:
        print(f"{RED}未通过 {len(failed)} 项: {', '.join(failed)}{RESET}")
        print("处理指引见 docs/rpa-workers.md 故障排查矩阵")
    else:
        print(f"{GREEN}全部通过，可以启动 Worker{RESET}")
    return 0 if not failed else 1


async def run_checks(cfg: WorkerConfig) -> list[tuple[str, bool, str]]:
    results: list[tuple[str, bool, str]] = []
    driver = DRIVERS[cfg.platform]()
    backend = BackendClient(cfg)

    # 1. 后端连通
    ok = await backend.health()
    results.append(("后端连通", ok, cfg.backend_url))
    if not ok:
        await backend.close()
        results.append(("RPA key 有效", False, "后端不可达，跳过"))
        return results

    # 2. RPA key 有效（dry_run 试心跳：只验 key 不落库，避免误标 online 后被 sweeper 判 offline 告警）
    try:
        await backend.heartbeat("online", meta={"doctor": True}, dry_run=True)
        results.append(("RPA key 有效", True, ""))
    except Exception as exc:  # noqa: BLE001
        results.append(("RPA key 有效", False, str(exc)[:80]))

    # 3~5/7. 浏览器 + 平台后台可达 + 登录态 + 选择器 + 平台机器人
    url = cfg.platform_url or driver.url
    try:
        async with async_playwright() as pw:
            session = await open_session(pw, cfg)
            browser_detail = "AdsPower CDP" if cfg.browser_provider == "adspower" else "本地 Chromium"
            results.append(("浏览器可启动", True, browser_detail))
            page = session.page
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                results.append(("平台后台可访问", False,
                                f"{exc}（核对入口地址与网络/代理，地址变更可用 PLATFORM_URL 覆盖）"[:120]))
                results.append(("登录态有效", False, "页面不可达，跳过"))
                results.append(("关键选择器在位", False, "页面不可达，跳过"))
                results.append(("平台自带机器人已关闭", False, "页面不可达，跳过"))
                await session.close()
            else:
                results.append(("平台后台可访问", True, url))

                status = await driver.check_status(page)
                if status == "login_expired":
                    results.append(("登录态有效", False, "请运行 python login.py 重新登录"))
                    results.append(("关键选择器在位", False, "未登录，跳过"))
                elif status == "selector_mismatch":
                    results.append(("登录态有效", True, ""))
                    results.append(("关键选择器在位", False,
                                    "页面结构已变更，请更新 Worker 选择器（见 docs/rpa-workers.md）"))
                else:
                    results.append(("登录态有效", True, ""))
                    results.append(("关键选择器在位", True, ""))

                bot_disabled = await driver.check_platform_bot_disabled(page)
                results.append(("平台自带机器人已关闭", bot_disabled,
                                "" if bot_disabled else "请在平台后台关闭自带机器人/自动回复，否则会双份回复"))
                await session.close()
    except Exception as exc:  # noqa: BLE001
        results.append(("浏览器可启动", False, f"{exc}（先运行 playwright install chromium）"[:100]))

    # 6. 媒体上下行回路
    try:
        media_id = await backend.upload_media(TINY_PNG, "doctor.png", "image/png", "image")
        import tempfile, os
        dest = os.path.join(tempfile.gettempdir(), f"doctor_{media_id}.png")
        await backend.download_media(media_id, dest)
        with open(dest, "rb") as f:
            same = f.read() == TINY_PNG
        os.remove(dest)
        results.append(("媒体上下行回路", same, "" if same else "下载字节与上传不一致"))
    except Exception as exc:  # noqa: BLE001
        results.append(("媒体上下行回路", False, str(exc)[:80]))

    await backend.close()
    return results


def main():
    parser = argparse.ArgumentParser(description="RPA Worker 自检")
    parser.add_argument("--platform", choices=sorted(DRIVERS), default=None)
    args = parser.parse_args()

    cfg = WorkerConfig.from_env(require_account=False)
    if args.platform:
        cfg.platform = args.platform
    if cfg.platform not in DRIVERS:
        raise SystemExit(f"无法识别的平台或 driver：{cfg.platform}")
    if cfg.browser_provider == "adspower" and not cfg.adspower_profile_id:
        raise SystemExit("AdsPower 自检需要 ADSPOWER_PROFILE_ID")
    if cfg.browser_provider != "adspower" and not cfg.account:
        raise SystemExit("未配置 ACCOUNT")
    results = asyncio.run(run_checks(cfg))
    raise SystemExit(_report(results))


if __name__ == "__main__":
    main()
