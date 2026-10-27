"""创作图装配（LangGraph 试点）：generate → compliance →（rewrite ↺）→ persist。

条件边：合规通过 → persist；未通过且改写次数 < MAX_REVISIONS → rewrite 后复检；
达到改写上限 → persist（status=failed，转人工修改）。

回退策略：节点均为纯函数，若移除本文件的图装配、改为顺序调用，行为等价。
"""
import logging

from langgraph.graph import END, START, StateGraph

from ..database import SessionLocal
from ..models import ContentItem, ContentVersion
from .nodes import (MAX_REVISIONS, compliance_node, generate_node,
                    persist_node, rewrite_node)
from .state import CreatorState

logger = logging.getLogger(__name__)


def _route_after_compliance(state: CreatorState) -> str:
    report = state.get("compliance_report") or {}
    if report.get("passed"):
        return "persist"
    if state.get("revision_count", 0) < MAX_REVISIONS:
        return "rewrite"
    logger.warning("创作改写达上限仍不合规，转人工 item_id=%s", state.get("item_id"))
    return "persist"  # failed 落库，转人工修改


def build_graph():
    g = StateGraph(CreatorState)
    g.add_node("generate", generate_node)
    g.add_node("compliance", compliance_node)
    g.add_node("rewrite", rewrite_node)
    g.add_node("persist", persist_node)
    g.add_edge(START, "generate")
    g.add_edge("generate", "compliance")
    g.add_conditional_edges("compliance", _route_after_compliance,
                            {"rewrite": "rewrite", "persist": "persist"})
    g.add_edge("rewrite", "compliance")
    g.add_edge("persist", END)
    return g.compile()


_graph = None


def get_graph():
    """惰性编译（进程级单例）。"""
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


async def run_creator_graph(item_id: int, platform: str, content_type: str,
                            variant_index: int = 0) -> ContentVersion | None:
    """创作管线入口：构造初始状态 → 跑图 → 返回落库的 ContentVersion。

    由队列任务 content_generate 调用（每个目标平台 × 每个风格变体一次图运行）。
    variant_index：一稿多版序号（0=第 1 版），决定注入提示词的风格指令。
    """
    db = SessionLocal()
    try:
        item = db.get(ContentItem, item_id)
        if item is None:
            logger.warning("创作任务找不到内容 item_id=%s", item_id)
            return None
        # 灵感参考：ContentItem.inspiration_id 指向灵感库条目（仿写场景）
        inspiration: dict = {}
        if item.inspiration_id:
            from ..models import InspirationItem
            insp = db.get(InspirationItem, item.inspiration_id)
            if insp is not None:
                inspiration = {"title": insp.title or "",
                               "content_text": insp.content_text or "",
                               "analysis": insp.analysis or {}}
        initial: CreatorState = {
            "item_id": item.id,
            "platform": platform,
            "content_type": content_type,
            "topic": item.topic or item.title,
            "selling_points": list(item.selling_points or []),
            "inspiration": inspiration,
            "variant_index": variant_index,
            "draft": {},
            "compliance_report": {},
            "revision_count": 0,
            "error": "",
        }
    finally:
        db.close()

    final = await get_graph().ainvoke(initial)
    if final.get("error"):
        logger.error("创作图失败 item_id=%s platform=%s error=%s", item_id, platform, final["error"])
        return None
    version_id = final.get("version_id")
    if not version_id:
        return None
    db = SessionLocal()
    try:
        return db.get(ContentVersion, version_id)
    finally:
        db.close()
