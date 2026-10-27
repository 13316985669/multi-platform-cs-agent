"""Phase 8 安全基线：CORS 白名单 / WS 鉴权 / Mock 开关 / health-detail 鉴权 / 素材签名 URL。"""
import uuid

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import models
from app.api.contents import _material_sign
from app.config import settings
from app.core.security import create_access_token
from app.main import app


def _make_admin(db) -> models.Agent:
    from app.core.security import hash_password
    agent = models.Agent(username=f"admin_{uuid.uuid4().hex[:6]}",
                         password_hash=hash_password("x"), display_name="管理员",
                         role="admin", status="resting")
    db.add(agent)
    db.commit()
    db.refresh(agent)
    return agent


class TestMockGate:
    def test_mock_enabled_by_default(self):
        """默认开启：Mock 面板可用（开发/演示）。"""
        client = TestClient(app)
        resp = client.post("/api/mock/incoming", json={
            "platform": "douyin", "user_id": f"u_{uuid.uuid4().hex[:6]}",
            "nickname": "测试", "content": "你好", "msg_type": "text"})
        assert resp.status_code == 200

    def test_mock_disabled_returns_404(self):
        """MOCK_ENABLED=false 时端点 404（不暴露存在性）。"""
        original = settings.mock_enabled
        settings.mock_enabled = False
        try:
            client = TestClient(app)
            resp = client.post("/api/mock/incoming", json={
                "platform": "douyin", "user_id": "u1", "nickname": "n",
                "content": "c", "msg_type": "text"})
            assert resp.status_code == 404
        finally:
            settings.mock_enabled = original


class TestWsAuth:
    def test_ws_without_token_rejected(self):
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws"):
                pass
        assert exc_info.value.code == 4401

    def test_ws_with_bad_token_rejected(self):
        client = TestClient(app)
        with pytest.raises(WebSocketDisconnect) as exc_info:
            with client.websocket_connect("/ws?token=bad-token"):
                pass
        assert exc_info.value.code == 4401

    def test_ws_with_valid_token_connected(self, db):
        agent = _make_admin(db)
        token = create_access_token(agent.id, agent.username)
        client = TestClient(app)
        with client.websocket_connect(f"/ws?token={token}") as ws:
            ws.send_text("ping")  # 连接保持，不被关闭


class TestHealthDetailAuth:
    def test_unauthenticated_401(self):
        client = TestClient(app)
        assert client.get("/api/health/detail").status_code == 401

    def test_admin_ok(self, db):
        agent = _make_admin(db)
        token = create_access_token(agent.id, agent.username)
        client = TestClient(app)
        resp = client.get("/api/health/detail", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert "queue" in resp.json()

    def test_health_public_still_open(self):
        """基础健康检查保持公开（负载均衡探活用）。"""
        client = TestClient(app)
        assert client.get("/api/health").status_code == 200


class TestMaterialSign:
    def _make_material(self, db, tmp_path) -> models.Material:
        f = tmp_path / "pic.png"
        f.write_bytes(b"\x89PNG\r\n\x1a\n")
        m = models.Material(kind="image", path=str(f), mime="image/png", size=8)
        db.add(m)
        db.commit()
        db.refresh(m)
        return m

    def test_no_sign_403(self, db, tmp_path):
        m = self._make_material(db, tmp_path)
        client = TestClient(app)
        assert client.get(f"/api/materials/{m.id}/file").status_code == 403

    def test_wrong_sign_403(self, db, tmp_path):
        m = self._make_material(db, tmp_path)
        client = TestClient(app)
        assert client.get(f"/api/materials/{m.id}/file?sign=deadbeef").status_code == 403

    def test_valid_sign_200(self, db, tmp_path):
        m = self._make_material(db, tmp_path)
        client = TestClient(app)
        resp = client.get(f"/api/materials/{m.id}/file?sign={_material_sign(m.id)}")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")

    def test_list_api_returns_signed_url(self, db, tmp_path):
        """列表接口下发的 url 自带签名（前端/素材包直接可用）。"""
        m = self._make_material(db, tmp_path)
        agent = _make_admin(db)
        token = create_access_token(agent.id, agent.username)
        client = TestClient(app)
        resp = client.get("/api/materials",
                          headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        row = next(r for r in resp.json() if r["id"] == m.id)
        assert f"sign={_material_sign(m.id)}" in row["url"]


class TestCors:
    def test_no_wildcard_origin(self):
        """默认空白名单：任何 Origin 都不应拿到 Access-Control-Allow-Origin（尤其不能是 *）。"""
        client = TestClient(app)
        resp = client.get("/api/health", headers={"Origin": "https://evil.example.com"})
        assert resp.headers.get("access-control-allow-origin") in (None, "")
        resp2 = client.options("/api/health", headers={
            "Origin": "https://evil.example.com",
            "Access-Control-Request-Method": "GET"})
        assert resp2.headers.get("access-control-allow-origin") in (None, "")
