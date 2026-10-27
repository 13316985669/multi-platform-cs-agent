"""把 DeepSeek 整理的 FAQ 文本写入现有知识库。

标准问保留顾客原问。斜杠前后各自成句时，第一句做标题，其余做相似问。
共享词尾的列举会展开成完整问句，裸词不入库。
第八、十二节以及第九、十节里的重复问法不另建条目。
"""
from __future__ import annotations

import argparse
import asyncio
import re
from pathlib import Path

Q_RE = re.compile(r"^Q(\d+)：顾客：(.+)$")
SECTION_RE = re.compile(r"^([一二三四五六七八九十]+)、")
SKIP_SECTIONS = {"八", "十二"}
REPEAT_SECTIONS = {"九", "十"}
GREETINGS = {"你好", "在吗", "您好", "嗨", "上午好", "下午好", "在么", "在嘛"}
QUESTION_WORD = re.compile(r"吗|呢|么|怎么|如何|多少|什么|是否|哪|啥|多久")
LEAD_VARIANT = re.compile(r"我要|不要|打不通|没人|电话|微信|二维码")
TOPIC_VARIANT = re.compile(r"开户|注册|年审|审计|地址|身份|注销|转让|续签")
PROMO_WORDS = ("3980", "周年", "优惠", "活动", "最后一天", "1280", "免费注册", "官费")
FOLLOW_WORDS = (
    "没人通过", "没人联系", "还没加", "打不通", "关机", "空号", "停机",
    "拒接", "没有接", "催通过", "通过一下", "通过下", "核对一下号码",
)
EXTRA_CATEGORIES = (
    ("价格贵考虑一下", "留资话术"),
    ("可以打电话吗", "留资话术"),
    ("能介绍一下服务吗", "注册"),
    ("你们是香港公司还是国内", "注册"),
)
PREFIXES = (
    "注册香港公司需要什么",
    "开户需要什么",
    "香港公司可以开",
    "可以注册",
    "可以开",
    "开户能",
    "需要什么",
)
ENDINGS = (
    "公司能注册吗",
    "公司开户",
    "可以注册吗",
    "能注册吗",
    "能开户吗",
    "可以开户吗",
    "怎么收费",
    "有什么要求",
    "有什么优势",
    "审计多少钱",
    "怎么做",
    "是什么",
    "多少钱",
    "吗",
    "呢",
)
BANK_WORDS = (
    "开户", "账户", "公户", "公司户", "对公", "个人户", "港卡", "信用卡", "收款",
    "汇丰", "渣打", "中银", "东亚", "大新", "华侨", "电子户", "实体户", "离岸",
    "NRA", "OSA",
)


def _parts(raw: str) -> list[str]:
    text = raw.strip().strip("？?。")
    found = []
    for part in text.split("/"):
        item = part.strip().strip("？?。 ")
        if item and item not in found:
            found.append(item)
    return found


def _legacy_title(raw: str) -> str:
    """上一次导入用的半句标题，斜杠或顿号前的第一段。"""
    first = re.split(r"[/、]", raw.strip())[0]
    return first.strip().strip("？?。 ")


def _is_standalone(part: str) -> bool:
    if QUESTION_WORD.search(part):
        return True
    return "几" in part and len(part) >= 4


def _ending_of(part: str) -> tuple[str, str]:
    for ending in ENDINGS:
        if part.endswith(ending) and len(part) > len(ending):
            return part[: -len(ending)], ending
    return "", ""


def _bare_object(part: str) -> bool:
    """列举里的短对象，例如「渣打」或「华侨吗」。完整问句不算。"""
    if not _is_standalone(part):
        return True
    head, ending = _ending_of(part)
    if not ending or len(ending) != 1 or not head or _is_standalone(head):
        return False
    return len(head) <= 3


def _prefix_of(part: str) -> str:
    for prefix in PREFIXES:
        if part.startswith(prefix) and len(part) > len(prefix):
            return prefix
    return ""


def _keep_raw(part: str) -> bool:
    if _is_standalone(part):
        return True
    if LEAD_VARIANT.search(part) and len(part) >= 4:
        return True
    return bool(TOPIC_VARIANT.search(part) and len(part) >= 4)


def _dedupe(items: list[str], title: str) -> list[str]:
    found = []
    for item in items:
        text = item.strip().strip("？?。 ")
        if text and text != title and text not in found:
            found.append(text)
    return found


def _spread_noun_tail(objects: list[str]) -> list[str]:
    """「新加坡/澳门账户」要把「账户」补到前面的对象上。"""
    if not objects:
        return objects
    last = objects[-1]
    for tail in ("账户", "公司"):
        if last.endswith(tail) and len(last) > len(tail):
            return [obj if obj.endswith(tail) else obj + tail for obj in objects[:-1]] + [last]
    return objects


def _join_prefix(prefix: str, obj: str) -> str:
    if prefix.endswith("开") and obj.startswith("开"):
        return prefix[:-1] + obj
    return prefix + obj


def _expand_prefix(parts: list[str], prefix: str) -> list[str]:
    particle = ""
    objects = [parts[0][len(prefix):]]
    for part in parts[1:]:
        if part.endswith("吗") or part.endswith("呢"):
            particle = part[-1]
            part = part[:-1]
        if part:
            objects.append(part)
    objects = _spread_noun_tail(objects)
    built = []
    for obj in objects:
        question = _join_prefix(prefix, obj)
        if particle and not question.endswith(particle):
            question += particle
        built.append(question)
    return built


def _expand_ending(parts: list[str]) -> list[str]:
    built = []
    for index, part in enumerate(parts):
        _head, ending = _ending_of(part)
        if len(ending) < 2:
            continue
        for prev in parts[:index]:
            if not _is_standalone(prev):
                built.append(prev + ending)
        break
    for part in parts:
        dangling = _is_standalone(part) and _bare_object(part)
        if _keep_raw(part) and not dangling:
            built.append(part)
    return built


def question_forms(raw: str) -> tuple[str, list[str], str]:
    """返回标题、相似问、以及上次导入的半句标题。"""
    parts = _parts(raw)
    legacy = _legacy_title(raw)
    if not parts:
        return "", [], legacy
    if len(parts) == 1 or all(_is_standalone(part) for part in parts):
        return parts[0], _dedupe(parts[1:], parts[0]), legacy
    title = "/".join(parts)
    prefix = _prefix_of(parts[0])
    if prefix and all(_bare_object(part) for part in parts[1:]):
        return title, _dedupe(_expand_prefix(parts, prefix), title), legacy
    return title, _dedupe(_expand_ending(parts), title), legacy


def parse_faq_text(text: str) -> list[dict]:
    """按 Q 编号切条。返回尚未分类的原始条目。"""
    section = ""
    items: list[dict] = []
    current: dict | None = None
    answer_lines: list[str] = []

    def finish() -> None:
        nonlocal current, answer_lines
        if current is None:
            return
        cleaned = []
        for line in answer_lines:
            row = line.strip()
            if row.startswith("A：") or row.startswith("A:"):
                row = row[2:].strip()
            if row:
                cleaned.append(row)
        current["answer"] = "\n".join(cleaned)
        items.append(current)
        current = None
        answer_lines = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        section_match = SECTION_RE.match(line)
        if section_match:
            finish()
            section = section_match.group(1)
            continue
        matched = Q_RE.match(line)
        if matched:
            finish()
            raw_question = matched.group(2).strip()
            title, similars, legacy = question_forms(raw_question)
            current = {
                "number": int(matched.group(1)),
                "section": section,
                "raw": raw_question,
                "parts": _parts(raw_question),
                "question": title,
                "similars": similars,
                "legacy": legacy,
                "answer": "",
            }
            continue
        if current is not None and line and not line.startswith("===") and not set(line) <= {"-"}:
            answer_lines.append(line)
    finish()
    return items


def _is_follow(question: str) -> bool:
    return any(word in question for word in FOLLOW_WORDS)


def _is_promo(primary: str) -> bool:
    return any(word in primary for word in PROMO_WORDS)


def _is_materials(answer: str) -> bool:
    return all(word in answer for word in ("公司名称", "经营范围", "注册资金", "股东"))


def _extra_category(question: str) -> str:
    for phrase, category in EXTRA_CATEGORIES:
        if phrase in question:
            return category
    return ""


def category_of(item: dict) -> str | None:
    """不入库时返回 None。"""
    section = item["section"]
    question = item["question"]
    raw = item.get("raw") or question
    answer = item["answer"]
    parts = item.get("parts") or _parts(raw)
    if section in SKIP_SECTIONS:
        return None
    if section in REPEAT_SECTIONS and not _extra_category(raw):
        return None
    if parts and all(part in GREETINGS for part in parts):
        return None
    if "能介绍一下" in raw and "想咨询什么" in answer:
        return None
    extra = _extra_category(raw)
    if extra:
        return extra
    if not question or not answer:
        return None
    if _is_follow(raw):
        return "跟进话术"
    primary = parts[0] if parts else question
    if _is_promo(primary):
        return "营销话术"
    if _is_materials(answer) and "开户主要" not in answer:
        return "资料"
    if section == "一":
        if any(word in raw for word in ("公司在哪", "香港的吗", "做几年", "做什么", "介绍")):
            return "注册"
        return "留资话术"
    if section == "三":
        return "开户"
    if section == "四":
        if "年审" in question and "审计" not in question.replace("年审", ""):
            return "年审"
        return "审计" if any(word in (question + answer) for word in ("审计", "报税", "记账", "利得税")) else "年审"
    if section == "五":
        return "地址"
    if section == "六":
        if any(word in question for word in ("商标", "牌照", "备案", "条码")):
            return "注册"
        return "身份"
    if section == "七":
        return "注册"
    if section == "十一":
        if any(word in question for word in BANK_WORDS):
            return "开户"
        if any(word in question for word in ("身份", "自雇", "续签")):
            return "身份"
        if any(word in question for word in ("税", "审计")):
            return "审计"
        return "注册"
    return "注册"


def records_from_text(text: str) -> list[dict]:
    """转成知识库记录。同一标准问只留答案更完整的一条。"""
    chosen: dict[str, dict] = {}
    for item in parse_faq_text(text):
        category = category_of(item)
        if not category or not item["question"] or not item["answer"]:
            continue
        record = {
            "title": item["question"][:256],
            "question": item["question"],
            "answer": item["answer"],
            "category": category,
            "similar_questions": item["similars"],
            "legacy_titles": [item["legacy"]] if item.get("legacy") else [],
        }
        current = chosen.get(record["title"])
        if current is None or len(record["answer"]) > len(current["answer"]):
            if current is not None:
                merged = [current["question"], *current["similar_questions"], *record["similar_questions"]]
                record["similar_questions"] = [
                    row for row in dict.fromkeys(merged)
                    if row and row != record["question"]
                ]
                record["legacy_titles"] = list(dict.fromkeys([*record["legacy_titles"], *current["legacy_titles"]]))
            chosen[record["title"]] = record
        else:
            current["similar_questions"] = list(dict.fromkeys([
                *current["similar_questions"], *record["similar_questions"],
            ]))
            current["legacy_titles"] = list(dict.fromkeys([*current["legacy_titles"], *record["legacy_titles"]]))
    return list(chosen.values())


def _same_answer(old_content: str, new_answer: str) -> bool:
    """现有答案和这次答案有一段相同，才允许把半句标题改成整句。"""
    old = (old_content or "").split("答：", 1)[-1].strip()
    new = (new_answer or "").strip()
    if not old or not new:
        return False
    if old == new or (len(new) >= 8 and new in old) or (len(old) >= 8 and old in new):
        return True
    return any(len(line.strip()) >= 8 and line.strip() in old for line in new.splitlines())


def _find_doc(db, model, title: str, legacy_titles: list[str], answer: str):
    exact = (
        db.query(model)
        .filter(model.doc_type == "faq", model.title == title, model.status != "archived")
        .order_by(model.id.asc())
        .first()
    )
    if exact is not None:
        return exact
    for legacy in legacy_titles:
        if not legacy or legacy == title:
            continue
        doc = (
            db.query(model)
            .filter(model.doc_type == "faq", model.title == legacy, model.status != "archived")
            .order_by(model.id.asc())
            .first()
        )
        if doc is not None and _same_answer(doc.content or "", answer):
            return doc
    return None


def apply_records(records: list[dict]) -> tuple[int, int]:
    """同一标题或上次的半句标题已存在则更新，并保持启用后重建索引。"""
    from app.database import SessionLocal, init_db
    from app.models import KnowledgeDoc
    from app.rag import ingest

    init_db()
    db = SessionLocal()
    created = 0
    updated = 0
    index_ids: list[int] = []
    try:
        for record in records:
            content = f"问：{record['question']}\n答：{record['answer']}"
            similars = json_similars(record["similar_questions"])
            doc = _find_doc(db, KnowledgeDoc, record["title"], record.get("legacy_titles") or [], record["answer"])
            if doc is None:
                doc = KnowledgeDoc(
                    title=record["title"],
                    doc_type="faq",
                    content=content,
                    category=record["category"][:64],
                    similar_questions=similars,
                    status="active",
                )
                db.add(doc)
                db.flush()
                created += 1
            else:
                doc.title = record["title"]
                doc.content = content
                doc.category = record["category"][:64]
                doc.similar_questions = similars
                doc.status = "active"
                doc.version = (doc.version or 1) + 1
                updated += 1
            index_ids.append(doc.id)
        db.commit()
    finally:
        db.close()
    asyncio.run(ingest.sync_doc_index(index_ids))
    return created, updated


def json_similars(items: list[str]) -> str:
    import json
    cleaned = []
    for item in items:
        text = item.strip()
        if text and text not in cleaned:
            cleaned.append(text)
    return json.dumps(cleaned, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="把整理版 FAQ 文本导入知识库")
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args(argv)
    text = args.input.read_text(encoding="utf-8")
    records = records_from_text(text)
    created, updated = apply_records(records)
    print(f"解析 {len(records)} 条，新建 {created}，更新 {updated}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
