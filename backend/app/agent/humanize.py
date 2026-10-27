"""拟人化发送：分条、打字延迟、发送节奏 —— 「不死板」的执行层。"""
import asyncio
import re

from ..config import settings

MAX_CHUNK_LEN = 80  # 兜底拆分阈值


def split_long_message(text: str) -> list[str]:
    """兜底拆分：单条超长按句号/换行再拆，保持短消息风格。"""
    if len(text) <= MAX_CHUNK_LEN:
        return [text]
    parts = re.split(r"(?<=[。！？!?\n])", text)
    chunks, current = [], ""
    for part in parts:
        if not part:
            continue
        if len(current) + len(part) <= MAX_CHUNK_LEN:
            current += part
        else:
            if current:
                chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    # 极端情况：单句就超长，硬切
    final = []
    for c in chunks:
        while len(c) > MAX_CHUNK_LEN:
            final.append(c[:MAX_CHUNK_LEN])
            c = c[MAX_CHUNK_LEN:]
        if c:
            final.append(c)
    return final


def typing_delay_ms(text: str) -> int:
    """按上一条消息字数模拟打字耗时。"""
    return settings.humanize_base_delay_ms + len(text) * settings.humanize_per_char_ms


async def send_humanized(messages: list[str], send_func):
    """逐条发送，条间加入打字延迟。

    send_func 返回 False 时停掉后续气泡（例如 AI 出站被模式复检拦住）。
    """
    chunks: list[str] = []
    for msg in messages:
        chunks.extend(split_long_message(msg.strip()))
    chunks = [c for c in chunks if c]

    for i, chunk in enumerate(chunks):
        if i > 0:
            await asyncio.sleep(typing_delay_ms(chunks[i - 1]) / 1000)
        kept_going = await send_func(chunk)
        if kept_going is False:
            return
