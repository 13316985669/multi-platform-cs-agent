"""企业微信 API 客户端：access_token 缓存、渠道活码创建、欢迎语发送。

- gettoken 缓存 7000s（企微 token 有效期 7200s）
- 业务接口 errcode 非 0 抛异常；42001/40014（token 失效）刷新后重试 1 次
"""
import logging
import time

from ..config import settings
from ..core import http
from ..database import SessionLocal
from ..models import WecomChannelCode

logger = logging.getLogger(__name__)

BASE = "https://qyapi.weixin.qq.com/cgi-bin"
GET_TOKEN_URL = f"{BASE}/gettoken"
ADD_CONTACT_WAY_URL = f"{BASE}/externalcontact/add_contact_way"
SEND_WELCOME_MSG_URL = f"{BASE}/externalcontact/send_welcome_msg"

# token 失效类错误码：刷新后重试一次
_TOKEN_EXPIRED_CODES = {42001, 40014}

_token_cache: dict = {"token": "", "expires_at": 0.0}


async def get_access_token(force_refresh: bool = False) -> str:
    """获取企微 access_token（内存缓存 7000s）。"""
    now = time.time()
    if not force_refresh and _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]
    if not settings.wecom_configured:
        raise RuntimeError("企微未配置（WECOM_CORP_ID / WECOM_SECRET）")
    data = await http.get_json(GET_TOKEN_URL, params={
        "corpid": settings.wecom_corp_id, "corpsecret": settings.wecom_secret})
    if data.get("errcode") != 0:
        raise RuntimeError(f"企微 gettoken 失败: {data.get('errcode')} {data.get('errmsg')}")
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + 7000
    return _token_cache["token"]


def reset_token_cache() -> None:
    """测试用：清空 token 缓存。"""
    _token_cache["token"] = ""
    _token_cache["expires_at"] = 0.0


async def _post_with_token(url: str, body: dict) -> dict:
    """带 token 的 POST；token 失效自动刷新重试 1 次。"""
    token = await get_access_token()
    data = await http.post_json(url, params={"access_token": token}, json_body=body)
    if data.get("errcode") in _TOKEN_EXPIRED_CODES:
        token = await get_access_token(force_refresh=True)
        data = await http.post_json(url, params={"access_token": token}, json_body=body)
    return data


async def create_contact_way(name: str, state: str,
                             bound_content_id: int = 0,
                             bound_account_id: int = 0) -> WecomChannelCode:
    """创建渠道活码并落库。

    TODO 联调校准：user 列表当前取 WECOM_AGENT_USER_ID 单个成员；
    多人活码/部门活码需在企微后台确认 user/party 字段组合。
    """
    if not settings.wecom_agent_user_id:
        raise RuntimeError("未配置 WECOM_AGENT_USER_ID（活码承接成员）")
    body = {
        "type": 1,               # 单人二维码
        "scene": 2,              # 小程序/链接等渠道场景
        "skip_verify": True,     # 免验证添加
        "state": state,
        "user": [settings.wecom_agent_user_id],
        "remark": name,
    }
    data = await _post_with_token(ADD_CONTACT_WAY_URL, body)
    if data.get("errcode") != 0:
        raise RuntimeError(f"创建活码失败: {data.get('errcode')} {data.get('errmsg')}")

    db = SessionLocal()
    try:
        code = WecomChannelCode(
            name=name, config_id=data.get("config_id", ""),
            qr_url=data.get("qr_code", ""), state=state,
            bound_content_id=bound_content_id, bound_account_id=bound_account_id)
        db.add(code)
        db.commit()
        db.refresh(code)
        logger.info("企微活码已创建: name=%s state=%s config_id=%s",
                    name, state, code.config_id)
        return code
    finally:
        db.close()


async def send_welcome_msg(welcome_code: str, text: str | None = None) -> None:
    """添加好友 20 秒内发送欢迎语文本（超过 20s welcome_code 失效）。"""
    body = {
        "welcome_code": welcome_code,
        "text": {"content": text or settings.wecom_welcome_text},
    }
    data = await _post_with_token(SEND_WELCOME_MSG_URL, body)
    if data.get("errcode") != 0:
        # 欢迎语失败不阻断归因流程，仅告警（常见：超过 20s 窗口）
        logger.warning("企微欢迎语发送失败: %s %s", data.get("errcode"), data.get("errmsg"))


def save_manual_code(name: str, state: str, qr_url: str,
                     bound_content_id: int = 0, bound_account_id: int = 0) -> WecomChannelCode:
    """离线场景：企微后台手工建活码后，把 state/二维码 URL 录入系统用于归因。"""
    db = SessionLocal()
    try:
        code = WecomChannelCode(name=name, qr_url=qr_url, state=state,
                                bound_content_id=bound_content_id,
                                bound_account_id=bound_account_id)
        db.add(code)
        db.commit()
        db.refresh(code)
        return code
    finally:
        db.close()