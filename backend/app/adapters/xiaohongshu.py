"""小红书适配器（按小红书商业开放平台 / ark 网关文档实现）。

- 鉴权：ark 网关签名（appId + sign + timestamp + method），OAuth token 自动刷新并落库
- 入站：ark 消息推送回调解析（信封 [{msgTag, sellerId, data}]，data 为 JSON 字符串；
  同时兼容扁平结构，便于联调期与 Mock 面板调试）
- 出站：私信发送封装；token 失效时自动刷新并重试一次
- Token 生命周期：platform_tokens 表持久化，重启不丢失；refreshToken 过期需重新授权
  （用 scripts/xhs_oauth.py exchange --code <新code> --write-env）
- 凭证未配置时降级为日志输出

契约校准说明（拿到开放平台权限后按真实文档核对，改动都收敛在本文件）：
- METHOD_SEND_MESSAGE：发送 method 名为占位值「im.sendMessage」，以真实文档为准
- _sign / verify_webhook：签名算法按 ark 网关公开规则实现（参数排序 + 首尾拼 secret 取 MD5），
  webhook 验签优先校验 URL query 中的 sign（ark 推送把签名放在 URL 参数里，body 不参与签名）
- normalize：ark 推送的 msgTag / data 字段名以真实回调为准，字段提取已做多候选键兼容

真实接入步骤见 docs/platform-integration.md。
"""
import asyncio
import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

import httpx

from ..config import settings
from ..schemas import InboundMessage
from .base import PlatformAdapter

logger = logging.getLogger(__name__)

GATEWAY_URL = "https://ark.xiaohongshu.com/ark/open_api/v3/common_controller"

# ark 网关 method 名（以开放平台后台真实文档为准，如有出入只需改这里）
METHOD_GET_ACCESS_TOKEN = "oauth.getAccessToken"
METHOD_REFRESH_TOKEN = "oauth.refreshToken"
METHOD_SEND_MESSAGE = "im.sendMessage"  # 占位名，待真实文档校准

# 发送路径的 token 刷新余量：剩余有效期不足 5 分钟时先刷新再发
SEND_REFRESH_MARGIN_SECONDS = 300
# 定时任务的刷新余量：官方规则为「剩余 >30 分钟时刷新不返回新 token」，
# 因此周期任务用 35 分钟余量，避免无意义调用
PERIODIC_REFRESH_MARGIN_SECONDS = 35 * 60

# token 失效错误码（ark 常见鉴权错误码，以真实返回为准，可补充）
_TOKEN_INVALID_CODES = {"10001", "10002", "10003", "401"}


class XhsSendError(RuntimeError):
    """小红书发送失败（非 token 类错误）。"""


class XhsTokenError(XhsSendError):
    """小红书 token 失效（可刷新重试）。"""


def _looks_like_im_event(msg_tag: str) -> bool:
    """ark 推送的 msgTag 是否为私信消息事件（订单/售后等业务推送不入客服消息流）。"""
    tag = msg_tag.lower()
    return any(k in tag for k in ("im", "msg", "message", "private", "chat"))


def _looks_like_comment_event(msg_tag: str) -> bool:
    """ark 推送的 msgTag 是否为评论事件。

    TODO: 确认实际接口地址（聚光评论推送 msgTag 值，探测阶段先打日志观察真实回调，
    校准后在 normalize 中启用评论分流；当前统一返回 None 仅记录）
    """
    tag = msg_tag.lower()
    return any(k in tag for k in ("comment", "note_comment", "reply"))


def _is_token_error(code: Any, msg: Any) -> bool:
    if str(code) in _TOKEN_INVALID_CODES:
        return True
    text = str(msg).lower()
    return "token" in text or "授权" in text or "access" in text


def _try_json(value: str) -> Any:
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _parse_expire(value: Any) -> datetime | None:
    """平台返回的 expiresAt 时间戳（秒或毫秒）→ UTC datetime。解析失败返回 None。"""
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return None
    if ts > 10_000_000_000:  # 毫秒时间戳
        ts //= 1000
    try:
        return datetime.utcfromtimestamp(ts)
    except (OverflowError, OSError, ValueError):
        return None


class XiaohongshuAdapter(PlatformAdapter):
    platform = "xiaohongshu"

    def __init__(self):
        self._token_lock = asyncio.Lock()

    # ---------- 鉴权 ----------

    def _sign(self, params: dict[str, Any]) -> str:
        """ark 网关签名：除 sign 外参数按 key 字母序拼接 k1v1k2v2...，首尾拼 appSecret 后取 MD5。

        以官方「sign 签名算法」文档为准，若有出入只需改本方法。
        """
        sorted_str = "".join(f"{k}{params[k]}" for k in sorted(params))
        return hashlib.md5(
            (settings.xhs_app_secret + sorted_str + settings.xhs_app_secret).encode()
        ).hexdigest()

    async def _call_gateway(self, method: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        """ark 网关统一调用：系统参数 + 业务参数 + 签名，POST JSON。"""
        params: dict[str, Any] = {
            "appId": settings.xhs_app_id,
            "version": "2.0",
            "timestamp": str(int(time.time() * 1000)),
            "method": method,
            **(extra or {}),
        }
        params["sign"] = self._sign(params)
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(GATEWAY_URL, json=params)
            return resp.json()

    # ---------- Token 生命周期（持久化 + 自动刷新） ----------

    def _load_stored_token(self):
        from ..database import SessionLocal
        from ..models import PlatformToken

        db = SessionLocal()
        try:
            row = (
                db.query(PlatformToken)
                .filter(PlatformToken.platform == self.platform)
                .first()
            )
            if row is not None:
                # 取出标量，避免 session 关闭后访问延迟属性
                return (row.access_token or "", row.refresh_token or "",
                        row.access_expires_at, row.refresh_expires_at)
            return None
        finally:
            db.close()

    def _store_token(self, access_token: str, refresh_token: str,
                     access_expires_at: datetime | None,
                     refresh_expires_at: datetime | None):
        from ..database import SessionLocal
        from ..models import PlatformToken

        db = SessionLocal()
        try:
            row = (
                db.query(PlatformToken)
                .filter(PlatformToken.platform == self.platform)
                .first()
            )
            if row is None:
                row = PlatformToken(platform=self.platform)
                db.add(row)
            row.access_token = access_token
            row.refresh_token = refresh_token
            row.access_expires_at = access_expires_at
            row.refresh_expires_at = refresh_expires_at
            db.commit()
        finally:
            db.close()
        logger.info("[小红书] token 已落库（access 过期时间: %s）", access_expires_at)

    def _current_tokens(self) -> tuple[str, str, datetime | None]:
        """当前可用 token：优先读库（含历次刷新结果），回退 .env 注入值。"""
        stored = self._load_stored_token()
        access = (stored[0] if stored else "") or settings.xhs_access_token
        refresh = (stored[1] if stored else "") or settings.xhs_refresh_token
        expires_at = stored[2] if stored else None
        return access, refresh, expires_at

    async def _refresh_and_store(self, refresh_token: str) -> str:
        data = await self._call_gateway(METHOD_REFRESH_TOKEN, {"refreshToken": refresh_token})
        payload = data.get("data") or {}
        access = payload.get("accessToken") or payload.get("access_token") or ""
        if not access:
            raise XhsTokenError(f"小红书 token 刷新失败: {data}")
        new_refresh = payload.get("refreshToken") or payload.get("refresh_token") or refresh_token
        access_exp = _parse_expire(
            payload.get("accessTokenExpiresAt") or payload.get("access_token_expires_at"))
        refresh_exp = _parse_expire(
            payload.get("refreshTokenExpiresAt") or payload.get("refresh_token_expires_at"))
        if refresh_exp and refresh_exp < datetime.utcnow():
            logger.warning("[小红书] refreshToken 已过期，需重新授权（scripts/xhs_oauth.py exchange）")
        self._store_token(access, new_refresh, access_exp, refresh_exp)
        return access

    async def get_valid_access_token(self) -> str:
        """获取可用 accessToken：剩余有效期不足发送余量时自动刷新（带锁防并发刷新）。"""
        async with self._token_lock:
            access, refresh, expires_at = self._current_tokens()
            if access and (expires_at is None
                           or datetime.utcnow() < expires_at - timedelta(seconds=SEND_REFRESH_MARGIN_SECONDS)):
                return access
            if not refresh:
                if access:
                    # 无 refresh token：只能继续用现有 token，失效时由发送路径报错
                    return access
                raise XhsTokenError("小红书未配置 access/refresh token")
            return await self._refresh_and_store(refresh)

    async def force_refresh(self) -> str:
        """强制刷新（token 失效错误码触发，或脚本手动刷新）。"""
        async with self._token_lock:
            _, refresh, _ = self._current_tokens()
            if not refresh:
                raise XhsTokenError("无 refresh_token，无法刷新，请重新授权")
            return await self._refresh_and_store(refresh)

    async def maybe_refresh(self, margin_seconds: int = PERIODIC_REFRESH_MARGIN_SECONDS) -> bool:
        """周期任务用：剩余有效期不足 margin 时刷新。返回是否发生了刷新。

        官方规则：剩余 >30 分钟时刷新为 no-op（返回原 token），故默认余量取 35 分钟。
        """
        if not settings.xhs_app_id:
            return False
        async with self._token_lock:
            access, refresh, expires_at = self._current_tokens()
            if not refresh:
                return False
            need = (not access) or (
                expires_at is not None
                and datetime.utcnow() >= expires_at - timedelta(seconds=margin_seconds))
            if not need:
                return False
            await self._refresh_and_store(refresh)
            return True

    # ---------- 适配器接口 ----------

    def ack_response(self) -> dict[str, Any]:
        """ark 消息推送要求的 ACK 格式（非此格式平台会判定失败并重推）。"""
        return {"success": True, "error_code": 0, "error_msg": ""}

    async def verify_webhook(self, headers: dict[str, str], body: bytes,
                             query: dict[str, str] | None = None) -> bool:
        """ark 推送验签：URL 请求参数（除 sign）按字母排序用 & 连接，首尾拼 appSecret 取 MD5，
        与 query 中的 sign 比对；请求 body 不参与签名（以官方文档为准）。

        未配置 secret 时放行（开发态）；兼容签名放 header 的旧网关形态。
        """
        if not settings.xhs_app_secret:
            return True
        query = query or {}
        sign = query.get("sign", "")
        if sign:
            joined = "&".join(f"{k}={query[k]}" for k in sorted(query) if k != "sign")
            expected = hashlib.md5(
                (settings.xhs_app_secret + joined + settings.xhs_app_secret).encode()
            ).hexdigest()
            return hmac.compare_digest(expected, sign)
        header_sign = headers.get("x-xhs-signature", "") or headers.get("X-Xhs-Signature", "")
        if header_sign:
            expected = hashlib.md5(body + settings.xhs_app_secret.encode()).hexdigest()
            return hmac.compare_digest(expected, header_sign)
        return False

    async def normalize(self, payload: dict[str, Any]) -> InboundMessage | None:
        """消息回调解析：ark 信封 [{msgTag, sellerId, data}]（webhooks 层已逐条拆包），
        兼容扁平结构。字段名以真实回调为准，此处做多候选键兼容。"""
        # 平台推送地址检测包 / 测试包
        if payload.get("test") is True:
            return None

        msg_tag = str(payload.get("msgTag") or payload.get("msg_tag") or "")
        data: dict[str, Any] = payload
        if "data" in payload and ("msgTag" in payload or "msg_tag" in payload or "sellerId" in payload):
            raw = payload.get("data")
            parsed = _try_json(raw) if isinstance(raw, str) else raw
            if not isinstance(parsed, dict):
                logger.warning("[小红书] 回调 data 非 JSON 对象，忽略: %s", str(raw)[:200])
                return None
            data = parsed

        # 评论事件探测：聚光评论推送 msgTag 待联调校准，先打日志观察真实回调（不入私信流）
        if msg_tag and _looks_like_comment_event(msg_tag):
            logger.info("[小红书] 探测到疑似评论推送 msgTag=%s keys=%s（校准后启用评论分流）",
                        msg_tag, sorted(data.keys()))
            return None

        # 非私信类业务推送（订单/售后等）不入客服消息流
        if msg_tag and not _looks_like_im_event(msg_tag):
            logger.info("[小红书] 忽略非私信推送 msgTag=%s", msg_tag)
            return None

        sender = data.get("sender") or {}
        user_id = (sender.get("user_id") or sender.get("userId")
                   or data.get("from_user_id") or data.get("fromUserId")
                   or data.get("userId") or data.get("user_id") or "")
        if not user_id:
            logger.info("[小红书] 回调缺少发送者 ID，忽略 msgTag=%s keys=%s",
                        msg_tag, sorted(data.keys()))
            return None

        msg_type = str(
            data.get("msg_type") or data.get("msgType") or data.get("messageType") or "text"
        ).lower()
        if msg_type not in ("text", "image", "video"):
            msg_type = "text"

        content = data.get("content", "")
        if isinstance(content, str):
            parsed_content = _try_json(content)
            if isinstance(parsed_content, dict):
                content = parsed_content.get("text") or json.dumps(parsed_content, ensure_ascii=False)
        elif isinstance(content, dict):
            content = content.get("text") or json.dumps(content, ensure_ascii=False)
        else:
            content = str(content)

        return InboundMessage(
            platform="xiaohongshu",
            platform_user_id=str(user_id),
            platform_conversation_id=str(
                data.get("conversation_id") or data.get("conversationId")
                or data.get("sessionId") or ""),
            platform_msg_id=str(
                data.get("msg_id") or data.get("msgId") or data.get("messageId") or ""),
            msg_type=msg_type,
            content=content,
            nickname=sender.get("nickname") or sender.get("nickName") or data.get("nickname") or "",
            avatar=sender.get("avatar") or data.get("avatar") or "",
            raw_payload=payload,
        )

    async def send(self, platform_conversation_id: str, platform_user_id: str,
                   content: str, msg_type: str = "text", media_id: str = "") -> str:
        if msg_type != "text":
            logger.warning("[小红书] 官方 API 暂不支持 %s 发送，降级文本（媒体请用 RPA 通道）", msg_type)
        access, refresh, _ = self._current_tokens()
        if not settings.xhs_app_id or not (access or refresh):
            mock_id = f"xhs_out_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
            logger.info("[小红书-未配置凭证,降级] 发送给 %s: %s", platform_user_id, content)
            return mock_id

        token = await self.get_valid_access_token()
        try:
            return await self._send_once(token, platform_conversation_id, content, msg_type)
        except XhsTokenError:
            # token 失效：强制刷新后重试一次，仍失败则抛出（队列/调用方负责重试与告警）
            logger.warning("[小红书] token 失效，强制刷新后重试")
            token = await self.force_refresh()
            return await self._send_once(token, platform_conversation_id, content, msg_type)

    async def _send_once(self, token: str, conversation_id: str,
                         content: str, msg_type: str) -> str:
        data = await self._call_gateway(METHOD_SEND_MESSAGE, {
            "accessToken": token,
            "conversationId": conversation_id,
            "msgType": msg_type,
            "content": content,
        })
        failed = data.get("success") is False or data.get("error_code") not in (None, 0, "0")
        if failed:
            code, msg = data.get("error_code"), data.get("error_msg", "")
            if _is_token_error(code, msg):
                raise XhsTokenError(f"token 失效: {data}")
            raise XhsSendError(f"小红书发送失败: {data}")
        payload = data.get("data") or {}
        return str(payload.get("msgId") or payload.get("msg_id") or "")
