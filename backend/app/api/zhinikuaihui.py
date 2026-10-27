"""知你快回插件回复接口。协议见插件 1.7.0「自己的回复接口接入说明」。"""
import logging

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from ..integrations.zhinikuaihui import check_api_key, handle_reply

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations/zhinikuaihui", tags=["zhinikuaihui"])


@router.post("/reply")
async def zhini_reply(request: Request,
                      authorization: str = Header(default=""),
                      x_api_key: str = Header(default="")):
    """同步返回回复正文。插件在超时内等待这一次 HTTP 响应，再自己发到网页。"""
    denied = check_api_key(authorization, x_api_key)
    if denied is not None:
        return JSONResponse(status_code=denied["status"], content=denied["body"])
    try:
        body = await request.json()
    except Exception:
        logger.info("知你快回请求体不是 JSON")
        return {"action": "skip", "reason": "请求体不是 JSON"}
    if not isinstance(body, dict):
        return {"action": "skip", "reason": "请求体不是 JSON 对象"}
    return await handle_reply(body)
