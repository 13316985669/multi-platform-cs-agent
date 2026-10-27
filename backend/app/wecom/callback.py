"""企业微信回调：GET URL 验证 + POST 事件接收（加粉归因 + 20s 欢迎语）。

- GET：企微后台保存回调 URL 时验证，解密 echostr 原样返回明文
- POST：解密 XML → change_external_contact/add_external_contact →
  按 State 查 WecomChannelCode 归因 → FunnelEvent(wecom) + Customer.wecom_added_at
  → welcome_code 20 秒内发欢迎语
- 处理全程 try 包裹，异常也返回 200/"success"，避免企微重推风暴
"""
import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

from ..config import settings
from ..database import SessionLocal
from ..models import Customer, FunnelEvent, WecomChannelCode
from . import channel_code
from .crypto import WecomCryptoError, check_signature, decrypt_message

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wecom", tags=["wecom"])


def _require_callback_config() -> None:
    if not settings.wecom_callback_configured:
        raise HTTPException(status_code=503, detail="企微回调未配置（WECOM_TOKEN / WECOM_ENCODING_AES_KEY）")


@router.get("/callback")
async def verify_url(
    msg_signature: str = Query(...), timestamp: str = Query(...),
    nonce: str = Query(...), echostr: str = Query(...),
):
    """企微回调 URL 验证：验签 + 解 echostr，明文原样返回。"""
    _require_callback_config()
    try:
        check_signature(settings.wecom_token, timestamp, nonce, echostr, msg_signature)
        plain, _receiveid = decrypt_message(echostr)
    except WecomCryptoError as exc:
        logger.warning("企微 URL 验证失败: %s", exc)
        raise HTTPException(status_code=403, detail="签名或解密失败") from exc
    logger.info("企微回调 URL 验证通过")
    return PlainTextResponse(plain)


@router.post("/callback")
async def receive_event(
    request: Request,
    msg_signature: str = Query(...), timestamp: str = Query(...),
    nonce: str = Query(...),
):
    """接收企微事件。任何异常都返回 success，防止企微重推。"""
    _require_callback_config()
    try:
        body = (await request.body()).decode("utf-8", errors="replace")
        encrypt = _xml_text(body, "Encrypt")
        if not encrypt:
            logger.warning("企微回调缺少 Encrypt 字段: %s", body[:200])
            return PlainTextResponse("success")
        check_signature(settings.wecom_token, timestamp, nonce, encrypt, msg_signature)
        plain, _receiveid = decrypt_message(encrypt)
        await _handle_event(plain)
    except WecomCryptoError as exc:
        logger.warning("企微回调验签/解密失败: %s", exc)
    except Exception:  # noqa: BLE001
        logger.exception("企微回调处理异常")
    return PlainTextResponse("success")


def _xml_text(xml: str, tag: str) -> str:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    node = root.find(tag)
    return (node.text or "").strip() if node is not None else ""


async def _handle_event(xml: str) -> None:
    """处理解密后的事件 XML。"""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        logger.warning("企微事件 XML 解析失败: %s", xml[:200])
        return

    def text(tag: str) -> str:
        node = root.find(tag)
        return (node.text or "").strip() if node is not None else ""

    msg_type = text("MsgType")
    event = text("Event")
    change_type = text("ChangeType")
    if not (msg_type == "event" and event == "change_external_contact"
            and change_type == "add_external_contact"):
        logger.info("企微事件忽略: msg_type=%s event=%s change=%s", msg_type, event, change_type)
        return

    state = text("State")
    external_user_id = text("ExternalUserID")
    welcome_code = text("WelcomeCode")
    logger.info("企微加粉事件: state=%s external=%s", state, external_user_id)

    db = SessionLocal()
    try:
        code = db.query(WecomChannelCode).filter(
            WecomChannelCode.state == state).first() if state else None
        if code is None:
            logger.warning("加粉事件 state=%s 未匹配到活码，按未归因记录", state)

        # 客户匹配：取该活码绑定账号最近 24h 内 lead 事件的客户（暗号链路最后一步）
        # TODO 联调校准：企微 external_userid 与平台客户无直接映射，
        #   当前按「最近留资客户」近似归因；精准方案需在推送口令时下发一次性 state。
        customer_id = 0
        if code is not None:
            since = datetime.utcnow() - timedelta(hours=24)
            q = db.query(FunnelEvent).filter(
                FunnelEvent.stage == "lead", FunnelEvent.created_at >= since)
            if code.bound_account_id:
                q = q.filter(FunnelEvent.account_id == code.bound_account_id)
            lead = q.order_by(FunnelEvent.id.desc()).first()
            if lead is not None and lead.customer_id:
                customer_id = lead.customer_id
                customer = db.get(Customer, customer_id)
                if customer is not None and customer.wecom_added_at is None:
                    customer.wecom_added_at = datetime.utcnow()

        db.add(FunnelEvent(
            stage="wecom", platform="",
            account_id=code.bound_account_id if code else 0,
            post_id=0, comment_id=0, customer_id=customer_id,
            guide_code=state[:32]))
        db.commit()
    finally:
        db.close()

    # 欢迎语：welcome_code 20 秒有效，立即发送
    if welcome_code and settings.wecom_configured:
        await channel_code.send_welcome_msg(welcome_code)
