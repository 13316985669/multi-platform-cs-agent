"""平台频控：遵守各平台私信发送规则，超限降级转人工，避免账号被处罚。

规则表可配。当前内置抖音规则（来自官方文档）：
- im_reply_msg：用户发来一条消息后 24h 内最多回复 6 条
- im_enter_direct_msg：用户进入会话事件 30s 内最多发 3 条

小红书/Mock 默认宽松限制（防失控兜底）。
"""
import time
from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class RateRule:
    name: str
    max_count: int
    window_seconds: int


# platform -> rules
# <platform>_rpa 为 RPA 通道专属规则：在平台规则基础上叠加更严的每分钟上限，
# 模拟真人节奏，降低自动化特征（Worker 侧另有逐字输入与随机间隔）
PLATFORM_RULES: dict[str, list[RateRule]] = {
    "douyin": [
        RateRule(name="reply_24h", max_count=6, window_seconds=24 * 3600),
    ],
    "xiaohongshu": [
        RateRule(name="reply_1h", max_count=20, window_seconds=3600),
    ],
    "douyin_rpa": [
        RateRule(name="rpa_minute", max_count=6, window_seconds=60),
        RateRule(name="reply_24h", max_count=6, window_seconds=24 * 3600),
    ],
    # 抖音企业号（e.douyin.com 后台 RPA，DOUYIN_ACCOUNT_TYPE=enterprise 时启用）：
    # 官方规则为用户回复后 48h 内可发 6 条（抖店为 24h/6 条）；
    # 主动触达 1h ≤40 人 / 1 天 ≤100 人 —— 本系统只做被动回复，不触发主动触达，故不内置
    "douyin_enterprise_rpa": [
        RateRule(name="rpa_minute", max_count=6, window_seconds=60),
        RateRule(name="reply_48h", max_count=6, window_seconds=48 * 3600),
    ],
    # 抖音来客（life.douyin.com 后台 RPA，DOUYIN_ACCOUNT_TYPE=laike 时启用）：
    # 本地生活 IM 官方回复窗口规则未经真账号核实，初期保守对齐企业号（48h/6 + 分钟 6），
    # 冒烟核实后校准
    "douyin_laike_rpa": [
        RateRule(name="rpa_minute", max_count=6, window_seconds=60),
        RateRule(name="reply_48h", max_count=6, window_seconds=48 * 3600),
    ],
    # 来客账号级熔断：按出站条数计，不按会话。一条长回复拆成多条也各算一条。
    "douyin_laike_account": [
        RateRule(name="account_hour", max_count=10, window_seconds=3600),
    ],
    "xiaohongshu_rpa": [
        RateRule(name="rpa_minute", max_count=6, window_seconds=60),
        RateRule(name="reply_1h", max_count=20, window_seconds=3600),
    ],
    "mock": [
        RateRule(name="reply_1h", max_count=100, window_seconds=3600),
    ],
    # ============ 评论回复频控（内容矩阵，防风控硬约束） ============
    # 抖音评论 API：分钟 5 条 / 小时 50 条（item.comment 权限）
    "douyin_comment": [
        RateRule(name="minute", max_count=5, window_seconds=60),
        RateRule(name="hour", max_count=50, window_seconds=3600),
    ],
    # 小红书评论 RPA：分钟 3 条 / 小时 30 条（模拟真人节奏）
    "xiaohongshu_rpa_comment": [
        RateRule(name="minute", max_count=3, window_seconds=60),
        RateRule(name="hour", max_count=30, window_seconds=3600),
    ],
    # 抖音评论 RPA 兜底（企业号不适用 item.comment 时）
    "douyin_rpa_comment": [
        RateRule(name="minute", max_count=3, window_seconds=60),
        RateRule(name="hour", max_count=30, window_seconds=3600),
    ],
    "mock_comment": [
        RateRule(name="hour", max_count=100, window_seconds=3600),
    ],
}

# (platform, conversation_key, rule_name) -> [timestamp, ...]
_counters: dict[tuple[str, str, str], list[float]] = defaultdict(list)


def check_and_count(platform: str, conversation_key: str, count: int = 1, *,
                    commit: bool = True) -> tuple[bool, str]:
    """检查并计数。返回 (是否允许, 命中的规则名)。commit 为假时只判断、不占用额度。"""
    rules = PLATFORM_RULES.get(platform, [])
    now = time.time()
    for rule in rules:
        key = (platform, conversation_key, rule.name)
        # 清理窗口外记录
        _counters[key] = [t for t in _counters[key] if now - t < rule.window_seconds]
        if len(_counters[key]) + count > rule.max_count:
            return False, rule.name
    if not commit:
        return True, ""
    for rule in rules:
        key = (platform, conversation_key, rule.name)
        _counters[key].extend([now] * count)
    return True, ""


def remaining(platform: str, conversation_key: str) -> dict[str, int]:
    """查询各规则剩余额度（用于调试/工作台展示）。"""
    now = time.time()
    result = {}
    for rule in PLATFORM_RULES.get(platform, []):
        key = (platform, conversation_key, rule.name)
        used = len([t for t in _counters[key] if now - t < rule.window_seconds])
        result[rule.name] = max(0, rule.max_count - used)
    return result


_REPLY_WINDOW_RULES = {"reply_24h", "reply_48h"}


def reset_reply_window(platform: str, conversation_key: str) -> None:
    """用户新发一条消息后，这一轮的 6 条回复额度重新计算。每分钟上限保留。"""
    platforms = {platform, f"{platform}_rpa"}
    if platform == "douyin":
        platforms.update({"douyin_enterprise_rpa", "douyin_laike_rpa"})
    for rate_platform in platforms:
        for name in _REPLY_WINDOW_RULES:
            _counters.pop((rate_platform, conversation_key, name), None)


def reset_all():
    """测试用：清空计数器。"""
    _counters.clear()
