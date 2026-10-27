"""账号可见模块：空配置保持全开，勾选后按页面实际请求放行。"""
import uuid

from fastapi.testclient import TestClient

from app.core.modules import allow_ws_event, allowed_modules
from app.core.security import create_access_token, hash_password
from app.main import app
from app.models import Agent


def _agent(db, *, role="agent", modules=None, username=None) -> Agent:
    agent = Agent(
        username=username or f"mod_{uuid.uuid4().hex[:8]}",
        password_hash=hash_password("secret"),
        display_name="测试",
        role=role,
        status="active",
        modules=modules,
    )
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


def _client(agent: Agent) -> TestClient:
    token = create_access_token(agent.id, agent.username)
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_allowed_modules_keeps_page_dependencies():
    assert allowed_modules("GET", "/api/quick-replies") == ["workbench", "settings"]
    assert allowed_modules("POST", "/api/quick-replies") == ["settings"]
    assert allowed_modules("POST", "/api/knowledge/recall-test") == ["workbench", "knowledge"]
    assert allowed_modules("GET", "/api/knowledge/banned-words") == ["settings"]
    assert allowed_modules("GET", "/api/knowledge/docs") == ["knowledge"]
    assert allowed_modules("GET", "/api/accounts") == ["accounts", "publish"]
    assert allowed_modules("POST", "/api/accounts") == ["accounts"]
    assert allowed_modules("GET", "/api/contents/publishable") == ["studio", "publish"]
    assert allowed_modules("GET", "/api/accounts/oauth/callback") is None
    assert allowed_modules("GET", "/api/materials/3/file") is None
    assert allowed_modules("POST", "/api/rpa/incoming") is None
    assert allowed_modules("GET", "/api/rpa/replies") == ["accounts"]
    assert allowed_modules("POST", "/api/agents/me/status") is None
    assert allowed_modules("GET", "/api/conversations") == ["workbench", "mock"]
    assert allowed_modules("POST", "/api/conversations/1/reply") == ["workbench"]


def test_unconfigured_agent_keeps_full_access(db):
    agent = _agent(db, modules=None)
    resp = _client(agent).get("/api/knowledge/docs")
    assert resp.status_code == 200


def test_workbench_only_can_use_chat_side_actions(db, conversation):
    agent = _agent(db, modules=["workbench"])
    client = _client(agent)
    assert client.get("/api/quick-replies").status_code == 200
    assert client.get("/api/agents").status_code == 200
    assert client.post("/api/knowledge/recall-test", json={"query": "多少钱"}).status_code == 200
    created = client.post("/api/tickets", json={
        "conversation_id": conversation.id, "type": "other", "title": "要发票", "content": "",
    })
    assert created.status_code == 200
    assert client.get(f"/api/tickets/by-conversation/{conversation.id}").status_code == 200
    uploaded = client.post("/api/media", files={"file": ("a.png", b"png", "image/png")})
    assert uploaded.status_code == 200
    assert client.get("/api/tickets").status_code == 403
    assert client.post("/api/knowledge/faq", json={
        "title": "t", "question": "q", "answer": "a",
    }).status_code == 403
    assert client.post("/api/quick-replies", json={"title": "t", "content": "c"}).status_code == 403


def test_publish_only_can_load_schedule_form(db):
    agent = _agent(db, modules=["publish"])
    client = _client(agent)
    assert client.get("/api/accounts").status_code == 200
    assert client.get("/api/contents/publishable").status_code == 200
    assert client.get("/api/publish/tasks").status_code == 200
    assert client.get("/api/knowledge/docs").status_code == 403
    assert client.get("/api/conversations").status_code == 403
    assert client.post("/api/accounts", json={"platform": "douyin", "account_name": "x"}).status_code == 403


def test_settings_only_reaches_banned_words_not_docs(db):
    agent = _agent(db, modules=["settings"])
    client = _client(agent)
    assert client.get("/api/knowledge/banned-words").status_code == 200
    created = client.post("/api/quick-replies", json={"title": "你好", "content": "在的"})
    assert created.status_code == 200
    assert client.get("/api/knowledge/docs").status_code == 403
    assert client.get("/api/rpa/workers").status_code == 200


def test_admin_ignores_stored_modules(db):
    agent = _agent(db, role="admin", modules=["workbench"])
    assert _client(agent).get("/api/knowledge/docs").status_code == 200


def test_create_agent_modules_rules(db):
    admin = _agent(db, role="admin", username=f"adm_{uuid.uuid4().hex[:6]}")
    client = _client(admin)
    missing = client.post("/api/agents", json={
        "username": f"n_{uuid.uuid4().hex[:6]}", "password": "secret", "display_name": "新客服",
    })
    assert missing.status_code == 200
    assert missing.json()["modules"] == ["workbench", "knowledge", "tickets", "leads", "dashboard"]

    empty = client.post("/api/agents", json={
        "username": f"e_{uuid.uuid4().hex[:6]}", "password": "secret",
        "display_name": "空", "modules": [],
    })
    assert empty.status_code == 400

    unknown = client.post("/api/agents", json={
        "username": f"u_{uuid.uuid4().hex[:6]}", "password": "secret",
        "display_name": "坏", "modules": ["not-a-module"],
    })
    assert unknown.status_code == 400

    own = client.patch(f"/api/agents/{admin.id}", json={"role": "agent"})
    assert own.status_code == 400


def test_rename_does_not_lock_unconfigured_agent(db):
    admin = _agent(db, role="admin")
    target = _agent(db, modules=None)
    resp = _client(admin).patch(f"/api/agents/{target.id}", json={"display_name": "改名"})
    assert resp.status_code == 200
    assert resp.json()["modules"] is None


def test_media_query_token_requires_workbench(db):
    blocked = _agent(db, modules=["publish"])
    allowed = _agent(db, modules=["workbench"])
    client = TestClient(app)
    denied = client.get(f"/api/media/missing?token={create_access_token(blocked.id, blocked.username)}")
    assert denied.status_code == 403
    missing = client.get(f"/api/media/missing?token={create_access_token(allowed.id, allowed.username)}")
    assert missing.status_code == 404


def test_signed_material_and_oauth_stay_outside_module_gate():
    client = TestClient(app)
    material = client.get("/api/materials/1/file")
    assert material.status_code == 403
    assert material.json()["detail"] == "签名无效"
    oauth = client.get("/api/accounts/oauth/callback")
    assert oauth.status_code != 401


def test_ws_event_filter():
    assert allow_ws_event(None, "new_message") is True
    assert allow_ws_event({"publish"}, "new_message") is False
    assert allow_ws_event({"publish"}, "publish_task_updated") is True
    assert allow_ws_event({"workbench"}, "ticket_created") is True
    assert allow_ws_event({"studio"}, "unknown_event") is False


def test_ws_connection_records_modules_and_filters(db):
    publish = _agent(db, modules=["publish"])
    client = TestClient(app)
    token = create_access_token(publish.id, publish.username)
    from app.api.ws import ConnectionManager, _Client, manager

    with client.websocket_connect(f"/ws?token={token}") as ws:
        matched = [item for item in manager.active if item.modules == {"publish"}]
        assert matched
        ws.send_text("ping")

    class _FakeWS:
        def __init__(self):
            self.sent: list[str] = []

        async def send_text(self, message: str):
            self.sent.append(message)

    local = ConnectionManager()
    pub = _FakeWS()
    chat = _FakeWS()
    local.active = [_Client(pub, {"publish"}), _Client(chat, {"workbench"})]  # type: ignore[list-item]

    import asyncio
    asyncio.run(local.broadcast("new_message", {"content": "secret"}))
    asyncio.run(local.broadcast("publish_task_updated", {"task_id": 9, "status": "success"}))
    assert pub.sent and "publish_task_updated" in pub.sent[0]
    assert "secret" not in "".join(pub.sent)
    assert chat.sent and "secret" in chat.sent[0]
    assert "publish_task_updated" not in "".join(chat.sent)
