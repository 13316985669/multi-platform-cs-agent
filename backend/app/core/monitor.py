"""轻量监控告警：滑动窗口错误计数 + 阈值触发 webhook 告警（钉钉/企微机器人）。

- record(event, detail)：埋点计数，窗口内超阈值则告警（带冷却期防刷屏）
- snapshot()：近 1 小时各事件计数，供 /api/health/detail 展示
- ALERT_WEBHOOK_URL 留空时只记日志，不发请求
"""
import asyncio
import logging
import time
from collections import deque

from ..config import settings

logger = logging.getLogger(__name__)

# 事件 -> (阈值, 窗口秒数)：窗口内达到阈值即告警
THRESHOLDS: dict[str, tuple[int, int]] = {
    "llm_failure": (3, 300),      # 5 分钟内 LLM 失败 3 次
    "webhook_failure": (5, 300),  # 5 分钟内 webhook 处理失败 5 次
    "tool_failure": (5, 300),     # 5 分钟内工具调用失败 5 次
    "api_send_fallback": (3, 300),      # 5 分钟内 API 发送失败降级 RPA 3 次（API 通道可能整体异常）
    "xhs_token_refresh_failure": (2, 3600),  # 1 小时内小红书 token 刷新失败 2 次（refreshToken 可能已过期）
    "account_health": (1, 3600),    # 账号健康度预警：单次即告（调用方已按天去重），冷却期防刷屏
    "stats_collect_failure": (5, 3600),  # 数据回采失败 1 小时 5 次
    "queue_task_failed": (3, 3600),   # 队列任务重试用尽最终失败 1 小时 3 次（全类型）
    "outbound_send_failed": (3, 300),  # 5 分钟内出站发送失败 3 次
    "rerank_failure": (3, 300),
    "embedding_failure": (3, 300),
}

_counters: dict[str, deque] = {}
_last_alert_at: dict[str, float] = {}


def record(event: str, detail: str = ""):
    """记录一次事件；窗口内超阈值时触发告警。"""
    now = time.time()
    dq = _counters.setdefault(event, deque())
    dq.append(now)
    threshold, window = THRESHOLDS.get(event, (5, 300))
    while dq and now - dq[0] > window:
        dq.popleft()
    logger.info("监控事件 %s（窗口内 %d 次）%s", event, len(dq), detail)
    if len(dq) >= threshold:
        _maybe_alert(event, len(dq), window, detail)


def count_last_hour(event: str) -> int:
    now = time.time()
    return sum(1 for t in _counters.get(event, ()) if now - t <= 3600)


def snapshot() -> dict[str, int]:
    """近 1 小时各事件计数。"""
    return {event: count_last_hour(event) for event in list(_counters)}


def _maybe_alert(event: str, count: int, window: int, detail: str):
    now = time.time()
    if now - _last_alert_at.get(event, 0) < settings.alert_cooldown_seconds:
        return
    _last_alert_at[event] = now
    text = (f"[{settings.app_name}] 告警：{event} 在 {window // 60} 分钟内发生 {count} 次。"
            f"最近详情：{detail[:200]}")
    logger.warning("ALERT %s", text)
    if not settings.alert_webhook_url:
        return
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_send_alert(text))
    except RuntimeError:
        # 非事件循环上下文（如同步测试），跳过网络发送
        pass


async def _send_alert(text: str):
    """发送钉钉/企微机器人消息（两者均兼容 text 格式）。"""
    import httpx

    payload = {"msgtype": "text", "text": {"content": text}}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(settings.alert_webhook_url, json=payload)
            if resp.status_code >= 400:
                logger.warning("告警 webhook 返回 %s", resp.status_code)
    except Exception:  # noqa: BLE001
        logger.exception("告警发送失败")
