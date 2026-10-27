"""Eval 回归跑分：提示词/RAG 改动后一键回归。

用法：
    cd backend && .venv\\Scripts\\Activate.ps1
    python -m evals.run_evals            # 需要 .env 配置 LLM_API_KEY
    python -m evals.run_evals --only 1,3,5

断言：契约合法性 + 关键行为（应转人工/应命中知识/应含关键词/消息长度/禁用话术）。
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.agent import prompt as prompt_mod  # noqa: E402
from app.agent.engine import _call_llm_with_retry  # noqa: E402
from app.config import settings  # noqa: E402

CASES_PATH = os.path.join(os.path.dirname(__file__), "cases.json")


def check_case(case: dict, reply) -> list[str]:
    """返回失败断言列表，空列表 = 通过。"""
    failures = []
    exp = case["expect"]

    if reply is None:
        return ["契约解析失败（两次重试后仍非法 JSON）"]

    if "handoff" in exp and reply.handoff != exp["handoff"]:
        failures.append(f"handoff 期望 {exp['handoff']} 实际 {reply.handoff}")
    if "handoff_reason" in exp and reply.handoff_reason != exp["handoff_reason"]:
        failures.append(f"handoff_reason 期望 {exp['handoff_reason']} 实际 {reply.handoff_reason}")
    if "handoff_reason_in" in exp and reply.handoff_reason not in exp["handoff_reason_in"]:
        failures.append(f"handoff_reason 期望属于 {exp['handoff_reason_in']} 实际 {reply.handoff_reason}")
    if "intent" in exp and reply.intent != exp["intent"]:
        failures.append(f"intent 期望 {exp['intent']} 实际 {reply.intent}")
    if "min_confidence" in exp and reply.confidence < exp["min_confidence"]:
        failures.append(f"confidence {reply.confidence} 低于 {exp['min_confidence']}")

    full_reply = " ".join(reply.reply_messages)
    for kw in exp.get("reply_contains", []):
        if kw not in full_reply:
            failures.append(f"回复应包含「{kw}」，实际：{full_reply[:80]}")
    for kw in exp.get("reply_not_contains", []):
        if kw in full_reply:
            failures.append(f"回复不应包含「{kw}」，实际：{full_reply[:80]}")
    if "max_message_len" in exp:
        for m in reply.reply_messages:
            if len(m) > exp["max_message_len"]:
                failures.append(f"单条消息超长（{len(m)} 字）：{m[:40]}...")
    if "tags_include" in exp:
        for tag in exp["tags_include"]:
            if tag not in reply.tags:
                failures.append(f"标签应包含「{tag}」，实际：{reply.tags}")
    if "lead_phone" in exp and reply.lead.phone != exp["lead_phone"]:
        failures.append(f"留资手机号期望 {exp['lead_phone']} 实际 {reply.lead.phone}")
    if "tool_call_in" in exp:
        actual_tool = reply.tool_call.name if reply.tool_call else None
        if actual_tool not in exp["tool_call_in"]:
            failures.append(f"tool_call 期望属于 {exp['tool_call_in']} 实际 {actual_tool}")
    return failures


async def knowledge_for_case(case: dict, *, live_rag: bool) -> str:
    """静态案例用用例里的知识。retrieve=true 且打开 live-rag 时先走检索。"""
    if live_rag and case.get("retrieve"):
        from app.rag.pipeline import retrieve
        result = await retrieve(case.get("user") or "", history=[])
        if result.passed and result.contexts:
            blocks = []
            for i, item in enumerate(result.contexts):
                blocks.append(f"【资料{i + 1}】（来源：{item.get('source') or ''}）\n{item.get('content') or ''}")
            return "\n\n".join(blocks)
        return "无匹配资料"
    return case.get("knowledge") or "无匹配资料"


async def run_case(case: dict, *, live_rag: bool = False, prompt_text: str | None = None) -> tuple[bool, list[str], object]:
    knowledge = await knowledge_for_case(case, live_rag=live_rag)
    renderer = prompt_mod.render_template if prompt_text else prompt_mod.render_prompt
    kwargs = dict(
        platform="douyin",
        knowledge_context=knowledge,
        history_text="（无历史，这是用户的第一条消息）",
        user_message=case["user"],
    )
    if prompt_text:
        rendered = renderer(prompt_text, **kwargs)
    else:
        rendered = renderer(**kwargs)
    reply = await _call_llm_with_retry(rendered)
    failures = check_case(case, reply)
    return not failures, failures, reply


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=str, default="", help="只跑指定用例，如 1,3,5")
    parser.add_argument("--live-rag", action="store_true", help="retrieve=true 的案例先检索再生成")
    parser.add_argument("--prompt", type=str, default="", help="用这份提示词文件代替线上 cs_agent.md")
    args = parser.parse_args()

    if not settings.llm_configured:
        print("未配置 LLM_API_KEY，无法跑 eval。请在 backend/.env 中配置。")
        sys.exit(1)

    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    if args.only:
        ids = {int(x) for x in args.only.split(",")}
        cases = [c for c in cases if c["id"] in ids]

    prompt_text = ""
    if args.prompt:
        with open(args.prompt, encoding="utf-8") as f:
            prompt_text = f.read()
    passed, failed = 0, 0
    for case in cases:
        ok, failures, reply = await run_case(
            case, live_rag=args.live_rag, prompt_text=prompt_text or None,
        )
        if ok:
            passed += 1
            print(f"✓ [{case['id']}] {case['name']}")
        else:
            failed += 1
            print(f"✗ [{case['id']}] {case['name']}")
            for f_ in failures:
                print(f"    - {f_}")
            if reply:
                print(f"    实际输出: {reply.model_dump_json(ensure_ascii=False)[:200]}")

    total = passed + failed
    print(f"\n===== 结果：{passed}/{total} 通过（{passed / total * 100:.0f}%）=====")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    asyncio.run(main())
