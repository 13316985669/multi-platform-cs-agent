"""自动备份：定时打包 SQLite 数据库（安全备份 API）与 Chroma 向量库，滚动保留最近 N 份。

- BACKUP_INTERVAL_HOURS 控制间隔，默认 24 小时
- BACKUP_DIR 默认 ./backups，BACKUP_KEEP 默认保留 7 份
- do_backup() 可直接同步调用（测试/手动备份用）
"""
import asyncio
import logging
import os
import shutil
import sqlite3
import time

from ..config import settings

logger = logging.getLogger(__name__)

_backup_task: asyncio.Task | None = None


def do_backup() -> str:
    """执行一次备份，返回备份目录路径。"""
    os.makedirs(settings.backup_dir, exist_ok=True)
    dest = os.path.join(settings.backup_dir, time.strftime("backup_%Y%m%d_%H%M%S"))
    os.makedirs(dest, exist_ok=True)

    # SQLite：用 backup API（WAL 模式下直接拷文件会丢未 checkpoint 的数据）
    if settings.database_url.startswith("sqlite"):
        db_path = settings.database_url.split("///")[-1]
        if os.path.exists(db_path):
            src = sqlite3.connect(db_path)
            dst = sqlite3.connect(os.path.join(dest, "app.db"))
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()

    # Chroma 向量库：目录整体拷贝
    if os.path.isdir(settings.chroma_dir):
        shutil.copytree(settings.chroma_dir, os.path.join(dest, "chroma"), dirs_exist_ok=True)

    # 上传目录（含 RPA 媒体：用户图片/语音）：目录整体拷贝
    if os.path.isdir(settings.upload_dir):
        shutil.copytree(settings.upload_dir, os.path.join(dest, "uploads"), dirs_exist_ok=True)

    _rotate()
    logger.info("备份完成: %s", dest)
    return dest


def _rotate():
    """滚动清理：只保留最近 backup_keep 份。"""
    try:
        backups = sorted(
            d for d in os.listdir(settings.backup_dir)
            if d.startswith("backup_") and os.path.isdir(os.path.join(settings.backup_dir, d))
        )
    except FileNotFoundError:
        return
    for old in backups[:-settings.backup_keep]:
        shutil.rmtree(os.path.join(settings.backup_dir, old), ignore_errors=True)
        logger.info("清理旧备份: %s", old)


async def backup_loop():
    while True:
        await asyncio.sleep(settings.backup_interval_hours * 3600)
        try:
            await asyncio.to_thread(do_backup)
        except Exception:  # noqa: BLE001
            logger.exception("自动备份失败")


def start_backup():
    global _backup_task
    if _backup_task is None or _backup_task.done():
        _backup_task = asyncio.create_task(backup_loop())
        logger.info("自动备份已启动（每 %d 小时，保留 %d 份）",
                    settings.backup_interval_hours, settings.backup_keep)


async def stop_backup():
    global _backup_task
    if _backup_task is not None:
        _backup_task.cancel()
        try:
            await _backup_task
        except asyncio.CancelledError:
            pass
        _backup_task = None
