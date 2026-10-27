"""Phase 7 测试：账号健康度 / 一稿多版 / 标题助手 / 账号报表与画像 / 关键词埋词 / 批量导入 / 品牌语气。"""
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from app.config import settings

# ============ 公共辅助 ============

def _make_account(db, platform="mock", auth_type="api", **kw) -> models.MatrixAccount:
    # open_id 默认随机：(platform, open_id) 有唯一索引，空串会跨测试撞唯一约束
    acc = models.MatrixAccount(
        platform=platform, account_name=f"acc_{uuid.uuid4().hex[:6]}",
        auth_type=auth_type, open_id=kw.pop("open_id", f"oid_{uuid.uuid4().hex[:10]}"),
        rpa_account=kw.pop("rpa_account", ""), status="active", **kw)
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


def _make_item(db, status="draft", title="测试选题") -> models.ContentItem:
    item = models.ContentItem(title=f"{title}_{uuid.uuid4().hex[:6]}", topic="主题",
                              selling_points=["卖点1"], status=status)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_version(db, item, platform="mock", compliance="passed",
                  title="标题", body="正文内容", **kw) -> models.ContentVersion:
    v = models.ContentVersion(content_item_id=item.id, platform=platform,
                              content_type="note", title=title, body=body,
                              compliance_status=compliance, **kw)
    db.add(v)
    db.commit()
    db.refresh(v)
    return v


def _make_post(db, account, platform="mock", play=100) -> models.Post:
    item = _make_item(db, status="approved")
    version = _make_version(db, item, platform=platform)
    task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                              scheduled_at=datetime.utcnow(), status="success",
                              published_at=datetime.utcnow())
    db.add(task)
    db.commit()
    post = models.Post(publish_task_id=task.id, account_id=account.id,
                       platform=platform,
                       platform_post_id=f"mock_{uuid.uuid4().hex[:8]}",
                       url="https://example.com/note/1", title=version.title,
                       stats_json={"play": play, "digg": play // 10, "comment": play // 50})
    db.add(post)
    db.commit()
    db.refresh(post)
    return post


def _override_agent(db, role="admin"):
    from app.api.deps import get_current_agent, require_admin
    from app.core.security import hash_password
    from app.main import app

    agent = models.Agent(username=f"t_{uuid.uuid4().hex[:8]}", display_name="测试",
                         role=role, status="offline", password_hash=hash_password("x"))
    db.add(agent)
    db.commit()
    app.dependency_overrides[get_current_agent] = lambda: agent
    if role == "admin":
        app.dependency_overrides[require_admin] = lambda: agent
    return agent


def _clear_overrides(db, agent):
    from app.main import app
    app.dependency_overrides.clear()
    try:
        db.delete(agent)
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()


def _encrypt_creds(saved_days_ago: int) -> str:
    from app.core import crypto
    return crypto.encrypt_json({
        "access_token": "at", "refresh_token": "rt", "expires_in": 86400,
        "saved_at": (datetime.utcnow() - timedelta(days=saved_days_ago)).isoformat(),
    })


# ============ 7-1 账号健康度 ============

class TestAccountHealth:
    def test_healthy_api_account(self, db):
        from app.core.account_health import compute_health
        acc = _make_account(db, credentials_enc=_encrypt_creds(1))
        result = compute_health(db, acc)
        assert result["level"] == "good"
        assert result["score"] == 100
        assert result["issues"] == []
        assert result["token_expires_at"] is not None

    def test_status_expired_is_bad(self, db):
        from app.core.account_health import compute_health
        acc = _make_account(db, credentials_enc=_encrypt_creds(1))
        acc.status = "expired"
        db.commit()
        result = compute_health(db, acc)
        assert result["level"] == "bad"
        assert any(i["code"] == "status_expired" for i in result["issues"])

    def test_token_age_warn_and_bad(self, db):
        """refresh_token 年龄口径：≥23 天 warn，≥30 天 bad（access_token 自动刷新不参与）。"""
        from app.core.account_health import compute_health
        acc_warn = _make_account(db, credentials_enc=_encrypt_creds(25))
        result = compute_health(db, acc_warn)
        assert result["level"] == "warn"
        assert result["issues"][0]["code"] == "token_expiring"
        assert "天后过期" in result["issues"][0]["message"]

        acc_bad = _make_account(db, credentials_enc=_encrypt_creds(31))
        result2 = compute_health(db, acc_bad)
        assert result2["level"] == "bad"
        assert any(i["code"] == "token_expired" for i in result2["issues"])

    def test_no_credentials_warn(self, db):
        from app.core.account_health import compute_health
        acc = _make_account(db)  # 无 credentials_enc
        result = compute_health(db, acc)
        assert result["level"] == "warn"
        assert any(i["code"] == "token_missing" for i in result["issues"])

    def test_rpa_worker_signals(self, db):
        from app.core.account_health import compute_health
        # 无 Worker → bad
        acc = _make_account(db, auth_type="rpa",
                            rpa_account=f"w_{uuid.uuid4().hex[:6]}")
        result = compute_health(db, acc)
        assert result["level"] == "bad"
        assert any(i["code"] == "worker_missing" for i in result["issues"])

        # 在线 Worker → 无 worker 问题
        acc2 = _make_account(db, auth_type="rpa",
                             rpa_account=f"w_{uuid.uuid4().hex[:6]}")
        db.add(models.RpaWorker(worker_id=f"wid_{uuid.uuid4().hex[:6]}",
                                account=acc2.rpa_account, platform=acc2.platform,
                                status="online"))
        db.commit()
        result2 = compute_health(db, acc2)
        assert result2["worker_status"] == "online"
        assert not any(i["code"].startswith("worker_") for i in result2["issues"])

        # 登录过期 Worker → warn
        acc3 = _make_account(db, auth_type="rpa",
                             rpa_account=f"w_{uuid.uuid4().hex[:6]}")
        db.add(models.RpaWorker(worker_id=f"wid_{uuid.uuid4().hex[:6]}",
                                account=acc3.rpa_account, platform=acc3.platform,
                                status="login_expired"))
        db.commit()
        result3 = compute_health(db, acc3)
        assert result3["level"] == "warn"
        assert any(i["code"] == "worker_login_expired" for i in result3["issues"])

    def test_publish_rate_low_warn(self, db):
        from app.core.account_health import compute_health
        acc = _make_account(db, credentials_enc=_encrypt_creds(1))
        item = _make_item(db)
        version = _make_version(db, item)
        # 1 成功 + 3 失败 = 25% 成功率（≥3 样本 → 预警）
        for status in ("success", "failed", "failed", "failed"):
            db.add(models.PublishTask(content_version_id=version.id, account_id=acc.id,
                                      scheduled_at=datetime.utcnow(), status=status))
        db.commit()
        result = compute_health(db, acc)
        assert any(i["code"] == "publish_rate_low" for i in result["issues"])
        assert result["score"] < 100

    def test_health_api(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            acc = _make_account(db, credentials_enc=_encrypt_creds(25))
            client = TestClient(app)
            resp = client.get("/api/accounts/health")
            assert resp.status_code == 200
            data = resp.json()
            entry = data[str(acc.id)]
            assert entry["level"] == "warn"
            assert entry["score"] < 100
            assert entry["issues"][0]["code"] == "token_expiring"
        finally:
            _clear_overrides(db, agent)

    def test_alert_dedup_per_day(self, db):
        """同一账号同一问题当天只告一次。"""
        from app.core import account_health
        account_health._alerted.clear()
        _make_account(db, credentials_enc=_encrypt_creds(25))
        first = account_health.check_all_and_alert()
        second = account_health.check_all_and_alert()
        assert first >= 1
        assert second == 0
        account_health._alerted.clear()


# ============ 7-2 一稿多版 + 标题助手 ============

class TestVariants:
    def test_generate_enqueues_variants(self, db):
        """variants=2 → 每平台入队 2 个任务，payload 带 variant_index 0/1。"""
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            item = _make_item(db)
            client = TestClient(app)
            resp = client.post(f"/api/contents/{item.id}/generate",
                               json={"platforms": ["xiaohongshu"], "content_type": "note",
                                     "variants": 2})
            assert resp.status_code == 200
            assert resp.json()["queued"] == 2
            tasks = db.query(models.QueueTask).filter(
                models.QueueTask.task_type == "content_generate",
            ).order_by(models.QueueTask.id.desc()).limit(2).all()
            indexes = sorted(t.payload["variant_index"] for t in tasks)
            assert indexes == [0, 1]
            assert all(t.payload["item_id"] == item.id for t in tasks)
        finally:
            _clear_overrides(db, agent)

    def test_generate_variants_bounds(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            item = _make_item(db)
            client = TestClient(app)
            resp = client.post(f"/api/contents/{item.id}/generate",
                               json={"platforms": ["douyin"], "variants": 5})
            assert resp.status_code == 422  # variants 上限 3
        finally:
            _clear_overrides(db, agent)

    @pytest.mark.asyncio
    async def test_handler_passes_variant_index(self, db, monkeypatch):
        """队列处理器透传 variant_index（缺省 0）。"""
        import app.creator.graph as graph_mod
        from app import services
        called = {}

        async def fake_run(item_id, platform, content_type, variant_index=0):
            called["variant_index"] = variant_index
            return None

        monkeypatch.setattr(graph_mod, "run_creator_graph", fake_run)
        await services.handle_content_generate(
            {"item_id": 1, "platform": "mock", "content_type": "note", "variant_index": 2})
        assert called["variant_index"] == 2
        await services.handle_content_generate(
            {"item_id": 1, "platform": "mock", "content_type": "note"})
        assert called["variant_index"] == 0

    @pytest.mark.asyncio
    async def test_persist_writes_variant_no(self, db):
        from app.creator.nodes import persist_node
        item = _make_item(db)
        uniq = uuid.uuid4().hex[:8]
        result = await persist_node({
            "item_id": item.id, "platform": "mock", "content_type": "note",
            "draft": {"title": f"标题{uniq}", "body": f"正文{uniq}", "tags": [],
                      "script": "", "cover_text": ""},
            "compliance_report": {"passed": True, "hits": [], "suggestions": []},
            "variant_index": 2,
        })
        version = db.get(models.ContentVersion, result["version_id"])
        assert version.variant_no == 3

    def test_variant_style_block(self):
        from app.creator.nodes import VARIANT_STYLES, _render_variant_block
        assert len(VARIANT_STYLES) == 3
        assert "痛点提问风" in _render_variant_block(1)
        assert "干货清单风" in _render_variant_block(2)
        assert "真实测评风" in _render_variant_block(0)
        # 越界索引收敛到最后一档
        assert "干货清单风" in _render_variant_block(9)

    def test_same_item_variants_not_duplicate(self, db):
        """同选题的多变体是刻意备选，不互相判重。"""
        from app.creator.dedup import check_duplicate, version_text
        uniq = uuid.uuid4().hex[:8]
        item = _make_item(db)
        v1 = _make_version(db, item, title=f"同题变体{uniq}",
                           body=f"几乎相同的正文{uniq}，便携好用")
        _make_version(db, item, title=f"同题变体{uniq}",
                      body=f"几乎相同的正文{uniq}，便携好用")
        siblings = db.query(models.ContentVersion).filter(
            models.ContentVersion.content_item_id == item.id,
            models.ContentVersion.id != v1.id).all()
        assert siblings  # 兄弟变体存在
        # 不排除时能找到兄弟（相似度≈1）
        report2 = check_duplicate(db, version_text(v1), platform="mock",
                                  exclude_version_id=v1.id)
        assert report2["similar_version_id"] in [s.id for s in siblings]
        # 排除同 item 后，similar_version_id 一定不是兄弟
        report3 = check_duplicate(db, version_text(v1), platform="mock",
                                  exclude_version_id=v1.id, exclude_item_id=item.id)
        assert report3["similar_version_id"] not in [s.id for s in siblings]


class TestTitleAssistant:
    def test_contract_parse(self):
        from app.creator.contracts import TitleSuggestions
        result = TitleSuggestions.model_validate({"titles": [
            {"text": "这个榨汁杯我能吹一年", "formula": "身份共鸣", "score": 88},
        ]})
        assert result.titles[0].score == 88

    def test_endpoint_503_without_llm(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        assert not settings.llm_configured
        agent = _override_agent(db)
        try:
            item = _make_item(db)
            version = _make_version(db, item)
            client = TestClient(app)
            resp = client.post(f"/api/contents/versions/{version.id}/titles")
            assert resp.status_code == 503
        finally:
            _clear_overrides(db, agent)

    def test_endpoint_with_mocked_llm(self, db, monkeypatch):
        from fastapi.testclient import TestClient
        import app.creator.llm as llm_mod
        from app.creator.contracts import TitleSuggestions
        from app.main import app

        async def fake_llm(prompt, contract):
            return TitleSuggestions.model_validate({"titles": [
                {"text": f"标题{i}", "formula": "数字清单", "score": 80 + i}
                for i in range(10)
            ]})

        monkeypatch.setattr(llm_mod, "call_llm_json", fake_llm)
        agent = _override_agent(db)
        try:
            item = _make_item(db)
            version = _make_version(db, item)
            client = TestClient(app)
            resp = client.post(f"/api/contents/versions/{version.id}/titles")
            assert resp.status_code == 200
            titles = resp.json()["titles"]
            assert len(titles) == 10
            assert titles[0]["formula"] == "数字清单"
        finally:
            _clear_overrides(db, agent)


# ============ 7-3 账号效果报表 + 画像回采 ============

class TestAccountReport:
    def test_report_aggregation(self, db):
        from app.analytics.attribution import account_report
        acc = _make_account(db, credentials_enc=_encrypt_creds(1))
        post = _make_post(db, acc, play=1000)
        # 漏斗事件：lead 2 次 + wecom 1 次（账号维度）
        for _ in range(2):
            db.add(models.FunnelEvent(stage="lead", platform=acc.platform,
                                      account_id=acc.id, post_id=post.id))
        db.add(models.FunnelEvent(stage="wecom", platform=acc.platform,
                                  account_id=acc.id, post_id=post.id))
        db.commit()
        rows = account_report(db, days=30)
        row = next(r for r in rows if r["account_id"] == acc.id)
        assert row["posts"] >= 1
        assert row["play"] >= 1000
        assert row["funnel"]["lead"] >= 2
        assert row["funnel"]["wecom"] >= 1
        assert row["health_level"] in ("good", "warn", "bad")
        assert row["publish_success_rate"] is not None

    def test_report_api(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            acc = _make_account(db)
            client = TestClient(app)
            resp = client.get("/api/analytics/accounts?days=30")
            assert resp.status_code == 200
            rows = resp.json()
            assert any(r["account_id"] == acc.id for r in rows)
        finally:
            _clear_overrides(db, agent)

    @pytest.mark.asyncio
    async def test_profile_backfill_mock(self, db):
        """Mock 通道画像回采：确定性合成粉丝/作品/获赞并落库。"""
        from app.analytics.collector import refresh_account_profiles
        acc = _make_account(db, platform="mock")
        processed = await refresh_account_profiles()
        assert processed >= 1
        db.refresh(acc)
        assert acc.profile_json["followers"] > 0
        assert acc.profile_json["works"] > 0
        assert "updated_at" in acc.profile_json

    @pytest.mark.asyncio
    async def test_profile_reconcile_from_outbox(self, db):
        """RPA collect_account 回执对账：acked → profile_json 落库 + consumed。"""
        import json

        from app.analytics.collector import reconcile_stats_outbox
        acc = _make_account(db, auth_type="rpa", rpa_account=f"w_{uuid.uuid4().hex[:6]}")
        db.add(models.RpaOutbox(
            account=acc.rpa_account, platform=acc.platform, msg_type="collect_account",
            content=json.dumps({"account_id": acc.id}),
            result=json.dumps({"followers": 1234, "works": 56, "liked": 7890}),
            status="acked"))
        db.commit()
        consumed = await reconcile_stats_outbox()
        assert consumed >= 1
        db.refresh(acc)
        assert acc.profile_json["followers"] == 1234
        row = db.query(models.RpaOutbox).filter(
            models.RpaOutbox.account == acc.rpa_account,
            models.RpaOutbox.msg_type == "collect_account").first()
        assert row.status == "consumed"

    def test_account_out_includes_profile(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            acc = _make_account(db)
            acc.profile_json = {"followers": 42, "works": 3, "liked": 99,
                                "updated_at": datetime.utcnow().isoformat()}
            db.commit()
            client = TestClient(app)
            resp = client.get("/api/accounts")
            assert resp.status_code == 200
            row = next(a for a in resp.json() if a["id"] == acc.id)
            assert row["profile"]["followers"] == 42
        finally:
            _clear_overrides(db, agent)


# ============ 发布弹窗可选内容接口（publishable） ============

class TestPublishable:
    def test_only_approved_and_passed(self, db):
        """只返回「审批通过 + 合规通过」的版本，draft/reviewing/failed 均排除。"""
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            uniq = uuid.uuid4().hex[:6]
            # 可发布：approved + passed（2 个变体）
            item_ok = _make_item(db, status="approved", title=f"可发布{uniq}")
            v1 = _make_version(db, item_ok, title=f"过审版本A{uniq}", variant_no=1)
            v2 = _make_version(db, item_ok, title=f"过审版本B{uniq}", variant_no=2)
            # 排除 1：draft + passed（未审批）
            item_draft = _make_item(db, status="draft", title=f"草稿{uniq}")
            v_draft = _make_version(db, item_draft, title=f"草稿版本{uniq}")
            # 排除 2：approved + failed（合规未过）
            item_fail = _make_item(db, status="approved", title=f"违规{uniq}")
            v_fail = _make_version(db, item_fail, compliance="failed",
                                   title=f"违规版本{uniq}")
            # 排除 3：reviewing + passed（审批中）
            item_review = _make_item(db, status="reviewing", title=f"待审{uniq}")
            v_review = _make_version(db, item_review, title=f"待审版本{uniq}")

            client = TestClient(app)
            resp = client.get("/api/contents/publishable")
            assert resp.status_code == 200
            version_ids = {r["version_id"] for r in resp.json()}
            assert {v1.id, v2.id} <= version_ids
            assert v_draft.id not in version_ids
            assert v_fail.id not in version_ids
            assert v_review.id not in version_ids
            # 字段透出：item_title / variant_no / platform
            row = next(r for r in resp.json() if r["version_id"] == v2.id)
            assert row["item_title"] == item_ok.title
            assert row["variant_no"] == 2
            assert row["platform"] == "mock"
        finally:
            _clear_overrides(db, agent)

    def test_route_not_shadowed_by_item_id(self, db):
        """/contents/publishable 不应被 /contents/{item_id} 路由吞掉（422 即说明被吞）。"""
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            client = TestClient(app)
            resp = client.get("/api/contents/publishable")
            assert resp.status_code == 200
            assert isinstance(resp.json(), list)
        finally:
            _clear_overrides(db, agent)


# ============ 7-4 关键词埋词 / 批量导入 / 品牌语气 ============

class TestKeywords:
    def test_contract_parse(self):
        from app.creator.contracts import KeywordSuggestions
        result = KeywordSuggestions.model_validate({"keywords": [
            {"word": "便携榨汁杯", "heat": "高"},
        ]})
        assert result.keywords[0].word == "便携榨汁杯"

    def test_keywords_coverage(self, db, monkeypatch):
        """覆盖检测：标题/正文/标签包含该词 → covered=True。"""
        from fastapi.testclient import TestClient
        import app.creator.llm as llm_mod
        from app.creator.contracts import KeywordSuggestions
        from app.main import app

        uniq = uuid.uuid4().hex[:6]

        async def fake_llm(prompt, contract):
            return KeywordSuggestions.model_validate({"keywords": [
                {"word": f"便携{uniq}", "heat": "高"},     # 标题含 → 已覆盖
                {"word": f"不相关词{uniq}", "heat": "低"},   # 不含 → 未覆盖
            ]})

        monkeypatch.setattr(llm_mod, "call_llm_json", fake_llm)
        agent = _override_agent(db)
        try:
            item = _make_item(db)
            version = _make_version(db, item, title=f"便携{uniq}好物",
                                    body=f"正文内容{uniq}")
            client = TestClient(app)
            resp = client.post(f"/api/contents/versions/{version.id}/keywords")
            assert resp.status_code == 200
            kws = {k["word"]: k for k in resp.json()["keywords"]}
            assert kws[f"便携{uniq}"]["covered"] is True
            assert kws[f"不相关词{uniq}"]["covered"] is False
        finally:
            _clear_overrides(db, agent)


class TestAccountImport:
    def test_import_mixed_lines(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            uniq = uuid.uuid4().hex[:6]
            lines = [
                f"douyin,导入主号{uniq},api,,分组A",                      # 正常 API
                f"xiaohongshu,导入小号{uniq},rpa,w_{uniq},分组A",          # 正常 RPA
                f"weibo,错误平台{uniq},api,,",                             # 坏平台
                f"douyin,,api,,",                                          # 缺名称
                f"xiaohongshu,缺Worker{uniq},rpa,,",                       # RPA 缺标识
                f"douyin,导入主号{uniq},api,,分组A",                       # 重复
            ]
            client = TestClient(app)
            resp = client.post("/api/accounts/import", json={"lines": lines})
            assert resp.status_code == 200
            data = resp.json()
            assert data["imported"] == 2
            assert len(data["failed"]) == 4
            reasons = " ".join(f["error"] for f in data["failed"])
            assert "平台" in reasons and "名称" in reasons and "Worker" in reasons and "已存在" in reasons
            # 落库验证
            acc = db.query(models.MatrixAccount).filter(
                models.MatrixAccount.account_name == f"导入小号{uniq}").first()
            assert acc is not None and acc.rpa_account == f"w_{uniq}"
            assert acc.group_name == "分组A"
        finally:
            _clear_overrides(db, agent)

    def test_import_requires_admin(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db, role="agent")
        try:
            client = TestClient(app)
            resp = client.post("/api/accounts/import", json={"lines": ["douyin,x"]})
            assert resp.status_code == 403
        finally:
            _clear_overrides(db, agent)


class TestBrandStyle:
    def test_settings_api_and_prompt_injection(self, db):
        from fastapi.testclient import TestClient
        from app.creator.nodes import _render_brand_style_block, _render_creator_prompt
        from app.main import app

        agent = _override_agent(db)
        original = settings.brand_style_guide
        try:
            client = TestClient(app)
            guide = f"温柔专业，自称小编{uuid.uuid4().hex[:4]}"
            resp = client.put("/api/settings/brand-style",
                              json={"brand_style_guide": guide})
            assert resp.status_code == 200
            assert resp.json()["brand_style_guide"] == guide
            assert settings.brand_style_guide == guide

            # 提示词注入验证
            assert guide in _render_brand_style_block()
            prompt = _render_creator_prompt({
                "platform": "xiaohongshu", "content_type": "note",
                "topic": "测试", "selling_points": [], "variant_index": 0,
            })
            assert guide in prompt

            resp2 = client.get("/api/settings/brand-style")
            assert resp2.json()["brand_style_guide"] == guide
        finally:
            settings.brand_style_guide = original
            _clear_overrides(db, agent)

    def test_empty_brand_style_fallback(self):
        from app.creator.nodes import _render_brand_style_block
        original = settings.brand_style_guide
        try:
            settings.brand_style_guide = ""
            assert "未配置品牌语气" in _render_brand_style_block()
        finally:
            settings.brand_style_guide = original

    def test_brand_style_requires_admin(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db, role="agent")
        try:
            client = TestClient(app)
            resp = client.put("/api/settings/brand-style",
                              json={"brand_style_guide": "x"})
            assert resp.status_code == 403
        finally:
            _clear_overrides(db, agent)
