"""离线生成 1～2 份候选提示词，并对同一批案例对照通过率。不替换线上 cs_agent.md。

用法：cd backend && python -m evals.prompt_ab
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openai import AsyncOpenAI  # noqa: E402

from app.agent.prompt import PROMPT_PATH, load_prompt  # noqa: E402
from app.config import settings  # noqa: E402
from evals.run_evals import CASES_PATH, run_case  # noqa: E402

CANDIDATE_DIR = os.path.join(os.path.dirname(PROMPT_PATH), "candidates")


async def propose_prompts(base: str, notes: list[str]) -> list[str]:
    """根据不佳备注改出最多两份完整提示词。失败时返回空列表。"""
    if not settings.llm_configured:
        return []
    prompt = (
        "下面是客服系统提示词，以及若干不佳回复备注。"
        "请给出 1 到 2 份完整改写，保留全部 {{占位符}}。"
        "只输出 JSON：{\"prompts\":[\"完整提示词\"]}\n\n"
        f"备注：\n" + "\n".join(notes[:8] or ["（无备注）"])
        + "\n\n当前提示词：\n" + base
    )
    client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key, timeout=30.0)
    resp = await client.chat.completions.create(
        model=settings.llm_model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=4000,
        response_format={"type": "json_object"},
    )
    data = json.loads(resp.choices[0].message.content or "{}")
    prompts = data.get("prompts") if isinstance(data.get("prompts"), list) else []
    return [str(item) for item in prompts if isinstance(item, str) and "{{user_message}}" in item][:2]


def write_candidates(prompts: list[str]) -> list[str]:
    os.makedirs(CANDIDATE_DIR, exist_ok=True)
    paths = []
    for i, text in enumerate(prompts, start=1):
        path = os.path.join(CANDIDATE_DIR, f"cs_agent.candidate{i}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        paths.append(path)
    return paths


async def score_prompt(prompt_text: str | None, cases: list[dict]) -> tuple[int, int]:
    passed = 0
    for case in cases:
        ok, _failures, _reply = await run_case(case, prompt_text=prompt_text)
        if ok:
            passed += 1
    return passed, len(cases)


async def main():
    if not settings.llm_configured:
        print("未配置 LLM_API_KEY")
        sys.exit(1)
    with open(CASES_PATH, encoding="utf-8") as f:
        cases = json.load(f)
    base = load_prompt()
    notes = [
        (case.get("review") or {}).get("note", "")
        for case in cases
        if isinstance(case.get("review"), dict)
    ]
    candidates = await propose_prompts(base, [note for note in notes if note])
    paths = write_candidates(candidates)
    base_passed, total = await score_prompt(None, cases)
    print(f"线上提示词 {base_passed}/{total}")
    for path in paths:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        passed, total = await score_prompt(text, cases)
        print(f"{os.path.basename(path)} {passed}/{total}")
    print("未替换 backend/app/prompts/cs_agent.md")


if __name__ == "__main__":
    asyncio.run(main())
