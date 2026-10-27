"""发出前的答题置信度。

Context Precision 看召回段落是否在回答用户原话，Faithfulness 看草稿陈述能否从相关段落推断。
两个分由代码按 RAGAS / DeepEval 的公式计算，不读取精排分，也不读取生成模型自报的 confidence。
"""
import json
import logging
import re

from ..config import settings
from ..llm import get_llm_client, record_usage
from . import evidence

logger = logging.getLogger(__name__)

PASS_SCORE = 0.6
_LIST_LEAD = "您要的我都列下面啦"
_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+\S|[一二三四五六七八九十百]+、\s*\S|（[一二三四五六七八九十百]+）\s*\S|\([一二三四五六七八九十百]+\)\s*\S)"
)
_LIST_LINE_RE = re.compile(r"^(?:\d+\s*[.、．:：)）]|[①②③④⑤⑥⑦⑧⑨⑩])\s*\S")
_TOPIC_WORDS = ("开户", "注册", "年审", "面签", "退货", "审计", "报税", "记账")
_PHRASE_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_HIGH_RISK_RE = re.compile(
    r"资料|清单|多少钱|价格|费用|服务费|几天|多久|工作日|银行|能不能过|保不保证|"
    r"退款|赔偿|税务|身份规划|怎么弄|要准备|办理步骤|怎么办|发票|优惠|政策"
)
_SCOPE_ASK_RE = re.compile(r"做不做|你们做|有没有|做吗|能不能注册|能不能办|接不接|做哪些|做什么")
_SERVICES = (
    ("香港", "公司注册", "香港公司注册"),
    ("香港", "注册", "香港公司注册"),
    ("香港", "开户", "香港银行开户"),
    ("香港", "年审", "香港公司年审"),
    ("香港", "审计", "香港公司审计"),
    ("新加坡", "注册", "新加坡公司注册"),
    ("新加坡", "开户", "新加坡银行开户"),
    ("新加坡", "年审", "新加坡公司年审"),
)


def context_precision(verdicts: list[int]) -> float:
    """相关段落排在前面的程度。没有任何相关段时为 0。"""
    if not verdicts or not any(verdicts):
        return 0.0
    relevant = 0
    total = 0.0
    hits = 0
    for index, verdict in enumerate(verdicts, start=1):
        if verdict:
            hits += 1
            relevant += 1
            total += hits / index
    if relevant == 0:
        return 0.0
    return total / relevant


def heading_of(context: dict) -> str:
    """章节标题、FAQ 的问句，或资料名。"""
    for raw in (context.get("content") or "").splitlines():
        line = raw.strip()
        if not line or (line.startswith("【") and line.endswith("】")):
            continue
        if line.startswith("问：") or line.startswith("问:"):
            return line
        if _HEADING_RE.match(line):
            return line
    return (context.get("source") or "").strip()


def topic_key(heading: str) -> str:
    for word in _TOPIC_WORDS:
        if word in (heading or ""):
            return word
    return heading or ""


def relevant_topic_keys(user_text: str, contexts: list[dict], verdicts: list[int]) -> list[str]:
    keys: list[str] = []
    for context, verdict in zip(contexts, verdicts):
        if not verdict:
            continue
        key = topic_key(heading_of(context))
        if key and key not in keys:
            keys.append(key)
    return keys


def ambiguous_keys(user_text: str, contexts: list[dict], verdicts: list[int]) -> list[str]:
    """两个不同主题都相关，而用户没有把它们都点名。同一标题下的多块不算两件事。"""
    keys = relevant_topic_keys(user_text, contexts, verdicts)
    if len(keys) < 2:
        return []
    named = [key for key in keys if key and key in (user_text or "")]
    if len(named) >= len(keys):
        return []
    return keys


def diagnose(user_text: str, contexts: list[dict], verdicts: list[int]) -> str:
    """只做一个根因。ambiguous、noise、recall、ok。"""
    if ambiguous_keys(user_text, contexts, verdicts):
        return "ambiguous"
    if verdicts and any(verdicts) and not all(verdicts):
        return "noise"
    if not any(verdicts):
        return "recall"
    return "ok"


def clarify_line(keys: list[str]) -> str:
    shown = [key for key in keys if key][:2]
    if len(shown) >= 2:
        return f"您说的是{shown[0]}还是{shown[1]}呀"
    return "您具体想办哪一项呀"


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _statements(messages: list[str]) -> list[str]:
    lines: list[str] = []
    for message in messages or []:
        for raw in (message or "").splitlines():
            line = raw.strip()
            if not line or line == _LIST_LEAD:
                continue
            if evidence.is_handoff_guide(line):
                continue
            lines.append(line)
    return lines


_SIMILAR_LINE_RE = re.compile(r"^\s*相似[:：]")


def _chinese_windows(text: str, min_size: int = 4) -> list[str]:
    """回复侧至少连续 4 个字。两三个字撞上资料，不能把整句算成有依据。"""
    chars = re.findall(r"[\u4e00-\u9fff]", text or "")
    windows: list[str] = []
    for size in range(min_size, min(8, len(chars) + 1)):
        for index in range(len(chars) - size + 1):
            windows.append("".join(chars[index:index + size]))
    return windows


def _evidence_fact_in_statement(statement: str, blob: str) -> bool:
    """资料里的三字事实出现在回复中，例如「身份证」。两个字不算。"""
    compact_statement = _compact(statement)
    for run in re.findall(r"[\u4e00-\u9fff]+", blob or ""):
        if len(run) < 3:
            continue
        for size in range(3, min(len(run), 8) + 1):
            for index in range(len(run) - size + 1):
                if run[index:index + size] in compact_statement:
                    return True
    return False


def _scoring_blob(contexts: list[dict]) -> str:
    """相似问只用于检索，不作为忠实度证据。"""
    parts: list[str] = []
    for item in contexts or []:
        kept = [
            raw for raw in (item.get("content") or "").splitlines()
            if not _SIMILAR_LINE_RE.match(raw)
        ]
        parts.append("\n".join(kept))
    return "\n".join(parts)


def statement_supported(statement: str, blob: str) -> bool:
    """编号行的关键短语都要落在证据里。散文至少要有连续 4 个字对得上。"""
    if evidence.claims_unsupported([statement], blob):
        return False
    compact_statement = _compact(statement)
    compact_blob = _compact(blob)
    if compact_statement and compact_statement in compact_blob:
        return True
    phrases = [item for item in _PHRASE_RE.findall(compact_statement) if len(item) >= 2]
    if _LIST_LINE_RE.match((statement or "").strip()):
        required = [item for item in phrases if len(item) >= 4] or phrases
        return bool(required) and all(item in compact_blob for item in required)
    if any(item in compact_blob for item in _chinese_windows(compact_statement)):
        return True
    if _evidence_fact_in_statement(statement, blob):
        return True
    folded = evidence.fold_digits(statement or "")
    return evidence._AMOUNT.search(folded) is not None


def faithfulness(messages: list[str], contexts: list[dict]) -> float:
    """能被相关段落推断的陈述占比。没有相关段落或没有陈述时为 0。"""
    blob = _scoring_blob(contexts)
    statements = _statements(messages)
    if not contexts or not statements or not blob.strip():
        return 0.0
    supported = sum(1 for line in statements if statement_supported(line, blob))
    return supported / len(statements)


def supported_messages(messages: list[str], contexts: list[dict]) -> list[str]:
    """只留下能从相关段落推断的句子。"""
    blob = _scoring_blob(contexts)
    kept: list[str] = []
    for message in messages or []:
        lines = []
        for raw in (message or "").splitlines():
            line = raw.strip()
            if not line or line == _LIST_LEAD:
                continue
            if evidence.is_handoff_guide(line) or statement_supported(line, blob):
                lines.append(line)
        if lines:
            kept.append("\n".join(lines))
    return kept


def used_precision(verdicts: list[int]) -> float:
    """写进草稿的段落都相关时精度为 1。仍混着无关段时按原顺序重算。"""
    if not verdicts:
        return 0.0
    if all(verdicts):
        return 1.0
    return context_precision(verdicts)


def risk_tier(user_text: str) -> str:
    """高风险只表示在要不能编的事实。对不上这些词的话是接话，不再默认高风险。"""
    text = user_text or ""
    if _HIGH_RISK_RE.search(text):
        return "high"
    if _SCOPE_ASK_RE.search(text):
        return "medium"
    return "low"


def scope_reply(user_text: str) -> str | None:
    """业务范围里已经写了的「做不做」。对不上就不编。"""
    text = user_text or ""
    if not _SCOPE_ASK_RE.search(text):
        return None
    labels: list[str] = []
    for region, key, label in _SERVICES:
        if key in text and region in text and label not in labels:
            labels.append(label)
    if not labels:
        if "做什么" in text or "做哪些" in text:
            return "香港公司注册、银行开户、年审这些我们都接"
        return None
    return f"做的呀，{'、'.join(labels)}我们接"


def answer_confidence_payload(*, faithfulness_score: float, precision: float, cause: str,
                              retried: bool, raw_precision: float | None = None) -> dict:
    score = min(faithfulness_score, precision)
    return {
        "faithfulness": round(faithfulness_score, 4),
        "context_precision": round(precision, 4),
        "score": round(score, 4),
        "cause": cause,
        "retried": retried,
        "raw_context_precision": None if raw_precision is None else round(raw_precision, 4),
    }


def passed(faithfulness_score: float, precision: float) -> bool:
    return faithfulness_score >= PASS_SCORE and precision >= PASS_SCORE


async def judge_relevance(user_text: str, contexts: list[dict], *, timeout: float) -> list[int] | None:
    """每一段是否能用来回答用户原话。失败返回 None，不根据精排分填 1。"""
    if not contexts:
        return []
    if not settings.llm_configured:
        return None
    blocks = []
    for index, context in enumerate(contexts, start=1):
        content = (context.get("content") or "").strip()
        blocks.append(f"[{index}] {heading_of(context)}\n{content[:500]}")
    prompt = (
        "判断每一段资料能不能用来回答用户原话。只看这一问，不要看检索分数，也不要因为资料里出现了相同的词就判成能用。\n"
        "能直接回答记 1，答的是另一件事记 0。\n"
        "只输出 JSON：{\"verdicts\":[1,0]}\n"
        "verdicts 的长度必须等于资料段数，按顺序对应。\n\n"
        f"用户原话：{user_text}\n\n资料：\n" + "\n\n".join(blocks)
    )
    try:
        client = get_llm_client(settings.llm_base_url, settings.llm_api_key)
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=120,
            response_format={"type": "json_object"},
            timeout=max(1.0, timeout),
        )
        record_usage(0, settings.llm_model, getattr(resp, "usage", None))
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:  # noqa: BLE001
        logger.exception("上下文相关性判定失败")
        return None
    raw = data.get("verdicts")
    if not isinstance(raw, list) or len(raw) != len(contexts):
        return None
    verdicts = []
    for item in raw:
        if item in (0, 1):
            verdicts.append(int(item))
        elif item is True:
            verdicts.append(1)
        elif item is False:
            verdicts.append(0)
        else:
            return None
    return verdicts
