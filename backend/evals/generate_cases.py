"""从未命中和不佳起草评测案例，写入 evals/drafts/，不改 cases.json。

用法：cd backend && python -m evals.generate_cases
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal  # noqa: E402
from app.evals_draft import build_eval_drafts  # noqa: E402

DRAFT_DIR = os.path.join(os.path.dirname(__file__), "drafts")


async def main():
    db = SessionLocal()
    try:
        drafts = await build_eval_drafts(db)
    finally:
        db.close()
    os.makedirs(DRAFT_DIR, exist_ok=True)
    path = os.path.join(DRAFT_DIR, "drafts.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(drafts, f, ensure_ascii=False, indent=2)
    cases_path = os.path.join(os.path.dirname(__file__), "cases.json")
    print(f"写入 {path} ，{len(drafts)} 条。未修改 {cases_path}")


if __name__ == "__main__":
    asyncio.run(main())
