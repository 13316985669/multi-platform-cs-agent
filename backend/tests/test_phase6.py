"""Phase 6 测试：查重 / 队列排期 / 改期日历 / 首评 / 数据回采归因 / 灵感库 / 审批流。"""
import json
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
    item = models.ContentItem(title=title, topic="主题", selling_points=["卖点1"],
                              status=status)
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


def _make_post(db, account, platform="mock", post_id="") -> models.Post:
    item = _make_item(db, status="approved")
    version = _make_version(db, item, platform=platform)
    task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                              scheduled_at=datetime.utcnow(), status="success",
                              published_at=datetime.utcnow())
    db.add(task)
    db.commit()
    post = models.Post(publish_task_id=task.id, account_id=account.id,
                       platform=platform,
                       platform_post_id=post_id or f"mock_{uuid.uuid4().hex[:8]}",
                       url="https://example.com/note/1", title=version.title)
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


# ============ 6-6 SimHash 查重 ============

class TestSimHash:
    def test_identical_text_similarity_1(self):
        from app.creator.dedup import simhash, similarity
        text = "这个榨汁杯真的太好用了，便携易清洗，续航也很久"
        assert similarity(simhash(text), simhash(text)) == 1.0

    def test_different_text_low_similarity(self):
        from app.creator.dedup import simhash, similarity
        a = simhash("这个榨汁杯真的太好用了，便携易清洗")
        b = simhash("猫咪烘干箱测评：静音效果出乎意料")
        assert similarity(a, b) < 0.9

    def test_near_duplicate_high_similarity(self):
        from app.creator.dedup import simhash, similarity
        a = simhash("这个榨汁杯真的太好用了，便携易清洗，续航也很久，推荐给姐妹们")
        b = simhash("这个榨汁杯真的太好用了，便携易清洗，续航也很久，推荐给大家")
        assert similarity(a, b) > 0.85

    def test_check_duplicate_finds_similar(self, db):
        from app.creator.dedup import check_duplicate, version_text
        uniq = uuid.uuid4().hex[:8]
        item = _make_item(db)
        old = _make_version(db, item, title=f"榨汁杯种草{uniq}",
                            body=f"便携好用易清洗{uniq}，续航久")
        # 排除自身 + 用一段无关文本 → 相似度应远低于阈值
        report = check_duplicate(db, f"完全无关的内容{uniq}：猫咪行为学分析",
                                 platform="mock", exclude_version_id=old.id)
        assert report["max_similarity"] < 0.9
        # 用与 old 几乎相同的文本对比 → 命中 old（相似度 ≈1）
        report2 = check_duplicate(db, version_text(old), platform="mock",
                                  exclude_version_id=0)
        assert report2["max_similarity"] >= 0.99
        assert report2["similar_version_id"] == old.id


class TestDupSoftBlock:
    def test_publish_blocked_without_force(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            item = _make_item(db, status="approved")
            version = _make_version(db, item, platform="douyin",
                                    dup_report={"max_similarity": 0.95,
                                                "similar_version_id": 1})
            account = _make_account(db, platform="douyin")
            client = TestClient(app)
            resp = client.post("/api/publish/tasks", json={
                "content_version_id": version.id, "account_ids": [account.id]})
            assert resp.status_code == 409
            assert "相似度" in resp.json()["detail"]
            resp2 = client.post("/api/publish/tasks", json={
                "content_version_id": version.id, "account_ids": [account.id],
                "force": True})
            assert resp2.status_code == 200
        finally:
            _clear_overrides(db, agent)


# ============ 6-3 队列排期 / 改期 / 日历 ============

class TestQueueSlots:
    def test_next_free_slot_basic(self, db):
        from app.publisher.queue import next_free_slot
        acc = _make_account(db, queue_enabled=True, queue_slots=["23:59"])
        slot = next_free_slot(db, acc)
        # 北京 23:59 = UTC 15:59
        assert (slot + timedelta(hours=8)).hour == 23
        assert (slot + timedelta(hours=8)).minute == 59
        assert slot > datetime.utcnow()

    def test_occupied_slot_skipped(self, db):
        from app.publisher.queue import next_free_slot
        acc = _make_account(db, queue_enabled=True, queue_slots=["23:59"])
        slot1 = next_free_slot(db, acc)
        item = _make_item(db)
        version = _make_version(db, item)
        db.add(models.PublishTask(content_version_id=version.id, account_id=acc.id,
                                  scheduled_at=slot1, status="pending"))
        db.commit()
        slot2 = next_free_slot(db, acc)
        assert slot2 > slot1  # 同一坑位被占，推到次日同时段
        assert (slot2 - slot1) >= timedelta(hours=20)

    def test_daily_limit_pushes_next_day(self, db):
        from app.publisher.queue import next_free_slot
        acc = _make_account(db, queue_enabled=True, queue_slots=["23:59"],
                            daily_publish_limit=1)
        slot1 = next_free_slot(db, acc)
        item = _make_item(db)
        version = _make_version(db, item)
        db.add(models.PublishTask(content_version_id=version.id, account_id=acc.id,
                                  scheduled_at=slot1, status="pending"))
        db.commit()
        slot2 = next_free_slot(db, acc)
        # 日限额 1 → 次日
        assert (slot2 + timedelta(hours=8)).date() > (slot1 + timedelta(hours=8)).date()

    def test_no_slots_raises(self, db):
        from app.publisher.queue import next_free_slot
        acc = _make_account(db, queue_enabled=True, queue_slots=[])
        with pytest.raises(ValueError):
            next_free_slot(db, acc)

    def test_best_slots_default(self, db):
        from app.publisher.queue import best_slots
        acc = _make_account(db, platform="douyin")
        source, slots = best_slots(db, acc)
        assert source == "default"
        assert slots == ["12:00", "18:00", "21:00"]

    def test_best_slots_from_history(self, db):
        from app.publisher.queue import best_slots
        acc = _make_account(db, platform="douyin")
        # 造 5 条 21 点（北京）发布且高播放的作品
        for _ in range(5):
            post = _make_post(db, acc, platform="douyin")
            post.stats_json = {"play": 1000}
            task = db.get(models.PublishTask, post.publish_task_id)
            task.published_at = datetime.utcnow().replace(hour=13, minute=0)  # UTC 13 = 北京 21
        # 2 条 3 点（北京）低播放
        for _ in range(2):
            post = _make_post(db, acc, platform="douyin")
            post.stats_json = {"play": 10}
            task = db.get(models.PublishTask, post.publish_task_id)
            task.published_at = datetime.utcnow().replace(hour=19, minute=0)  # UTC 19 = 北京 3+1
        db.commit()
        source, slots = best_slots(db, acc)
        assert source == "history"
        assert slots[0] == "21:00"


class TestRescheduleCalendar:
    def test_reschedule_pending_only(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            item = _make_item(db, status="approved")
            version = _make_version(db, item)
            acc = _make_account(db)
            task = models.PublishTask(content_version_id=version.id, account_id=acc.id,
                                      scheduled_at=datetime.utcnow(), status="pending")
            db.add(task)
            db.commit()
            client = TestClient(app)
            new_time = (datetime.utcnow() + timedelta(hours=3)).isoformat()
            resp = client.post(f"/api/publish/tasks/{task.id}/reschedule",
                               json={"scheduled_at": new_time})
            assert resp.status_code == 200
            db.expire_all()
            assert abs((db.get(models.PublishTask, task.id).scheduled_at
                        - datetime.fromisoformat(new_time)).total_seconds()) < 2

            task2 = models.PublishTask(content_version_id=version.id, account_id=acc.id,
                                       scheduled_at=datetime.utcnow(), status="success")
            db.add(task2)
            db.commit()
            resp2 = client.post(f"/api/publish/tasks/{task2.id}/reschedule",
                                json={"scheduled_at": new_time})
            assert resp2.status_code == 400
        finally:
            _clear_overrides(db, agent)

    def test_calendar_groups_by_beijing_day(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            item = _make_item(db, status="approved")
            version = _make_version(db, item)
            acc = _make_account(db)
            # UTC 16:30 = 北京次日 00:30 → 应归入北京日期
            at = datetime.utcnow().replace(hour=16, minute=30) + timedelta(days=1)
            db.add(models.PublishTask(content_version_id=version.id, account_id=acc.id,
                                      scheduled_at=at, status="pending"))
            db.commit()
            client = TestClient(app)
            month = (at + timedelta(hours=8)).strftime("%Y-%m")
            resp = client.get(f"/api/publish/calendar?month={month}")
            assert resp.status_code == 200
            days = resp.json()
            expect_key = (at + timedelta(hours=8)).strftime("%Y-%m-%d")
            assert any(d["date"] == expect_key and len(d["tasks"]) >= 1 for d in days)
        finally:
            _clear_overrides(db, agent)

    def test_enqueue_occupies_slots(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            item = _make_item(db, status="approved")
            version = _make_version(db, item)
            acc = _make_account(db, queue_enabled=True, queue_slots=["23:58"])
            client = TestClient(app)
            resp = client.post("/api/publish/enqueue", json={
                "content_version_id": version.id, "account_ids": [acc.id]})
            assert resp.status_code == 200
            task = resp.json()[0]
            scheduled = datetime.fromisoformat(task["scheduled_at"])
            assert (scheduled + timedelta(hours=8)).hour == 23
            assert (scheduled + timedelta(hours=8)).minute == 58
            # 未开启队列的账号 → 400
            acc2 = _make_account(db, queue_enabled=False)
            resp2 = client.post("/api/publish/enqueue", json={
                "content_version_id": version.id, "account_ids": [acc2.id]})
            assert resp2.status_code == 400
        finally:
            _clear_overrides(db, agent)


# ============ 6-4 首评引流 ============

class TestFirstComment:
    @pytest.mark.asyncio
    async def test_first_comment_sent_and_recorded(self, db):
        from app.comments.first_comment import handle_first_comment

        acc = _make_account(db, platform="mock")
        post = _make_post(db, acc)
        version = db.get(models.ContentVersion,
                         db.get(models.PublishTask, post.publish_task_id).content_version_id)
        version.first_comment = "需要报价的宝子私信我"
        db.commit()

        await handle_first_comment({"post_id": post.id})
        db.expire_all()
        fc = db.query(models.PostComment).filter(
            models.PostComment.post_id == post.id,
            models.PostComment.author_id == "self").first()
        assert fc is not None
        assert fc.status == "replied"
        assert "报价" in fc.content

    @pytest.mark.asyncio
    async def test_first_comment_code_rendered(self, db):
        from app.comments.first_comment import handle_first_comment

        code = f"暗号{uuid.uuid4().hex[:6]}"
        db.add(models.CommentRule(platform="", intent="price", keywords=[],
                                  reply_templates=["x"], guide_code=code,
                                  enabled=True, priority=99))
        acc = _make_account(db, platform="mock")
        post = _make_post(db, acc)
        version = db.get(models.ContentVersion,
                         db.get(models.PublishTask, post.publish_task_id).content_version_id)
        version.first_comment = "私信回复【{code}】领资料"
        db.commit()

        await handle_first_comment({"post_id": post.id})
        db.expire_all()
        fc = db.query(models.PostComment).filter(
            models.PostComment.post_id == post.id,
            models.PostComment.author_id == "self").first()
        assert fc is not None
        assert code in fc.content
        assert "{code}" not in fc.content

    @pytest.mark.asyncio
    async def test_first_comment_drain_blocked(self, db):
        from app.comments.first_comment import handle_first_comment

        acc = _make_account(db, platform="mock")
        post = _make_post(db, acc)
        version = db.get(models.ContentVersion,
                         db.get(models.PublishTask, post.publish_task_id).content_version_id)
        version.first_comment = "加我微信 xxx 发你资料"  # 引流词零容忍
        db.commit()

        await handle_first_comment({"post_id": post.id})
        db.expire_all()
        fc = db.query(models.PostComment).filter(
            models.PostComment.post_id == post.id,
            models.PostComment.author_id == "self").first()
        assert fc is None  # 被出口守卫拦截

    @pytest.mark.asyncio
    async def test_first_comment_no_template_noop(self, db):
        from app.comments.first_comment import handle_first_comment

        acc = _make_account(db, platform="mock")
        post = _make_post(db, acc)
        await handle_first_comment({"post_id": post.id})
        db.expire_all()
        assert db.query(models.PostComment).filter(
            models.PostComment.post_id == post.id).count() == 0

    def test_enqueue_first_comment_delayed(self, db):
        from app.comments.first_comment import enqueue_first_comment
        from app.core.queue import _fetch_next_pending

        enqueue_first_comment(123)
        db.expire_all()
        task = db.query(models.QueueTask).filter(
            models.QueueTask.task_type == "first_comment").order_by(
            models.QueueTask.id.desc()).first()
        assert task is not None
        assert task.not_before is not None and task.not_before > datetime.utcnow()
        assert _fetch_next_pending() != task.id  # 延迟任务不可被提前消费


# ============ 6-2 数据回采 + 归因 ============

class TestStatsCollection:
    @pytest.mark.asyncio
    async def test_mock_stats_collected(self, db):
        from app.analytics.collector import collect_due_stats

        acc = _make_account(db, platform="mock")
        post = _make_post(db, acc, platform="mock")
        n = await collect_due_stats()
        assert n >= 1
        db.expire_all()
        post = db.get(models.Post, post.id)
        assert post.stats_json["play"] > 0
        assert post.stats_updated_at is not None
        snaps = db.query(models.PostStatSnapshot).filter(
            models.PostStatSnapshot.post_id == post.id).all()
        assert len(snaps) >= 1
        assert snaps[-1].play == post.stats_json["play"]

    @pytest.mark.asyncio
    async def test_rpa_collect_enqueued_and_reconciled(self, db):
        from app.analytics.collector import collect_due_stats, reconcile_stats_outbox

        acc = _make_account(db, platform="xiaohongshu", auth_type="rpa",
                            rpa_account=f"rpa_{uuid.uuid4().hex[:6]}")
        post = _make_post(db, acc, platform="xiaohongshu")
        await collect_due_stats()
        db.expire_all()
        outbox = db.query(models.RpaOutbox).filter(
            models.RpaOutbox.msg_type == "collect_stats",
            models.RpaOutbox.account == acc.rpa_account).first()
        assert outbox is not None
        assert json.loads(outbox.content)["post_id"] == post.id

        # 模拟 Worker 回执
        outbox.status = "acked"
        outbox.result = json.dumps({"play": 500, "digg": 50, "comment": 5,
                                    "share": 3, "collect": 20})
        db.commit()
        consumed = await reconcile_stats_outbox()
        assert consumed >= 1
        db.expire_all()
        assert db.get(models.Post, post.id).stats_json["play"] == 500
        assert db.get(models.RpaOutbox, outbox.id).status == "consumed"


class TestContentRoi:
    def test_roi_aggregation(self, db):
        from app.analytics.attribution import content_roi

        acc = _make_account(db, platform="mock")
        post = _make_post(db, acc, platform="mock")
        post.stats_json = {"play": 1000, "digg": 100, "comment": 10}
        version = db.get(models.ContentVersion,
                         db.get(models.PublishTask, post.publish_task_id).content_version_id)
        item_id = version.content_item_id
        db.add(models.FunnelEvent(stage="comment", platform="mock",
                                  account_id=acc.id, post_id=post.id))
        db.add(models.FunnelEvent(stage="lead", platform="mock", account_id=acc.id))
        db.commit()

        rows = content_roi(db, days=30)
        row = next(r for r in rows if r["content_item_id"] == item_id)
        assert row["play"] == 1000
        assert row["funnel"]["comment"] == 1
        assert row["funnel"]["lead"] == 1

    def test_analytics_api(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            acc = _make_account(db, platform="mock")
            post = _make_post(db, acc, platform="mock")
            post.stats_json = {"play": 42}
            db.add(models.PostStatSnapshot(post_id=post.id, play=42, digg=4))
            db.commit()
            client = TestClient(app)
            resp = client.get("/api/analytics/posts?sort=play")
            assert resp.status_code == 200
            assert any(p["id"] == post.id for p in resp.json())
            resp2 = client.get(f"/api/analytics/posts/{post.id}/trend")
            assert resp2.status_code == 200
            assert resp2.json()[-1]["play"] == 42
            resp3 = client.get("/api/analytics/contents")
            assert resp3.status_code == 200
        finally:
            _clear_overrides(db, agent)


# ============ 6-5 灵感库 ============

class TestInspiration:
    def test_create_and_list(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db)
        try:
            client = TestClient(app)
            resp = client.post("/api/inspiration", json={
                "platform": "xiaohongshu", "title": "爆款标题A",
                "source_url": "https://xhs.com/a", "content_text": "正文"})
            assert resp.status_code == 200
            item_id = resp.json()["id"]
            assert resp.json()["status"] == "new"
            listing = client.get("/api/inspiration?platform=xiaohongshu")
            assert any(i["id"] == item_id for i in listing.json())
        finally:
            _clear_overrides(db, agent)

    def test_import_dedupe(self, db, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app

        monkeypatch.setattr(settings, "rpa_api_key", "test_rpa_key")
        client = TestClient(app)
        payload = {"platform": "douyin", "keyword": "烘干箱", "items": [
            {"title": "爆款1", "source_url": "https://dy.com/1"},
            {"title": "爆款2", "source_url": "https://dy.com/2"},
        ]}
        headers = {"X-Rpa-Key": "test_rpa_key"}
        r1 = client.post("/api/inspiration/import", json=payload, headers=headers)
        assert r1.json()["created"] == 2
        r2 = client.post("/api/inspiration/import", json=payload, headers=headers)
        assert r2.json()["created"] == 0
        assert r2.json()["skipped"] == 2
        # 无 key → 401
        r3 = client.post("/api/inspiration/import", json=payload)
        assert r3.status_code == 401

    @pytest.mark.asyncio
    async def test_analyze_and_imitate(self, db, monkeypatch):
        from app.api import inspiration as insp_api
        from app.creator.contracts import InspirationAnalysis

        async def fake_llm(prompt, contract):
            assert contract is InspirationAnalysis
            return InspirationAnalysis(title_formula="数字+痛点", structure="痛点→方案",
                                       hooks=["你知道吗"], selling_angle="性价比",
                                       why_viral="真实", reusable_points=["短句"])
        monkeypatch.setattr(insp_api, "call_llm_json", fake_llm)

        item = models.InspirationItem(platform="xiaohongshu", source="manual",
                                      title="爆款标题", content_text="正文")
        db.add(item)
        db.commit()
        db.refresh(item)

        agent = models.Agent(username=f"t_{uuid.uuid4().hex[:8]}", display_name="t",
                             role="admin", status="offline", password_hash="x")
        db.add(agent)
        db.commit()

        out = await insp_api.analyze_inspiration(item.id, agent=agent, db=db)
        assert out.status == "analyzed"
        assert out.analysis["title_formula"] == "数字+痛点"

        result = insp_api.imitate_inspiration(item.id, agent=agent, db=db)
        content = db.get(models.ContentItem, result["content_item_id"])
        assert content.inspiration_id == item.id
        assert content.status == "draft"
        db.expire_all()
        assert db.get(models.InspirationItem, item.id).status == "used"

    @pytest.mark.asyncio
    async def test_generate_injects_inspiration(self, db, monkeypatch):
        """创作图：带灵感的内容生成时提示词包含爆款拆解块。"""
        from app.creator import nodes
        from app.creator.contracts import ComplianceResult, GeneratedVersion
        from app.creator.graph import run_creator_graph

        captured = {}

        async def fake_llm(prompt, contract):
            if contract is ComplianceResult:
                return ComplianceResult(passed=True, hits=[], suggestions=[])
            captured["prompt"] = prompt
            return GeneratedVersion(title="仿写标题", body="仿写正文", tags=["t"])
        monkeypatch.setattr(nodes, "call_llm_json", fake_llm)

        insp = models.InspirationItem(
            platform="xiaohongshu", source="manual", title="独特爆款标题XYZ",
            content_text="爆款正文", analysis={"title_formula": "悬念式"})
        db.add(insp)
        db.commit()
        item = _make_item(db)
        item.inspiration_id = insp.id
        db.commit()

        version = await run_creator_graph(item.id, "xiaohongshu", "note")
        assert version is not None
        assert "独特爆款标题XYZ" in captured["prompt"]
        assert "悬念式" in captured["prompt"]
        assert "严禁抄袭" in captured["prompt"]


# ============ 6-7 审批流 ============

class TestApprovalFlow:
    def test_submit_requires_passed_versions(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db, role="agent")
        try:
            client = TestClient(app)
            item = _make_item(db)
            resp = client.post(f"/api/contents/{item.id}/submit")
            assert resp.status_code == 400  # 无版本
            _make_version(db, item, compliance="pending")
            resp2 = client.post(f"/api/contents/{item.id}/submit")
            assert resp2.status_code == 400  # 版本未过合规
        finally:
            _clear_overrides(db, agent)

    def test_full_approval_cycle(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db, role="admin")
        try:
            client = TestClient(app)
            item = _make_item(db)
            _make_version(db, item, compliance="passed")

            # 提交审核
            r1 = client.post(f"/api/contents/{item.id}/submit")
            assert r1.status_code == 200
            assert r1.json()["status"] == "reviewing"

            # 未审批前不可发布
            version = item.versions[0]
            acc = _make_account(db)
            r_block = client.post("/api/publish/tasks", json={
                "content_version_id": version.id, "account_ids": [acc.id]})
            assert r_block.status_code == 400
            assert "审批" in r_block.json()["detail"]

            # 驳回必须填原因
            r2 = client.post(f"/api/contents/{item.id}/review",
                             json={"action": "reject", "note": ""})
            assert r2.status_code == 400
            r3 = client.post(f"/api/contents/{item.id}/review",
                             json={"action": "reject", "note": "标题太夸张"})
            assert r3.status_code == 200
            assert r3.json()["status"] == "draft"
            assert r3.json()["review_note"] == "标题太夸张"

            # 重新提交 → 通过
            client.post(f"/api/contents/{item.id}/submit")
            r4 = client.post(f"/api/contents/{item.id}/review",
                             json={"action": "approve", "note": "OK"})
            assert r4.status_code == 200
            assert r4.json()["status"] == "approved"
            assert r4.json()["reviewed_by"] == agent.id

            # 审批通过后可发布
            r5 = client.post("/api/publish/tasks", json={
                "content_version_id": version.id, "account_ids": [acc.id]})
            assert r5.status_code == 200

            # 审计留痕
            logs = db.query(models.AuditLog).filter(
                models.AuditLog.target == f"content:{item.id}").all()
            actions = {log.action for log in logs}
            assert "content_submit" in actions
            assert "content_review_approve" in actions
            assert "content_review_reject" in actions
        finally:
            _clear_overrides(db, agent)

    def test_non_admin_cannot_review(self, db):
        from fastapi.testclient import TestClient
        from app.main import app

        agent = _override_agent(db, role="agent")
        try:
            client = TestClient(app)
            item = _make_item(db, status="reviewing")
            resp = client.post(f"/api/contents/{item.id}/review",
                               json={"action": "approve"})
            assert resp.status_code == 403
        finally:
            _clear_overrides(db, agent)
