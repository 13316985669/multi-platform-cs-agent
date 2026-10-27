"""检索质量离线评测：hit@3 / MRR@3 基线。

用法：
    cd backend && .venv\\Scripts\\python.exe -m evals.run_retrieval_eval
    .venv\\Scripts\\python.exe -m evals.run_retrieval_eval --limit 100

样本：data/dialog_clean/consult_paths.jsonl 里 status=可回答 的 question，按问题去重。
预期命中：标题、问句或相似问与样本问题一致的启用 FAQ。
评测保持确定性：查询改写固定为原句、不做放宽重试（不调 LLM）；
向量与精排走真实配置（.env 里配了才生效），衡量的是当前线上检索形态。
报告写入 evals/retrieval_eval_report.json，阈值调整另找用户确认。
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import KnowledgeDoc  # noqa: E402
from app.rag import ingest, pipeline, rewrite  # noqa: E402

DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "dialog_clean"
DATA_PATHS = [DATA_DIR / "consult_paths.jsonl", DATA_DIR / "answer_review.jsonl"]
REPORT_PATH = Path(__file__).resolve().parent / "retrieval_eval_report.json"
MISSED_LISTED = 50


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _load_faq_map() -> tuple[dict[str, int], list[tuple[str, int]]]:
    """启用 FAQ：exact 标签（标题/问句/相似问）-> doc_id，以及（标题/问句）标签列表供包含匹配。"""
    db = SessionLocal()
    try:
        docs = db.query(KnowledgeDoc).filter(
            KnowledgeDoc.doc_type == "faq", KnowledgeDoc.status == "active",
        ).all()
    finally:
        db.close()
    faq_map: dict[str, int] = {}
    core_labels: list[tuple[str, int]] = []
    for doc in docs:
        first = (doc.content or "").partition("\n")[0].removeprefix("问：").removeprefix("问:")
        core = {_compact(doc.title or ""), _compact(first)}
        labels = set(core)
        labels.update(_compact(item) for item in ingest.load_similar_questions(doc.similar_questions))
        for label in labels:
            if label and label not in faq_map:
                faq_map[label] = doc.id
        for label in core:
            if label:
                core_labels.append((label, doc.id))
    return faq_map, core_labels


def _expected_doc(question: str, faq_map: dict[str, int],
                  core_labels: list[tuple[str, int]]) -> int | None:
    """先精确匹配；再包含匹配（互相包含且长度≥4），多义时跳过。"""
    phrase = _compact(question)
    if not phrase:
        return None
    exact = faq_map.get(phrase)
    if exact is not None:
        return exact
    matched: set[int] = set()
    for label, doc_id in core_labels:
        if len(label) < 4 or len(phrase) < 4:
            continue
        if label in phrase or phrase in label:
            matched.add(doc_id)
    if len(matched) == 1:
        return matched.pop()
    return None


def _load_samples(limit: int) -> list[str]:
    """样本问题：consult_paths 可回答的规范问 + answer_review 的顾客原话，按 compact 去重保序。"""
    seen: set[str] = set()
    samples: list[str] = []
    for path in DATA_PATHS:
        if not path.is_file():
            continue
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("status") not in (None, "可回答"):
                    continue
                question = (row.get("question") or "").strip()
                if not question:
                    continue
                key = _compact(question)
                if not key or key in seen:
                    continue
                seen.add(key)
                samples.append(question)
    return samples[:limit] if limit else samples


async def _offline_rewrite(query: str, history=None, summary: str = "") -> list[str]:
    return [query]


async def _offline_broaden(*_args, **_kwargs) -> None:
    return None


async def main(limit: int) -> None:
    rewrite.rewrite_query = _offline_rewrite
    rewrite.broaden_query = _offline_broaden
    ingest.rebuild_bm25_from_db()

    faq_map, core_labels = _load_faq_map()
    samples = _load_samples(limit)
    hits = 0
    mrr_sum = 0.0
    passed_count = 0
    evaluated = 0
    skipped_no_expected = 0
    reasons: dict[str, int] = {}
    missed: list[dict] = []
    for question in samples:
        expected = _expected_doc(question, faq_map, core_labels)
        if expected is None:
            skipped_no_expected += 1
            continue
        result = await pipeline.retrieve(question)
        evaluated += 1
        reasons[result.reason] = reasons.get(result.reason, 0) + 1
        doc_ids = [item.get("doc_id") for item in result.contexts]
        if expected in doc_ids:
            hits += 1
            mrr_sum += 1.0 / (doc_ids.index(expected) + 1)
            if result.passed:
                passed_count += 1
        elif len(missed) < MISSED_LISTED:
            missed.append({"question": question, "reason": result.reason, "got_doc_ids": doc_ids})

    report = {
        "samples_total": len(samples),
        "evaluated": evaluated,
        "skipped_no_expected_faq": skipped_no_expected,
        "hit_at_3": round(hits / evaluated, 4) if evaluated else None,
        "mrr_at_3": round(mrr_sum / evaluated, 4) if evaluated else None,
        "passed_rate": round(passed_count / evaluated, 4) if evaluated else None,
        "reasons": reasons,
        "missed_examples": missed,
        "params": {
            "rag_rerank_threshold": settings.rag_rerank_threshold,
            "rag_dense_min_score": settings.rag_dense_min_score,
            "rag_dense_absolute_min": settings.rag_dense_absolute_min,
            "rag_relative_score_ratio": settings.rag_relative_score_ratio,
            "embedding_configured": settings.embedding_configured,
            "rerank_configured": settings.rerank_configured,
        },
    }
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"samples={len(samples)} evaluated={evaluated} skipped={skipped_no_expected}")
    if evaluated:
        print(f"hit@3={report['hit_at_3']} mrr@3={report['mrr_at_3']} passed={report['passed_rate']}")
    print("reasons:", json.dumps(reasons, ensure_ascii=False))
    print(f"report written: {REPORT_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="检索质量离线评测（hit@3 / MRR@3）")
    parser.add_argument("--limit", type=int, default=0, help="最多评测的样本数，0 = 全部")
    args = parser.parse_args()
    asyncio.run(main(args.limit))
