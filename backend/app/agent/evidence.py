"""发出前的证据核对。事实只能来自检索正文、本次工具结果或人工原话。"""
import re

_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(元|块|折|件|%|天|小时|个月|月|年)")
_LOGISTICS = re.compile(r"中通|圆通|顺丰|韵达|申通|极兔|转运中心|[A-Za-z]{2,}\d{6,}")
_MODEL = re.compile(r"[A-Za-z]{1,6}-?\d{2,}")
_TICKET_NO = re.compile(r"T\d{8}-[A-Za-z0-9]{4,}")
_PAIRS = (
    ("不支持", "支持"),
    ("不包邮", "包邮"),
    ("不能退", "能退"),
    ("不可退", "可退"),
    ("不可以", "可以"),
)
_POLICY = (
    ("不包邮", "包邮"),
    ("不能退", "能退"),
    ("不可退", "可退"),
    ("不支持开发票", "开发票"),
    ("不能换", "能换"),
)
_CN_DIGIT = {
    "零": "0", "〇": "0", "一": "1", "二": "2", "两": "2", "三": "3",
    "四": "4", "五": "5", "六": "6", "七": "7", "八": "8", "九": "9",
}
_TEN = re.compile(r"([一二两三四五六七八九])?十([一二两三四五六七八九])?")
_HANDOFF_GUIDE = re.compile(r"同事|帮您确认|帮你确认|不敢乱说|我再问问|确认一下")


def evidence_text(contexts: list[dict], tool_results: list[dict], agent_lines: list[str]) -> str:
    parts = [item.get("content") or "" for item in contexts]
    parts.extend(str(item) for item in tool_results)
    parts.extend(agent_lines)
    return "\n".join(parts)


def fold_digits(text: str) -> str:
    """把中文数字收成阿拉伯数字，让「七天」能对上「7天」。"""
    def ten(match: re.Match) -> str:
        left = _CN_DIGIT.get(match.group(1) or "", "")
        right = _CN_DIGIT.get(match.group(2) or "", "")
        if left and right:
            return str(int(left) * 10 + int(right))
        if left:
            return str(int(left) * 10)
        if right:
            return str(10 + int(right))
        return "10"

    folded = _TEN.sub(ten, text or "")
    return "".join(_CN_DIGIT.get(ch, ch) for ch in folded)


def is_handoff_guide(message: str) -> bool:
    """转人工说明可以提到缺口，但不能顺手报一个证据里没有的数字。"""
    if not _HANDOFF_GUIDE.search(message or ""):
        return False
    folded = fold_digits(message)
    return _AMOUNT.search(folded) is None and _LOGISTICS.search(message or "") is None


def conflicting_units(contexts: list[dict]) -> set[str]:
    """同一单位出现两个不同数字时，这一问不能选边。"""
    found: dict[str, set[str]] = {}
    for item in contexts or []:
        for match in _AMOUNT.finditer(fold_digits(item.get("content") or "")):
            found.setdefault(match.group(2), set()).add(match.group(1))
    return {unit for unit, numbers in found.items() if len(numbers) > 1}


def _model_tokens(message: str) -> list[str]:
    """型号扫描前先剔掉工单号，工单号不算型号。"""
    return _MODEL.findall(_TICKET_NO.sub(" ", message or ""))


def claims_unsupported(messages: list[str], evidence: str) -> bool:
    """回复里的数字、物流专名或相反政策在证据里对不上。"""
    raw = evidence or ""
    blob = fold_digits(raw)
    for message in messages:
        if is_handoff_guide(message):
            continue
        folded = fold_digits(message or "")
        for match in _AMOUNT.finditer(folded):
            number = match.group(1)
            if re.search(rf"(?<!\d){re.escape(number)}(?!\d)", blob) is None:
                return True
        for token in _LOGISTICS.findall(message or ""):
            if token not in raw:
                return True
        for token in _model_tokens(message or ""):
            if token not in raw and token not in blob:
                return True
        if _polarity_conflict(message or "", raw):
            return True
        if _policy_missing(message or "", raw):
            return True
    return False


def partition_messages(messages: list[str], evidence: str,
                       contexts: list[dict] | None = None) -> tuple[list[str], list[str]]:
    """留下对得上的气泡，拿掉对不上的。转人工说明单独保留。"""
    units = conflicting_units(contexts or [])
    kept: list[str] = []
    dropped: list[str] = []
    for message in messages:
        if is_handoff_guide(message):
            kept.append(message)
            continue
        if units and _picks_one_conflicting_number(message, contexts or [], units):
            dropped.append(message)
            continue
        if claims_unsupported([message], evidence):
            dropped.append(message)
        else:
            kept.append(message)
    return kept, dropped


def _uses_units(message: str, units: set[str]) -> bool:
    folded = fold_digits(message)
    return any(match.group(2) in units for match in _AMOUNT.finditer(folded))


def _picks_one_conflicting_number(message: str, contexts: list[dict], units: set[str]) -> bool:
    """同一单位有两个数字时，只报其中一个算选边。原文把两个数字都写上则保留。"""
    folded_msg = fold_digits(message)
    if not _uses_units(message, units):
        return False
    folded_src = fold_digits("\n".join(item.get("content") or "" for item in contexts))
    for unit in units:
        source_numbers = set(re.findall(rf"(\d+(?:\.\d+)?)\s*{re.escape(unit)}", folded_src))
        message_numbers = set(re.findall(rf"(\d+(?:\.\d+)?)\s*{re.escape(unit)}", folded_msg))
        if message_numbers and not source_numbers <= message_numbers:
            return True
    return False


def _policy_missing(reply: str, evidence: str) -> bool:
    """回复把包邮、退换、开发票说死，但证据里根本没写。"""
    for negative, positive in _POLICY:
        reply_pos = positive in reply.replace(negative, "")
        reply_neg = negative in reply
        evidence_pos = positive in evidence.replace(negative, "")
        evidence_neg = negative in evidence
        if reply_pos and not evidence_pos and not evidence_neg:
            return True
        if reply_neg and not evidence_neg and not evidence_pos:
            return True
    return False


def _polarity_conflict(reply: str, evidence: str) -> bool:
    for negative, positive in _PAIRS:
        reply_pos = positive in reply.replace(negative, "")
        reply_neg = negative in reply
        evidence_pos = positive in evidence.replace(negative, "")
        evidence_neg = negative in evidence
        if reply_pos and evidence_neg and not evidence_pos:
            return True
        if reply_neg and evidence_pos and not evidence_neg:
            return True
    return False
