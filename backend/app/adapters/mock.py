"""Mock 通道：内置模拟平台，无需真实资质即可跑通全流程。

- 入站：POST /api/mock/incoming 直接构造 InboundMessage
- 出站：仅记录日志，消息已存库，工作台可见
"""
import logging
import time
import uuid
from typing import Any

from ..schemas import InboundMessage
from .base import PlatformAdapter

logger = logging.getLogger(__name__)


class MockAdapter(PlatformAdapter):
    platform = "mock"

    async def verify_webhook(self, headers: dict[str, str], body: bytes,
                             query: dict[str, str] | None = None) -> bool:
        return True

    async def normalize(self, payload: dict[str, Any]) -> InboundMessage | None:
        return InboundMessage(
            platform=payload.get("platform", "mock"),
            platform_user_id=payload.get("user_id", "mock_user_001"),
            platform_conversation_id=payload.get("conversation_id", ""),
            platform_msg_id=payload.get("msg_id", f"mock_{uuid.uuid4().hex[:16]}"),
            msg_type=payload.get("msg_type", "text"),
            content=payload.get("content", ""),
            nickname=payload.get("nickname", "模拟用户"),
            avatar=payload.get("avatar", ""),
            raw_payload=payload,
        )

    async def send(self, platform_conversation_id: str, platform_user_id: str,
                   content: str, msg_type: str = "text", media_id: str = "") -> str:
        mock_id = f"mock_out_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
        logger.info("[Mock 通道] 发送给 %s: [%s] %s", platform_user_id, msg_type, content)
        return mock_id
