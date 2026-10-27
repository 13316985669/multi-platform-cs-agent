"""准确性回归：不调用大模型。检查子问题拆分、事实核对和兜底边界。

提示词契约仍用 `python -m evals.run_evals`。
带知识库的检索合并见 tests/test_rag.py::test_covering_retrieve_merges_two_faqs。

用法：
    cd backend && python -m evals.run_faithfulness
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import evidence  # noqa: E402
from app.rag import pipeline  # noqa: E402


CASES = [
    {
        "name": "单问不拆",
        "check": "facet",
        "user": "多少钱",
        "facets": [],
    },
    {
        "name": "价格和包邮拆开",
        "check": "facet",
        "user": "多少钱和包邮吗",
        "facets": ["多少钱", "包邮吗"],
    },
    {
        "name": "价格必须来自资料",
        "check": "ground",
        "reply": "这款 99 元",
        "evidence": "标准款 99 元",
        "ok": True,
    },
    {
        "name": "不能报资料里没有的价格",
        "check": "ground",
        "reply": "这款 199 元",
        "evidence": "标准款 99 元",
        "ok": False,
    },
    {
        "name": "七天对得上 7 天",
        "check": "ground",
        "reply": "七天无理由",
        "evidence": "7天无理由退换",
        "ok": True,
    },
    {
        "name": "两份价格冲突时不选边",
        "check": "conflict",
        "reply": "这款 99 元",
        "contexts": ["标准款 99 元", "活动价 199 元"],
    },
    {
        "name": "缺口可以用口语转人工",
        "check": "guide",
        "reply": "包邮这句我让同事确认哈",
    },
    {
        "name": "兜底不能新编包邮",
        "check": "ground",
        "reply": "全国包邮",
        "evidence": "标准款 99 元",
        "ok": False,
    },
]


def run_case(case: dict) -> list[str]:
    kind = case["check"]
    if kind == "facet":
        actual = pipeline.split_facets(case["user"])
        if actual != case["facets"]:
            return [f"子问题期望 {case['facets']} 实际 {actual}"]
        return []
    if kind == "ground":
        bad = evidence.claims_unsupported([case["reply"]], case["evidence"])
        if bad == case["ok"]:
            return [f"核对结果与期望相反：{case['reply']}"]
        return []
    if kind == "conflict":
        contexts = [{"content": text} for text in case["contexts"]]
        blob = "\n".join(case["contexts"])
        kept, _dropped = evidence.partition_messages([case["reply"]], blob, contexts)
        if kept:
            return [f"冲突价格仍被留下：{kept}"]
        return []
    if kind == "guide":
        if not evidence.is_handoff_guide(case["reply"]):
            return ["转人工说明被当成了事实句"]
        return []
    return [f"未知检查 {kind}"]


def main() -> None:
    failed = 0
    for case in CASES:
        failures = run_case(case)
        if failures:
            failed += 1
            print(f"FAIL {case['name']}")
            for item in failures:
                print(f"    - {item}")
        else:
            print(f"OK {case['name']}")
    total = len(CASES)
    passed = total - failed
    print(f"\n===== 准确性：{passed}/{total} 通过 =====")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    main()
