"""小红书 RPA 发布通道：写 rpa_outbox，由 xhs_publish_worker 拉取执行。

异步通道：publish() 只负责入队（is_async=True），调度器保持任务 publishing 状态，
Worker 回执后由 reconcile_async_tasks 落定 success/failed。
素材下发：content JSON 中的 material_ids 对应 /api/materials/{id}/file（Worker 带 X-Rpa-Key 下载）。
"""
import json
import logging

from sqlalchemy.orm import Session

from ...models import ContentVersion, MatrixAccount, PublishTask, RpaOutbox
from .base import PublishChannel

logger = logging.getLogger(__name__)


class XhsRpaPublishChannel(PublishChannel):
    platform = "xiaohongshu"
    is_async = True  # 异步通道：入队即返回，Worker 回执后落定

    async def publish(self, db: Session, task: PublishTask, version: ContentVersion,
                      account: MatrixAccount) -> tuple[str, str]:
        if not account.rpa_account:
            raise RuntimeError("小红书 RPA 账号未配置 rpa_account")
        payload = {
            "kind": "publish_note",
            "title": version.title,
            "body": version.body,
            "tags": version.tags or [],
            "material_ids": version.material_ids or [],
            "task_id": task.id,
        }
        row = RpaOutbox(
            account=account.rpa_account, platform="xiaohongshu",
            platform_conversation_id="", platform_user_id="",
            msg_type="publish_note", content=json.dumps(payload, ensure_ascii=False),
        )
        db.add(row)
        db.flush()  # 取 outbox id（同事务，随调度器 commit）
        logger.info("小红书发布入队 outbox_id=%s account=%s title=%s",
                    row.id, account.rpa_account, version.title)
        return f"outbox:{row.id}", ""
