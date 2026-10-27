"""平台适配器抽象基类：新增平台只需实现这三个方法。"""
from abc import ABC, abstractmethod
from typing import Any

from ..schemas import InboundMessage


class PlatformAdapter(ABC):
    platform: str = "base"

    @abstractmethod
    async def verify_webhook(self, headers: dict[str, str], body: bytes,
                             query: dict[str, str] | None = None) -> bool:
        """校验 webhook 签名/来源合法性。query 为 URL 请求参数（ark 等网关把签名放在 URL 里）。"""
        ...

    def ack_response(self) -> dict[str, Any]:
        """webhook ACK 响应体。各平台格式不同：抖音 {"code": 0}；
        小红书 ark 推送要求 {"success": true, ...}，否则平台判定失败并重推。"""
        return {"code": 0}

    @abstractmethod
    async def normalize(self, payload: dict[str, Any]) -> InboundMessage | None:
        """平台 payload → 统一入站消息。返回 None 表示该事件忽略（如自己发出的消息回执）。"""
        ...

    @abstractmethod
    async def send(self, platform_conversation_id: str, platform_user_id: str,
                   content: str, msg_type: str = "text", media_id: str = "") -> str:
        """统一消息 → 平台 API。返回平台侧消息 ID（未配置凭证时返回 mock id 并打日志）。

        media_id：RPA 通道媒体文件 ID（图片/语音）；官方 API 适配器当前仅支持文本。
        """
        ...
