"""认证接口：登录、改密码。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core.security import create_access_token, hash_password, verify_password
from ..database import get_db
from ..models import Agent
from ..schemas import AgentOut, LoginRequest, PasswordChange, TokenResponse
from .deps import get_current_agent

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login", response_model=TokenResponse)
def login(req: LoginRequest, db: Session = Depends(get_db)):
    agent = db.query(Agent).filter(Agent.username == req.username).first()
    password_ok = bool(agent is not None and verify_password(req.password, agent.password_hash))
    if agent is None or not password_ok:
        raise HTTPException(401, "账号或密码错误")
    token = create_access_token(agent.id, agent.username)
    return TokenResponse(access_token=token, agent=AgentOut.model_validate(agent))


@router.get("/me", response_model=AgentOut)
def me(agent: Agent = Depends(get_current_agent)):
    return agent


@router.post("/password")
def change_password(req: PasswordChange, agent: Agent = Depends(get_current_agent),
                    db: Session = Depends(get_db)):
    if not verify_password(req.old_password, agent.password_hash):
        raise HTTPException(400, "原密码错误")
    agent.password_hash = hash_password(req.new_password)
    db.commit()
    return {"ok": True}
