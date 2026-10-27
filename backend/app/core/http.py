"""外部 HTTP 调用公共层：超时 + 网络级异常指数退避重试（最多 3 次）。

业务级错误（平台返回非 0 错误码）由调用方判定抛出，本层只兜底网络/服务异常。
"""
import asyncio
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


async def request_json(method: str, url: str, *, params: dict | None = None,
                       json_body: dict | None = None, headers: dict | None = None,
                       content: bytes | None = None, timeout: float = 15.0,
                       retries: int = MAX_RETRIES) -> dict[str, Any]:
    """发送请求并返回 JSON。网络/5xx 异常指数退避重试；最终失败抛 httpx.HTTPError。

    返回 dict 后调用方自行检查平台业务错误码（如 err_no/errorcode/errcode）。
    """
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, params=params, json=json_body,
                                            headers=headers, content=content)
                resp.raise_for_status()
                return resp.json()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            last_exc = exc
            # 4xx 属于请求/权限问题，重试无意义，直接抛出
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                raise
            delay = 2 ** attempt
            logger.warning("HTTP %s %s 失败（第 %d/%d 次）: %s", method, url,
                           attempt + 1, retries, exc)
            if attempt < retries - 1:
                await asyncio.sleep(delay)
    raise httpx.HTTPError(f"请求重试 {retries} 次仍失败: {url}") from last_exc


async def get_json(url: str, *, params: dict | None = None, headers: dict | None = None,
                   timeout: float = 15.0, retries: int = MAX_RETRIES) -> dict[str, Any]:
    return await request_json("GET", url, params=params, headers=headers,
                              timeout=timeout, retries=retries)


async def post_json(url: str, *, params: dict | None = None, json_body: dict | None = None,
                    headers: dict | None = None, content: bytes | None = None,
                    timeout: float = 15.0, retries: int = MAX_RETRIES) -> dict[str, Any]:
    return await request_json("POST", url, params=params, json_body=json_body,
                              headers=headers, content=content,
                              timeout=timeout, retries=retries)
