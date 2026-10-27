"""抖音适配器（按抖音开放平台官方文档实现）。

- 入站：解析 im_receive_msg 事件（conversation_short_id / server_message_id / message_type / user_infos）
- 出站：POST https://open.douyin.com/im/send/msg/ （scene=im_reply_msg, channel=2 智能客服）
- 凭证未配置时：verify 放行、send 降级为日志输出（便于 Mock 全流程调试）

权限现状（2026 年核实）：
- 「私信消息管理」权限新申请通道已关闭，仅存量已授权应用可用；
  新准入仅余「线索业务」路径（ISV 需服务客户线索广告日耗 ≥10 万；经营者自研需公司日耗 ≥2 万）
- 「私信消息解码」权限：7 月 6 日起私信中手机号/固话强制星号脱敏，
  需在「控制台 - 能力管理 - 能力实验室」额外申请该权限且商家重新扫码授权，
  否则 webhook 推送的留资信息为脱敏文本（如 138****5678）。
  严禁自研解密逻辑，必须走官方解码接口。

真实接入步骤见 docs/platform-integration.md。
"""
import hashlib
import hmac
import json
import logging
import time
import uuid
from typing import Any

import httpx

from ..config import settings
from ..schemas import InboundMessage
from .base import PlatformAdapter

logger = logging.getLogger(__name__)

SEND_URL = "https://open.douyin.com/im/send/msg/"


class DouyinAdapter(PlatformAdapter):
    platform = "douyin"

    async def verify_webhook(self, headers: dict[str, str], body: bytes,
                             query: dict[str, str] | None = None) -> bool:
        """签名校验：未配置 secret 时放行（开发态）；配置后按 HMAC-SHA256 校验。"""
        if not settings.douyin_client_secret:
            return True
        signature = headers.get("x-douyin-signature", "") or headers.get("X-Douyin-Signature", "")
        if not signature:
            return False
        expected = hmac.new(
            settings.douyin_client_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    async def normalize(self, payload: dict[str, Any]) -> InboundMessage | None:
        event = payload.get("event", "")
        if event != "im_receive_msg":
            # im_send_msg（自己发出的回执）、im_enter_direct_msg 等事件不入消息流
            return None
        try:
            content = json.loads(payload.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            content = payload.get("content") if isinstance(payload.get("content"), dict) else {}

        msg_type = content.get("message_type", "text")
        if msg_type not in ("text", "image", "video"):
            msg_type = "text"
        # 注意：未完成「私信消息解码」授权时，文本中的手机号/固话为脱敏格式（如 138****5678），
        # 下游 AI 留资提取只能拿到脱敏号码；完整号码需商家申请解码权限并重新授权后才有
        text = content.get("text", "") if isinstance(content.get("text"), str) else ""
        if not text and content.get("text"):
            text = str(content.get("text"))

        user_infos = content.get("user_infos") or []
        nickname, avatar = "", ""
        from_open_id = payload.get("from_user_id", "")
        for info in user_infos:
            if info.get("open_id") == from_open_id:
                nickname = info.get("nick_name", "")
                avatar = info.get("avatar", "")
                break

        return InboundMessage(
            platform="douyin",
            platform_user_id=from_open_id,
            platform_conversation_id=str(content.get("conversation_short_id", "")),
            platform_msg_id=str(content.get("server_message_id", "")),
            msg_type=msg_type,
            content=text,
            nickname=nickname,
            avatar=avatar,
            raw_payload=payload,
        )

    def is_comment_event(self, payload: dict[str, Any]) -> bool:
        """是否为评论事件（item.comment 权限推送）。"""
        # TODO: 确认实际接口地址（评论事件 webhook 的事件名，联调用真实推送校准）
        event = str(payload.get("event", ""))
        return "comment" in event.lower()

    def normalize_comment(self, payload: dict[str, Any]) -> dict | None:
        """评论事件解析为评论引擎 payload。

        TODO: 确认实际接口地址（评论事件字段名联调用真实推送校准，此处按常见结构多候选兼容）
        """
        try:
            content = json.loads(payload.get("content") or "{}")
        except (json.JSONDecodeError, TypeError):
            content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        comment_id = (content.get("comment_id") or payload.get("comment_id") or "")
        item_id = (content.get("item_id") or payload.get("item_id") or "")
        if not comment_id or not item_id:
            logger.warning("[抖音] 评论事件缺 comment_id/item_id: %s", str(payload)[:200])
            return None
        return {
            "platform": "douyin",
            "platform_post_id": str(item_id),
            "platform_comment_id": str(comment_id),
            "parent_comment_id": str(content.get("reply_to_comment_id", "") or ""),
            "author_id": str(content.get("user_id") or payload.get("from_user_id", "")),
            "author_nickname": content.get("nickname", ""),
            "content": str(content.get("content") or content.get("text") or ""),
        }

    async def send(self, platform_conversation_id: str, platform_user_id: str,
                   content: str, msg_type: str = "text", media_id: str = "") -> str:
        if msg_type != "text":
            # 官方 API 图片/语音发送未实现：降级为文本提示，避免静默丢失
            logger.warning("[抖音] 官方 API 暂不支持 %s 发送，降级文本（媒体请用 RPA 通道）", msg_type)
        if not settings.douyin_configured:
            mock_id = f"dy_out_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            logger.info("[抖音-未配置凭证,降级] 发送给 %s: %s", platform_user_id, content)
            return mock_id

        body = {
            "conversation_id": platform_conversation_id,
            "scene": "im_reply_msg",
            "channel": 2,  # 智能客服回复
            "msg_type": msg_type,
            "content": json.dumps({"text": content}, ensure_ascii=False),
        }
        headers = {"access-token": settings.douyin_access_token}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(SEND_URL, json=body, headers=headers)
            data = resp.json()
        if data.get("err_no", data.get("code", 0)) not in (0, None):
            raise RuntimeError(f"抖音发送失败: {data}")
        return str(data.get("data", {}).get("msg_id", ""))
