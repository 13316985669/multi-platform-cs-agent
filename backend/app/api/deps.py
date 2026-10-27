"""API 依赖：JWT 认证、角色校验、模块拦截。"""
import inspect

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from ..core.security import decode_access_token
from ..database import SessionLocal, get_db
from ..models import Agent

bearer = HTTPBearer(auto_error=False)


def get_current_agent(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> Agent:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    payload = decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录")
    agent = db.get(Agent, int(payload["sub"]))
    if agent is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在")
    return agent


def require_admin(agent: Agent = Depends(get_current_agent)) -> Agent:
    if agent.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "需要管理员权限")
    return agent


def agent_for_module_check(request: Request) -> Agent:
    """模块中间件用。测试里覆盖了 get_current_agent 时沿用那个账号。"""
    override = request.app.dependency_overrides.get(get_current_agent)
    if override is not None:
        agent = override()
        if inspect.isawaitable(agent):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
        if agent is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
        return agent
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录")
    payload = decode_access_token(token)
    if payload is None or "sub" not in payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录")
    try:
        agent_id = int(payload["sub"])
    except (TypeError, ValueError):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录") from None
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        if agent is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "账号不存在")
        agent.role
        agent.modules
        db.expunge(agent)
        return agent
    finally:
        db.close()
