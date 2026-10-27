"""会话级串行锁：同一会话的 AI 答复不并发执行。

fast lane 并发消费后，同一会话的两条消息仍必须按序处理（引擎按最新
用户消息答题，并发会互相覆盖）。进程内 dict + guard 锁，知你快回
HTTP 直答路径与队列消费路径共享同一把锁，消除跨入口竞态。
"""
import asyncio

_locks: dict[int, asyncio.Lock] = {}
_guard = asyncio.Lock()


async def conversation_lock(conversation_id: int) -> asyncio.Lock:
    """获取会话专属锁（不存在则创建）。"""
    async with _guard:
        lock = _locks.get(conversation_id)
        if lock is None:
            lock = asyncio.Lock()
            _locks[conversation_id] = lock
        return lock
