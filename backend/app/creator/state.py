"""创作图状态定义。

不用 LangGraph checkpointer：状态持久化仍走 DB 表（ContentVersion），
图只管单次运行的流程编排，与项目「DB 即状态机」风格一致。
"""
from typing import Any, TypedDict


class CreatorState(TypedDict, total=False):
    item_id: int
    platform: str
    content_type: str
    topic: str
    selling_points: list[str]
    inspiration: dict[str, Any]     # 参考爆款（灵感库）：title/content_text/analysis
    variant_index: int              # 一稿多版：0=第 1 版（默认风格），1/2=风格变体
    draft: dict[str, Any]           # GeneratedVersion.model_dump()
    compliance_report: dict[str, Any]  # {"passed": bool, "hits": [...], "suggestions": [...]}
    revision_count: int
    version_id: int                 # persist 后落库的 ContentVersion.id
    error: str
