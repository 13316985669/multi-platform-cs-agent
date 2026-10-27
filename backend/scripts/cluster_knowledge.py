"""把已启用的杂乱 FAQ 归成业务知识和三类话术。

由规则归类，不调用外部模型，也不新写价格和清单。
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict

from scripts.clean_history_to_kb import (
    CAT_FOLLOW,
    CAT_LEAD,
    CAT_MARKET,
    BUSINESS_CATEGORIES,
    has_hard_fact,
    is_clarifying_answer,
    is_contact_only_question,
    is_lead,
    is_opening_menu,
    is_promo_answer,
    is_weak_fact,
    factual_clauses,
    knowledge_category,
    norm_key,
    similar,
    standard_question,
)

PROCESS_CATEGORIES = set(BUSINESS_CATEGORIES) | {"其他", CAT_LEAD, CAT_MARKET, CAT_FOLLOW}
SCRIPT_CATEGORIES = {CAT_LEAD, CAT_MARKET, CAT_FOLLOW}
TITLE_FOR = {
    CAT_LEAD: "怎么留电话或微信",
    CAT_MARKET: "现在有什么活动价",
    CAT_FOLLOW: "客户没回复怎么跟进",
}
NOISE_EXACT = {"稍等", "可以的", "没有", "嗯呢", "可以", "好的", "ok", "嗯", "哦", "是的", "对"}


def classify_faq(question: str, answer: str) -> str | None:
    """返回分类；噪声返回 None。"""
    question = (question or "").strip()
    answer = (answer or "").strip()
    if not answer:
        return None
    if is_contact_only_question(question) or is_opening_menu(question, answer) or is_clarifying_answer(answer):
        return None
    if is_weak_fact(answer) or norm_key(answer) in NOISE_EXACT:
        return None
    if len(norm_key(answer)) < 8 and not has_hard_fact(answer):
        return None
    blob = question + answer
    if is_promo_answer(answer) or "3980" in blob or "周年" in answer:
        return CAT_MARKET
    if any(word in answer for word in ("还在线", "在吗", "通过一下", "通过下", "催你", "催您")) and "3980" not in answer:
        return CAT_FOLLOW
    if is_lead(answer):
        return CAT_LEAD
    if any(word in answer for word in ("汇丰", "渣打", "中银", "东亚")):
        return "开户"
    if sum(word in answer for word in ("公司名称", "经营范围", "注册资金", "股东", "通行证", "流水")) >= 2:
        return "资料"
    category = knowledge_category("其他", "", answer)
    if category:
        return category
    if "资料" in question or "材料" in question:
        return "资料"
    if any(word in question for word in ("多少钱", "费用", "收费", "价格")):
        return "费用"
    if "流程" in question:
        return "流程"
    if "年审" in blob:
        return "年审"
    if "审计" in blob:
        return "审计"
    if "开户" in blob or "银行" in blob:
        return "开户"
    if "注册" in blob:
        return "注册"
    return None


def _script_kind(category: str, answer: str) -> str:
    raw = answer or ""
    if category == CAT_MARKET:
        return "活动价"
    if category == CAT_FOLLOW:
        if "通过" in raw:
            return "催通过"
        if "接听" in raw or "没接" in raw:
            return "催接听"
        return "还在线"
    if "二维码" in raw:
        return "发二维码"
    if "安排专员" in raw:
        return "安排专员"
    if any(word in raw for word in ("vx", "VX", "微信", "v信", "V信")):
        return "加微信"
    return "留电话"


def _best_answer(rows: list[dict], category: str, kind_key: str) -> str:
    counts: dict[str, int] = defaultdict(int)
    display: dict[str, str] = {}
    for row in rows:
        text = (row["answer"] or "").strip()
        key = norm_key(text)
        if not key:
            continue
        counts[key] += 1
        current = display.get(key)
        if current is None or len(text) < len(current):
            display[key] = text
    if not counts:
        return ""
    anchors = {
        "留电话": "联系方式多少",
        "加微信": "加下vx",
        "安排专员": "安排专员",
        "发二维码": "二维码",
        "活动价": "3980",
        "还在线": "还在线",
        "催通过": "通过",
    }
    anchor = anchors.get(kind_key, "")

    def rank(key: str) -> tuple:
        text = display[key]
        if kind_key == "活动价":
            full = int("周年" in text and "3980" in text and "The current" not in text)
            return (full, int("3980" in text), len(text))
        if kind_key in {"还在线", "催通过", "加微信"}:
            hit = int(bool(anchor and anchor in text))
            return (hit, counts[key], len(text))
        hit = int(bool(anchor and anchor in text))
        return (hit, counts[key], -len(text))

    return display[max(counts, key=rank)]


def build_replacements(rows: list[dict]) -> tuple[list[dict], list[int]]:
    """返回新 FAQ 和要停用的旧 id。只处理导入进来的杂乱分类。"""
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    disable: list[int] = []
    for row in rows:
        if row["category"] not in PROCESS_CATEGORIES and not is_opening_menu(row["question"] or row["title"], row["answer"]):
            continue
        disable.append(row["id"])
        if row["category"] not in PROCESS_CATEGORIES:
            continue
        kind = classify_faq(row["question"] or row["title"], row["answer"])
        if kind is None:
            continue
        answer = row["answer"]
        if kind not in SCRIPT_CATEGORIES:
            answer = factual_clauses(answer) or answer
            if not has_hard_fact(answer):
                continue
            key = standard_question(kind, answer)
        else:
            key = _script_kind(kind, answer)
        grouped[(kind, key)].append({**row, "answer": answer})
    created = []
    for (kind, key), items in grouped.items():
        answer = _best_answer(items, kind, key)
        if kind == CAT_MARKET:
            answer = factual_clauses(answer) or answer
        if kind not in SCRIPT_CATEGORIES:
            answer = factual_clauses(answer) or answer
        if not answer or is_weak_fact(answer) or is_clarifying_answer(answer):
            continue
        title = key if kind in SCRIPT_CATEGORIES else key
        if kind in SCRIPT_CATEGORIES:
            title = {
                "活动价": "现在有什么活动价",
                "催通过": "客户没通过怎么跟进",
                "催接听": "客户没接听怎么跟进",
                "还在线": "客户没回复怎么跟进",
                "发二维码": "怎么发二维码",
                "安排专员": "客户已留电话怎么回复",
                "加微信": "怎么加微信",
                "留电话": "怎么留电话",
            }.get(key, key)
        similars = []
        for row in items:
            text = (row["question"] or "").strip()
            if text and text != title and text not in similars and not is_contact_only_question(text):
                if text not in {"其他", "注册", "开户", "年审", "审计", "身份", "税务"}:
                    similars.append(text)
        created.append({
            "title": title[:256],
            "category": kind,
            "content": f"问：{title}\n答：{answer}",
            "similar_questions": json.dumps(similars[:12], ensure_ascii=False),
        })
    return created, disable


def apply_replacements(created: list[dict], disable_ids: list[int]) -> None:
    """停用旧 FAQ，写入归类结果，并重建这些文档的索引。"""
    import asyncio

    from app.database import SessionLocal, init_db
    from app.models import KnowledgeDoc
    from app.rag import ingest

    init_db()
    db = SessionLocal()
    new_ids: list[int] = []
    try:
        if disable_ids:
            docs = db.query(KnowledgeDoc).filter(KnowledgeDoc.id.in_(disable_ids)).all()
            for doc in docs:
                doc.status = "disabled"
        for item in created:
            doc = KnowledgeDoc(
                title=item["title"],
                doc_type="faq",
                content=item["content"],
                category=item["category"],
                similar_questions=item["similar_questions"],
                status="active",
            )
            db.add(doc)
            db.flush()
            new_ids.append(doc.id)
        db.commit()
    finally:
        db.close()
    asyncio.run(ingest.sync_doc_index(disable_ids + new_ids))


def main() -> None:
    import json
    from pathlib import Path
    from collections import Counter

    rows = json.loads((Path(__file__).resolve().parents[1] / "data" / "faq_export.json").read_text(encoding="utf-8"))
    created, disable_ids = build_replacements(rows)
    print("disable", len(disable_ids), "create", len(created))
    print(Counter(item["category"] for item in created))
    for item in created:
        print("---", item["category"], item["title"])
        print(item["content"][:200].replace("\n", " | "))
    if "--apply" not in sys.argv:
        return
    apply_replacements(created, disable_ids)


if __name__ == "__main__":
    main()
