"""创作管线的通用 LLM JSON 调用：严格契约校验 + 解析失败重试 1 次 + 主备模型切换。

与 agent/engine.py 的调用模式一致，但契约类型泛化（引擎的 _call_llm_once 绑定 AgentReply，
此处泛化以复用同一套重试/降级策略于生成、合规审核、评论意图分类等场景）。
"""
import json
import logging
from typing import TypeVar

from openai import AsyncOpenAI
from pydantic import BaseModel, ValidationError

from ..config import settings
from ..core import monitor
from ..database import SessionLocal
from ..models import LlmUsage

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _record_usage(model: str, usage) -> None:
    """token 用量记录（独立短会话写入；失败静默）。"""
    if usage is None:
        return
    try:
        db = SessionLocal()
        try:
            db.add(LlmUsage(
                conversation_id=0, model=model,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
            ))
            db.commit()
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.warning("token 用量记录失败", exc_info=True)


def _parse_json_contract(raw: str, contract: type[T]) -> T:
    """容错解析：剥 markdown 代码块，提取第一个 JSON 对象。"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise json.JSONDecodeError("未找到 JSON 对象", text, 0)
    return contract.model_validate(json.loads(text[start:end + 1]))


async def _call_once(prompt: str, contract: type[T], *, base_url: str, api_key: str,
                     model: str) -> tuple[T | None, bool]:
    """单模型调用 + 契约校验；解析失败带错误反馈重试 1 次。返回 (结果, 是否硬失败)。"""
    client = AsyncOpenAI(base_url=base_url, api_key=api_key)
    messages = [{"role": "user", "content": prompt}]
    raw = ""
    for attempt in range(2):
        try:
            resp = await client.chat.completions.create(
                model=model, messages=messages, temperature=0.5,
                response_format={"type": "json_object"},
            )
            _record_usage(model, getattr(resp, "usage", None))
            raw = resp.choices[0].message.content or ""
            return _parse_json_contract(raw, contract), False
        except (ValidationError, json.JSONDecodeError) as exc:
            logger.warning("契约解析失败（第 %d 次）: %s", attempt + 1, exc)
            if attempt == 0:
                messages.append({"role": "assistant", "content": raw})
                messages.append({"role": "user", "content":
                    f"你的输出不符合契约，错误：{exc}。请只输出正确的 JSON 对象，不要输出其他内容。"})
            continue
        except Exception:  # noqa: BLE001
            logger.exception("LLM 调用失败 model=%s", model)
            monitor.record("llm_failure", f"creator model={model}")
            return None, True
    return None, False


async def call_llm_json(prompt: str, contract: type[T]) -> T | None:
    """主模型调用，硬失败自动切备用模型。未配置 LLM 或多次失败返回 None。"""
    if not settings.llm_configured:
        logger.warning("LLM 未配置，创作管线降级")
        return None
    result, hard_failed = await _call_once(
        prompt, contract,
        base_url=settings.llm_base_url, api_key=settings.llm_api_key, model=settings.llm_model,
    )
    if hard_failed and settings.llm_fallback_configured:
        logger.warning("主 LLM 不可用，切换备用模型 %s", settings.llm_fallback_model)
        monitor.record("llm_failure", f"creator 主模型 {settings.llm_model} 失败，切换备用")
        result, _ = await _call_once(
            prompt, contract,
            base_url=settings.llm_fallback_base_url or settings.llm_base_url,
            api_key=settings.llm_fallback_api_key, model=settings.llm_fallback_model,
        )
    return result
