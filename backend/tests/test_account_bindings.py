"""账号授权绑定、出站分队列、外部作品登记、开关落库。"""
import uuid
from datetime import datetime

import pytest

from app import models
from app.config import settings
from app.core.runtime_settings import load_persisted


def _agent(db, role="admin"):
    from app.api.deps import get_current_agent, require_admin
    from app.core.security import hash_password
    from app.main import app

    agent = models.Agent(
        username=f"b_{uuid.uuid4().hex[:8]}", display_name="测试",
        role=role, status="offline", password_hash=hash_password("x"),
    )
    db.add(agent)
    db.commit()
    app.dependency_overrides[get_current_agent] = lambda: agent
    if role == "admin":
        app.dependency_overrides[require_admin] = lambda: agent
    return agent


def _clear(db, agent):
    from app.main import app
    app.dependency_overrides.clear()
    db.rollback()


def _account(db, name="店A", rpa="shop-a", platform="xiaohongshu"):
    account = models.MatrixAccount(
        platform=platform, account_name=name, auth_type="rpa",
        open_id=f"pending_{uuid.uuid4().hex[:8]}", rpa_account=rpa, status="active",
    )
    db.add(account)
    db.commit()
    return account


def test_binding_conflict_and_assignment(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "rpa_api_key", "bind-key")
    agent = _agent(db)
    account = _account(db)
    client = TestClient(app)
    try:
        created = client.post("/api/account-bindings", json={
            "account_id": account.id, "duty": "dm", "driver": "xhs_ark",
            "provider": "adspower", "adspower_profile_id": "profile-1",
            "worker_id": "worker-1",
        })
        assert created.status_code == 200, created.text
        clash = client.post("/api/account-bindings", json={
            "account_id": account.id, "duty": "comment", "driver": "xhs_comment",
            "provider": "adspower", "adspower_profile_id": "profile-1",
            "worker_id": "worker-2",
        })
        assert clash.status_code == 409
        assert "各用一个环境" in clash.json()["detail"]

        headers = {"X-Rpa-Key": "bind-key"}
        assigned = client.get("/api/rpa/assignment", params={"worker_id": "worker-1"}, headers=headers)
        assert assigned.status_code == 200
        body = assigned.json()
        assert body["bound"] is True
        assert body["driver"] == "xhs_ark"
        assert body["account"] == "shop-a"
        assert body["msg_types"] == ["text", "image", "voice"]

        heart = client.post("/api/rpa/heartbeat", headers=headers, json={
            "worker_id": "worker-1", "account": "shop-a", "platform": "xiaohongshu",
            "status": "online", "meta": {"binding_id": created.json()["id"]},
        })
        assert heart.status_code == 200
        db.expire_all()
        row = db.get(models.AccountBinding, created.json()["id"])
        assert row.auth_status == "authorized"
    finally:
        _clear(db, agent)


def test_from_profile_names_the_logged_in_account(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "rpa_api_key", "bind-key")
    agent = _agent(db)
    client = TestClient(app)
    try:
        created = client.post("/api/account-bindings/from-profile", json={
            "platform": "xiaohongshu", "duty": "dm",
            "adspower_profile_id": "p9", "profile_name": "环境甲",
        })
        assert created.status_code == 200, created.text
        body = created.json()
        assert body["worker_id"].startswith("worker-")
        assert body["worker_id"] != "worker-auto"
        assert len(body["worker_id"]) == len("worker-") + 8
        assert body["rpa_account"] == "ads-p9"
        named = client.post("/api/rpa/identity/self", headers={"X-Rpa-Key": "bind-key"}, json={
            "worker_id": body["worker_id"], "profile_id": "p9", "account_name": "小红书主号",
        })
        assert named.status_code == 200, named.text
        db.expire_all()
        account = db.query(models.MatrixAccount).filter_by(rpa_account="ads-p9").one()
        assert account.account_name == "小红书主号"
    finally:
        _clear(db, agent)


def test_fleet_lists_only_enabled_bindings(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "rpa_api_key", "bind-key")
    agent = _agent(db)
    first = _account(db, name="店1", rpa="shop-fleet-1")
    second = _account(db, name="店2", rpa="shop-fleet-2")
    third = _account(db, name="店3", rpa="shop-fleet-3")
    client = TestClient(app)
    headers = {"X-Rpa-Key": "bind-key"}
    try:
        created = []
        for account, profile in ((first, "pf-1"), (second, "pf-2"), (third, "pf-3")):
            resp = client.post("/api/account-bindings", json={
                "account_id": account.id, "duty": "dm", "driver": "xhs_ark",
                "provider": "adspower", "adspower_profile_id": profile,
                "worker_id": f"worker-{profile}",
            })
            assert resp.status_code == 200, resp.text
            created.append(resp.json())
        stopped = client.put(
            f"/api/account-bindings/{created[2]['id']}",
            json={"auth_status": "disabled"},
        )
        assert stopped.status_code == 200, stopped.text
        listed = client.get("/api/rpa/fleet", headers=headers)
        assert listed.status_code == 200, listed.text
        bindings = listed.json()["bindings"]
        assert {item["worker_id"] for item in bindings} >= {"worker-pf-1", "worker-pf-2"}
        assert "worker-pf-3" not in {item["worker_id"] for item in bindings}
        assert {item["adspower_profile_id"] for item in bindings} >= {"pf-1", "pf-2"}
    finally:
        _clear(db, agent)


def test_from_profile_keeps_explicit_idle_worker(db):
    from fastapi.testclient import TestClient
    from app.main import app

    agent = _agent(db)
    db.add(models.RpaWorker(
        worker_id="worker-auto", account="", platform="idle", status="idle",
        last_heartbeat_at=datetime.utcnow(),
    ))
    db.commit()
    client = TestClient(app)
    try:
        created = client.post("/api/account-bindings/from-profile", json={
            "platform": "xiaohongshu", "duty": "dm",
            "adspower_profile_id": "p-explicit", "profile_name": "环境乙",
            "worker_id": "worker-auto",
        })
        assert created.status_code == 200, created.text
        assert created.json()["worker_id"] == "worker-auto"
    finally:
        _clear(db, agent)


def test_reopen_binding_sets_flag(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "rpa_api_key", "bind-key")
    agent = _agent(db)
    account = _account(db)
    client = TestClient(app)
    headers = {"X-Rpa-Key": "bind-key"}
    try:
        created = client.post("/api/account-bindings", json={
            "account_id": account.id, "duty": "dm", "driver": "xhs_ark",
            "provider": "adspower", "adspower_profile_id": "profile-9",
            "worker_id": "worker-reopen",
        })
        assert created.status_code == 200, created.text
        binding_id = created.json()["id"]
        resp = client.post(f"/api/account-bindings/{binding_id}/reopen")
        assert resp.status_code == 200, resp.text
        db.expire_all()
        row = db.get(models.AccountBinding, binding_id)
        assert row.reopen_requested == 1
        heart = client.post("/api/rpa/heartbeat", headers=headers, json={
            "worker_id": "worker-reopen", "account": "shop-a", "platform": "xiaohongshu",
            "status": "login_expired",
            "meta": {"binding_id": binding_id, "ads_error": "paid subscriptions"},
        })
        assert heart.status_code == 200, heart.text
        assert heart.json()["reopen"] is True
        db.expire_all()
        worker = db.query(models.RpaWorker).filter_by(worker_id="worker-reopen").one()
        assert worker.meta["ads_error"] == "paid subscriptions"
    finally:
        _clear(db, agent)


def test_outbox_msg_types_and_release(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app

    monkeypatch.setattr(settings, "rpa_api_key", "bind-key")
    client = TestClient(app)
    for msg_type, content in (("text", "你好"), ("comment_reply", "{}")):
        db.add(models.RpaOutbox(
            account="shop-a", platform="xiaohongshu", content=content, msg_type=msg_type,
        ))
    db.commit()
    headers = {"X-Rpa-Key": "bind-key"}
    pulled = client.get("/api/rpa/outbox", params={
        "worker_id": "worker-c", "account": "shop-a", "msg_types": "comment_reply",
    }, headers=headers)
    assert pulled.status_code == 200
    messages = pulled.json()["messages"]
    assert len(messages) == 1
    assert messages[0]["msg_type"] == "comment_reply"
    released = client.post("/api/rpa/release", headers=headers, json={
        "outbox_id": messages[0]["outbox_id"], "worker_id": "worker-c",
    })
    assert released.status_code == 200
    db.expire_all()
    row = db.get(models.RpaOutbox, messages[0]["outbox_id"])
    assert row.status == "pending"
    assert row.attempts == 0
    still = db.query(models.RpaOutbox).filter_by(account="shop-a", msg_type="text").one()
    assert still.status == "pending"


def test_douyin_account_type_persists_and_rewrites_dm_driver(db):
    from fastapi.testclient import TestClient
    from app.core.runtime_settings import load_persisted
    from app.main import app

    agent = _agent(db)
    account = _account(db, name="抖店", rpa="dy-shop", platform="douyin")
    client = TestClient(app)
    original = settings.douyin_account_type
    try:
        created = client.post("/api/account-bindings", json={
            "account_id": account.id, "duty": "dm", "driver": "douyin_feige",
            "provider": "adspower", "adspower_profile_id": "pf-dy",
            "worker_id": "worker-dy-type",
        })
        assert created.status_code == 200, created.text
        comment = client.post("/api/account-bindings", json={
            "account_id": account.id, "duty": "comment", "driver": "douyin_comment",
            "provider": "adspower", "adspower_profile_id": "pf-dy-c",
            "worker_id": "worker-dy-comment",
        })
        assert comment.status_code == 200, comment.text
        saved = client.put("/api/settings/douyin-account-type", json={"account_type": "enterprise"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["account_type"] == "enterprise"
        settings.douyin_account_type = "feige"
        load_persisted(db)
        assert settings.douyin_account_type == "enterprise"
        db.expire_all()
        assert db.get(models.AccountBinding, created.json()["id"]).driver == "douyin_enterprise"
        assert db.get(models.AccountBinding, comment.json()["id"]).driver == "douyin_comment"
    finally:
        settings.douyin_account_type = original
        client.put("/api/settings/douyin-account-type", json={"account_type": original})
        _clear(db, agent)


def test_douyin_laike_driver_and_rewrite(db):
    """DOUYIN_ACCOUNT_TYPE=laike：可创建 douyin_laike 绑定，切换后已有私信绑定改写为来客 driver。"""
    from fastapi.testclient import TestClient
    from app.core.runtime_settings import load_persisted
    from app.main import app

    agent = _agent(db)
    account = _account(db, name="来客商家", rpa="dy-laike", platform="douyin")
    account2 = _account(db, name="来客分店", rpa="dy-laike-2", platform="douyin")
    client = TestClient(app)
    original = settings.douyin_account_type
    try:
        created = client.post("/api/account-bindings", json={
            "account_id": account.id, "duty": "dm", "driver": "douyin_feige",
            "provider": "adspower", "adspower_profile_id": "pf-laike",
            "worker_id": "worker-dy-laike",
        })
        assert created.status_code == 200, created.text
        laike = client.post("/api/account-bindings", json={
            "account_id": account2.id, "duty": "dm", "driver": "douyin_laike",
            "provider": "adspower", "adspower_profile_id": "pf-laike-2",
            "worker_id": "worker-dy-laike-2",
        })
        assert laike.status_code == 200, laike.text  # laike driver 已注册且属于 dm 职责

        saved = client.put("/api/settings/douyin-account-type", json={"account_type": "laike"})
        assert saved.status_code == 200, saved.text
        assert saved.json()["account_type"] == "laike"
        settings.douyin_account_type = "feige"
        load_persisted(db)
        assert settings.douyin_account_type == "laike"
        db.expire_all()
        # 已有私信绑定被改写为来客 driver
        assert db.get(models.AccountBinding, created.json()["id"]).driver == "douyin_laike"
        assert db.get(models.AccountBinding, laike.json()["id"]).driver == "douyin_laike"
    finally:
        settings.douyin_account_type = original
        client.put("/api/settings/douyin-account-type", json={"account_type": original})
        _clear(db, agent)


def test_per_account_douyin_type_coexists_with_global_default(db):
    """指定了形态的账号不被全局切换改写；未指定的账号跟随全局默认。"""
    from fastapi.testclient import TestClient
    from app.main import app

    agent = _agent(db)
    pinned = _account(db, name="指定企业号", rpa="dy-pinned", platform="douyin")
    inherited = _account(db, name="跟随默认", rpa="dy-inherit", platform="douyin")
    client = TestClient(app)
    original = settings.douyin_account_type
    try:
        settings.douyin_account_type = "feige"
        typed = client.put(f"/api/accounts/{pinned.id}", json={"douyin_account_type": "enterprise"})
        assert typed.status_code == 200, typed.text
        assert typed.json()["douyin_account_type"] == "enterprise"
        pinned_bind = client.post("/api/account-bindings", json={
            "account_id": pinned.id, "duty": "dm", "driver": "douyin_enterprise",
            "provider": "adspower", "adspower_profile_id": "pf-pinned",
            "worker_id": "worker-pinned",
        })
        assert pinned_bind.status_code == 200, pinned_bind.text
        inherited_bind = client.post("/api/account-bindings", json={
            "account_id": inherited.id, "duty": "dm", "driver": "douyin_feige",
            "provider": "adspower", "adspower_profile_id": "pf-inherit",
            "worker_id": "worker-inherit",
        })
        assert inherited_bind.status_code == 200, inherited_bind.text

        saved = client.put("/api/settings/douyin-account-type", json={"account_type": "laike"})
        assert saved.status_code == 200, saved.text
        db.expire_all()
        assert db.get(models.AccountBinding, pinned_bind.json()["id"]).driver == "douyin_enterprise"
        assert db.get(models.AccountBinding, inherited_bind.json()["id"]).driver == "douyin_laike"

        changed = client.put(f"/api/accounts/{pinned.id}", json={"douyin_account_type": "laike"})
        assert changed.status_code == 200, changed.text
        db.expire_all()
        assert db.get(models.AccountBinding, pinned_bind.json()["id"]).driver == "douyin_laike"
        assert db.get(models.AccountBinding, inherited_bind.json()["id"]).driver == "douyin_laike"
    finally:
        settings.douyin_account_type = original
        client.put("/api/settings/douyin-account-type", json={"account_type": original})
        _clear(db, agent)


def test_from_profile_sets_douyin_account_type(db):
    """从环境建账号时可以一步指定来客，私信绑定直接用来客 driver。"""
    from fastapi.testclient import TestClient
    from app.main import app

    agent = _agent(db)
    client = TestClient(app)
    original = settings.douyin_account_type
    try:
        settings.douyin_account_type = "feige"
        created = client.post("/api/account-bindings/from-profile", json={
            "platform": "douyin", "duty": "dm", "adspower_profile_id": "pf-step",
            "profile_name": "来客环境", "douyin_account_type": "laike",
        })
        assert created.status_code == 200, created.text
        assert created.json()["driver"] == "douyin_laike"
        account = db.query(models.MatrixAccount).filter_by(rpa_account="ads-pf-step").one()
        assert account.douyin_account_type == "laike"
    finally:
        settings.douyin_account_type = original
        _clear(db, agent)


def test_switch_persists(db):
    from fastapi.testclient import TestClient
    from app.api.deps import get_current_agent, require_admin
    from app.main import app

    agent = _agent(db)
    client = TestClient(app)
    original = settings.comment_auto_reply_enabled
    try:
        resp = client.put("/api/settings/auto-switches", json={"comment_auto_reply_enabled": True})
        assert resp.status_code == 200
        settings.comment_auto_reply_enabled = False
        load_persisted(db)
        assert settings.comment_auto_reply_enabled is True
    finally:
        client.put("/api/settings/auto-switches", json={"comment_auto_reply_enabled": original})
        app.dependency_overrides.pop(get_current_agent, None)
        app.dependency_overrides.pop(require_admin, None)


@pytest.mark.asyncio
async def test_external_comment_registers_post(db, monkeypatch):
    monkeypatch.setattr(settings, "comment_auto_reply_enabled", False)
    account = _account(db, rpa=f"ext-{uuid.uuid4().hex[:6]}")
    from app.comments.engine import handle_inbound_comment

    url = f"https://www.xiaohongshu.com/explore/{uuid.uuid4().hex[:8]}"
    await handle_inbound_comment({
        "platform": "xiaohongshu",
        "platform_comment_id": f"c-{uuid.uuid4().hex[:8]}",
        "account": account.rpa_account,
        "post_url": url,
        "content": "多少钱",
    })
    db.expire_all()
    post = db.query(models.Post).filter(models.Post.url == url).one()
    task = db.get(models.PublishTask, post.publish_task_id)
    assert task.status == "external"
    listed = db.query(models.ContentItem).filter(models.ContentItem.topic == "外部登记").count()
    assert listed >= 1


@pytest.mark.asyncio
async def test_comment_reply_uses_knowledge_and_survives_retrieve_failure(monkeypatch):
    from app.comments.engine import _generate_reply
    from app.rag.pipeline import RetrievalResult

    async def hit(_content, top_k=2, **kwargs):
        return RetrievalResult(passed=True, contexts=[{"content": "标准款 99 元"}])

    seen = {}

    async def fake_llm(prompt, contract):
        seen["prompt"] = prompt
        return contract(reply="私信我发你价格")

    monkeypatch.setattr("app.rag.pipeline.retrieve", hit)
    monkeypatch.setattr("app.comments.engine.call_llm_json", fake_llm)
    assert await _generate_reply("多少钱", "price") == "私信我发你价格"
    assert "99 元" in seen["prompt"]

    async def boom(*_args, **_kwargs):
        raise RuntimeError("检索不可用")

    monkeypatch.setattr("app.rag.pipeline.retrieve", boom)

    async def short(_prompt, contract):
        return contract(reply="私信我")

    monkeypatch.setattr("app.comments.engine.call_llm_json", short)
    assert await _generate_reply("在吗", "consult") == "私信我"
