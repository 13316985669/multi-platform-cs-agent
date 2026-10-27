"""Webhook 接收层：验签 → 入队 → 立即 ACK（秒级响应，慢任务异步处理）。

ACK 格式按平台差异化（adapter.ack_response()）：
- 抖音等：{"code": 0}
- 小红书 ark 推送：{"success": true, "error_code": 0, "error_msg": ""}，否则平台判定失败并重推
"""
import logging

from fastapi import APIRouter, HTTPException, Request

from ..adapters import get_adapter
from ..config import settings
from ..core.queue import enqueue
from ..schemas import MockIncoming

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


@router.post("/webhooks/{platform}")
async def receive_webhook(platform: str, request: Request):
    """平台 webhook 统一入口。只做轻量处理，立即返回 200。"""
    body = await request.body()
    adapter = get_adapter(platform)
    ack = adapter.ack_response()

    if not await adapter.verify_webhook(dict(request.headers), body,
                                        dict(request.query_params)):
        logger.warning("webhook 验签失败 platform=%s", platform)
        # 仍返回 200，避免平台反复重推错误签名请求
        return ack

    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        return ack

    if isinstance(payload, list):
        # ark 消息推送为数组信封 [{msgTag, sellerId, data}]：逐条入队，逐条 normalize
        for item in payload:
            if isinstance(item, dict):
                item["platform"] = platform
                enqueue("inbound_message", item)
        return ack

    if isinstance(payload, dict):
        payload["platform"] = platform
        # 事件分流：抖音评论事件进评论引擎（item.comment），私信消息走进站消息流
        if platform == "douyin" and hasattr(adapter, "is_comment_event") \
                and adapter.is_comment_event(payload):
            comment = adapter.normalize_comment(payload)
            if comment:
                enqueue("inbound_comment", comment)
            return ack
        enqueue("inbound_message", payload)
    return ack


@router.post("/api/mock/incoming")
async def mock_incoming(req: MockIncoming):
    """Mock 通道：模拟平台用户发消息（前端模拟面板用）。

    生产环境必须设置 MOCK_ENABLED=false 关闭——该端点无鉴权，开着任何人都能伪造入站消息。
    """
    if not settings.mock_enabled:
        raise HTTPException(404, "Not Found")
    import uuid
    payload = {
        "channel": "mock",  # 通道标识：强制走 MockAdapter 解析，platform 仅作展示用平台标识
        "platform": req.platform,
        "user_id": req.user_id,
        "nickname": req.nickname,
        "content": req.content,
        "msg_type": req.msg_type,
        "msg_id": f"mock_{uuid.uuid4().hex[:16]}",
        "conversation_id": f"mock_conv_{req.user_id}",
    }
    enqueue("inbound_message", payload)
    return {"ok": True}
