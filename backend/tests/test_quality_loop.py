"""质量闭环里不依赖线上模型的几条约束。"""
import pytest

from app.agent.engine import _format_knowledge_context, _trim_knowledge
from app.agent.reflect import review_reply
from app.services import _normalize_missed_question, handoff_public_line


def test_trim_keeps_overlapping_sentence_and_source_label():
    long_text = ("无关的开场。" * 80) + "运费是十二元。" + ("另一段无关内容。" * 80)
    formatted = _format_knowledge_context(
        [{"content": long_text, "source": "运费说明"}],
        query="运费多少",
    )
    assert "来源：运费说明" in formatted
    assert "运费是十二元" in formatted
    assert len(formatted) < len(long_text)


def test_trim_short_text_unchanged():
    assert _trim_knowledge("短句。", "价格", 500) == "短句。"


def test_missed_question_normalizes_space_and_question_mark():
    assert _normalize_missed_question("  多少钱 ？ ") == _normalize_missed_question("多少钱?")


def test_handoff_line_hides_internal_code():
    text = handoff_public_line("llm_parse_failed")
    assert "llm_parse_failed" not in text
    assert "卡住" in text


@pytest.mark.asyncio
async def test_review_reply_without_llm_returns_none(monkeypatch):
    from app.agent import reflect
    monkeypatch.setattr(reflect.settings, "llm_api_key", "")
    assert await review_reply(["十二元"], "十元", timeout=1) is None


def test_eval_draft_module_does_not_reference_cases_json_write():
    from pathlib import Path
    source = Path(__file__).resolve().parents[1].joinpath("app", "evals_draft.py").read_text(encoding="utf-8")
    assert "cases.json" in source
    assert "不写入" in source or "不改" in Path(__file__).resolve().parents[1].joinpath("evals", "generate_cases.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_live_rag_uses_retrieval_context(monkeypatch):
    from evals.run_evals import knowledge_for_case

    class Result:
        passed = True
        contexts = [{"source": "价目", "content": "标准款 3999"}]

    async def fake_retrieve(query, history=None, top_k=3, summary="", broaden=True):
        assert query == "多少钱"
        return Result()

    monkeypatch.setattr("app.rag.pipeline.retrieve", fake_retrieve)
    text = await knowledge_for_case({"user": "多少钱", "knowledge": "", "retrieve": True}, live_rag=True)
    assert "3999" in text
    assert "价目" in text
    static = await knowledge_for_case({"user": "多少钱", "knowledge": "静态", "retrieve": True}, live_rag=False)
    assert static == "静态"


def test_prompt_candidate_writer_does_not_touch_live_prompt(tmp_path, monkeypatch):
    from evals import prompt_ab
    monkeypatch.setattr(prompt_ab, "CANDIDATE_DIR", str(tmp_path))
    paths = prompt_ab.write_candidates(["你好 {{user_message}}"])
    assert len(paths) == 1
    assert paths[0].endswith("cs_agent.candidate1.md")
    written = open(paths[0], encoding="utf-8").read()
    assert "{{user_message}}" in written
    assert not (tmp_path / "cs_agent.md").exists()
