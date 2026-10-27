"""WebSocket 管理：工作台实时推送新消息/会话变更。"""
import json
import logging
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.modules import allow_ws_event, visible_modules
from ..core.security import decode_access_token
from ..database import SessionLocal
from ..models import Agent

logger = logging.getLogger(__name__)

router = APIRouter()


@dataclass
class _Client:
    ws: WebSocket
    modules: set[str] | None  # None = 接收全部事件


class ConnectionManager:
    def __init__(self):
        self.active: list[_Client] = []

    async def connect(self, ws: WebSocket, modules: set[str] | None):
        await ws.accept()
        self.active.append(_Client(ws, modules))
        logger.info("WebSocket 客户端接入，当前 %d 个", len(self.active))

    def disconnect(self, ws: WebSocket):
        self.active = [client for client in self.active if client.ws is not ws]

    async def broadcast(self, event: str, data: dict[str, Any]):
        """按账号模块发送。断开的连接自动清理。"""
        message = json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str)
        dead = []
        for client in self.active:
            if not allow_ws_event(client.modules, event):
                continue
            try:
                await client.ws.send_text(message)
            except Exception:  # noqa: BLE001
                dead.append(client.ws)
        for ws in dead:
            self.disconnect(ws)


manager = ConnectionManager()


def broadcast_sync(event: str, data: dict[str, Any]):
    """供同步上下文（队列 worker 外的 DB 操作后）安全广播。"""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(manager.broadcast(event, data))
    except RuntimeError:
        pass


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket, token: str = ""):
    """工作台实时通道。广播含私信明文，必须鉴权。

    浏览器 WebSocket 握手无法带 Authorization 头，JWT 走 ?token= 查询参数
    （与 /api/media/{id} 同一方案；生产环境务必 HTTPS，避免 token 进访问日志明文）。
    """
    payload = decode_access_token(token) if token else None
    if payload is None or "sub" not in payload:
        await websocket.close(code=4401)
        return
    db = SessionLocal()
    try:
        try:
            agent = db.get(Agent, int(payload["sub"]))
        except (TypeError, ValueError):
            agent = None
        if agent is None:
            await websocket.close(code=4401)
            return
        modules = visible_modules(agent)
    finally:
        db.close()
    await manager.connect(websocket, modules)
    try:
        while True:
            # 保持连接，客户端心跳/消息目前不处理业务
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
