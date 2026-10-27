"""客服管理：账号 CRUD、在线状态切换。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..core.modules import CS_MODULE_KEYS, ModuleSelectionError, normalize_modules
from ..core.security import hash_password
from ..database import get_db
from ..models import Agent
from ..schemas import AgentCreate, AgentOut, AgentStatusUpdate, AgentUpdate
from .deps import get_current_agent, require_admin

router = APIRouter(prefix="/api/agents", tags=["agents"])


def _modules_for_role(role: str, modules: list[str] | None, *, creating: bool) -> list[str] | None:
    """管理员不落限制。新建客服未传模块时默认客服组。"""
    if role == "admin":
        return None
    if modules is None and creating:
        return list(CS_MODULE_KEYS)
    if modules is None:
        return None
    try:
        return normalize_modules(modules)
    except ModuleSelectionError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.get("", response_model=list[AgentOut])
def list_agents(agent: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    return [AgentOut.model_validate(a) for a in db.query(Agent).order_by(Agent.id).all()]


@router.post("", response_model=AgentOut)
def create_agent(req: AgentCreate, admin: Agent = Depends(require_admin),
                 db: Session = Depends(get_db)):
    if db.query(Agent).filter(Agent.username == req.username).first():
        raise HTTPException(400, "账号已存在")
    role = req.role if req.role in ("admin", "agent") else "agent"
    agent = Agent(
        username=req.username,
        password_hash=hash_password(req.password),
        display_name=req.display_name,
        role=role,
        max_concurrent=req.max_concurrent if req.max_concurrent is not None else 20,
        modules=_modules_for_role(role, req.modules, creating=True),
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


@router.post("/me/status", response_model=AgentOut)
def update_my_status(req: AgentStatusUpdate, agent: Agent = Depends(get_current_agent),
                     db: Session = Depends(get_db)):
    agent.status = req.status
    db.commit()
    return agent


@router.patch("/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: int, req: AgentUpdate, admin: Agent = Depends(require_admin),
                 db: Session = Depends(get_db)):
    target = db.get(Agent, agent_id)
    if target is None:
        raise HTTPException(404, "客服不存在")
    if req.max_concurrent is not None:
        if req.max_concurrent < 0:
            raise HTTPException(400, "接待上限不能为负数")
        target.max_concurrent = req.max_concurrent
    if req.display_name is not None:
        name = req.display_name.strip()
        if not name:
            raise HTTPException(400, "姓名不能为空")
        target.display_name = name
    previous_role = target.role
    if req.role is not None:
        if target.id == admin.id and req.role != admin.role:
            raise HTTPException(400, "不能修改自己的角色")
        target.role = req.role
    if target.role == "admin":
        target.modules = None
    elif "modules" in req.model_fields_set:
        if req.modules is None:
            raise HTTPException(400, "至少选择一个模块")
        target.modules = _modules_for_role(target.role, req.modules, creating=False)
    elif previous_role == "admin" and target.role == "agent":
        # 从管理员降为客服且没带勾选时，落到客服组，避免变成不限制。
        target.modules = list(CS_MODULE_KEYS)
    if req.password is not None:
        password = req.password.strip()
        if not password:
            raise HTTPException(400, "密码不能为空")
        target.password_hash = hash_password(password)
    db.commit()
    db.refresh(target)
    return target


@router.delete("/{agent_id}")
def delete_agent(agent_id: int, admin: Agent = Depends(require_admin),
                 db: Session = Depends(get_db)):
    target = db.get(Agent, agent_id)
    if target is None:
        raise HTTPException(404, "客服不存在")
    if target.id == admin.id:
        raise HTTPException(400, "不能删除自己")
    db.delete(target)
    db.commit()
    return {"ok": True}
