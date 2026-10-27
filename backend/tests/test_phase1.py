"""Phase 1 测试：凭证加密 / 账号矩阵 / 创作图（mock LLM）/ Mock 发布全链路。"""
import uuid
from datetime import datetime, timedelta

import pytest

from app import models
from app.config import settings
from app.core import crypto


def _make_item(db, title="测试内容", status="draft"):
    item = models.ContentItem(title=title, topic="便携榨汁杯种草",
                              selling_points=["便携", "易清洗"], status=status)
    db.add(item)
    db.commit()
    return item


def _make_account(db, platform="mock", auth_type="api", limit=5):
    acc = models.MatrixAccount(
        platform=platform, account_name=f"测试账号_{uuid.uuid4().hex[:6]}",
        auth_type=auth_type, open_id=f"openid_{uuid.uuid4().hex[:8]}",
        daily_publish_limit=limit, status="active")
    db.add(acc)
    db.commit()
    return acc


def _make_version(db, item, platform="mock", compliance="passed"):
    v = models.ContentVersion(
        content_item_id=item.id, platform=platform, content_type="note",
        title="标题", body="正文", tags=["好物"], compliance_status=compliance)
    db.add(v)
    db.commit()
    return v


class TestCrypto:
    def test_roundtrip(self):
        token = crypto.encrypt_json({"access_token": "abc123", "expires_in": 7200})
        assert token != "abc123"
        assert crypto.decrypt_json(token) == {"access_token": "abc123", "expires_in": 7200}

    def test_decrypt_failure_returns_empty(self):
        assert crypto.decrypt_json("not-a-valid-token") == {}
        assert crypto.decrypt_json("") == {}


class TestAccountsApi:
    def test_crud_and_credentials_hidden(self, db):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.api.deps import get_current_agent, require_admin

        from app.core.security import hash_password
        # status=offline：避免被 auto_assign 当作在线客服分配会话（污染其他测试）
        admin = models.Agent(username="t_admin", display_name="管理员", role="admin",
                             status="offline", password_hash=hash_password("x"))
        db.add(admin)
        db.commit()

        app.dependency_overrides[get_current_agent] = lambda: admin
        app.dependency_overrides[require_admin] = lambda: admin
        try:
            client = TestClient(app)
            # 创建 RPA 账号
            r = client.post("/api/accounts", json={
                "platform": "xiaohongshu", "account_name": "小红书主号",
                "auth_type": "rpa", "rpa_account": "xhs_main"})
            assert r.status_code == 200, r.text
            acc_id = r.json()["id"]

            # 列表不泄露凭证字段
            r = client.get("/api/accounts")
            assert r.status_code == 200
            row = next(a for a in r.json() if a["id"] == acc_id)
            assert "credentials_enc" not in row
            assert row["has_credentials"] is False
            assert row["rpa_account"] == "xhs_main"

            # 更新额度/分组
            r = client.put(f"/api/accounts/{acc_id}",
                           json={"daily_publish_limit": 3, "group_name": "美妆线"})
            assert r.status_code == 200
            assert r.json()["daily_publish_limit"] == 3

            # RPA 账号缺 rpa_account 拒绝创建
            r = client.post("/api/accounts", json={
                "platform": "douyin", "account_name": "坏账号", "auth_type": "rpa"})
            assert r.status_code == 400

            # 删除
            r = client.delete(f"/api/accounts/{acc_id}")
            assert r.status_code == 200
            assert db.get(models.MatrixAccount, acc_id) is None
        finally:
            app.dependency_overrides.clear()
            # 清理测试客服，避免影响其他用例的 auto_assign 行为
            db.delete(admin)
            db.commit()


class TestCreatorGraph:
    """创作图全流程（mock LLM 客户端，patch 节点内的 call_llm_json）。"""

    @staticmethod
    def _gen(platform="xiaohongshu"):
        from app.creator.contracts import GeneratedVersion
        return GeneratedVersion(title="这个榨汁杯真的绝", body="姐妹们冲", tags=["好物"],
                                script="", cover_text="便携神器")

    @pytest.mark.asyncio
    async def test_pass_path(self, db, monkeypatch):
        """一次通过：generate → compliance(passed) → persist。"""
        from app.creator import nodes
        from app.creator.contracts import ComplianceResult
        from app.creator.graph import run_creator_graph

        async def fake_llm(prompt, contract):
            if contract is ComplianceResult:
                return ComplianceResult(passed=True, hits=[], suggestions=[])
            return self._gen()
        monkeypatch.setattr(nodes, "call_llm_json", fake_llm)

        item = _make_item(db)
        version = await run_creator_graph(item.id, "xiaohongshu", "note")
        assert version is not None
        assert version.compliance_status == "passed"
        assert version.title == "这个榨汁杯真的绝"
        db.expire_all()
        # 6-7 审批流：生成不再自动流转状态，保持 draft 直到人工提交审核
        assert db.get(models.ContentItem, item.id).status == "draft"
        assert version.dup_report.get("checked_at")  # 6-6 查重报告已生成

    @pytest.mark.asyncio
    async def test_rewrite_loop_path(self, db, monkeypatch):
        """改写循环：首次不合规 → rewrite → 复检通过。"""
        from app.creator import nodes
        from app.creator.contracts import ComplianceResult
        from app.creator.graph import run_creator_graph

        calls = {"compliance": 0}

        async def fake_llm(prompt, contract):
            if contract is ComplianceResult:
                calls["compliance"] += 1
                if calls["compliance"] == 1:
                    return ComplianceResult(
                        passed=False,
                        hits=[{"word": "最好", "reason": "极限词", "severity": "high"}],
                        suggestions=["去掉极限词"])
                return ComplianceResult(passed=True, hits=[], suggestions=[])
            return self._gen()
        monkeypatch.setattr(nodes, "call_llm_json", fake_llm)

        item = _make_item(db)
        version = await run_creator_graph(item.id, "xiaohongshu", "note")
        assert version is not None
        assert version.compliance_status == "passed"
        assert calls["compliance"] == 2  # 初检 + 复检各一次

    @pytest.mark.asyncio
    async def test_max_revision_persist_failed(self, db, monkeypatch):
        """改写上限：连续不合规 → persist failed 转人工。"""
        from app.creator import nodes
        from app.creator.contracts import ComplianceResult
        from app.creator.graph import run_creator_graph

        async def fake_llm(prompt, contract):
            if contract is ComplianceResult:
                return ComplianceResult(
                    passed=False,
                    hits=[{"word": "微信", "reason": "引流词", "severity": "high"}],
                    suggestions=["移除联系方式"])
            return self._gen()
        monkeypatch.setattr(nodes, "call_llm_json", fake_llm)

        item = _make_item(db)
        version = await run_creator_graph(item.id, "xiaohongshu", "note")
        assert version is not None
        assert version.compliance_status == "failed"

    @pytest.mark.asyncio
    async def test_banned_word_fails_compliance(self, db, monkeypatch):
        """合规检测命中违禁词/引流词 → failed（即使 LLM 误判通过，词库 high 命中也兜底）。"""
        from app.creator import nodes
        from app.creator.contracts import ComplianceResult, GeneratedVersion

        async def fake_llm(prompt, contract):
            if contract is ComplianceResult:
                return ComplianceResult(passed=True, hits=[], suggestions=[])  # LLM 漏判
            return GeneratedVersion(title="全网最低价的杯子", body="加微信有优惠",
                                    tags=[], script="", cover_text="")
        monkeypatch.setattr(nodes, "call_llm_json", fake_llm)

        from app.creator.graph import run_creator_graph
        item = _make_item(db)
        version = await run_creator_graph(item.id, "xiaohongshu", "note")
        # 词库命中 high → 改写循环后仍命中 → failed
        assert version is not None
        assert version.compliance_status == "failed"
        hit_words = [h["word"] for h in (version.compliance_report or {}).get("hits", [])]
        assert any(w in hit_words for w in ("全网最低", "微信"))


class TestMockPublishFlow:
    @pytest.mark.asyncio
    async def test_task_to_post_via_scheduler(self, db, monkeypatch):
        """发布任务创建 → Mock 通道 → scheduler 跑一轮 → Post 落库。"""
        from app.publisher.scheduler import run_due_tasks

        monkeypatch.setattr(settings, "publish_auto_enabled", True)
        item = _make_item(db, status="approved")
        version = _make_version(db, item)
        account = _make_account(db)
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow() - timedelta(seconds=1))
        db.add(task)
        db.commit()

        picked = await run_due_tasks()
        assert task.id in picked
        db.expire_all()
        task = db.get(models.PublishTask, task.id)
        assert task.status == "success"
        assert task.platform_post_id.startswith("mock_post_")
        assert task.published_at is not None

        post = db.query(models.Post).filter(models.Post.publish_task_id == task.id).first()
        assert post is not None
        assert post.platform_post_id == task.platform_post_id
        assert post.platform == "mock"

    @pytest.mark.asyncio
    async def test_daily_limit_defers_to_next_day(self, db, monkeypatch):
        """日额度超限：任务推迟到次日，不执行。"""
        from app.publisher.scheduler import run_due_tasks

        monkeypatch.setattr(settings, "publish_auto_enabled", True)
        item = _make_item(db, status="approved")
        version = _make_version(db, item)
        account = _make_account(db, limit=1)

        # 造一个今日已成功的任务占满额度
        done = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow(), status="success",
                                  published_at=datetime.utcnow())
        db.add(done)
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow() - timedelta(seconds=1))
        db.add(task)
        db.commit()

        picked = await run_due_tasks()
        assert task.id not in picked
        db.expire_all()
        task = db.get(models.PublishTask, task.id)
        assert task.status == "pending"
        assert task.scheduled_at > datetime.utcnow()  # 已推迟

    @pytest.mark.asyncio
    async def test_switch_off_blocks_publish(self, db, monkeypatch):
        """熔断开关关闭时调度器不执行任何任务。"""
        from app.publisher.scheduler import run_due_tasks

        monkeypatch.setattr(settings, "publish_auto_enabled", False)
        item = _make_item(db, status="approved")
        version = _make_version(db, item)
        account = _make_account(db)
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow() - timedelta(seconds=1))
        db.add(task)
        db.commit()
        assert await run_due_tasks() == []
        db.expire_all()
        assert db.get(models.PublishTask, task.id).status == "pending"

    @pytest.mark.asyncio
    async def test_failed_task_retries_then_failed(self, db, monkeypatch):
        """通道异常：retries<3 回 pending，达到上限置 failed。"""
        from app.publisher import scheduler

        monkeypatch.setattr(settings, "publish_auto_enabled", True)
        item = _make_item(db, status="approved")
        version = _make_version(db, item)
        account = _make_account(db)
        task = models.PublishTask(content_version_id=version.id, account_id=account.id,
                                  scheduled_at=datetime.utcnow() - timedelta(seconds=1),
                                  retries=2)  # 已重试 2 次，本次失败即终态
        db.add(task)
        db.commit()

        class BoomChannel:
            platform = "mock"

            async def publish(self, db, task, version, account):
                raise RuntimeError("模拟通道故障")

        monkeypatch.setattr(scheduler, "get_channel", lambda account: BoomChannel())
        await scheduler.run_due_tasks()
        db.expire_all()
        task = db.get(models.PublishTask, task.id)
        assert task.status == "failed"
        assert task.retries == 3
        assert "模拟通道故障" in task.error
