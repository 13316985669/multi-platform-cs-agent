"""按授权页绑定启动对应平台脚本。

没有启用绑定时，若 .env.local 仍有 ACCOUNT 和 PLATFORM，按原来的方式启动。
绑定在运行中被修改时进程退出码为 0，可由 run_assigned.ps1 重新拉起。
"""
import asyncio
import logging

from base_worker import (AssignmentChanged, BackendClient, BaseWorker, CommentWorker,
                         WorkerConfig)

logger = logging.getLogger("rpa_worker")


def make_worker(cfg: WorkerConfig):
    name = cfg.driver_name or cfg.platform
    if name in ("douyin", "douyin_feige"):
        from douyin_feige_worker import FeigeDriver
        return BaseWorker(cfg, FeigeDriver())
    if name == "douyin_enterprise":
        from douyin_enterprise_worker import EnterpriseDriver
        return BaseWorker(cfg, EnterpriseDriver())
    if name == "douyin_laike":
        from douyin_laike_worker import LaikeDriver
        return BaseWorker(cfg, LaikeDriver())
    if name in ("xiaohongshu", "xhs_ark"):
        from xhs_ark_worker import XhsArkDriver
        return BaseWorker(cfg, XhsArkDriver())
    if name in ("douyin_comment", "douyin_enterprise_comment"):
        from douyin_comment_worker import DouyinCommentDriver
        return CommentWorker(cfg, DouyinCommentDriver())
    if name in ("xhs_comment", "xiaohongshu_comment"):
        from xhs_comment_worker import XhsCommentDriver
        return CommentWorker(cfg, XhsCommentDriver())
    if name in ("xhs_publish", "xiaohongshu_publish"):
        from xhs_publish_worker import PublishWorker, XhsPublishDriver
        return PublishWorker(cfg, XhsPublishDriver())
    raise SystemExit(f"无法识别的 driver 或平台：{name}")


def _apply_bound(cfg: WorkerConfig, data: dict) -> WorkerConfig:
    account = (data.get("account") or "").strip()
    if not account:
        raise SystemExit("绑定的矩阵账号没有填写 Worker 账号标识（rpa_account）")
    cfg.account = account
    cfg.driver_name = data.get("driver") or ""
    cfg.platform = cfg.driver_name
    cfg.browser_provider = data.get("provider") or "local"
    cfg.adspower_profile_id = data.get("adspower_profile_id") or ""
    cfg.binding_id = int(data.get("binding_id") or 0)
    cfg.msg_types = ",".join(data.get("msg_types") or [])
    if data.get("platform_url"):
        cfg.platform_url = data["platform_url"]
    if cfg.browser_provider == "adspower" and not cfg.adspower_profile_id:
        raise SystemExit("这条绑定选择了 AdsPower，但没有环境 ID")
    logger.info(
        "已领取绑定 #%s driver=%s account=%s provider=%s",
        cfg.binding_id, cfg.driver_name, cfg.account, cfg.browser_provider,
    )
    return cfg


async def wait_for_assignment(cfg: WorkerConfig) -> WorkerConfig:
    """没有绑定时先以空闲心跳露面，领到任务再打开浏览器。"""
    from browser_session import AdsPowerClient

    client = BackendClient(cfg)
    announced = False
    try:
        while True:
            data = await client.fetch_assignment()
            if data.get("bound"):
                return _apply_bound(cfg, data)
            profiles = []
            ads_error = ""
            try:
                profiles = await AdsPowerClient(
                    cfg.adspower_api_base, cfg.adspower_api_key,
                ).list_profiles()
            except Exception as exc:  # noqa: BLE001
                ads_error = str(exc)[:200]
                logger.warning("读取 AdsPower 环境列表失败: %s", ads_error)
            await client.heartbeat("idle", meta={
                "browser_profiles": profiles, "binding_id": 0, "ads_error": ads_error,
            })
            if not announced:
                logger.info("Worker %s 空闲，等待账号页绑定", cfg.worker_id)
                announced = True
            await asyncio.sleep(cfg.heartbeat_interval)
    finally:
        await client.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = WorkerConfig.from_env(require_account=False)
    cfg = asyncio.run(wait_for_assignment(cfg))
    worker = make_worker(cfg)
    try:
        asyncio.run(worker.run())
    except AssignmentChanged as exc:
        logger.info("%s", exc)
        raise SystemExit(0)
    except KeyboardInterrupt:
        logger.info("Worker 已停止")
        raise SystemExit(0)


if __name__ == "__main__":
    main()
