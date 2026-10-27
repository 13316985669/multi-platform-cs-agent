"""评论回复规则匹配：关键词 + 意图 + 优先级。

规则为空时由引擎按开关决定是否 LLM 生成兜底回复。
内置默认规则在首次启动时落库（可后台修改），保证开箱可用的「问价/咨询 → 暗号引导私信」链路。
"""
import logging
import random

from sqlalchemy.orm import Session

from ..models import CommentRule

logger = logging.getLogger(__name__)

# 内置默认规则（init_default_rules 落库；模板中 {code} 会被替换为暗号）
DEFAULT_RULES = [
    {
        "platform": "", "intent": "price",
        "keywords": ["多少钱", "怎么卖", "价格", "价"],
        "reply_templates": ["价格私您啦，评论区不方便发哈", "宝子私信我一下，给您发报价~"],
        "guide_code": "价格", "priority": 10,
    },
    {
        "platform": "", "intent": "consult",
        "keywords": ["怎么买", "哪里买", "链接", "咨询", "怎么联系"],
        "reply_templates": ["已私您啦，注意看私信哈", "私信您啦宝子，记得看消息~"],
        "guide_code": "咨询", "priority": 10,
    },
    {
        "platform": "", "intent": "praise",
        "keywords": ["好用", "不错", "喜欢", "种草"],
        "reply_templates": ["谢谢宝子喜欢🥰", "感谢支持呀"],
        "guide_code": "", "priority": 5,
    },
]


def init_default_rules(db: Session):
    """首次启动写入默认规则（已有规则则不重复写）。"""
    if db.query(CommentRule).count() > 0:
        return
    for r in DEFAULT_RULES:
        db.add(CommentRule(**r, enabled=True))
    db.commit()
    logger.info("已写入 %d 条默认评论回复规则", len(DEFAULT_RULES))


def match_rule(db: Session, content: str, platform: str, intent: str = "") -> CommentRule | None:
    """匹配规则：平台匹配（空=全平台）→ 关键词或意图命中 → priority 降序取第一。"""
    rules = (
        db.query(CommentRule)
        .filter(CommentRule.enabled.is_(True))
        .order_by(CommentRule.priority.desc(), CommentRule.id)
        .all()
    )
    for rule in rules:
        if rule.platform and rule.platform != platform:
            continue
        keyword_hit = any(k and k in content for k in (rule.keywords or []))
        intent_hit = rule.intent and rule.intent == intent
        # 规则需至少一个匹配维度；两个维度都配置时要求同时命中（更精准）
        if rule.keywords and rule.intent:
            if keyword_hit and intent_hit:
                return rule
        elif keyword_hit or intent_hit:
            return rule
    return None


def render_reply(rule: CommentRule) -> tuple[str, str]:
    """从规则模板池随机选一条并替换 {code} 变量。返回 (回复文本, 暗号)。"""
    templates = rule.reply_templates or []
    if not templates:
        return "", rule.guide_code or ""
    text = random.choice(templates)
    code = rule.guide_code or ""
    return text.replace("{code}", code).strip(), code
