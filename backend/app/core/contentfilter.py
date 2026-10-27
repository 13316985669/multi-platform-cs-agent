"""内容安全守卫：出口违禁词过滤 + 入口风险分级 + 留资脱敏。

出口（AI 回复）：
- 发送前扫描广告法违禁词，命中则安全改写（替换为合规表述）
- 改写后仍命中（无法安全改写）则返回需转人工标记

入口（用户消息）：
- check_inbound 按 block/warn 两级分级：block=涉政暴恐等严重违规（转人工、AI 不回复），warn=辱骂嘲讽（打标记录、正常处理）
- DB 中 BannedWord.direction="in" 的词会合并进来（category 字段存级别：block | warn）

留资信息（手机号/微信号）在日志中脱敏。
"""
import re

# 内置广告法违禁词库（可在后台维护扩充，DB 中的 BannedWord 会合并进来）
DEFAULT_BANNED_WORDS: dict[str, str] = {
    # 极限词
    "最好": "很好", "最佳": "很好", "第一": "领先", "全网最低": "很优惠",
    "最便宜": "很实惠", "最低价": "优惠价", "顶级": "高品质", "极致": "出色",
    "万能": "多功能", "绝对": "非常", "永久": "持久", "100%": "",
    "国家级": "", "世界级": "", "全球首发": "新品首发", "销量第一": "热销",
    # 医疗/功效承诺
    "治疗": "改善", "治愈": "", "根治": "", "药到病除": "", "包治百病": "",
    "抗癌": "", "防癌": "", "壮阳": "", "减肥神效": "",
    # 承诺词
    "稳赚不赔": "", "保本": "", "无风险": "低风险", " guaranteed": "",
}

# ============ 入口风险词库（用户消息分级） ============

# block 级：涉政/暴恐/违法，命中即转人工且 AI 不回复
DEFAULT_INBOUND_BLOCK_WORDS: list[str] = [
    "炸弹", "爆炸物", "枪支买卖", "毒品", "冰毒", "摇头丸",
    "办证刻章", "假币", "洗钱", "恐怖组织",
]

# warn 级：辱骂/恶意骚扰，打标记录但正常处理
DEFAULT_INBOUND_WARN_WORDS: list[str] = [
    "傻逼", "煞笔", "沙雕", "废物客服", "垃圾客服", "脑残", "智障",
    "去死", "滚蛋", "王八蛋",
]

_db_words_cache: dict[str, str] = {}
_db_inbound_words: dict[str, str] = {}  # word -> level(block|warn)
_cache_loaded = False


def load_db_words(words: list[tuple]):
    """从 DB 加载违禁词，与内置词库合并。

    元素为 (word, category) 或 (word, category, direction)：
    - direction="out"（默认）：出口违禁词，命中后改写/删除
    - direction="in"：入口风险词，category 字段作为级别（block | warn，默认 block）
    """
    global _db_words_cache, _db_inbound_words, _cache_loaded
    _db_words_cache = {}
    _db_inbound_words = {}
    for item in words:
        word, category = item[0], item[1]
        direction = item[2] if len(item) > 2 else "out"
        if direction == "in":
            _db_inbound_words[word] = category if category in ("block", "warn") else "block"
        else:
            _db_words_cache[word] = ""
    _cache_loaded = True


def _all_words() -> dict[str, str]:
    merged = dict(DEFAULT_BANNED_WORDS)
    merged.update(_db_words_cache)
    return merged


def scan(text: str) -> list[str]:
    """返回文本中命中的违禁词列表。"""
    return [w for w in _all_words() if w and w in text]


def sanitize(text: str) -> tuple[str, list[str]]:
    """安全改写：命中词替换为合规表述。返回 (改写后文本, 命中词列表)。"""
    hit = scan(text)
    if not hit:
        return text, []
    cleaned = text
    for word in hit:
        replacement = _all_words().get(word, "")
        cleaned = cleaned.replace(word, replacement)
    # 替换后仍残留（replacement 本身违规的极端情况）则再扫一次
    remaining = scan(cleaned)
    for word in remaining:
        cleaned = cleaned.replace(word, "")
    return cleaned.strip(), hit


_PHONE_RE = re.compile(r"(1[3-9]\d)\d{4}(\d{4})")


def mask_lead(text: str) -> str:
    """日志脱敏：手机号中间四位打码。"""
    return _PHONE_RE.sub(r"\1****\2", text)


# ============ 引流词（评论区/发布内容零容忍，私信通道不用此表） ============
#
# 注意：本表独立于出口违禁词库，只用于「公开内容」（帖子/视频文案、评论区回复）。
# 私信客服的合规承接（如推送企微口令）会合法提及"微信"，切勿把本表并入 DEFAULT_BANNED_WORDS。
DRAIN_WORDS: list[str] = [
    "微信", "微信号", "加微", "加V", "加v", "薇", "vx", "VX", "威信",
    "二维码", "扫码", "QQ", "qq群", "手机号", "电话联系我", "私我发",
]


def scan_drain(text: str) -> list[str]:
    """扫描站外引流词。公开内容（创作/评论回复）命中即不合规。"""
    if not text:
        return []
    return [w for w in DRAIN_WORDS if w in text]


# ============ 入口内容安全 ============

def check_inbound(text: str) -> dict:
    """用户消息风险分级。

    返回 {"level": "ok"|"warn"|"block", "hit": [命中词]}：
    - block：涉政/暴恐/违法 → 转人工、AI 不回复
    - warn：辱骂/骚扰 → 消息打标、正常处理
    """
    if not text:
        return {"level": "ok", "hit": []}
    block_hits = [w for w in DEFAULT_INBOUND_BLOCK_WORDS if w in text]
    warn_hits = [w for w in DEFAULT_INBOUND_WARN_WORDS if w in text]
    for word, level in _db_inbound_words.items():
        if word and word in text:
            (block_hits if level == "block" else warn_hits).append(word)
    if block_hits:
        return {"level": "block", "hit": sorted(set(block_hits))}
    if warn_hits:
        return {"level": "warn", "hit": sorted(set(warn_hits))}
    return {"level": "ok", "hit": []}
