"""把美洽历史对话洗成可发送的咨询资料，不写入知识库。

有原话就留原话，没有事实就配留资原句。投诉、退款、点名要真人标为转人工。
不改写固定答案，也不改现有 FAQ 导入。

用法（在仓库根目录）::

    python backend/scripts/clean_history_to_kb.py --input 历史对话.xlsx --output-dir backend/data/dialog_clean
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import logging
import re
import zipfile
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger("clean_history")

SHEET_NAME = "历史对话"
SIMILARITY = 0.88
FOLLOW_GAP_SECONDS = 5 * 60
OUTPUT_FILES = (
    "business_knowledge.jsonl",
    "lead_capture.jsonl",
    "marketing_activity.jsonl",
    "follow_up.jsonl",
    "consult_paths.jsonl",
    "answer_review.jsonl",
    "clean_turns.csv",
    "stats.json",
)

MSG_RE = re.compile(
    r"^(?P<who>.+?)\s+(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*(?P<body>.*)$"
)
PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
QQ_RE = re.compile(r"(?:QQ|qq|扣扣)\s*[:：]?\s*(\d{5,12})")
WECHAT_LABELED_RE = re.compile(
    r"(?:微信|wx|vx|VX|V信|v信)\s*[:：是]?\s*([A-Za-z][A-Za-z0-9_-]{4,})"
)
WECHAT_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{5,19}$")
URL_RE = re.compile(r"https?://\S+|www\.\S+")
VIDEO_RE = re.compile(r"video/\S+")
EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "]+",
    flags=re.UNICODE,
)
PUNCT_RE = re.compile(r"[\s，,。！？?、~～；;：:（）()【】\[\]“”\"'!！·…—\-]+")
LIST_LINE_RE = re.compile(r"^\d+\s*[:：.、．]")
LEAD_SPLIT_RE = re.compile(r"(?=您留|您需不需要留|留个|方便留|您看您方便|您方便留|您考虑)")
HYPE_WORDS = (
    "做了10年", "做了快10年", "做了9年", "做了快9年", "200号人", "200人",
    "快速下户", "一手渠道", "一手代办", "本地服务商", "很多优势", "很多的优势",
    "实地考察", "2800", "4430",
)
GRADE_WORDS = ("不计算", "捣乱", "刷屏", "同行", "推广")
ASK_FULL = "您这边联系方式多少呢，我们专员给您讲解一下办理的流程、资料和费用，让您参考了解下"
ASK_PREFIX = "您这边联系方式多少呢，我们专员给您讲解一下"
ARRANGE = "好的，这就给您安排专员，请稍等1-3分钟"
BANK_TYPE = "您好，您这边是想开公司户还是个人户呢？"
SCOPE_ALL = "香港公司注册、银行开户、年审这些我们都接"
PROMPT_HANDOFF = ("开发票", "身份规划", "税务筹划", "资产配置", "房产投资")
HANDOFF_WORDS = ("投诉", "辱骂", "退款", "赔偿", "转人工", "找真人", "人工客服", "哪个党")

BUSINESS_CATEGORIES = ("注册", "开户", "审计", "年审", "费用", "资料", "流程")
CAT_LEAD = "留资话术"
CAT_MARKET = "营销话术"
CAT_FOLLOW = "跟进话术"
MANAGED_CATEGORIES = BUSINESS_CATEGORIES + (CAT_LEAD, CAT_MARKET, CAT_FOLLOW)
CONSULT_MATRIX = []
for _biz, _facets in (
    ("注册", ("费用", "资料", "流程", "时效", "远程办理", "做不做")),
    ("开户", ("费用", "资料", "流程", "时效", "远程办理", "银行", "个人户或公司户", "做不做")),
    ("年审", ("费用", "资料", "流程", "时效", "远程办理", "做不做")),
    ("审计", ("费用", "资料", "流程", "时效", "远程办理", "做不做")),
    ("身份", ("费用", "资料", "流程", "时效", "远程办理")),
    ("新加坡", ("费用", "资料")),
    ("内地公司", ("费用", "资料")),
    ("税务", ("费用", "资料")),
    ("地址", ("费用",)),
    ("资质许可", ("做不做",)),
    ("商标", ("做不做",)),
):
    for _facet in _facets:
        CONSULT_MATRIX.append((_biz, _facet))

SCOPE_PATHS = (
    ("注册", "做不做", "你们做香港公司注册吗", "做的呀，香港公司注册我们接"),
    ("开户", "做不做", "有没有香港开户", "做的呀，香港银行开户我们接"),
    ("年审", "做不做", "香港年审做不做", "做的呀，香港公司年审我们接"),
    ("审计", "做不做", "香港审计做不做", "做的呀，香港公司审计我们接"),
    ("新加坡", "做不做", "新加坡公司注册做不做", "做的呀，新加坡公司注册我们接"),
    ("其他", "做不做", "你们做什么", SCOPE_ALL),
)


def norm_key(text: str) -> str:
    return PUNCT_RE.sub("", text or "").lower()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def similar(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def parse_time(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def mask_customer(text: str) -> str:
    """只脱敏顾客已经写下的号码和账号。"""
    raw = text or ""

    def wechat(match: re.Match) -> str:
        token = match.group(1)
        if token.lower() in {"hello", "wechat"}:
            return match.group(0)
        return "[微信]"

    raw = PHONE_RE.sub("[手机号]", raw)
    raw = EMAIL_RE.sub("[邮箱]", raw)
    raw = QQ_RE.sub("[QQ]", raw)
    raw = WECHAT_LABELED_RE.sub(wechat, raw)
    compact = re.sub(r"\s+", "", raw)
    if WECHAT_TOKEN_RE.match(compact) and compact.lower() not in {"hello", "wechat"}:
        return "[微信]"
    return raw


def strip_links(text: str) -> str:
    cleaned = URL_RE.sub("", text or "")
    cleaned = VIDEO_RE.sub("", cleaned)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()


def noise_reason(text: str) -> str | None:
    """整句噪声才丢。夹着实质内容的句子由调用方先去掉链接。"""
    raw = (text or "").strip()
    if not raw:
        return "empty"
    if any(word in raw for word in ("收到一条", "暂不支持", "抖音视频", "未知消息", "其他类型消息")):
        return "system"
    lowered = raw.lower()
    if "redbook" in lowered or "message_type" in lowered or "小红书卡片" in raw or "留资卡" in raw:
        return "card"
    if raw.startswith("{") or (raw.startswith("[") and not any(
        token in raw for token in ("[手机号]", "[微信]", "[QQ]", "[邮箱]")
    )):
        return "card"
    if VIDEO_RE.search(raw) and not strip_links(raw):
        return "video"
    if URL_RE.search(raw) and not strip_links(raw):
        return "link"
    no_emoji = EMOJI_RE.sub("", raw).replace("🌹", "").strip()
    if not no_emoji:
        return "emoji"
    if re.fullmatch(r"[\W_]+", no_emoji, flags=re.UNICODE):
        return "symbol"
    compact = norm_key(no_emoji)
    if compact in {"好的", "请稍等", "好", "嗯", "哦", "你好", "您好", "在吗", "在么", "1"}:
        return "ack"
    if len(compact) <= 1:
        return "single"
    return None


def split_raw_lines(content: str) -> list[str]:
    text = html.unescape(content or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    return [line.strip() for line in text.split("\n") if line.strip()]


def parse_messages(content: str) -> list[dict]:
    """按角色和时间切开。没有时间戳的续行并进上一条。"""
    messages: list[dict] = []
    for line in split_raw_lines(content):
        matched = MSG_RE.match(line)
        if matched:
            speaker = matched.group("who").strip()
            role = "customer" if speaker == "顾客" else "agent"
            body = matched.group("body").strip()
            messages.append({
                "role": role,
                "speaker": speaker,
                "time": parse_time(matched.group("ts")),
                "time_text": matched.group("ts"),
                "text": body,
            })
        elif messages:
            messages[-1]["text"] = (messages[-1]["text"] + "\n" + line).strip()
    return messages


def is_hype(text: str) -> bool:
    return any(word in (text or "") for word in HYPE_WORDS)


def strip_hype(text: str) -> str:
    """宣传分句从知识里去掉。带活动价或清单事实的句子保留。"""
    kept: list[str] = []
    fact_markers = ("3980", "10周年", "活动价", "公司名称", "汇丰", "渣打", "下证", "官费", "注册资金", "经营范围")
    for part in re.split(r"[。！？\n]+", text or ""):
        part = part.strip("，,。 ")
        if not part:
            continue
        if any(word in part for word in HYPE_WORDS) and not any(marker in part for marker in fact_markers):
            continue
        kept.append(part)
    return "\n".join(kept).strip()


def is_materials(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    keys = ("公司名称", "经营范围", "注册资金", "股东")
    return all(key in compact for key in keys)


def is_marketing(text: str) -> bool:
    return any(word in (text or "") for word in ("10周年", "3980", "最后一天", "活动名额", "优惠名额", "活动价", "预留一个优惠", "预留优惠"))


def is_lead(text: str) -> bool:
    raw = text or ""
    if is_materials(raw):
        return False
    if any(word in raw for word in (
        "联系方式", "微信多少", "微信号", "加微信", "加下vx", "加vx", "加wx",
        "留个", "留一下", "二维码", "您微信", "电话多少", "安排专员", "电话或者",
        "V多少", "v多少", "通过下", "加您", "专员", "业务经理", "您加了", "方便电话",
        "微多少", "加我", "通过一下", "稍等两分钟", "稍等1", "稍等 1",
        "号码是多少", "方便联系", "联系的号码", "号码多少", "联系号码",
        "先了解", "参考了解", "发您", "加下微信",
    )):
        return True
    return bool(re.search(r"加下?\s*[vVwW][xX]", raw))


def is_weak_fact(text: str) -> bool:
    """留资句拆完后剩下的客套，不能当知识。"""
    compact = norm_key(text)
    if not compact:
        return True
    if compact in {
        "让您参考了解下", "您可以先了解下", "先了解下", "参考了解下", "您先了解下",
        "详细了解下", "您可以先接收参考下", "给您讲解一下",
    }:
        return True
    if len(compact) <= 16 and any(word in (text or "") for word in ("了解下", "讲解一下", "参考一下", "参考下")):
        return True
    return False


def is_contact_only_question(text: str) -> bool:
    """脱敏后只剩号码、微信或加我，不是咨询。"""
    raw = text or ""
    for token in ("[手机号]", "[微信]", "[QQ]", "[邮箱]"):
        raw = raw.replace(token, "")
    raw = re.sub(r"加我|微信|vx|VX|电话|微", "", raw)
    return len(norm_key(raw)) < 2


def has_hard_fact(text: str) -> bool:
    raw = text or ""
    if is_materials(raw):
        return True
    if any(word in raw for word in ("3980", "10周年", "活动价", "汇丰", "渣打", "中银", "东亚", "最后一天", "优惠名额")):
        return True
    if any(word in raw for word in ("公司名称", "经营范围", "注册资金", "股东")):
        return True
    return re.search(r"\d+\s*天|\d+个工作日|\d+元", raw) is not None


def is_clarifying_answer(text: str) -> bool:
    """客服反问或开场菜单，没有可引用的事实。"""
    raw = text or ""
    if has_hard_fact(raw) and "想咨询什么" not in raw and "注册还是开户" not in raw:
        return False
    return any(phrase in raw for phrase in (
        "想咨询什么", "咨询什么呢", "注册还是开户", "了解注册还是", "您是想咨询哪个",
        "需要了解注册还是", "想了解注册公司还是", "开户审计那些",
    ))


def is_opening_menu(question: str, answer: str) -> bool:
    folded = norm_key(question)
    if "介绍" in folded and "服务" in folded and is_clarifying_answer(answer):
        return True
    return "可以提供注册" in (answer or "") and "想咨询什么" in (answer or "")


def is_promo_answer(text: str) -> bool:
    raw = text or ""
    if is_materials(raw):
        return False
    return any(word in raw for word in ("3980", "10周年", "最后一天", "优惠名额", "活动价"))


def knowledge_category(business: str, facet: str, answer: str) -> str | None:
    """业务知识只留七类。活动价不进业务知识。"""
    if is_promo_answer(answer) or is_clarifying_answer(answer):
        return None
    if facet in ("费用", "资料", "流程"):
        return facet
    if business in BUSINESS_CATEGORIES:
        return business
    if facet in ("银行", "个人户或公司户"):
        return "开户"
    if "年审" in (answer or ""):
        return "年审"
    if "审计" in (answer or ""):
        return "审计"
    return None


def standard_question(category: str, answer: str) -> str:
    """不用顾客整段原话。按事实类型收成一句标准问。"""
    if category == "资料":
        if "开户" in (answer or "") and "注册香港公司主要涉及" not in (answer or ""):
            return "开户需要什么资料"
        return "注册需要什么资料"
    return {
        "费用": "办理费用怎么算",
        "流程": "办理流程是怎样的",
        "注册": "香港公司注册怎么办",
        "开户": "香港银行开户怎么办",
        "年审": "香港公司年审怎么办",
        "审计": "香港公司审计怎么办",
    }.get(category, category)


def factual_clauses(text: str) -> str:
    """同一句里先留下事实，联系方式分句另走留资。"""
    if is_materials(text):
        return text
    kept = []
    for part in re.split(r"[，,。！？；;\n]+", text or ""):
        part = part.strip("，,。 ")
        if not part or is_lead(part) or is_hype(part) or is_weak_fact(part):
            continue
        kept.append(part)
    return "，".join(kept).strip()


def is_arrange(text: str) -> bool:
    raw = text or ""
    return "安排专员" in raw and "请稍等" in raw


def is_bank_type(text: str) -> bool:
    raw = text or ""
    return "公司户" in raw and "个人" in raw and ("还是" in raw or "或者" in raw)


def is_follow(text: str, gap_seconds: float | None) -> bool:
    raw = text or ""
    if any(word in raw for word in ("还在线", "在吗", "在么", "在嘛")):
        return True
    return gap_seconds is not None and gap_seconds >= FOLLOW_GAP_SECONDS


def handoff_kind(text: str) -> str | None:
    raw = text or ""
    if any(word in raw for word in ("投诉", "辱骂", "骗子")):
        return "投诉"
    if "退款" in raw:
        return "退款"
    if "赔偿" in raw:
        return "赔偿"
    if any(word in raw for word in ("转人工", "找真人", "人工客服", "真人客服")):
        return "要真人"
    if any(word in raw for word in ("哪个党", "政治", "宗教")):
        return "敏感"
    return None


def prompt_handoff_kind(text: str) -> str | None:
    raw = text or ""
    for word in PROMPT_HANDOFF:
        if word in raw:
            return word
    if "开发票" in raw or "发票" in raw:
        return "开发票"
    return None


def split_clauses(text: str) -> list[str]:
    """按句号切开，编号清单保持整段。"""
    lines = (text or "").split("\n")
    if any(LIST_LINE_RE.match(line.strip()) for line in lines):
        return [text.strip()]
    parts = re.split(r"(?<=[。！？])", text or "")
    clauses = [part.strip() for part in parts if part.strip()]
    return clauses or ([text.strip()] if text.strip() else [])


def split_lead_tail(text: str) -> tuple[str, str]:
    parts = LEAD_SPLIT_RE.split(text or "", maxsplit=1)
    if len(parts) == 1:
        return text.strip(), ""
    head = parts[0].strip("，,。！! ")
    tail = "".join(parts[1:]).strip()
    return head, tail


def facets_of(text: str) -> list[str]:
    raw = text or ""
    found: list[str] = []

    def add(name: str) -> None:
        if name not in found:
            found.append(name)

    if any(word in raw for word in ("多少钱", "怎么收费", "如何收费", "什么价格", "费用", "收费", "报价", "活动价", "多少费用")):
        add("费用")
    if any(word in raw for word in ("资料", "材料", "条件", "需要什么", "准备什么")):
        add("资料")
    if any(word in raw for word in ("流程", "怎么办理", "如何办理", "怎么注册", "如何注册", "怎么办", "怎么弄")):
        add("流程")
    if any(word in raw for word in ("多久", "几天", "多长时间", "周期", "下证", "下户")):
        add("时效")
    if any(word in raw for word in ("哪些银行", "什么银行", "汇丰", "渣打", "中银")):
        add("银行")
    if any(word in raw for word in ("不到香港", "不用去香港", "不用到香港", "人不到", "不用本人", "远程")):
        add("远程办理")
    if "个人户" in raw or "公司户" in raw:
        add("个人户或公司户")
    if any(word in raw for word in ("做不做", "你们做", "有没有", "做吗", "能不能办", "接不接", "做哪些", "做什么")):
        add("做不做")
    return found


def business_of(question: str, answer: str, topic: str) -> str:
    blob = f"{question}\n{answer}"
    question_only = question or ""
    if "新加坡" in blob:
        return "新加坡"
    if any(word in blob for word in ("身份续签", "身份规划", "续签")):
        return "身份"
    if any(word in blob for word in ("食品经营", "卫生许可", "道路运输", "医疗器械", "进出口许可", "劳务派遣", "ODI", "ICP", "牌照")):
        return "资质许可"
    if "商标" in question_only or ("商标" in blob and "注册香港" not in blob):
        return "商标"
    if any(word in blob for word in ("代理记账", "小规模", "一般纳税人", "税务异常", "税务筹划", "报税")):
        return "税务"
    if any(word in question_only for word in ("有限公司", "外资公司", "股份公司", "分公司")) and "香港" not in question_only:
        return "内地公司"
    if "审计" in question_only and "年审" not in question_only.replace("审计", ""):
        return "审计"
    if "年审" in question_only or (topic == "年审" and "年审" in blob):
        return "年审"
    if any(word in question_only for word in ("开户", "对公", "公户", "银行账户", "银行开户")):
        return "开户"
    if any(word in (answer or "") for word in ("汇丰", "渣打")) and "注册香港公司主要涉及" not in (answer or ""):
        return "开户"
    if any(word in question_only for word in ("地址挂靠", "秘书地址")):
        return "地址"
    if any(word in blob for word in ("注册", "香港公司", "3980", "活动价")):
        return "注册"
    if topic:
        return topic
    return "其他"


def rewrite_question(text: str, topic: str) -> str:
    raw = (text or "").strip()
    if re.search(r"那年审|年审呢", raw):
        return "香港公司年审呢"
    if raw in {"年审", "那年审"}:
        return "香港公司年审呢"
    if topic == "开户" and raw in {"多久", "多少钱", "怎么收费"}:
        return f"开户{raw}"
    return raw


def default_close(business: str) -> str:
    if business in {"注册", "开户", "年审", "审计", "身份", "地址", "新加坡"}:
        return f"{ASK_PREFIX}{business if business != '注册' else '注册香港公司'}"
    if business and business != "其他":
        return f"{ASK_PREFIX}{business}"
    return ASK_FULL


def action_of(text: str) -> str:
    raw = text or ""
    if "二维码" in raw:
        return "发二维码"
    if "安排专员" in raw:
        return "安排专员"
    if re.search(r"微信|vx|VX|wx|WX", raw):
        return "加微信"
    return "留电话"


def trigger_of(question: str, *, follow: bool, refused: bool, left_contact: bool) -> tuple[str, bool]:
    if follow:
        return "客户沉默", False
    if refused:
        return "客户拒绝电话", False
    if left_contact:
        return "客户已留电话", False
    facets = facets_of(question)
    if "费用" in facets:
        return "客户问价", False
    if "流程" in facets or "资料" in facets:
        return "客户问流程", False
    if any(word in (question or "") for word in ("注册", "开户", "年审", "审计", "办理")):
        return "其他", True
    return "其他", True


def stage_of(*, follow: bool, left_contact: bool, quoted: bool, first: bool) -> str:
    if follow:
        return "沉默唤醒"
    if left_contact:
        return "客户已留电话"
    if quoted:
        return "已报价"
    if first:
        return "首次响应"
    return "客户未留资"


def customer_left_contact(text: str) -> bool:
    raw = text or ""
    return "[手机号]" in raw or "[微信]" in raw or "[QQ]" in raw or "[邮箱]" in raw


def customer_refused_phone(text: str) -> bool:
    return any(word in (text or "") for word in ("不方便电话", "别打电话", "不要电话", "不接电话", "别打给我"))


def filter_reason(row: dict) -> str | None:
    valid = str(row.get("有效对话") or "").strip()
    missed = str(row.get("遗漏对话") or "").strip()
    if valid != "有效":
        return "invalid"
    if missed != "未遗漏":
        return "missed"
    grade = str(row.get("对话分级") or "")
    biz = str(row.get("业务大类") or "")
    for word in GRADE_WORDS:
        if word in grade or word in biz:
            return "grade"
    if not str(row.get("对话内容") or "").strip():
        return "empty"
    return None


def channel_of(row: dict) -> str:
    source = str(row.get("来源渠道") or "").strip()
    if source and source.lower() != "nan":
        return source
    dialog = str(row.get("对话渠道") or "").strip()
    if dialog.lower() == "nan":
        return ""
    return dialog


def _cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() == "nan":
        return ""
    return text


def df_to_rows(frame) -> list[dict]:
    rows = []
    for record in frame.to_dict(orient="records"):
        rows.append({key: _cell(value) for key, value in record.items()})
    return rows


def _rows_from_sheet_xml(data: bytes) -> list[dict]:
    row_re = re.compile(br"<row r=\"(\d+)\">(.*?)</row>", re.S)
    cell_re = re.compile(br"<c r=\"([A-Z]+)(\d+)\"[^>]*>(?:<v>(.*?)</v>)?</c>", re.S)
    header: dict[str, str] = {}
    rows: list[dict] = []
    for match in row_re.finditer(data):
        index = int(match.group(1))
        cells: dict[str, str] = {}
        for cell in cell_re.finditer(match.group(2)):
            column = cell.group(1).decode()
            value = html.unescape((cell.group(3) or b"").decode("utf-8", "replace"))
            cells[column] = value
        if index == 1:
            header = {column: name for column, name in cells.items() if name}
            continue
        rows.append({header[column]: value for column, value in cells.items() if column in header})
    return rows


def load_rows_xml(path: Path) -> list[dict]:
    """美洽导出使用非标准 t=str，pandas 空列时走这里。"""
    with zipfile.ZipFile(path) as archive:
        sheet_name = "xl/worksheets/sheet1.xml"
        try:
            workbook = archive.read("xl/workbook.xml").decode("utf-8")
        except KeyError:
            workbook = ""
        if workbook and SHEET_NAME not in workbook and "历史对话" not in html.unescape(workbook):
            logger.warning("workbook 未直接标出历史对话，仍读第一张表")
        data = archive.read(sheet_name)
    return _rows_from_sheet_xml(data)


def rows_from_xlsx_bytes(blob: bytes) -> list[dict]:
    """从上传的 xlsx 字节读历史对话。pandas 失败时改读表 XML。"""
    try:
        import pandas as pd
        frame = pd.read_excel(io.BytesIO(blob), sheet_name=SHEET_NAME, dtype=str, engine="openpyxl")
        rows = df_to_rows(frame)
        if rows and any(row.get("对话内容") for row in rows):
            return rows
        logger.warning("pandas 读到的对话内容为空，改用 XML")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pandas 读取失败，改用 XML：%s", exc)
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        name = "xl/worksheets/sheet1.xml"
        if name not in archive.namelist():
            sheets = [item for item in archive.namelist() if item.startswith("xl/worksheets/sheet")]
            if not sheets:
                raise ValueError("不是可解析的历史对话表")
            name = sheets[0]
        data = archive.read(name)
    return _rows_from_sheet_xml(data)


def unanswered_questions(result: dict) -> list[str]:
    """顾客问过、但没有可入库答案的原句。"""
    answered = {(item.get("question") or "").strip() for item in result["business_knowledge"]}
    gaps: list[str] = []
    for item in result["lead_capture"] + result["follow_up"] + result["marketing_activity"]:
        question = (item.get("context") or "").strip()
        if question and question not in answered and question not in gaps:
            gaps.append(question)
    return gaps


def load_rows(path: Path) -> list[dict]:
    try:
        import pandas as pd
        frame = pd.read_excel(path, sheet_name=SHEET_NAME, dtype=str, engine="openpyxl")
        rows = df_to_rows(frame)
        content = [row.get("对话内容", "") for row in rows]
        if rows and any(content):
            return rows
        logger.warning("pandas 读到的对话内容为空，改用 XML")
    except Exception as exc:  # noqa: BLE001
        logger.warning("pandas 读取失败，改用 XML：%s", exc)
    return load_rows_xml(path)


def _prepare_message(message: dict, role_keep: str) -> tuple[dict | None, str | None]:
    text = message["text"]
    if role_keep == "customer":
        text = mask_customer(text)
    else:
        text = strip_links(text) if URL_RE.search(text) or VIDEO_RE.search(text) else text
    reason = noise_reason(text)
    if reason:
        return None, reason
    copied = dict(message)
    copied["text"] = text.strip()
    return copied, None


def _gap_seconds(previous: dict | None, current: dict) -> float | None:
    if previous is None or previous.get("time") is None or current.get("time") is None:
        return None
    return (current["time"] - previous["time"]).total_seconds()


def extract_dialogue(row: dict) -> dict:
    """从一段对话抽出知识、留资、营销、跟进和逐条消息。"""
    dialogue_id = _cell(row.get("对话ID"))
    visitor_id = _cell(row.get("访客ID"))
    created = _cell(row.get("对话创建时间"))
    agent_column = _cell(row.get("客服昵称"))
    channel = channel_of(row)
    parsed = parse_messages(_cell(row.get("对话内容")))
    kept: list[dict] = []
    drop_reasons: Counter = Counter()
    for message in parsed:
        prepared, reason = _prepare_message(message, message["role"])
        if prepared is None:
            drop_reasons[reason or "noise"] += 1
            continue
        kept.append(prepared)

    knowledge: list[dict] = []
    leads: list[dict] = []
    marketing: list[dict] = []
    followups: list[dict] = []
    turns: list[dict] = []
    handoff_hits: list[str] = []
    prompt_hits: list[str] = []
    topic = ""
    quoted = False
    first_agent = True
    pending_question = ""
    pending_raw = ""
    seq = 0

    def add_turn(message: dict, labels: list[str]) -> None:
        nonlocal seq
        seq += 1
        turns.append({
            "对话ID": dialogue_id,
            "序号": seq,
            "角色": message["role"],
            "昵称": message["speaker"],
            "时间": message.get("time_text") or "",
            "正文": message["text"],
            "标签": "|".join(labels),
        })

    index = 0
    while index < len(kept):
        message = kept[index]
        if message["role"] == "customer":
            kind = handoff_kind(message["text"])
            prompt_kind = prompt_handoff_kind(message["text"])
            if kind:
                handoff_hits.append(kind)
            if prompt_kind:
                prompt_hits.append(prompt_kind)
            add_turn(message, ["customer"])
            rewritten = rewrite_question(message["text"], topic)
            detected = business_of(rewritten, "", topic)
            if detected != "其他":
                topic = detected
            pending_question = (pending_question + "\n" + rewritten).strip()
            pending_raw = (pending_raw + "\n" + message["text"]).strip()
            index += 1
            continue

        burst: list[dict] = []
        while index < len(kept) and kept[index]["role"] == "agent":
            burst.append(kept[index])
            index += 1
        question = pending_question
        question_facets = facets_of(question)
        refused = customer_refused_phone(question)
        left_contact = customer_left_contact(question)
        parts: list[tuple[str, str]] = []
        agent_name = burst[0]["speaker"] or agent_column
        previous = None
        for item in burst:
            gap = _gap_seconds(previous, item)
            follow = previous is not None and previous["role"] == "agent" and is_follow(item["text"], gap)
            labels: list[str] = []
            if follow:
                labels.append("follow_up")
                followups.append(_script_row(
                    dialogue_id, visitor_id, "follow_up", item["text"], question, topic,
                    agent_name, created, channel, follow=True, refused=refused,
                    left_contact=False, quoted=quoted, first=False,
                ))
                if is_lead(item["text"]):
                    labels.append("lead_capture")
                    leads.append(_script_row(
                        dialogue_id, visitor_id, "lead_capture", item["text"], question, topic,
                        agent_name, created, channel, follow=True, refused=refused,
                        left_contact=False, quoted=quoted, first=False,
                    ))
                add_turn(item, labels)
                previous = item
                continue
            if is_bank_type(item["text"]) and "开户" in f"{question}{topic}":
                labels.append("business_knowledge")
                parts.append(("个人户或公司户", item["text"]))
                add_turn(item, labels)
                previous = item
                first_agent = False
                continue
            if is_arrange(item["text"]):
                labels.append("lead_capture")
                leads.append(_script_row(
                    dialogue_id, visitor_id, "lead_capture", item["text"], question, topic or "其他",
                    agent_name, created, channel, follow=False, refused=False,
                    left_contact=True, quoted=quoted, first=False,
                ))
                add_turn(item, labels)
                previous = item
                continue
            for clause in split_clauses(item["text"]):
                head, tail = split_lead_tail(clause)
                chunks = [head] if not tail else [head, tail]
                for chunk in chunks:
                    if not chunk or noise_reason(chunk):
                        continue
                    price_fact = is_marketing(chunk) and any(
                        word in chunk for word in ("3980", "10周年", "活动价", "最后一天")
                    )
                    if price_fact:
                        fact = chunk.strip("，,！!。 ")
                        labels.append("marketing_activity")
                        marketing.append(_script_row(
                            dialogue_id, visitor_id, "marketing_activity", fact, question, topic,
                            agent_name, created, channel, follow=False, refused=False,
                            left_contact=False, quoted=True, first=first_agent,
                        ))
                        parts.append(("费用", fact))
                        quoted = True
                    if is_lead(chunk):
                        labels.append("lead_capture")
                        leads.append(_script_row(
                            dialogue_id, visitor_id, "lead_capture", chunk.strip("，,！!。 "), question, topic,
                            agent_name, created, channel, follow=False, refused=refused,
                            left_contact=left_contact, quoted=quoted, first=first_agent,
                        ))
                        if not price_fact:
                            fact = factual_clauses(chunk)
                            if fact and len(norm_key(fact)) >= 6 and not is_weak_fact(fact):
                                facet = _facet_for_answer(fact, question_facets)
                                labels.append("business_knowledge")
                                parts.append((facet, fact))
                    if price_fact or is_lead(chunk):
                        continue
                    cleaned = strip_hype(chunk)
                    if is_weak_fact(cleaned):
                        continue
                    if not cleaned or (is_lead(cleaned) and not is_materials(cleaned)):
                        if cleaned and is_lead(cleaned):
                            labels.append("lead_capture")
                            leads.append(_script_row(
                                dialogue_id, visitor_id, "lead_capture", cleaned, question, topic,
                                agent_name, created, channel, follow=False, refused=refused,
                                left_contact=left_contact, quoted=quoted, first=first_agent,
                            ))
                        continue
                    facet = _facet_for_answer(cleaned, question_facets)
                    labels.append("business_knowledge")
                    parts.append((facet, cleaned))
            add_turn(item, labels or ["agent"])
            previous = item
            first_agent = False
        if parts and question and not handoff_hits:
            blob = "\n".join(text for _facet, text in parts)
            business = business_of(question, blob, topic)
            question_business = business_of(question, "", topic)
            mismatch = (
                question_business not in {"", "其他"}
                and business not in {"", "其他"}
                and question_business != business
            )
            context = pending_raw[:300]
            for facet, text in parts:
                knowledge.append({
                    "dialogue_id": dialogue_id,
                    "visitor_id": visitor_id,
                    "business": business,
                    "facet": facet,
                    "question": question,
                    "answer": text,
                    "context": context,
                    "agent": agent_name,
                    "date": created,
                    "source": channel,
                    "need_review": mismatch,
                    "question_facets": question_facets,
                })
            if len(question_facets) >= 2 or len(parts) >= 2:
                knowledge.append({
                    "dialogue_id": dialogue_id,
                    "visitor_id": visitor_id,
                    "business": business,
                    "facet": "+".join(dict.fromkeys(facet for facet, _text in parts)),
                    "question": question,
                    "answer": "\n".join(text for _facet, text in parts),
                    "context": context,
                    "agent": agent_name,
                    "date": created,
                    "source": channel,
                    "need_review": mismatch,
                    "question_facets": question_facets,
                    "combined": True,
                })
        pending_question = ""
        pending_raw = ""

    substantive = sum(len(item["text"]) for item in kept)
    return {
        "dialogue_id": dialogue_id,
        "visitor_id": visitor_id,
        "created": created,
        "channel": channel,
        "agent": agent_column,
        "substantive": substantive,
        "knowledge": knowledge,
        "leads": leads,
        "marketing": marketing,
        "followups": followups,
        "turns": turns,
        "handoff": handoff_hits,
        "prompt_handoff": prompt_hits,
        "drop_reasons": drop_reasons,
        "parsed": len(parsed),
        "kept_messages": len(kept),
    }


def _facet_for_answer(text: str, question_facets: list[str]) -> str:
    if is_materials(text):
        return "资料"
    if is_marketing(text) or any(word in text for word in ("费用", "收费", "多少钱", "活动价")):
        return "费用"
    if any(word in text for word in ("汇丰", "渣打", "哪些银行")):
        return "银行"
    if any(word in text for word in ("多久", "几天", "下证", "下户")):
        return "时效"
    if any(word in text for word in ("不到香港", "不用去香港", "不用本人", "远程")):
        return "远程办理"
    if "个人户" in text or "公司户" in text:
        return "个人户或公司户"
    if any(word in text for word in ("流程", "先办", "步骤")):
        return "流程"
    if len(question_facets) == 1:
        return question_facets[0]
    return "其他"


def _script_row(dialogue_id, visitor_id, kind, script, question, topic, agent, created, channel, **flags) -> dict:
    business = business_of(question, script, topic)
    trigger, review = trigger_of(
        question, follow=flags["follow"], refused=flags["refused"], left_contact=flags["left_contact"],
    )
    if kind == "marketing_activity":
        trigger, review = ("客户问价", False)
    stage = stage_of(
        follow=flags["follow"], left_contact=flags["left_contact"],
        quoted=flags["quoted"], first=flags["first"],
    )
    offer = ""
    if "3980" in script:
        offer = "3980"
    elif "10周年" in script:
        offer = "10周年"
    elif "活动" in script or "优惠" in script:
        offer = "活动"
    return {
        "dialogue_id": dialogue_id,
        "visitor_id": visitor_id,
        "type": kind,
        "trigger": trigger,
        "stage": stage,
        "business": business if business != "其他" else (topic or "其他"),
        "script": script.strip(),
        "action": action_of(script),
        "channel": channel,
        "offer": offer,
        "agent": agent,
        "date": created,
        "need_review": review,
        "also_lead_capture": bool(flags["follow"] and is_lead(script)),
        "question": question,
        "context": question[:300],
    }


def _richest_ids(dialogues: list[dict]) -> set[str]:
    best: dict[str, tuple[int, str, str]] = {}
    for item in dialogues:
        visitor = item["visitor_id"] or item["dialogue_id"]
        current = best.get(visitor)
        stamp = item["created"] or ""
        candidate = (item["substantive"], stamp, item["dialogue_id"])
        if current is None or candidate > current:
            best[visitor] = candidate
    return {dialogue_id for _score, _stamp, dialogue_id in best.values()}


def _cluster(records: list[dict], text_key: str) -> list[list[dict]]:
    ranked = sorted(records, key=lambda item: item.get(text_key) or "")
    counts = Counter(norm_key(item.get(text_key) or "") for item in ranked)
    ordered = sorted(records, key=lambda item: (-counts[norm_key(item.get(text_key) or "")], item.get(text_key) or ""))
    clusters: list[dict] = []
    for item in ordered:
        key = norm_key(item.get(text_key) or "")
        if len(key) < 4:
            placed = next((cluster for cluster in clusters if cluster["key"] == key), None)
        else:
            placed = next((cluster for cluster in clusters if similar(cluster["key"], key) >= SIMILARITY), None)
        if placed is None:
            clusters.append({"key": key, "items": [item]})
        else:
            placed["items"].append(item)
    return [cluster["items"] for cluster in clusters]


def _most_common_text(records: list[dict], field: str) -> str:
    counts: dict[str, int] = Counter()
    display: dict[str, str] = {}
    for item in records:
        text = (item.get(field) or "").strip()
        if not text:
            continue
        key = norm_key(text)
        counts[key] += 1
        current = display.get(key)
        if current is None or len(text) > len(current):
            display[key] = text
    if not counts:
        return ""
    best = max(counts, key=lambda key: (counts[key], len(display[key])))
    return display[best]


def _pick_answer(records: list[dict]) -> tuple[str, int, str]:
    visitors: dict[str, set[str]] = defaultdict(set)
    display: dict[str, str] = {}
    for item in records:
        text = (item.get("answer") or "").strip()
        if not text:
            continue
        key = norm_key(text)
        visitors[key].add(item.get("visitor_id") or item.get("dialogue_id") or "")
        current = display.get(key)
        if current is None or len(text) > len(current):
            display[key] = text
    if not visitors:
        return "", 0, ""
    ranked = sorted(visitors, key=lambda key: (-len(visitors[key]), -len(display[key]), key))
    best = ranked[0]
    alternative = ""
    if len(ranked) > 1:
        second = ranked[1]
        if len(visitors[second]) >= 2 and len(visitors[second]) * 2 >= len(visitors[best]):
            alternative = display[second]
    return display[best], len(visitors[best]), alternative


def _lead_close(leads: list[dict], business: str) -> str:
    pool = [
        item for item in leads
        if item.get("business") == business and item.get("action") != "安排专员" and not item.get("also_lead_capture")
    ]
    if not pool:
        pool = [item for item in leads if item.get("action") != "安排专员"]
    chosen = _most_common_text(pool, "script")
    return chosen or default_close(business)


def _publish_scripts(records: list[dict], richest: set[str]) -> list[dict]:
    usable = [item for item in records if item.get("dialogue_id") in richest]
    if not usable:
        usable = records
    merged: list[dict] = []
    for cluster in _cluster(usable, "script"):
        script = _most_common_text(cluster, "script")
        visitors = {item.get("visitor_id") or item.get("dialogue_id") for item in cluster}
        sample = []
        for item in cluster:
            if item["dialogue_id"] not in sample:
                sample.append(item["dialogue_id"])
        base = max(cluster, key=lambda item: (len(item.get("script") or ""), item.get("dialogue_id") or ""))
        need_review = any(item.get("need_review") for item in cluster) and script == ""
        row = {
            "id": f"{base['dialogue_id']}-s",
            "dialogue_id": base["dialogue_id"],
            "type": base["type"],
            "handoff": False,
            "trigger": _most_common_text(cluster, "trigger") or base.get("trigger") or "其他",
            "stage": _most_common_text(cluster, "stage") or base.get("stage") or "",
            "business": _most_common_text(cluster, "business") or base.get("business") or "其他",
            "script": script,
            "action": _most_common_text(cluster, "action") or base.get("action") or "",
            "channel": _most_common_text(cluster, "channel") or base.get("channel") or "",
            "offer": _most_common_text(cluster, "offer"),
            "context": base.get("context") or "",
            "also_lead_capture": any(item.get("also_lead_capture") for item in cluster),
            "metadata": {
                "agent": base.get("agent") or "",
                "date": base.get("date") or "",
                "need_review": bool(base.get("need_review")),
                "count": len(visitors),
                "dialogue_ids": sample[:8],
            },
        }
        if need_review:
            row["metadata"]["need_review"] = True
        merged.append(row)
    for index, row in enumerate(merged, start=1):
        row["id"] = f"{row['dialogue_id']}-{index}"
    return merged


def _publish_knowledge(records: list[dict]) -> tuple[list[dict], list[dict], int]:
    publish: list[dict] = []
    review: list[dict] = []
    seen_hash: set[str] = set()
    hash_removed = 0
    usable = [
        item for item in records
        if not item.get("need_review")
        and not item.get("combined")
        and not is_weak_fact(item.get("answer") or "")
        and not (is_lead(item.get("answer") or "") and "3980" not in (item.get("answer") or "") and not is_materials(item.get("answer") or ""))
    ]
    for item in records:
        if item.get("need_review"):
            review.append({
                "dialogue_id": item["dialogue_id"],
                "business": item["business"],
                "facet": item["facet"],
                "question": item["question"],
                "answer": item["answer"],
                "context": item["context"],
                "reason": "业务线对不上",
            })
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in usable:
        grouped[(item["business"], item["facet"])].append(item)
    running = 0
    for (business, facet), group in grouped.items():
        for questions in _cluster(group, "question"):
            answer, support, alternative = _pick_answer(questions)
            if not answer:
                continue
            raw_question = _most_common_text(questions, "question")
            if is_clarifying_answer(answer) or is_opening_menu(raw_question, answer):
                review.append({
                    "dialogue_id": questions[0]["dialogue_id"],
                    "business": business,
                    "facet": facet,
                    "question": raw_question,
                    "answer": answer,
                    "context": questions[0].get("context") or "",
                    "reason": "客服反问",
                })
                continue
            if is_promo_answer(answer):
                if not (alternative and not is_promo_answer(alternative) and not is_clarifying_answer(alternative)):
                    continue
                answer = alternative
            if is_contact_only_question(raw_question):
                review.append({
                    "dialogue_id": questions[0]["dialogue_id"],
                    "business": business,
                    "facet": facet,
                    "question": raw_question,
                    "answer": answer,
                    "context": questions[0].get("context") or "",
                    "reason": "问句只剩联系方式",
                })
                continue
            category = knowledge_category(business, facet, answer)
            if category is None or (support < 2 and not has_hard_fact(answer)):
                continue
            question = standard_question(category, answer)
            digest = sha256_text(norm_key(question) + "\n" + norm_key(answer))
            if digest in seen_hash:
                hash_removed += 1
                continue
            seen_hash.add(digest)
            if alternative and not is_promo_answer(alternative):
                review.append({
                    "dialogue_id": questions[0]["dialogue_id"],
                    "business": category,
                    "facet": facet,
                    "question": question,
                    "answer": alternative,
                    "context": questions[0].get("context") or "",
                    "reason": "次数接近的另一种说法",
                })
            visitors = {item.get("visitor_id") or item.get("dialogue_id") for item in questions}
            similar_questions = []
            for item in questions:
                text = (item.get("question") or "").strip()
                if (
                    text
                    and text != question
                    and text not in similar_questions
                    and not is_contact_only_question(text)
                    and norm_key(text) != norm_key(question)
                ):
                    similar_questions.append(text)
            exemplar = questions[0]
            running += 1
            publish.append({
                "id": f"{exemplar['dialogue_id']}-{running}",
                "dialogue_id": exemplar["dialogue_id"],
                "type": "business_knowledge",
                "business": category,
                "question": question,
                "answer": answer,
                "context": exemplar.get("context") or "",
                "content": f"问：{question}\n答：{answer}",
                "metadata": {
                    "source": exemplar.get("source") or "",
                    "agent": exemplar.get("agent") or "",
                    "date": exemplar.get("date") or "",
                    "facet": facet,
                    "count": len(visitors),
                    "answer_support": support,
                    "similar_questions": similar_questions[:20],
                    "need_review": False,
                },
            })
    return publish, review, hash_removed


def _combined_paths(records: list[dict], leads: list[dict]) -> list[dict]:
    combined = [item for item in records if item.get("combined") and not item.get("need_review")]
    paths = []
    for questions in _cluster(combined, "question"):
        answer, support, _alternative = _pick_answer(questions)
        if not answer:
            continue
        question = _most_common_text(questions, "question")
        business = _most_common_text(questions, "business") or "其他"
        exemplar = questions[0]
        paths.append({
            "business": business,
            "facet": exemplar.get("facet") or "",
            "question": question,
            "answer": answer,
            "close_script": _lead_close(leads, business),
            "offer": "活动价" if "3980" in answer or "10周年" in answer else "",
            "handoff": False,
            "ready": True,
            "source": exemplar.get("source") or "",
            "similar_questions": [],
            "content": f"问：{question}\n答：{answer}",
            "answer_support": support,
        })
    return paths


def _matrix_paths(knowledge: list[dict], leads: list[dict]) -> list[dict]:
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in knowledge:
        if item.get("combined"):
            continue
        by_key[(item["business"], item["facet"])].append(item)
    paths = []
    for business, facet in CONSULT_MATRIX:
        group = by_key.get((business, facet), [])
        answer, support, _alt = _pick_answer(group) if group else ("", 0, "")
        question = _most_common_text(group, "question") if group else ""
        close = _lead_close(leads, business)
        if facet == "个人户或公司户" and business == "开户" and not answer:
            answer = BANK_TYPE
            question = question or "我要开户"
            close = ""
        paths.append({
            "business": business,
            "facet": facet,
            "question": question,
            "answer": answer,
            "close_script": "" if answer == BANK_TYPE else close,
            "offer": "活动价" if answer and ("3980" in answer or "10周年" in answer) else "",
            "handoff": False,
            "ready": bool(answer or close),
            "source": "",
            "similar_questions": [],
            "content": f"问：{question}\n答：{answer}" if question and answer else "",
            "answer_support": support,
            "status": "可回答" if answer else "只能留资",
        })
    for business, facet, question, answer in SCOPE_PATHS:
        paths.append({
            "business": business,
            "facet": facet,
            "question": question,
            "answer": answer,
            "close_script": "",
            "offer": "",
            "handoff": False,
            "ready": True,
            "source": "prompt",
            "similar_questions": [],
            "content": f"问：{question}\n答：{answer}",
            "answer_support": 0,
            "status": "可回答",
        })
    for label in ("投诉", "退款", "赔偿", "要真人", "敏感"):
        paths.append({
            "business": "其他",
            "facet": label,
            "question": label,
            "answer": "",
            "close_script": "",
            "offer": "",
            "handoff": True,
            "ready": True,
            "source": "policy",
            "similar_questions": [],
            "content": "",
            "answer_support": 0,
            "status": "仍然要接入",
        })
    return paths


def clean_dialogues(rows: list[dict]) -> dict:
    stats: Counter = Counter()
    message_drops: Counter = Counter()
    dialogues = []
    stats["rows_total"] = len(rows)
    for row in rows:
        try:
            reason = filter_reason(row)
            if reason:
                stats[reason] += 1
                continue
            extracted = extract_dialogue(row)
            stats["kept"] += 1
            stats["parsed_messages"] += extracted["parsed"]
            stats["kept_messages"] += extracted["kept_messages"]
            message_drops.update(extracted["drop_reasons"])
            dialogues.append(extracted)
        except Exception:  # noqa: BLE001
            stats["row_error"] += 1
            logger.exception("跳过无法清洗的对话 %s", _cell(row.get("对话ID")))
    richest = _richest_ids(dialogues)
    stats["visitor_collapsed"] = max(0, len(dialogues) - len(richest))
    knowledge_records = [item for dialogue in dialogues for item in dialogue["knowledge"]]
    script_source = [dialogue for dialogue in dialogues if dialogue["dialogue_id"] in richest]
    leads = [item for dialogue in script_source for item in dialogue["leads"]]
    marketing = [item for dialogue in script_source for item in dialogue["marketing"]]
    followups = [item for dialogue in script_source for item in dialogue["followups"]]
    published, review, hash_removed = _publish_knowledge(knowledge_records)
    lead_rows = _publish_scripts(leads, richest)
    marketing_rows = _publish_scripts(marketing, richest)
    follow_rows = _publish_scripts(followups, richest)
    paths = _matrix_paths(knowledge_records, leads) + _combined_paths(knowledge_records, leads)
    ready_false = sum(1 for item in paths if not item.get("ready") and not item.get("handoff"))
    facet_stats = {}
    for item in paths:
        if item.get("source") in {"prompt", "policy"}:
            continue
        if item.get("facet") and "+" in str(item.get("facet")):
            continue
        key = f"{item['business']}/{item['facet']}"
        if key in facet_stats:
            continue
        facet_stats[key] = {
            "status": item.get("status") or ("可回答" if item.get("answer") else "只能留资"),
            "support": item.get("answer_support") or 0,
        }
    turns = [turn for dialogue in dialogues for turn in dialogue["turns"]]
    return {
        "business_knowledge": published,
        "lead_capture": lead_rows,
        "marketing_activity": marketing_rows,
        "follow_up": follow_rows,
        "consult_paths": paths,
        "answer_review": review,
        "clean_turns": turns,
        "stats": {
            "rows_total": stats["rows_total"],
            "rows_kept": stats["kept"],
            "dropped": {
                "invalid": stats["invalid"],
                "missed": stats["missed"],
                "grade": stats["grade"],
                "empty": stats["empty"],
                "row_error": stats["row_error"],
            },
            "messages": {
                "parsed": stats["parsed_messages"],
                "kept": stats["kept_messages"],
                "dropped": dict(message_drops),
            },
            "outputs": {
                "business_knowledge": len(published),
                "lead_capture": len(lead_rows),
                "marketing_activity": len(marketing_rows),
                "follow_up": len(follow_rows),
                "consult_paths": len(paths),
                "answer_review": len(review),
                "clean_turns": len(turns),
            },
            "dedup": {
                "visitor_collapsed": stats["visitor_collapsed"],
                "hash_removed": hash_removed,
                "script_merged": max(0, len(leads) - len(lead_rows)),
            },
            "facets": facet_stats,
            "ready_false": ready_false,
            "prompt_still_handoff": list(PROMPT_HANDOFF),
        },
    }


def write_outputs(result: dict, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    for name in (
        "business_knowledge", "lead_capture", "marketing_activity", "follow_up",
        "consult_paths", "answer_review",
    ):
        target = directory / f"{name}.jsonl"
        with target.open("w", encoding="utf-8") as handle:
            for row in result[name]:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    turns = directory / "clean_turns.csv"
    with turns.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["对话ID", "序号", "角色", "昵称", "时间", "正文", "标签"])
        writer.writeheader()
        writer.writerows(result["clean_turns"])
    (directory / "stats.json").write_text(
        json.dumps(result["stats"], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _row(dialogue_id: str, content: str, *, visitor: str = "", channel: str = "抖音", created: str = "2026-09-29 10:00:00", agent: str = "刘小姐") -> dict:
    return {
        "对话ID": dialogue_id,
        "访客ID": visitor or dialogue_id,
        "有效对话": "有效",
        "遗漏对话": "未遗漏",
        "对话分级": "",
        "业务大类": "",
        "来源渠道": channel,
        "对话渠道": channel,
        "客服昵称": agent,
        "对话创建时间": created,
        "对话内容": content,
    }


MATERIALS = (
    "您好，注册香港公司主要涉及：\n"
    "1：公司名称\n"
    "2：地址（我司提供），\n"
    "3：经营范围\n"
    "4：注册资金\n"
    "5: 股东董事相关身份信息"
)
PRICE = "现在是我们10周年庆典3980的活动价(含3900的官费)，包含秘书服务，秘书地址，绿盒子全套"
LEAD = "您这边联系方式多少呢，我们专员给您讲解一下办理的流程、资料和费用，让您参考了解下"


async def polish_business_questions(rows: list[dict]) -> list[dict]:
    """有 LLM 时把标准问再收一句。失败或未配置就保持规则给出的标准问。不改答案。"""
    if not rows:
        return rows
    try:
        from app.config import settings
    except Exception:  # noqa: BLE001
        return rows
    if not settings.llm_configured:
        return rows
    from openai import AsyncOpenAI

    client = AsyncOpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key)
    payload = [
        {"id": item["id"], "category": item["business"], "question": item["question"], "answer": item["answer"][:500]}
        for item in rows[:40]
    ]
    prompt = (
        "把下面每条整理成一句标准问。不要保留顾客整段口语，不要新写价格、清单、天数或银行。\n"
        "答案里没有的事实不要写进问题。开场白和反问 keep=false。\n"
        "只输出 JSON：{\"items\":[{\"id\":\"\",\"question\":\"\",\"keep\":true}]}\n"
        + json.dumps(payload, ensure_ascii=False)
    )
    try:
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        raw = resp.choices[0].message.content or ""
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        logger.warning("标准问整理失败，沿用规则问句", exc_info=True)
        return rows
    rewritten = {item.get("id"): item for item in data.get("items") or [] if isinstance(item, dict)}
    kept = []
    for row in rows:
        update = rewritten.get(row["id"])
        if update is None:
            kept.append(row)
            continue
        if update.get("keep") is False:
            continue
        question = str(update.get("question") or "").strip()
        if question and not is_contact_only_question(question):
            row = dict(row)
            row["question"] = question[:256]
            row["content"] = f"问：{row['question']}\n答：{row['answer']}"
        kept.append(row)
    return kept


def sample_rows() -> list[dict]:
    """自检用的对话，号码是虚构的。"""
    mixed = "\n".join([
        "顾客 2026-09-29 10:08:58 你好，请问需要多少费用，需要什么材料",
        f"刘小姐 2026-09-29 10:09:19 {MATERIALS.splitlines()[0]}",
        *MATERIALS.splitlines()[1:],
        f"刘小姐 2026-09-29 10:09:21 {LEAD}",
        f"刘小姐 2026-09-29 10:09:32 {PRICE}，！您需不需要留个联系方式，给您预留一个优惠名额呢！",
        "顾客 2026-09-29 10:10:13 13800138000.下午联系",
        f"刘小姐 2026-09-29 10:10:25 {ARRANGE}",
    ])
    silent = "\n".join([
        "顾客 2026-09-29 09:27:44 你好",
        "刘小姐 2026-09-29 09:27:52 你好",
        "顾客 2026-09-29 09:28:18 我要注册香港公司",
        f"刘小姐 2026-09-29 09:28:27 {LEAD}",
        "顾客 2026-09-29 09:29:11 注册需要什么条件",
        f"刘小姐 2026-09-29 09:29:22 {MATERIALS.splitlines()[0]}",
        *MATERIALS.splitlines()[1:],
        "刘小姐 2026-09-29 09:36:13 老板，您还在线吗？目前香港公司等还是有很多的优势，您方便留个联系方式吗？",
        "刘小姐 2026-09-29 09:42:53 或者您方便加下vx吗，您接收的微信号多少呢",
    ])
    rows = [
        _row("8846928756", mixed, visitor="v-mixed"),
        _row("8846877792", silent, visitor="v-silent", created="2026-09-29 09:27:34"),
        _row("bank-type", "\n".join([
            "顾客 2026-09-29 11:00:00 我要开户",
            f"刘小姐 2026-09-29 11:00:08 {BANK_TYPE}",
        ]), visitor="v-bank"),
        _row("refuse-phone", "\n".join([
            "顾客 2026-09-29 12:00:00 不方便电话",
            "刘小姐 2026-09-29 12:00:10 或者您方便加下vx吗，微信号多少呢",
        ]), visitor="v-refuse"),
        _row("complaint", "\n".join([
            "顾客 2026-09-29 13:00:00 我要投诉你们",
            "刘小姐 2026-09-29 13:00:08 抱歉，我让同事来处理",
        ]), visitor="v-complaint"),
    ]
    for index in range(3):
        rows.append(_row(
            f"price-a-{index}",
            "\n".join([
                "顾客 2026-09-29 14:00:00 香港公司注册多少钱",
                f"刘小姐 2026-09-29 14:00:08 {PRICE}",
            ]),
            visitor=f"v-price-a-{index}",
        ))
    for index in range(2):
        rows.append(_row(
            f"price-b-{index}",
            "\n".join([
                "顾客 2026-09-29 15:00:00 香港公司注册多少钱",
                "刘小姐 2026-09-29 15:00:08 具体费用看您所需服务，没有统一报价。",
            ]),
            visitor=f"v-price-b-{index}",
        ))
    rows.append(_row(
        "hype",
        "\n".join([
            "顾客 2026-09-29 16:00:00 这个办理靠谱吗",
            "刘小姐 2026-09-29 16:00:08 我们是做了快10年的本地服务商，渠道什么的都比较稳定靠谱，您考虑预留个联系方式吗？",
        ]),
        visitor="v-hype",
    ))
    rows.append(_row(
        "phone-only",
        "\n".join([
            "顾客 2026-09-29 18:00:00 13800138001",
            "刘小姐 2026-09-29 18:00:08 老板，您是需要了解注册还是开户审计那些呢",
        ]),
        visitor="v-phone",
    ))
    rows.append(_row(
        "menu",
        "\n".join([
            "顾客 2026-09-29 19:00:00 能介绍一下咱们的服务吗？",
            "刘小姐 2026-09-29 19:00:08 您好老板，这边可以提供注册，开户，年审，地址挂靠等服务，您是想咨询什么呢",
        ]),
        visitor="v-menu",
    ))
    rows.append(_row("missed", "顾客 2026-09-29 17:00:00 多少钱", visitor="v-miss"))
    rows[-1]["遗漏对话"] = "遗漏"
    return rows


def self_check() -> None:
    result = clean_dialogues(sample_rows())
    knowledge = result["business_knowledge"]
    blob = "\n".join(item["answer"] for item in knowledge)
    assert "1：公司名称" in blob and "股东董事" in blob, blob
    assert "3980" not in blob
    assert all(item["business"] in BUSINESS_CATEGORIES for item in knowledge)
    assert any(item["business"] == "资料" and item["question"] == "注册需要什么资料" for item in knowledge)
    assert "留个联系方式" not in blob
    assert "做了快10年" not in blob and "本地服务商" not in blob
    assert "想咨询什么" not in blob
    assert not any(is_contact_only_question(item["question"]) for item in knowledge)
    marketing = "\n".join(item["script"] for item in result["marketing_activity"])
    assert "3980" in marketing
    leads = result["lead_capture"]
    arrange = [item for item in leads if item["script"] == ARRANGE or "安排专员" in item["script"]]
    assert arrange and arrange[0]["action"] == "安排专员"
    assert arrange[0]["stage"] == "客户已留电话"
    assert all("[手机号]" in item["正文"] or "13800138000" not in item["正文"] for item in result["clean_turns"])
    assert any(
        item["stage"] == "沉默唤醒" and item["handoff"] is False and "还在线" in item["script"]
        for item in result["follow_up"]
    )
    bank = [item for item in result["consult_paths"] if item["facet"] == "个人户或公司户" and item["answer"] == BANK_TYPE]
    assert bank and bank[0]["handoff"] is False and bank[0]["close_script"] == ""
    refused = [item for item in leads if item["trigger"] == "客户拒绝电话"]
    assert refused and refused[0]["action"] == "加微信"
    assert any(item.get("handoff") is True and item.get("facet") == "投诉" for item in result["consult_paths"])
    assert "没有统一报价" in blob
    assert "3980" not in blob
    empty = [item for item in result["consult_paths"] if item["business"] == "新加坡" and item["facet"] == "费用"]
    assert empty and empty[0]["answer"] == "" and empty[0]["close_script"] and empty[0]["ready"] is True
    assert result["stats"]["dropped"]["missed"] == 1
    assert result["stats"]["ready_false"] == 0
    logger.info("自检通过")


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    self_check()
    parser = argparse.ArgumentParser(description="美洽历史对话洗成咨询知识、留资、营销和跟进")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not args.input.exists():
        raise SystemExit(f"输入文件不存在：{args.input}")
    rows = load_rows(args.input)
    result = clean_dialogues(rows)
    write_outputs(result, args.output_dir)
    stats = result["stats"]
    logger.info("输入 %s 行，保留 %s 行，输出 %s", stats["rows_total"], stats["rows_kept"], args.output_dir)
    logger.info("ready_false=%s", stats["ready_false"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
