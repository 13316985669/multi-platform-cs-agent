"""留资话术：只用历史对话里的原句。手机号必须是当前这条消息里的完整 11 位。"""
from __future__ import annotations

import re
from datetime import datetime

ASK_PREFIX = "您这边联系方式多少呢，我们专员给您讲解一下"
ASK_FULL = "您这边联系方式多少呢，我们专员给您讲解一下办理的流程、资料和费用，让您参考了解下"
ARRANGE = "好的，这就给您安排专员，请稍等1-3分钟"
BANK_TYPE = "您好，您这边是想开公司户还是个人户呢？"

_PHONE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_WECHAT = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{4,19}$")
# 前缀式：（任意引导语）+ 微信/微信号/weixin/wx/vx + 分隔符 + ID。允许纯数字 ID（QQ 风格），「微信」字样即用户明示。
# ID 必须顶到句尾，避免把「微信可以加吗」这类问句里的词误当 ID。
_WECHAT_PREFIX = re.compile(
    r"^(?:.*?)\s*(?:微信号?|weixin|wx|vx)(?:号)?[:：是为是\s，,、]*([A-Za-z0-9_-]{5,20})$", re.IGNORECASE,
)
# 后缀式：ID + 微信（真实语料有「278158820微信」这种写法）。
_WECHAT_SUFFIX = re.compile(
    r"^([A-Za-z0-9_-]{5,20})(?:微信|weixin|wx|vx)$", re.IGNORECASE,
)
# 尾部常见收尾标点与客套词（可连用，如「，谢谢」），识别前剥掉。
_TRAILING_PUNCT_RE = re.compile(
    r"(?:[。．.!！?？，,、;；~～]|谢谢|多谢|麻烦了|辛苦了|哈|呀|呢|啦|哦|噢)+$"
)
_HANDOFF = ("投诉", "退款", "赔偿", "转人工", "真人", "人工客服")
_BUSINESS = (
    "注册香港公司",
    "开银行账户",
    "银行开户",
    "身份续签",
    "秘书地址",
    "地址挂靠",
    "开户",
    "年审",
    "审计",
    "执照",
    "牌照",
    "商标",
    "新加坡",
)


def phone_in_message(text: str) -> str:
    matched = _PHONE.search(text or "")
    return matched.group(1) if matched else ""


def wechat_in_message(text: str) -> str:
    """识别用户留下的微信号。前缀/后缀式允许纯数字 ID（明示「微信」）；裸消息只认字母开头。"""
    token = re.sub(r"\s+", "", text or "")
    token = _TRAILING_PUNCT_RE.sub("", token)
    if token.lower() in {"hello", "wechat", "weixin", "wx", "vx", "v", "微信", "微信号"}:
        return ""
    matched = _WECHAT_PREFIX.match(token)
    if matched:
        return matched.group(1)
    matched = _WECHAT_SUFFIX.match(token)
    if matched:
        return matched.group(1)
    if _WECHAT.match(token):
        return token
    return ""


def must_handoff(text: str) -> bool:
    raw = text or ""
    return any(word in raw for word in _HANDOFF)


def business_phrase(user_text: str, recent: list[dict] | None = None) -> str:
    parts = [user_text or ""]
    for row in recent or []:
        if row.get("sender_type") == "user":
            parts.append(row.get("content") or "")
    blob = "\n".join(parts)
    for phrase in _BUSINESS:
        if phrase in blob:
            return phrase
    return ""


def contact_ask(business: str) -> str:
    if business:
        return f"{ASK_PREFIX}{business}"
    return ASK_FULL


def _mentions_account(user_text: str, recent: list[dict] | None) -> bool:
    blob = user_text or ""
    for row in recent or []:
        if row.get("sender_type") == "user":
            blob += "\n" + (row.get("content") or "")
    return any(word in blob for word in ("开户", "开银行账户", "银行账户"))


def library_script(category: str) -> str:
    """从已启用的话术库取一条原句。没有则返回空。"""
    from ..database import SessionLocal
    from ..models import KnowledgeDoc

    db = SessionLocal()
    try:
        doc = (
            db.query(KnowledgeDoc)
            .filter(
                KnowledgeDoc.doc_type == "faq",
                KnowledgeDoc.status == "active",
                KnowledgeDoc.category == category,
            )
            .order_by(KnowledgeDoc.hit_count.desc(), KnowledgeDoc.id.asc())
            .first()
        )
        if doc is None:
            return ""
        _question, _, answer = (doc.content or "").partition("\n")
        return answer.removeprefix("答：").removeprefix("答:").strip()
    finally:
        db.close()


def wants_to_proceed(text: str) -> bool:
    """明确要办理。接着问资料、费用、流程不算。"""
    raw = text or ""
    return any(phrase in raw for phrase in (
        "我要注册", "我要办", "帮我办", "想注册", "想办理", "怎么办理",
        "可以，办", "可以办吧", "办吧", "给我办", "我要开户",
    ))


def script_for_miss(user_text: str, recent: list[dict] | None = None) -> str | None:
    """知识没命中时用的原句。投诉和要真人返回 None，交给转人工。"""
    if must_handoff(user_text):
        return None
    if phone_in_message(user_text) or wechat_in_message(user_text):
        return ARRANGE
    text = user_text or ""
    named_type = "个人户" in text or "公司户" in text
    wants_open = any(word in text for word in ("开户", "开银行账户", "银行账户"))
    if wants_open and not named_type:
        return BANK_TYPE
    if named_type and _mentions_account(text, recent):
        return contact_ask("开户")
    return contact_ask(business_phrase(text, recent))


def save_lead(*, customer_id: int, conversation_id: int, platform: str, nickname: str,
              contact: str, contact_type: str, business: str) -> None:
    """同一会话同一号码只记一行。客户身上的最近一次留资同时更新。"""
    from ..database import SessionLocal
    from ..models import Customer, LeadCapture

    contact = (contact or "").strip()
    if not contact:
        return
    db = SessionLocal()
    try:
        exists = (
            db.query(LeadCapture)
            .filter(LeadCapture.conversation_id == conversation_id, LeadCapture.contact == contact)
            .first()
        )
        if exists is None:
            db.add(LeadCapture(
                customer_id=customer_id,
                conversation_id=conversation_id,
                platform=platform or "",
                contact=contact,
                contact_type=contact_type,
                nickname=(nickname or "")[:64],
                business=(business or "")[:64],
            ))
        customer = db.get(Customer, customer_id)
        if customer is not None:
            if contact_type == "phone":
                customer.lead_phone = contact
            elif contact_type == "wechat":
                customer.lead_wechat = contact
            if business:
                customer.lead_note = business[:256]
            customer.updated_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()
