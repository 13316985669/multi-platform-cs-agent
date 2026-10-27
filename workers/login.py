"""RPA Worker 登录引导：打开有头浏览器 → 手工扫码登录 → 登录态落盘。

用法：python login.py [--platform douyin|douyin_enterprise|douyin_laike|xiaohongshu]
登录态保存在 profiles/<account>/ 目录，后续 Worker 以 headless 模式复用，无需重复登录。
"""
import argparse
import asyncio

from playwright.async_api import async_playwright

from base_worker import WorkerConfig
from browser_session import open_session
from douyin_enterprise_worker import EnterpriseDriver
from douyin_feige_worker import FeigeDriver
from douyin_laike_worker import LaikeDriver
from xhs_ark_worker import XhsArkDriver

DRIVERS = {"douyin": FeigeDriver, "douyin_enterprise": EnterpriseDriver,
           "douyin_laike": LaikeDriver, "xiaohongshu": XhsArkDriver}


async def guided_login(cfg: WorkerConfig):
    driver = DRIVERS[cfg.platform]()
    url = cfg.platform_url or driver.url
    where = (f"AdsPower 环境 {cfg.adspower_profile_id}"
             if cfg.browser_provider == "adspower"
             else f"profile 目录: {cfg.profile_dir}/{cfg.account}")
    print(f"即将打开浏览器，请手工登录 {cfg.platform} 平台后台（扫码/账号密码均可）")
    print(f"登录成功并看到客服工作台后，回到这里按回车确认。{where}")

    async with async_playwright() as pw:
        cfg.headless = False  # 登录必须有界面；AdsPower 同样不用无头
        session = await open_session(pw, cfg)
        page = session.page

        # 导航容错：网络/代理/URL 问题给出友好提示并允许重试，不直接抛 traceback
        while True:
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"✘ 打开平台后台失败：{exc}")
                print("  请检查：1) 本机网络/代理；2) 平台入口地址是否变更"
                      "（可在 .env.local 配置 PLATFORM_URL 覆盖）")
                choice = await asyncio.get_running_loop().run_in_executor(
                    None, input, "按回车重试，输入 q 退出: ")
                if choice.strip().lower() == "q":
                    await session.close()
                    return False

        await asyncio.get_running_loop().run_in_executor(None, input, "登录完成后按回车继续...")

        status = await driver.check_status(page)
        if status == "online":
            print("✔ 登录态验证通过，已保存。可以启动 Worker 了。")
        else:
            print(f"✘ 登录态验证失败（status={status}），请重新运行本脚本完成登录。")
        await session.close()
        return status == "online"


def main():
    parser = argparse.ArgumentParser(description="RPA Worker 登录引导")
    parser.add_argument("--platform",
                        choices=["douyin", "douyin_enterprise", "douyin_laike", "xiaohongshu"], default=None)
    args = parser.parse_args()

    cfg = WorkerConfig.from_env(require_account=False)
    if cfg.browser_provider == "adspower" and not cfg.adspower_profile_id:
        raise SystemExit("AdsPower 登录需要在 .env.local 填写 ADSPOWER_PROFILE_ID")
    if cfg.browser_provider != "adspower" and not cfg.account:
        raise SystemExit("未配置 ACCOUNT（本地浏览器登录态目录需要它）")
    if args.platform:
        cfg.platform = args.platform
    ok = asyncio.run(guided_login(cfg))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
