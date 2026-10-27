"""把美洽历史对话洗成待审核 FAQ 草稿，不写入知识库。

用法（在仓库根目录或 backend/ 下均可）：

    python backend/scripts/clean_meiqia_to_faq.py
    python backend/scripts/clean_meiqia_to_faq.py --input 历史对话.xlsx --output FAQ草稿.xlsx

默认读取桌面上的美洽导出，写出桌面 ``FAQ草稿.xlsx``。
``有效对话`` 为「无效」、``遗漏对话`` 为「遗漏」的会话整段丢弃（``未遗漏`` 保留）。
"""
from __future__ import annotations

import argparse
import html
import io
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

ROW_RE = re.compile(br'<row r="(\d+)">(.*?)</row>', re.S)
CELL_RE = re.compile(br'<c r="([A-Z]+)(\d+)"[^>]*>(?:<v>(.*?)</v>)?</c>', re.S)
MSG_RE = re.compile(
    r"^(?P<who>.+?)\s+(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*(?P<body>.*)$"
)
PHONE_RE = re.compile(r"1[3-9]\d{9}")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.\w+")
WECHAT_RE = re.compile(r"(微信|wx|vx|VX|V信|v信)[:：\s]*[A-Za-z][A-Za-z0-9_-]{4,}")
EMOJI_RE = re.compile(r"\[[^\]]{1,16}\]")
URL_RE = re.compile(r"https?://\S+")
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
ILLEGAL_XLSX = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
PUNCT_RE = re.compile(
    "["
    + r"\s，,。！？?、~～；;：:（）()【】\[\]“”·!"
    + "\"'"
    + "]+"
)

GREETINGS = {
    "你好", "您好", "在吗", "在么", "在嘛", "好的", "好", "是的", "是", "对", "嗯", "哦",
    "哈喽", "hi", "hello", "你好美女", "美女你好", "在的", "可以", "行", "谢谢", "感谢",
}
BANNED_ANSWER = (
    "留个联系方式", "联系方式多少", "您联系方式", "做了10年", "做了快10年", "做了9年",
    "做了快9年", "200号人", "200人", "快速下户", "一手渠道", "2800", "4430",
)
LEAD_HINTS = (
    "联系方式", "微信号", "加微信", "加您", "加你", "留个", "留一下", "二维码",
    "专员", "还在线", "快速下户", "一手", "200号", "200人", "本地服务商",
    "很多的优势", "实地考察", "稍等", "通过下",
)
# 顾客栏里混进来的广告、客服套话、平台「已留资」通知，不能当问法
CUSTOMER_JUNK = (
    "留个联系方式", "企业号主页", "短信群发", "安排专人", "类型不限",
    "看主页", "代办执照", "不会频繁打扰", "已预约", "尽快和我联系",
    "已经提供了我的联系方式", "提供了我的联系方式",
)
REMOTE_Q_RE = re.compile(
    r"不用去香港|不用到香港|不到香港|不在香港|不去香港|跑香港|本人不在|长期不在香港|"
    r"不用本人|人不到|内[地陆](能办|可以办|办理|怎么办)|在内[地陆]办"
)
# 平台一次性塞进会话的标准开场白，不跟随上一条话题
CANNED_OPENERS = (
    "我要办理业务什么价格",
    "我想要联系你们怎么联系",
    "我要开银行账户",
    "我要注册香港公司",
    "能介绍一下咱们的服务吗",
    "公司年审怎么收费",
    "如何获取资料流程及费用",
)

MATERIALS_ANSWER = (
    "注册香港公司主要涉及：\n"
    "1：公司名称\n"
    "2：地址（我司提供）\n"
    "3：经营范围\n"
    "4：注册资金\n"
    "5：股东董事相关身份信息"
)
PRICE_ANSWER = "10周年庆典3980的活动价（含3900的官费），包含秘书服务、秘书地址、绿盒子全套。"
PRICE_DEPENDS = "具体的费用主要是看您所需服务，没有统一报价。"
BANK_ANSWER = "可以对接汇丰、渣打、大新华侨、东亚、中银等银行。"
AUDIT_ANSWER = "审计的费用和您的公司情况有关，比如公司流水，规模等等，您留个联系方式我，我发您具体的费用报价表您先看下"
ANNUAL_ANSWER = AUDIT_ANSWER
BOOK_ANSWER = "香港没有代理记账，每年做年审审计，费用根据公司营收确定。"
SERVICE_ANSWER = "您好老板，这边可以提供注册，开户，年审，地址挂靠等服务，您是想咨询什么呢"
CONTACT_ANSWER = "留电话或微信，由专员联系。"
REG_CONTACT = "您这边联系方式多少呢，我们专员给您讲解一下办理的流程、资料和费用，让您参考了解下"
REG_CONTACT_PHONE = "您这边电话联系方式多少呢，我们专员给您讲解一下办理的流程、资料和费用，让您参考了解下"
BANK_TYPE_POLITE = "您好，您这边是想开公司户还是个人户呢？"
BANK_TYPE_SHORT = "你是要办个人还是公司"
IDENTITY_WAIT = "好的 ，您稍等两分钟，这边让我们专员联系您为您介绍一下"
COMPANY_ACCOUNT = "我们这边能直接对接汇丰渣打大新华侨东亚中银等这些，也能快速下户，费用这一块需要专员根据您的情况去报价，您看您方便留个联系方式吗，可以做个对比哈"
PERSONAL_ACCOUNT = "个人是需要1000左右的办理费用，您这边还需要了解，就可以留个联系方式，我们香港专员联系您"
SOLO_FEE = "您好，办理个人户需要1000左右的费用，你可以接受的话留个联系方式电话或者微信哦，我安排专员和你介绍办理"
PRICE_ASK = "我要办理业务，什么价格？"
CONTACT_WORDS = {"微信", "加我", "加微信", "v", "vx", "电话", "微"}
REMOTE_EXPLICIT = "可以，人不到香港也能办。"
TRUST_ANSWER = "我们是做了快10年的本地服务商，渠道比较稳定靠谱。"
BENEFIT4_ANSWER = (
    "注册香港公司的好处有：\n"
    "1. 税务优势，免税政策，合理避税\n"
    "2. 经营范围极少限制\n"
    "3. 低成本设立国际公司\n"
    "4. 公司注册与维护更便捷"
)
BENEFIT5_ANSWER = BENEFIT4_ANSWER + "\n5. 资金自由，取名自由"
CITY_GZ_ANSWER = "深圳、广州、香港、新加坡都有点。"
CITY_ANSWER = "深圳、香港、新加坡都有点。"
BAOAN_WHERE_ANSWER = "我们在深圳宝安。"
BAOAN_YES_ANSWER = "是的。"
EACCOUNT_ANSWER = "就是电子对公户。"
EBANK_ANSWER = "富港。"
ZERO_FLOW_ANSWER = "无流水审计大概1500左右。"
PRESENT_ANSWER = "可以不用。"
ACCOUNT_DAYS_ANSWER = "看您符合哪个。"
CERT_DAYS_ANSWER = "注册最快1-3天可下证。"

# 标题、分类、没有顾客原话时的兜底标准问
INTENT_META = {
    "reg_materials": ("香港公司注册资料", "注册", "注册香港公司需要什么资料"),
    "reg_price": ("香港公司注册费用", "注册", "香港公司注册多少钱"),
    "address_fee": ("秘书地址是否另收费", "注册", "秘书地址或地址挂靠怎么收费"),
    "reg_remote": ("人不到香港能否注册", "注册", "人不到香港可以注册公司吗"),
    "bank_remote": ("人不到香港能否办对公开户", "开户", "人在内地可以办香港对公开户吗"),
    "bank_open": ("银行开户", "开户", "香港银行开户怎么办理"),
    "bank_type": ("公司户还是个人户", "开户", "开公司户还是个人户"),
    "bank_list": ("可对接银行", "开户", "可以对接哪些银行"),
    "bank_price": ("开户费用", "开户", "银行开户多少钱"),
    "annual_fee": ("年审费用", "年审审计", "公司年审怎么收费"),
    "audit_fee": ("审计费用", "年审审计", "审计怎么收费"),
    "bookkeeping": ("香港有没有代理记账", "年审审计", "香港公司要代理记账吗"),
    "services": ("业务范围", "服务与联系", "你们能办哪些业务"),
    "duration": ("办理要多久", "服务与联系", "办理要多久"),
    "process": ("办理流程", "服务与联系", "办理流程是怎样的"),
    "contact": ("怎么联系", "服务与联系", "怎么联系你们"),
    "identity": ("香港身份续签", "注册", "注册香港公司和身份续签有什么关系"),
    "singapore": ("新加坡公司", "服务与联系", "可以注册新加坡公司吗"),
    "license": ("牌照", "服务与联系", "可以办牌照吗"),
    "trademark": ("商标", "服务与联系", "可以办商标吗"),
    "biz_license": ("执照", "服务与联系", "执照怎么办理"),
    "individual": ("个体户", "注册", "个体户怎么办理"),
    "reg_interest": ("想注册香港公司", "注册", "我要注册香港公司"),
    "trust_years": ("办理靠不靠谱", "服务与联系", "这个办理靠谱吗"),
    "legit_company": ("公司是否正规", "服务与联系", "是正规的么"),
    "benefit4": ("香港公司有什么好处", "注册", "香港公司有什么好处"),
    "benefit5": ("注册香港公司能做什么", "注册", "注册香港公司能干啥"),
    "office_gz": ("公司在深圳广州香港新加坡吗", "服务与联系", "公司在哪里"),
    "office_cities": ("公司在深圳香港新加坡吗", "服务与联系", "公司地址在香港吗"),
    "baoan_where": ("位置在哪里", "服务与联系", "位置在哪里"),
    "baoan_yes": ("在不在宝安区", "服务与联系", "你们在宝安区吗"),
    "eaccount_kind": ("电子公户是不是第三方账户", "开户", "电子公户是第三方平台的账户吗"),
    "eaccount_bank": ("电子对公户是哪个银行", "开户", "电子对公户是哪个银行"),
    "zero_flow_audit": ("无流水审计多少钱", "年审审计", "无流水审计报税多少钱"),
    "must_attend": ("注册要不要本人到场", "注册", "需本人亲自到场吗"),
    "account_days": ("多久下公户", "开户", "多久下公户"),
    "cert_days": ("注册下证要多久", "注册", "注册加开户周期要多久"),
    "reg_addr_where": ("注册地址在哪里", "注册", "注册地址在哪里"),
}
# 这些答案允许出现「做了快10年」，其它 FAQ 仍然不许
ALLOW_YEARS = {"trust_years"}
VERBATIM_INTENTS = {
    "reg_interest", "bank_open", "price_contact", "process", "bank_price", "bank_remote",
    "duration", "address_fee", "biz_license", "reg_addr_where", "identity",
    "company_acct", "personal_acct", "solo_fee", "audit_fee", "annual_fee",
}
PRECISE_STATUS = {
    "zero_flow_audit": "活动价待核实",
}
PRECISE_ANSWERS = {
    "trust_years": TRUST_ANSWER,
    "legit_company": "肯定是正规的呀。",
    "benefit4": BENEFIT4_ANSWER,
    "benefit5": BENEFIT5_ANSWER,
    "office_gz": CITY_GZ_ANSWER,
    "office_cities": CITY_ANSWER,
    "baoan_where": BAOAN_WHERE_ANSWER,
    "baoan_yes": BAOAN_YES_ANSWER,
    "eaccount_kind": EACCOUNT_ANSWER,
    "eaccount_bank": EBANK_ANSWER,
    "zero_flow_audit": ZERO_FLOW_ANSWER,
    "must_attend": PRESENT_ANSWER,
    "account_days": ACCOUNT_DAYS_ANSWER,
    "cert_days": CERT_DAYS_ANSWER,
}
REMOTE_INTENTS = {"reg_remote", "bank_remote"}
FAQ_HEADERS = [
    "标题", "分类", "标准问", "相似问", "答案", "答案状态",
    "问题次数", "答案支撑次数", "备选答案", "样例对话ID", "品牌分布", "时间范围",
]
GAP_HEADERS = ["标题", "分类", "标准问", "相似问", "问题次数", "样例对话ID"]


def should_drop(valid: str, missed: str) -> str | None:
    """无效、遗漏整段不进入统计。``未遗漏`` 不能当成遗漏。"""
    v = (valid or "").strip()
    m = (missed or "").strip()
    if v == "无效" or v.startswith("无效"):
        return "invalid"
    if m == "遗漏":
        return "missed"
    return None


def scrub(text: str) -> str:
    text = EMOJI_RE.sub("", text)
    text = URL_RE.sub("", text)
    text = PHONE_RE.sub("", text)
    text = EMAIL_RE.sub("", text)
    text = WECHAT_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def is_customer_junk(text: str) -> bool:
    return any(item in text for item in CUSTOMER_JUNK)


def is_canned(text: str) -> bool:
    return any(item in PUNCT_RE.sub("", text) for item in CANNED_OPENERS)


def is_noise(text: str) -> bool:
    raw = text.strip()
    if not raw:
        return True
    if raw.startswith("{") or raw.startswith("["):
        return True
    needles = (
        "不支持该消息", "收到一条", "meiqia", "CARD", "留资卡", "未知消息",
        "redbook_msg", "message_type",
    )
    if any(item in raw for item in needles):
        return True
    compact = scrub(raw)
    if not compact or compact.lower() in GREETINGS:
        return True
    if re.fullmatch(r"\d{2,11}", compact):
        return True
    if re.fullmatch(r"(?:\[[^\]]{1,16}\])+", raw.replace(" ", "")):
        return True
    return False


def parse_messages(content: str) -> list[tuple[str, str]]:
    """按「角色 + 时间」切句；没有时间戳的行挂到上一句。"""
    text = html.unescape(content or "").replace("\r\n", "\n").replace("\r", "\n")
    messages: list[tuple[str, str]] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        matched = MSG_RE.match(line)
        if matched:
            role = "cust" if matched.group("who").strip() == "顾客" else "agent"
            messages.append((role, matched.group("body").strip()))
        elif messages:
            role, body = messages[-1]
            messages[-1] = (role, (body + "\n" + line).strip())
    return messages


def norm_key(text: str) -> str:
    return PUNCT_RE.sub("", text).lower()


def has_materials(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    keys = ["公司名称", "地址", "经营范围", "注册资金", "股东"]
    if not all(key in compact for key in keys):
        return False
    positions = [compact.find(key) for key in keys]
    return positions == sorted(positions)


def extract_agent_facts(text: str) -> dict[str, str]:
    """从一条客服发言里抽出可单独成条的事实。返回值不含获客尾巴。"""
    found: dict[str, str] = {}
    compact = re.sub(r"\s+", "", text)
    if has_materials(text):
        found["reg_materials"] = MATERIALS_ANSWER
    if "3980" in compact and ("3900" in compact or "官费" in compact):
        found["reg_price"] = PRICE_ANSWER
    elif "看您所需服务" in compact or "费用主要是看您所需" in compact:
        found["reg_price_depends"] = PRICE_DEPENDS
    if all(name in compact for name in ("汇丰", "渣打", "中银")) and "200号" not in compact:
        found["bank_list"] = BANK_ANSWER
    if "代理记账" in compact and "没有" in compact:
        found["bookkeeping"] = BOOK_ANSWER
    elif "审计" in compact and "无流水" not in compact and ("流水" in compact or "规模" in compact):
        found["audit_fee"] = AUDIT_ANSWER
    if "年审的费用" in compact or (
        "年审" in compact and "流水" in compact and "代理记账" not in compact and "审计" not in compact
    ):
        found["annual_fee"] = ANNUAL_ANSWER
    if (
        "可以提供" in compact
        and "注册" in compact
        and ("开户" in compact or "年审" in compact)
        and "3980" not in compact
        and "200号" not in compact
        and "本地服务商" not in compact
    ):
        found["services"] = SERVICE_ANSWER
    return found


def _strip_lead_tail(text: str) -> str:
    """去掉同一句里索要联系方式的后半句。名单和事实留在前面。"""
    kept = []
    for part in re.split(r"[。！？\n]+", text):
        part = re.split(
            r"[，,\s]*(?:您这边|您看您|您考虑|您方便|你方便|您留|你留|留个|留一下|方便电话|方便加|请问)",
            part,
            maxsplit=1,
        )[0]
        part = re.sub(r"公司?\d+号人", "", part)
        part = part.replace("欢迎实地考察", "").replace("一手代办渠道", "").replace("一手渠道", "")
        part = scrub(part).strip("，,。 ")
        if not part:
            continue
        if any(hint in part for hint in (
            "联系方式", "微信", "vx", "VX", "专员", "二维码", "还在线",
            "实地考察", "一手", "稍等", "电话方便",
        )):
            continue
        kept.append(part)
    return "\n".join(kept)


def precise_hits(question: str, agent_text: str) -> list[tuple[str, str]]:
    """问句和紧跟着的回复必须对得上，才算这一条。"""
    q = PUNCT_RE.sub("", question)
    answer = _strip_lead_tail(agent_text)
    if not q or not answer:
        return []
    hits: list[tuple[str, str]] = []

    def add(intent: str, text: str):
        if all(item[0] != intent for item in hits):
            hits.append((intent, text))

    if "靠谱" in q and "香港公司" not in q and ("做了快10年" in answer or "做了10年" in answer):
        add("trust_years", TRUST_ANSWER)
    if "正规" in q and "正规" in answer:
        add("legit_company", "肯定是正规的呀。")
    if "好处" in q and "有必要" not in q and "条件" not in q and "资料" not in q and "材料" not in q and "税务优势" in answer and "资金自由" not in answer:
        add("benefit4", BENEFIT4_ANSWER)
    if any(key in q for key in ("能干啥", "能干嘛", "优势是什么", "香港优势")) and "怎么联系" not in q and "资金自由" in answer:
        add("benefit5", BENEFIT5_ANSWER)
    location = any(key in q for key in (
        "公司在哪", "公司地址", "地址在香港", "地址在哪", "店开在哪", "开在哪里",
        "你们公司在", "你们公司是哪里", "你公司在哪", "公司是哪里", "在哪办公", "办理地点在哪",
    ))
    if location and "注册地址" not in q and "宝安" not in q and "位置在哪" not in q:
        if all(city in answer for city in ("深圳", "香港", "新加坡")) and "宝安" not in answer:
            if "广州" in answer:
                add("office_gz", CITY_GZ_ANSWER)
            else:
                add("office_cities", CITY_ANSWER)
    if "位置在哪" in q and "宝安" in answer and "新加坡" not in answer:
        add("baoan_where", BAOAN_WHERE_ANSWER)
    if "宝安区" in q and re.search(r"(^|\n)是的(啊|呀|呢|哈)?$", answer):
        add("baoan_yes", BAOAN_YES_ANSWER)
    if ("第三方" in q or ("电子" in q and "公户" in q)) and "银行" not in q and "对公" in answer and "富港" not in answer:
        add("eaccount_kind", EACCOUNT_ANSWER)
    if "电子" in q and any(key in q for key in ("哪个银行", "那个银行", "什么银行")) and "富港" in answer:
        add("eaccount_bank", EBANK_ANSWER)
    if "无流水" in q and any(key in q for key in ("审计", "报税", "多少")) and "1500" in answer:
        add("zero_flow_audit", ZERO_FLOW_ANSWER)
    if "本人" in q and "到场" in q and ("可以不用" in answer or "不用本人" in answer):
        add("must_attend", PRESENT_ANSWER)
    if ("下公户" in q or "下户" in q) and "符合" in answer:
        add("account_days", ACCOUNT_DAYS_ANSWER)
    if "周期" in q and "多久" in q and "下证" in answer and ("1-3天" in answer or "1-3个工作日" in answer):
        add("cert_days", CERT_DAYS_ANSWER)
    return hits


def remote_confirm_kind(text: str) -> str | None:
    """客服确认「人不到香港也能办」。长段获客话术不算。"""
    explicit = (
        "不需要到香港", "不用去香港", "不用到香港", "人在内地也可以",
        "内地也可以", "不用本人", "人不到香港也可以", "可以远程",
    )
    for part in re.split(r"[。！？\n]+", text):
        cleaned = scrub(part)
        if not cleaned or any(hint in cleaned for hint in LEAD_HINTS):
            continue
        if any(phrase in cleaned for phrase in explicit):
            return "explicit"
        compact = re.sub(r"[。！!,，~～\s]", "", cleaned)
        if len(compact) <= 12 and re.fullmatch(r"(老板|您好|你好)*(也?可以的?|可以办理|也能办)(老板)?", compact):
            return "also" if "也" in compact else "yes"
    return None


def classify_many(text: str, topic: str) -> list[str]:
    t = PUNCT_RE.sub("", text)
    if not t:
        return []
    if t.lower() in CONTACT_WORDS:
        return ["contact"]
    found: list[str] = []

    def add(intent: str):
        if intent not in found:
            found.append(intent)

    if "代理记账" in t:
        add("bookkeeping")
    if any(k in t for k in ("地址挂靠", "秘书地址", "加地址", "地址多少钱", "地址费用", "地址费", "挂靠")):
        add("address_fee")

    bankish = any(k in t for k in ("对公", "开户", "公户", "银行账户", "银行开户"))
    remoteish = REMOTE_Q_RE.search(t) is not None
    if bankish and remoteish:
        add("bank_remote")
    elif remoteish:
        add("reg_remote")
    if any(k in t for k in ("个人户", "公司户")):
        add("bank_type")
    if any(k in t for k in ("哪些银行", "什么银行", "汇丰", "渣打")):
        add("bank_list")
    if any(k in t for k in ("多久", "几天", "多长时间")) and "下户" not in t and "下公户" not in t and not ("周期" in t and "多久" in t):
        add("duration")
    if any(k in t for k in ("续签", "身份", "创业经营")):
        add("identity")
    if "个体" in t:
        add("individual")
    if "新加坡" in t:
        add("singapore")
    if "牌照" in t:
        add("license")
    if "商标" in t:
        add("trademark")
    if "执照" in t:
        add("biz_license")
    if "注册地址" in t and any(k in t for k in ("哪里", "在哪")):
        add("reg_addr_where")

    materialish = any(k in t for k in ("材料", "资料", "条件"))
    if materialish:
        if any(k in t for k in ("开户", "银行", "对公")) or topic == "bank":
            add("bank_open")
        else:
            add("reg_materials")

    priceish = any(k in t for k in (
        "多少钱", "怎么收费", "如何收费", "什么价格", "什么价", "费用", "收费", "优惠", "活动价", "报价",
    ))
    if priceish:
        add(_price_intent(t, topic))
        if "审计" in t and "年审" in t.replace("审计", ""):
            add("audit_fee")
            add("annual_fee")
    elif "年审" in t:
        add("annual_fee")
    elif "审计" in t:
        add("audit_fee")

    if any(k in t for k in ("流程", "怎么注册", "如何注册", "怎样注册", "怎么办理", "如何办理")):
        add("process")
    if any(k in t for k in (
        "怎么联系", "如何联系", "怎样联系", "怎么沟通", "如何沟通", "加微信", "加我微信",
        "微信联系", "联系你们", "怎么加你", "怎么加您",
    )) or ("联系方式" in t and not priceish and not materialish):
        add("contact")
    if any(k in t for k in ("什么服务", "哪些服务", "业务范围", "能做什么", "介绍一下", "咱们的服务", "你们的服务")):
        add("services")
    if not priceish and (
        any(k in t for k in ("开银行账户", "银行开户", "我要开户", "开公户", "开个户"))
        or t in {"开户", "银行开户"}
        or ("开户" in t and "reg_materials" not in found and not remoteish)
    ):
        add("bank_open")

    if found:
        return found
    if t in {
        "注册", "公司注册", "香港公司注册", "我要注册香港公司", "注册香港公司", "注册公司",
        "香港开公司", "香港公司", "我要办公司", "我要注册", "开公司", "我想注册香港公司",
        "想注册香港公司", "想注册公司", "我要开公司",
    } or re.fullmatch(r"(我想|我要|想|需要)?注册(一家|一个)?(香港)?公司", t):
        return ["reg_interest"]
    return []


def _price_intent(text: str, topic: str) -> str:
    """句里点了名的业务优先；只有「多少钱」这类短问才沿用当前话题。"""
    if "审计" in text and "年审" not in text.replace("审计", ""):
        return "audit_fee"
    if "年审" in text:
        return "annual_fee"
    if "地址" in text or "挂靠" in text:
        return "address_fee"
    if any(key in text for key in ("开户", "银行", "对公")):
        return "bank_price"
    if any(key in text for key in ("注册", "香港公司", "香港")):
        return "reg_price"
    short = text in {"多少钱", "怎么收费", "什么价格", "什么价", "费用多少", "多少费用", "收费", "费用", "优惠"}
    if short or len(text) <= 8:
        return {
            "annual": "annual_fee",
            "audit": "audit_fee",
            "address": "address_fee",
            "bank": "bank_price",
        }.get(topic, "reg_price")
    return "reg_price"


def topic_of(intents: list[str], previous: str) -> str:
    mapping = {
        "reg_materials": "reg", "reg_price": "reg", "reg_remote": "reg", "reg_interest": "reg",
        "address_fee": "address", "identity": "reg", "individual": "reg",
        "bank_remote": "bank", "bank_open": "bank", "bank_type": "bank",
        "bank_list": "bank", "bank_price": "bank",
        "annual_fee": "annual", "audit_fee": "audit", "bookkeeping": "audit",
    }
    for intent in reversed(intents):
        if intent in mapping:
            return mapping[intent]
    return previous


def biz_topic(biz: str) -> str:
    return {"注册": "reg", "开户": "bank", "执照": "license"}.get((biz or "").strip(), "")


def load_conversations_from_bytes(blob: bytes):
    """从 xlsx 字节清洗会话。不落盘。"""
    try:
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            data = zf.read("xl/worksheets/sheet1.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ValueError("不是可解析的历史对话表") from exc
    return _conversations_from_sheet(data)


def load_conversations(path: Path):
    return load_conversations_from_bytes(Path(path).read_bytes())


def _first_reply_row(hits: list[dict], intent: str, title: str, category: str, answer: str, alt: str = "") -> dict | None:
    matched = [item for item in hits if item["intent"] == intent and item["reply"] == answer]
    if not matched:
        return None
    if not answer_is_clean(answer, intent):
        return None
    book = PhraseBook()
    cids = set()
    meta = {}
    for item in matched:
        book.add(item["question"], item["cid"])
        cids.add(item["cid"])
        meta[item["cid"]] = {"brand": item["brand"], "created": item["created"]}
    asked_cids = {item["cid"] for item in hits if item["intent"] == intent}
    standard = book.counts()[0][0]
    return {
        "标题": title,
        "分类": category,
        "标准问": standard,
        "相似问": similar_lines(book, standard),
        "答案": answer,
        "答案状态": "可审核",
        "问题次数": len(asked_cids or cids),
        "答案支撑次数": len(cids),
        "备选答案": alt,
        "样例对话ID": sample_ids(cids),
        "品牌分布": brand_text(cids, meta),
        "时间范围": time_span(cids, meta),
    }


def apply_first_reply_faqs(first_hits: list[dict], faq_rows: list[dict], gap_rows: list[dict]):
    """答案只用顾客下一句的原话。两句次数相同或只差一个词时，第二句放备选，不拼成新句子。"""
    specs = [
        ("reg_interest", "想注册香港公司", "注册", REG_CONTACT, REG_CONTACT_PHONE),
        ("bank_open", "银行开户", "开户", BANK_TYPE_POLITE, BANK_TYPE_SHORT),
        ("price_contact", "办理业务什么价格", "注册", REG_CONTACT_PHONE, REG_CONTACT),
        ("process", "办理流程", "服务与联系", REG_CONTACT, ""),
        ("bank_price", "开户费用", "开户", REG_CONTACT, ""),
        ("bank_remote", "人不到香港能否办对公开户", "开户", REG_CONTACT, ""),
        ("duration", "办理要多久", "服务与联系", REG_CONTACT, ""),
        ("address_fee", "秘书地址是否另收费", "注册", REG_CONTACT, ""),
        ("biz_license", "执照", "服务与联系", REG_CONTACT, ""),
        ("reg_addr_where", "注册地址在哪里", "注册", REG_CONTACT, ""),
        ("identity", "香港身份续签", "注册", IDENTITY_WAIT, ""),
        ("company_acct", "开公司户", "开户", COMPANY_ACCOUNT, ""),
        ("personal_acct", "开个人户", "开户", PERSONAL_ACCOUNT, ""),
        ("solo_fee", "办理个人户费用", "注册", SOLO_FEE, ""),
    ]
    # 公司户、个人户、个人户费用按原句归类，不跟意图里的其它问法混在一起
    hits = []
    for item in first_hits:
        copied = dict(item)
        if copied["reply"] == COMPANY_ACCOUNT and "公司户" in copied["question"] and copied["question"] != "我要开银行账户":
            copied["intent"] = "company_acct"
        elif copied["reply"] == PERSONAL_ACCOUNT and "个人户" in copied["question"]:
            copied["intent"] = "personal_acct"
        elif copied["reply"] == SOLO_FEE:
            copied["intent"] = "solo_fee"
        hits.append(copied)
    built = []
    for intent, title, category, answer, alt in specs:
        row = _first_reply_row(hits, intent, title, category, answer, alt)
        if row:
            built.append(row)
    if not built:
        return faq_rows, gap_rows
    titles = {row["标题"] for row in built}
    faq_rows = [row for row in faq_rows if row["标题"] not in titles]
    gap_rows = [row for row in gap_rows if row["标题"] not in titles]
    if any(row["标题"] in ("开公司户", "开个人户") for row in built):
        gap_rows = [row for row in gap_rows if row["标题"] != "公司户还是个人户"]
    faq_rows.extend(built)
    faq_rows.sort(key=lambda row: (-row["问题次数"], -row["答案支撑次数"], row["标题"]))
    return faq_rows, gap_rows


def clean_xlsx_bytes(blob: bytes):
    convs, stats, _valid_values, _missed_values = load_conversations_from_bytes(blob)
    questions, paired, supports, asked, meta, first_hits = collect(convs)
    faq_rows, gap_rows = build_rows(questions, paired, supports, asked, meta)
    faq_rows, gap_rows = apply_first_reply_faqs(first_hits, faq_rows, gap_rows)
    return faq_rows, gap_rows, stats


def _conversations_from_sheet(data: bytes):
    stats: dict = Counter()
    value_valid: Counter = Counter()
    value_missed: Counter = Counter()
    convs = []
    for match in ROW_RE.finditer(data):
        if int(match.group(1)) == 1:
            continue
        cells = {}
        for cell in CELL_RE.finditer(match.group(2)):
            col = cell.group(1).decode()
            val = html.unescape((cell.group(3) or b"").decode("utf-8", "replace"))
            cells[col] = val
        valid = cells.get("C", "")
        missed = cells.get("D", "")
        value_valid[valid.strip() or "（空）"] += 1
        value_missed[missed.strip() or "（空）"] += 1
        reason = should_drop(valid, missed)
        if reason:
            stats[reason] += 1
            continue
        content = cells.get("CK", "")
        if not content.strip():
            stats["empty"] += 1
            continue
        stats["kept"] += 1
        convs.append({
            "cid": cells.get("A", "").strip(),
            "biz": cells.get("V", "").strip(),
            "brand": cells.get("Y", "").strip(),
            "created": cells.get("BS", "").strip(),
            "messages": parse_messages(content),
        })
    return convs, stats, value_valid, value_missed


class PhraseBook:
    def __init__(self):
        self.display: dict[str, str] = {}
        self.convs: dict[str, set[str]] = defaultdict(set)

    def add(self, text: str, cid: str):
        key = norm_key(text)
        if not key:
            return
        self.convs[key].add(cid)
        current = self.display.get(key)
        if current is None or len(text) < len(current):
            self.display[key] = text

    def counts(self) -> list[tuple[str, int]]:
        ranked = [(self.display[key], len(cids)) for key, cids in self.convs.items()]
        ranked.sort(key=lambda item: (-item[1], len(item[0]), item[0]))
        return ranked


def collect(convs: list[dict]):
    questions: dict[str, PhraseBook] = defaultdict(PhraseBook)
    paired: dict[str, PhraseBook] = defaultdict(PhraseBook)
    supports: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    asked: dict[str, set[str]] = defaultdict(set)
    meta: dict[str, dict] = {}
    first_hits: list[dict] = []

    for conv in convs:
        cid = conv["cid"]
        meta[cid] = {"brand": conv["brand"], "created": conv["created"]}
        base_topic = biz_topic(conv["biz"])
        topic = base_topic
        # 连续几条顾客快捷提问不能互相继承话题；只有客服接过话，下一句才沿用话题
        inherit_topic = False
        pending_text: str | None = None
        pending_intents: list[str] = []
        seen_fact: set[str] = set()
        seen_remote: set[str] = set()
        recorded_first = False

        for role, body in conv["messages"]:
            if role == "cust":
                pending_text = None
                pending_intents = []
                recorded_first = False
                if is_noise(body):
                    continue
                shown = scrub(body)
                if not shown or shown.lower() in GREETINGS or is_customer_junk(shown):
                    continue
                use_topic = base_topic if is_canned(shown) or not inherit_topic else topic
                intents = classify_many(shown, use_topic)
                if norm_key(shown) == norm_key(PRICE_ASK):
                    intents = [item for item in intents if item != "reg_price"]
                    intents.append("price_contact")
                inherit_topic = False
                pending_text = shown
                pending_intents = intents
                if not intents:
                    continue
                topic = topic_of(intents, use_topic)
                for intent in intents:
                    asked[intent].add(cid)
                    questions[intent].add(shown, cid)
                continue

            inherit_topic = True
            if pending_text is None or recorded_first:
                if pending_text is None:
                    continue
            else:
                reply = scrub(body)
                if reply and not reply.startswith("{") and "CARD" not in reply and "不支持" not in reply:
                    for intent in pending_intents:
                        first_hits.append({
                            "intent": intent,
                            "question": pending_text,
                            "reply": reply,
                            "cid": cid,
                            "brand": conv["brand"],
                            "created": conv["created"],
                        })
                    recorded_first = True
            if pending_text is None:
                continue
            for intent, _fact in extract_agent_facts(body).items():
                bucket = "reg_price" if intent == "reg_price_depends" else intent
                if bucket not in pending_intents or intent in seen_fact:
                    continue
                seen_fact.add(intent)
                supports[bucket][intent].add(cid)
                paired[bucket].add(pending_text, cid)
            if any(item in pending_intents for item in REMOTE_INTENTS) and "remote" not in seen_remote:
                kind = remote_confirm_kind(body)
                remote_intent = next(item for item in pending_intents if item in REMOTE_INTENTS)
                if kind:
                    supports[remote_intent][kind].add(cid)
                    seen_remote.add("remote")
                    paired[remote_intent].add(pending_text, cid)
            for intent, _answer in precise_hits(pending_text, body):
                if intent in seen_fact:
                    continue
                seen_fact.add(intent)
                supports[intent][intent].add(cid)
                asked[intent].add(cid)
                questions[intent].add(pending_text, cid)
                paired[intent].add(pending_text, cid)
    return questions, paired, supports, asked, meta, first_hits


def _prefer_short(pool: list[tuple[str, int]]) -> list[tuple[str, int]]:
    short = [(text, count) for text, count in pool if len(text) <= 40]
    return short or pool


def pick_standard(intent: str, paired_book: PhraseBook, question_book: PhraseBook) -> str:
    pool = _prefer_short(paired_book.counts() or question_book.counts())
    if intent == "contact":
        pool = question_book.counts()
        preferred = [(text, count) for text, count in pool if re.search(r"怎么|如何|怎样|哪里", text)]
        if preferred:
            pool = preferred
        pool = _prefer_short(pool)
    if pool and not re.search(r"怎么|如何|吗|什么|多少|哪些|多久", pool[0][0]):
        top_n = pool[0][1]
        clearer = [
            (text, count) for text, count in pool
            if re.search(r"怎么|如何|吗|什么|多少|哪些|多久", text) and count >= max(2, top_n // 2)
        ]
        if clearer:
            pool = clearer
    if pool:
        return pool[0][0]
    return INTENT_META[intent][2]


def similar_lines(book: PhraseBook, standard: str, limit: int = 20) -> str:
    skip = norm_key(standard)
    lines = []
    for text, _count in book.counts():
        if norm_key(text) == skip or is_noise(text) or text.lower() in GREETINGS or len(text) > 40:
            continue
        lines.append(text)
        if len(lines) >= limit:
            break
    return "\n".join(lines)


def brand_text(cids: set[str], meta: dict[str, dict]) -> str:
    counter: Counter = Counter()
    for cid in cids:
        brand = (meta.get(cid) or {}).get("brand") or ""
        counter[brand or "未填"] += 1
    return "；".join(f"{name} {count}" for name, count in counter.most_common())


def time_span(cids: set[str], meta: dict[str, dict]) -> str:
    dates = []
    for cid in cids:
        created = (meta.get(cid) or {}).get("created") or ""
        matched = DATE_RE.search(created)
        if matched:
            dates.append(matched.group(0))
    if not dates:
        return ""
    return f"{min(dates)} ~ {max(dates)}"


def sample_ids(cids: set[str], limit: int = 8) -> str:
    return "、".join(sorted(cid for cid in cids if cid)[:limit])


def answer_is_clean(text: str, intent: str = "") -> bool:
    banned = BANNED_ANSWER
    if intent in ALLOW_YEARS:
        banned = tuple(word for word in banned if not word.startswith("做了"))
    if intent in VERBATIM_INTENTS:
        banned = tuple(word for word in banned if word not in ("留个联系方式", "联系方式多少", "您联系方式"))
    if intent == "company_acct":
        banned = tuple(word for word in banned if word != "快速下户")
    if any(word in text for word in banned):
        return False
    if PHONE_RE.search(text) or EMAIL_RE.search(text):
        return False
    return True


def remote_answer(fact_map: dict[str, set[str]]) -> tuple[str, set[str]]:
    explicit = fact_map.get("explicit", set())
    also = fact_map.get("also", set())
    yes = fact_map.get("yes", set())
    all_ids = explicit | also | yes
    if len(all_ids) < 2:
        return "", set()
    if explicit:
        return REMOTE_EXPLICIT, all_ids
    if len(also) >= len(yes):
        return "也可以", all_ids
    return "可以", all_ids


def build_rows(questions, paired, supports, asked, meta):
    faq_rows = []
    gap_rows = []
    for intent, (title, category, _fallback) in INTENT_META.items():
        q_ids = asked.get(intent, set())
        qcount = len(q_ids)
        fact_map = supports.get(intent, {})
        alt = ""
        support_ids: set[str] = set()
        answer = ""
        status = ""

        if intent == "reg_price":
            fixed_ids = fact_map.get("reg_price", set())
            depends_ids = fact_map.get("reg_price_depends", set())
            if fixed_ids:
                answer = PRICE_ANSWER
                support_ids = set(fixed_ids)
                status = "活动价待核实"
                if len(depends_ids) >= 5:
                    alt = PRICE_DEPENDS
                    status = "活动价待核实；有冲突"
        elif intent == "contact" and qcount:
            answer = CONTACT_ANSWER
            status = "人工归纳"
        elif intent in REMOTE_INTENTS:
            answer, support_ids = remote_answer(fact_map)
            if answer:
                status = "可审核"
        elif intent in PRECISE_ANSWERS:
            support_ids = set(fact_map.get(intent, set()))
            if support_ids:
                answer = PRECISE_ANSWERS[intent]
                status = PRECISE_STATUS.get(intent, "可审核")
        else:
            for ids in fact_map.values():
                support_ids |= set(ids)
            if len(support_ids) >= 2:
                answer = {
                    "reg_materials": MATERIALS_ANSWER,
                    "bank_list": BANK_ANSWER,
                    "audit_fee": AUDIT_ANSWER,
                    "annual_fee": ANNUAL_ANSWER,
                    "bookkeeping": BOOK_ANSWER,
                    "services": SERVICE_ANSWER,
                }.get(intent, "")
                if answer:
                    status = "可审核"

        if answer and not answer_is_clean(answer, intent):
            answer, status, alt = "", "", ""
        if alt and not answer_is_clean(alt, intent):
            alt = ""

        evidence = support_ids or q_ids
        standard = pick_standard(intent, paired[intent], questions[intent])
        similar_source = paired[intent] if intent != "contact" else questions[intent]
        similars = similar_lines(similar_source, standard)
        if answer:
            faq_rows.append({
                "标题": title,
                "分类": category,
                "标准问": standard,
                "相似问": similars,
                "答案": answer,
                "答案状态": status,
                "问题次数": qcount,
                "答案支撑次数": len(support_ids),
                "备选答案": alt,
                "样例对话ID": sample_ids(support_ids or q_ids),
                "品牌分布": brand_text(evidence, meta),
                "时间范围": time_span(evidence, meta),
            })
        elif qcount:
            gap_rows.append({
                "标题": title,
                "分类": category,
                "标准问": standard,
                "相似问": similars,
                "问题次数": qcount,
                "样例对话ID": sample_ids(q_ids),
            })
    faq_rows.sort(key=lambda row: (-row["问题次数"], -row["答案支撑次数"], row["标题"]))
    gap_rows.sort(key=lambda row: (-row["问题次数"], row["标题"]))
    return faq_rows, gap_rows


def write_xlsx(path: Path, faq_rows: list[dict], gap_rows: list[dict]):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E79")
    wrap = Alignment(wrap_text=True, vertical="top")

    def fill(ws, headers, rows):
        ws.append(headers)
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for row in rows:
            ws.append([ILLEGAL_XLSX.sub("", str(row.get(header, ""))) for header in headers])
        for excel_row in ws.iter_rows(min_row=2):
            for cell in excel_row:
                cell.alignment = wrap
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        wide = {"C", "D", "E", "I"}
        for col in ws.columns:
            letter = col[0].column_letter
            ws.column_dimensions[letter].width = 42 if letter in wide else (28 if letter == "A" else 18)

    faq_sheet = wb.active
    faq_sheet.title = "FAQ草稿"
    fill(faq_sheet, FAQ_HEADERS, faq_rows)
    gap_sheet = wb.create_sheet("待补答案")
    fill(gap_sheet, GAP_HEADERS, gap_rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def materials_in_order(answer: str) -> bool:
    parts = ["1：公司名称", "2：地址", "3：经营范围", "4：注册资金", "5：股东董事"]
    pos = 0
    for part in parts:
        found = answer.find(part, pos)
        if found < 0:
            return False
        pos = found + len(part)
    return True


def verify(faq_rows: list[dict], gap_rows: list[dict]) -> list[str]:
    problems = []
    by_title = {row["标题"]: row for row in faq_rows}
    titles = set(by_title) | {row["标题"] for row in gap_rows}
    materials = by_title.get("香港公司注册资料")
    if materials is None or not materials_in_order(materials["答案"]):
        problems.append("注册资料五条不完整或顺序不对")
    price = by_title.get("香港公司注册费用")
    if price is None or "3980" not in price["答案"] or "待核实" not in price["答案状态"]:
        problems.append("3980 没有单独成条或未标待核实")
    for row in faq_rows:
        blob = f"{row['答案']}\n{row['备选答案']}"
        if "2800" in blob or "4430" in blob:
            problems.append(f"{row['标题']} 答案含偶发价格")
        if "200号人" in blob:
            problems.append(f"{row['标题']} 答案含获客或宣传句")
        if row["标题"] in ("香港公司注册资料", "香港公司有什么好处", "业务范围", "银行开户", "香港公司注册费用") and "联系方式" in blob:
            problems.append(f"{row['标题']} 不该追加留资")
        if row["标题"] != "办理靠不靠谱" and any(word in blob for word in ("做了10年", "做了快10年", "做了9年")):
            problems.append(f"{row['标题']} 答案含年限宣传")
        if PHONE_RE.search(blob):
            problems.append(f"{row['标题']} 答案含手机号")
    remote = {"人不到香港能否注册", "人不到香港能否办对公开户"}
    if remote - titles:
        problems.append("内地注册和内地开户没有分成两条")
    trust = by_title.get("办理靠不靠谱")
    if trust is None or "做了快10年" not in trust["答案"] or "代办香港公司靠谱吗" in (trust["相似问"] or ""):
        problems.append("靠谱问答没有按同一句答案拆开")
    legit = by_title.get("公司是否正规")
    if legit is None or "正规" not in legit["答案"] or legit["答案"] == (trust or {}).get("答案"):
        problems.append("正规没有单独成条")
    benefit4 = by_title.get("香港公司有什么好处")
    benefit5 = by_title.get("注册香港公司能做什么")
    if benefit4 is None or "税务优势" not in benefit4["答案"] or "资金自由" in benefit4["答案"]:
        problems.append("好处四条被并进了五条")
    if benefit5 is None or "资金自由" not in benefit5["答案"]:
        problems.append("能干啥没有单独留下第五点")
    audit = by_title.get("审计费用")
    zero = by_title.get("无流水审计多少钱")
    if zero is None or "1500" not in zero["答案"] or "待核实" not in zero["答案状态"]:
        problems.append("无流水审计没有单独标价")
    if audit and ("1500" in audit["答案"] or "无流水" in (audit["相似问"] or "")):
        problems.append("1500 写进了审计费用")
    if "没有运营" in f"{(zero or {}).get('标准问', '')}\n{(zero or {}).get('相似问', '')}":
        problems.append("没有运营被当成无流水审计的问法")
    account_days = by_title.get("多久下公户")
    cert_days = by_title.get("注册下证要多久")
    if account_days is None or "符合" not in account_days["答案"]:
        problems.append("下公户没有单独成条")
    if cert_days is None or "1-3天" not in cert_days["答案"]:
        problems.append("下证天数没有单独成条")
    if account_days and cert_days and account_days["答案"] == cert_days["答案"]:
        problems.append("下户和下证被收成同一句")
    register = by_title.get("想注册香港公司")
    if register is None or register["答案"] != REG_CONTACT or register["备选答案"] != REG_CONTACT_PHONE:
        problems.append("注册开场下一句不是留联系方式原句")
    opened = by_title.get("银行开户")
    if opened is None or opened["答案"] != BANK_TYPE_POLITE or "联系方式" in opened["答案"] or opened["备选答案"] != BANK_TYPE_SHORT:
        problems.append("开户下一句被改成留资或拼句")
    asked_price = by_title.get("办理业务什么价格")
    if asked_price is None or "3980" in asked_price["答案"] or asked_price["答案"] != REG_CONTACT_PHONE:
        problems.append("价格问法的下一句被写成了3980")
    if price and price["标准问"] == PRICE_ASK:
        problems.append("3980 用了价格问法做标准问")
    if audit is None or audit["答案"] != AUDIT_ANSWER or "留个联系方式" not in audit["答案"]:
        problems.append("审计答案不是带留资的原句")
    annual = by_title.get("年审费用")
    if annual is None or annual["答案"] != AUDIT_ANSWER:
        problems.append("年审下一句被改写成另一句")
    services = by_title.get("业务范围")
    if services is None or services["答案"] != SERVICE_ANSWER or "联系方式多少" in services["答案"]:
        problems.append("服务范围答案不是原句")
    identity = by_title.get("香港身份续签")
    if identity is None or identity["答案"] != IDENTITY_WAIT:
        problems.append("身份续签答案不是原句")
    company = by_title.get("开公司户")
    personal = by_title.get("开个人户")
    if company is None or "快速下户" not in company["答案"] or company["答案"] != COMPANY_ACCOUNT:
        problems.append("开公司户答案被改写")
    if personal is None or personal["答案"] != PERSONAL_ACCOUNT or personal["答案"] == (company or {}).get("答案"):
        problems.append("开个人户和开公司户被合成一句")
    solo = by_title.get("办理个人户费用")
    if solo is None or solo["答案"] != SOLO_FEE:
        problems.append("个人户费用答案不是原句")
    for name in ("新加坡公司", "牌照", "商标"):
        row = by_title.get(name)
        if row and row["答案"]:
            problems.append(f"{name} 只有一句原话，不应编统一答案")
    return problems


def self_check():
    assert should_drop("无效", "未遗漏") == "invalid"
    assert should_drop("有效", "遗漏") == "missed"
    assert should_drop("有效", "未遗漏") is None
    sample = (
        "顾客 2026-09-29 10:09:11 注册需要什么条件&#xA;"
        "刘小姐 2026-09-29 10:09:22 您好，注册香港公司主要涉及：&#xA;"
        "1：公司名称&#xA;"
        "2：地址（我司提供），&#xA;"
        "3：经营范围&#xA;"
        "4：注册资金&#xA;"
        "5: 股东董事相关身份信息&#xA;"
        "刘小姐 2026-09-29 10:09:30 您这边联系方式多少呢"
    )
    messages = parse_messages(sample)
    assert len(messages) == 3, messages
    assert "1：公司名称" in messages[1][1] and "股东董事" in messages[1][1]
    facts = extract_agent_facts(messages[1][1])
    assert facts.get("reg_materials") == MATERIALS_ANSWER
    assert "联系方式" not in facts["reg_materials"]
    assert "reg_price" not in extract_agent_facts(messages[2][1])
    assert classify_many("注册需要什么条件", "") == ["reg_materials"]
    assert classify_many("我要注册香港公司", "") == ["reg_interest"]
    assert "bank_remote" in classify_many("内陆能办港企对公吗", "")
    assert "reg_remote" in classify_many("不用去香港可办", "reg")
    assert "reg_remote" not in classify_many("比内地贵这么多么", "")
    assert "reg_remote" not in classify_many("不用了香港是港币", "")
    assert _price_intent("我要办理业务什么价格", "annual") == "reg_price"
    assert _price_intent("多少钱", "annual") == "annual_fee"
    assert _price_intent("每年审计又多少钱", "annual") == "audit_fee"
    assert "reg_remote" not in classify_many("内地人在香港办公司", "")
    assert is_customer_junk("已预约，提供了我的联系方式，尽快和我联系")
    fake = [{
        "cid": "1", "biz": "", "brand": "", "created": "2026-09-01",
        "messages": [
            ("cust", "公司年审怎么收费"),
            ("cust", "我要办理业务，什么价格？"),
        ],
    }]
    questions, *_rest = collect(fake)
    annual_texts = [text for text, _count in questions["annual_fee"].counts()]
    price_texts = [text for text, _count in questions["reg_price"].counts()]
    assert annual_texts == ["公司年审怎么收费"], annual_texts
    assert "我要办理业务，什么价格？" not in price_texts
    assert "reg_price" in classify_many("怎么收费", "reg")
    assert "annual_fee" in classify_many("公司年审怎么收费", "")
    promo = "现在是我们10周年庆典3980的活动价(含3900的官费)，包含秘书服务，秘书地址，绿盒子全套，您留个联系方式"
    assert extract_agent_facts(promo)["reg_price"] == PRICE_ANSWER
    assert "留个联系方式" not in PRICE_ANSWER and "做了10年" not in PRICE_ANSWER
    assert remote_confirm_kind("也可以的老板") == "also"
    assert remote_confirm_kind("您留个联系方式，也可以的老板") is None
    assert classify_many("微信", "") == ["contact"]
    assert classify_many("加我", "") == ["contact"]
    assert "bank_type" not in classify_many("个人", "")
    assert "bank_type" not in classify_many("公司", "")
    trust_hit = precise_hits(
        "这个办理靠谱吗",
        "我们是做了快10年的本地服务商，渠道什么的都比较稳定靠谱，您考虑预留个联系方式吗？",
    )
    assert trust_hit == [("trust_years", TRUST_ANSWER)], trust_hit
    assert precise_hits("代办香港公司靠谱吗", "靠谱哈") == []
    assert precise_hits("是正规的么", "肯定是正规的呀")[0][0] == "legit_company"
    four = precise_hits("香港公司有什么好处", "1.税务优势 2.经营范围极少限制 3.低成本 4.注册与维护更便捷")
    assert four[0][0] == "benefit4" and "资金自由" not in four[0][1]
    five = precise_hits("注册香港公司能干啥", "税务优势，资金自由，取名自由")
    assert five[0][0] == "benefit5"
    assert precise_hits("内地经营健身房有必要吗？好处有哪些", "税务优势") == []


def default_input() -> Path:
    desktop = Path.home() / "Desktop"
    named = desktop / "历史对话_20261013092029_52a298b6c387706f.xlsx"
    if named.exists():
        return named
    found = sorted(desktop.glob("历史对话_*.xlsx"))
    if not found:
        raise SystemExit(f"找不到历史对话 xlsx：{named}")
    return found[-1]


def main(argv: list[str] | None = None) -> int:
    self_check()
    parser = argparse.ArgumentParser(description="美洽历史对话洗成待审核 FAQ 草稿")
    parser.add_argument("--input", type=Path, default=None, help="美洽导出的 xlsx")
    parser.add_argument("--output", type=Path, default=None, help="FAQ 草稿 xlsx，默认桌面")
    args = parser.parse_args(argv)
    src = args.input or default_input()
    dest = args.output or (Path.home() / "Desktop" / "FAQ草稿.xlsx")
    if not src.exists():
        raise SystemExit(f"输入文件不存在：{src}")

    convs, stats, value_valid, value_missed = load_conversations(src)
    questions, paired, supports, asked, meta, first_hits = collect(convs)
    faq_rows, gap_rows = build_rows(questions, paired, supports, asked, meta)
    faq_rows, gap_rows = apply_first_reply_faqs(first_hits, faq_rows, gap_rows)
    try:
        write_xlsx(dest, faq_rows, gap_rows)
    except PermissionError:
        raise SystemExit(f"无法写入 {dest}，请先关闭已打开的 Excel 文件") from None

    print(f"输入: {src}")
    print(f"输出: {dest}")
    print(f"有效对话取值: {dict(value_valid)}")
    print(f"遗漏对话取值: {dict(value_missed)}")
    print(f"跳过无效: {stats['invalid']}")
    print(f"跳过遗漏: {stats['missed']}")
    print(f"跳过空正文: {stats['empty']}")
    print(f"纳入会话: {stats['kept']}")
    print(f"FAQ草稿: {len(faq_rows)}")
    print(f"待补答案: {len(gap_rows)}")
    print("--- FAQ ---")
    for row in faq_rows:
        print(
            f"{row['问题次数']:>5} 支撑{row['答案支撑次数']:<5} "
            f"[{row['答案状态']}] {row['标题']} | {row['标准问']}"
        )
    print("--- 待补 ---")
    for row in gap_rows:
        print(f"{row['问题次数']:>5} {row['标题']} | {row['标准问']}")
    problems = verify(faq_rows, gap_rows)
    if problems:
        print("核对未通过:")
        for item in problems:
            print(" -", item)
        return 1
    print("核对通过: 资料五条完整；3980 单独待核实；获客话术和宣传句不在答案里；内地注册与内地开户分成两条。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
