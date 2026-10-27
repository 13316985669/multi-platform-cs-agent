"""运维能力测试：多模型热备切换 / 监控告警 / 自动备份。"""
import logging
import os
import time

import httpx
import pytest
from openai import APITimeoutError

from app.agent import engine
from app.core import backup, monitor
from app.models import Message
from app.rag import pipeline as rag_pipeline
from app.schemas import AgentReply


# ============ 多模型热备 ============

async def test_llm_fallback_switch(monkeypatch):
    """主模型硬失败 → 自动切换备用模型。"""
    calls: list[str] = []

    async def fake_once(prompt, *, base_url, api_key, model, conversation_id=0, timeout=None):
        calls.append(model)
        if model == "primary-model":
            return None, True  # 硬失败
        return AgentReply(reply_messages=["备用模型回复"], confidence=0.8), False

    monkeypatch.setattr(engine, "_call_llm_once", fake_once)
    monkeypatch.setattr(engine.settings, "llm_model", "primary-model")
    monkeypatch.setattr(engine.settings, "llm_fallback_model", "backup-model")
    monkeypatch.setattr(engine.settings, "llm_fallback_api_key", "fallback-key")

    reply = await engine._call_llm_with_retry("test prompt")

    assert reply is not None
    assert reply.reply_messages == ["备用模型回复"]
    assert calls == ["primary-model", "backup-model"]


async def test_llm_no_fallback_when_not_configured(monkeypatch):
    """未配置备用模型时，硬失败直接返回 None。"""
    calls: list[str] = []

    async def fake_once(prompt, *, base_url, api_key, model, conversation_id=0, timeout=None):
        calls.append(model)
        return None, True

    monkeypatch.setattr(engine, "_call_llm_once", fake_once)
    monkeypatch.setattr(engine.settings, "llm_model", "primary-model")
    monkeypatch.setattr(engine.settings, "llm_fallback_model", "")
    monkeypatch.setattr(engine.settings, "llm_fallback_api_key", "")

    reply = await engine._call_llm_with_retry("test prompt")

    assert reply is None
    assert calls == ["primary-model"]


async def test_llm_parse_failure_uses_fallback(monkeypatch):
    """解析失败时也切换备用模型，不再直接放弃。"""
    calls: list[str] = []

    async def fake_once(prompt, *, base_url, api_key, model, conversation_id=0, timeout=None):
        calls.append(model)
        if model == "primary-model":
            return None, False
        return AgentReply(reply_messages=["备用模型接上了"], confidence=0.6), False

    monkeypatch.setattr(engine, "_call_llm_once", fake_once)
    monkeypatch.setattr(engine.settings, "llm_model", "primary-model")
    monkeypatch.setattr(engine.settings, "llm_fallback_model", "backup-model")
    monkeypatch.setattr(engine.settings, "llm_fallback_api_key", "fallback-key")

    reply = await engine._call_llm_with_retry("test prompt")

    assert reply is not None
    assert reply.reply_messages == ["备用模型接上了"]
    assert calls == ["primary-model", "backup-model"]


async def test_fallback_uses_its_own_timeout(monkeypatch):
    """备用模型不沿用已经耗尽的剩余时间。"""
    seen: list[tuple[str, float | None]] = []

    async def fake_once(prompt, *, base_url, api_key, model, conversation_id=0, timeout=None):
        seen.append((model, timeout))
        if model == "primary-model":
            return None, True
        return AgentReply(reply_messages=["备用模型回复"], confidence=0.8), False

    monkeypatch.setattr(engine, "_call_llm_once", fake_once)
    monkeypatch.setattr(engine.settings, "llm_model", "primary-model")
    monkeypatch.setattr(engine.settings, "llm_fallback_model", "backup-model")
    monkeypatch.setattr(engine.settings, "llm_fallback_api_key", "fallback-key")

    await engine._call_llm_with_retry("test prompt", timeout=2)

    assert seen == [
        ("primary-model", 2),
        ("backup-model", engine.FALLBACK_TIMEOUT_SECONDS),
    ]


async def test_read_timeout_is_a_warning(monkeypatch, caplog):
    """读超时只记秒数，不打完整堆栈。"""

    class _Completions:
        async def create(self, **_kwargs):
            raise APITimeoutError(request=httpx.Request("POST", "https://example.invalid"))

    class _Chat:
        completions = _Completions()

    class _Client:
        def __init__(self, **_kwargs):
            self.chat = _Chat()

    monkeypatch.setattr(engine, "get_llm_client", lambda *_a, **_k: _Client())
    with caplog.at_level(logging.WARNING):
        reply, hard_failed = await engine._call_llm_once(
            "hi", base_url="http://example.invalid", api_key="k", model="gpt-4o-mini", timeout=6,
        )

    assert reply is None
    assert hard_failed is True
    warnings = [record for record in caplog.records if "读超时" in record.message]
    assert warnings
    assert "timeout=6" in warnings[0].message
    assert warnings[0].exc_info is None


async def test_judge_skipped_when_draft_time_is_short(monkeypatch):
    """剩余时间不够给草稿留 15 秒时，不调用相关性判定。"""
    called = False

    async def fake_judge(*_a, **_k):
        nonlocal called
        called = True
        return [1]

    monkeypatch.setattr("app.agent.confidence.judge_relevance", fake_judge)
    retrieval = rag_pipeline.RetrievalResult(
        passed=True, contexts=[{"content": "注册资料", "source": "注册.md"}],
    )
    started = time.monotonic() - (engine.REPLY_BUDGET_SECONDS - 16)
    meta = await engine._select_for_answer("怎么注册", retrieval, [], "", started)

    assert called is False
    assert meta["action"] == "stop"
    assert meta["cause"] == "unscored"


async def test_short_budget_does_not_call_draft_model(monkeypatch):
    """剩余不足 4 秒时不创建主模型客户端。"""

    def boom(*_a, **_k):
        raise AssertionError("不应创建主模型客户端")

    monkeypatch.setattr(engine, "get_llm_client", boom)
    retrieval = rag_pipeline.RetrievalResult(
        passed=True, contexts=[{"content": "注册资料", "source": "注册.md"}],
    )
    started = time.monotonic() - (engine.REPLY_BUDGET_SECONDS - 3)
    reply = await engine._answer_from_knowledge(
        1, 1, "mock", "怎么弄", "", retrieval, started, [], [],
    )
    assert reply is None


@pytest.mark.asyncio
async def test_short_budget_materials_question_asks_colleague(db, conversation, monkeypatch):
    """知识已命中但来不及写草稿时，请同事确认，不发卡住话术。"""
    db.add(Message(
        conversation_id=conversation.id, sender_type="user", msg_type="text",
        content="行，香港公司我要注册，怎么弄",
    ))
    db.commit()

    async def fake_retrieve(query, history=None, top_k=3, summary=""):
        return rag_pipeline.RetrievalResult(
            passed=True, contexts=[{"content": "公司名称", "source": "注册.md"}],
        )

    async def fake_llm(*_a, **_k):
        raise AssertionError("剩余时间不够时不应调用主模型")

    real = time.monotonic
    origin = {"value": None}

    def late_clock():
        if origin["value"] is None:
            origin["value"] = real()
            return origin["value"]
        return origin["value"] + engine.REPLY_BUDGET_SECONDS - 3

    monkeypatch.setattr(engine.time, "monotonic", late_clock)
    monkeypatch.setattr(engine.pipeline, "retrieve", fake_retrieve)
    monkeypatch.setattr(engine, "_call_llm_with_retry", fake_llm)
    monkeypatch.setattr(engine.settings, "llm_api_key", "test-key")

    await engine.process_ai_reply(conversation.id)
    db.expire_all()
    sent = "\n".join(
        row.content for row in db.query(Message).filter(
            Message.conversation_id == conversation.id, Message.sender_type == "ai",
        )
    )
    assert "确认" in sent
    assert "卡了一下" not in sent


# ============ 监控告警 ============

def test_monitor_record_and_snapshot():
    monitor.record("tool_failure", "测试事件")
    assert monitor.snapshot().get("tool_failure", 0) >= 1


def test_monitor_threshold_alert_without_webhook(monkeypatch):
    """超阈值触发告警逻辑；未配置 webhook 时只记日志不发送（不抛异常即通过）。"""
    monkeypatch.setattr(monitor.settings, "alert_webhook_url", "")
    monkeypatch.setattr(monitor.settings, "alert_cooldown_seconds", 0)
    for _ in range(monitor.THRESHOLDS["webhook_failure"][0]):
        monitor.record("webhook_failure", "压测")
    assert monitor.count_last_hour("webhook_failure") >= monitor.THRESHOLDS["webhook_failure"][0]


# ============ 自动备份 ============

def test_backup_creates_files(tmp_path, monkeypatch):
    monkeypatch.setattr(backup.settings, "backup_dir", str(tmp_path))
    dest = backup.do_backup()
    assert os.path.isdir(dest)
    # 测试库文件应被备份（sqlite backup API）
    assert os.path.exists(os.path.join(dest, "app.db"))


def test_backup_rotation(tmp_path, monkeypatch):
    """滚动清理：超过 keep 数量的旧备份被删除。"""
    monkeypatch.setattr(backup.settings, "backup_dir", str(tmp_path))
    monkeypatch.setattr(backup.settings, "backup_keep", 2)
    for i in range(4):
        # 伪造带时间戳递增的旧备份目录
        fake = tmp_path / f"backup_2024010{i}_000000"
        fake.mkdir()
        (fake / "app.db").write_text("x")
    backup._rotate()
    remaining = [d for d in os.listdir(tmp_path) if d.startswith("backup_")]
    assert len(remaining) == 2
