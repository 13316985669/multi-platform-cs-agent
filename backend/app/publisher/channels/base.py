"""发布通道抽象：新增平台发布能力只需实现 PublishChannel 并注册到 channels/__init__.py。"""
from abc import ABC, abstractmethod

from sqlalchemy.orm import Session

from ...models import ContentVersion, MatrixAccount, PublishTask


class PublishChannel(ABC):
    platform: str = "base"

    @abstractmethod
    async def publish(self, db: Session, task: PublishTask, version: ContentVersion,
                      account: MatrixAccount) -> tuple[str, str]:
        """执行发布。返回 (platform_post_id, post_url)。失败抛异常（调度器负责重试）。"""
        ...
