"""查询改写：多轮对话指代消解 + 同义多路召回。

把「那这个多少钱」结合历史改写成独立完整问题，并生成 1 个同义改写。
没有更早对话、也不是店铺事实问、问题里也没有指代时直接用原句。第一轮没检索到时再放宽一次。
LLM 未配置或调用失败时返回原查询。
"""
import json
import logging
import re

from ..config import settings
from ..llm import get_llm_client, record_usage

logger = logging.getLogger(__name__)

_REWRITE_HEAD = (
    "你是查询改写助手。根据对话历史，把用户最新问题改写成一个独立完整的检索问题"
    "（补全指代和省略），再给出 1 个同义改写版本。\n"
    "只输出 JSON："
)
_REWRITE_JSON = '{"standalone": "独立完整问题", "variants": ["同义改写"]}'
_ANAPHORA = re.compile(r"这个|那个|这款|那款|它|刚才|上面|前面说的")
_SHOP_FACT = re.compile(
    r"多少钱|什么价|价格|几元|几块|包邮|运费|邮费|发货|退货|退换|换货|退款|发票|质保|保修|"
    r"库存|有货|营业|几点|地址|尺码|颜色|优惠|折扣|几天|多久|周末"
)
_BROADEN_HEAD = (
    "上一轮检索没有命中。请把用户问题改成一句更宽的检索问法："
    "去掉口语废话，补上对话里的商品名，不要重复已经搜过的句子。"
    "只输出 JSON："
)
_BROADEN_JSON = '{"query": "更宽的检索问句"}'


def _earlier_history(query: str, history: list[dict]) -> list[dict]:
    """去掉当前这句，只留更早的轮次。历史本身不含当前句时原样返回。"""
    if not history:
        return []
    last = history[-1]
    if last.get("sender_type") == "user" and last.get("content") == query:
        return history[:-1]
    return list(history)


def _render_prompt(history_text: str, query: str) -> str:
    # 不用 str.format：示例 JSON 里的花括号会被当成占位符，抛 KeyError。
    return (
        _REWRITE_HEAD
        + _REWRITE_JSON
        + "\n\n对话历史：\n"
        + history_text
        + "\n\n用户最新问题："
        + query
    )


def needs_rewrite(query: str, history: list[dict]) -> bool:
    """有更早对话、指代，或在问店铺事实时，生成检索问法。纯寒暄不改写。"""
    earlier = _earlier_history(query, history)
    if earlier:
        return True
    text = query or ""
    if _ANAPHORA.search(text):
        return True
    return bool(_SHOP_FACT.search(text))


async def rewrite_query(query: str, history: list[dict], summary: str = "") -> list[str]:
    """返回改写后的查询列表（含原查询，最多 3 条）。未配置或失败时返回 [query]。"""
    if not settings.llm_configured or not needs_rewrite(query, history):
        return [query]
    earlier = _earlier_history(query, history)
    try:
        lines = []
        if summary:
            lines.append(f"【早前对话小结】{summary}")
        lines.extend(
            f"{'用户' if h['sender_type'] == 'user' else '客服'}: {h['content']}"
            for h in earlier
        )
        history_text = "\n".join(lines) or "（无更早对话）"
        client = get_llm_client(settings.llm_base_url, settings.llm_api_key)
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": _render_prompt(history_text, query)}],
            temperature=0,
            max_tokens=200,
            response_format={"type": "json_object"},
            timeout=8.0,
        )
        record_usage(0, settings.llm_model, getattr(resp, "usage", None))
        data = json.loads(resp.choices[0].message.content)
        queries = [data.get("standalone") or query]
        queries.extend(v for v in (data.get("variants") or []) if v)
        queries.append(query)
        # 去重保序
        seen, result = set(), []
        for q in queries:
            if q and q not in seen:
                seen.add(q)
                result.append(q)
        return result[:3]
    except Exception:  # noqa: BLE001
        logger.exception("查询改写失败，使用原查询")
        return [query]


async def broaden_query(query: str, used: list[str], reason: str, history: list[dict],
                        summary: str = "") -> str | None:
    """第一轮未命中后，只要一句更宽的新问法。失败或超时返回 None。"""
    if not settings.llm_configured:
        return None
    earlier = _earlier_history(query, history)
    lines = []
    if summary:
        lines.append(f"【早前对话小结】{summary}")
    lines.extend(
        f"{'用户' if h['sender_type'] == 'user' else '客服'}: {h['content']}"
        for h in earlier
    )
    prompt = (
        _BROADEN_HEAD + _BROADEN_JSON
        + f"\n\n失败原因：{reason}"
        + "\n已经搜过：\n" + "\n".join(used or [query])
        + "\n\n对话：\n" + ("\n".join(lines) or "（无）")
        + f"\n\n用户原话：{query}"
    )
    try:
        client = get_llm_client(settings.llm_base_url, settings.llm_api_key)
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=120,
            response_format={"type": "json_object"},
            timeout=8.0,
        )
        record_usage(0, settings.llm_model, getattr(resp, "usage", None))
        text = (json.loads(resp.choices[0].message.content).get("query") or "").strip()
    except Exception:  # noqa: BLE001
        logger.exception("放宽改写失败，停止重试")
        return None
    if not text or text in set(used or []):
        return None
    return text
