"""Mock 发布通道：全流程联调用，不触碰真实平台。"""
import logging
import uuid

from sqlalchemy.orm import Session

from ...models import ContentVersion, MatrixAccount, PublishTask
from .base import PublishChannel

logger = logging.getLogger(__name__)


class MockPublishChannel(PublishChannel):
    platform = "mock"

    async def publish(self, db: Session, task: PublishTask, version: ContentVersion,
                      account: MatrixAccount) -> tuple[str, str]:
        post_id = f"mock_post_{uuid.uuid4().hex[:12]}"
        url = f"https://mock.local/{account.platform}/post/{post_id}"
        logger.info("[Mock发布] account=%s version=%s title=%s -> %s",
                    account.account_name, version.id, version.title, url)
        return post_id, url
