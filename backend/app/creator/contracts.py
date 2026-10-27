"""创作管线的 LLM 输出契约（pydantic 严格校验）。"""
from typing import Literal

from pydantic import BaseModel, Field


class GeneratedVersion(BaseModel):
    """LLM 生成的平台版本内容。"""
    title: str = ""
    body: str = ""
    tags: list[str] = Field(default_factory=list)
    script: str = ""
    cover_text: str = ""


class ComplianceHit(BaseModel):
    word: str = ""
    reason: str = ""
    severity: Literal["high", "medium", "low"] = "medium"


class ComplianceResult(BaseModel):
    """LLM 合规审核输出。"""
    passed: bool = True
    hits: list[ComplianceHit] = Field(default_factory=list)
    suggestions: list[str] = Field(default_factory=list)


class CommentIntent(BaseModel):
    """评论意图分类输出（Phase 3 评论引擎使用）。"""
    intent: Literal["consult", "price", "praise", "complaint", "spam", "irrelevant"] = "irrelevant"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class InspirationAnalysis(BaseModel):
    """爆款拆解输出（Phase 6 灵感库使用）。"""
    title_formula: str = ""
    structure: str = ""
    hooks: list[str] = Field(default_factory=list)
    selling_angle: str = ""
    why_viral: str = ""
    reusable_points: list[str] = Field(default_factory=list)


class TitleSuggestion(BaseModel):
    """标题助手单个候选（Phase 7）。"""
    text: str = ""
    formula: str = ""       # 标题公式（数字清单/痛点提问/悬念反转/身份共鸣等）
    score: float = 0.0      # 爆款潜力评分 0-100


class TitleSuggestions(BaseModel):
    """标题助手输出：10 个候选标题。"""
    titles: list[TitleSuggestion] = Field(default_factory=list)


class KeywordSuggestion(BaseModel):
    """关键词埋词单个候选（Phase 7）。"""
    word: str = ""
    heat: str = ""          # 热度定性：高 | 中 | 低
    covered: bool = False   # 当前版本标题/正文/标签是否已覆盖


class KeywordSuggestions(BaseModel):
    """关键词埋词输出：品类热搜词推荐 + 覆盖检测。"""
    keywords: list[KeywordSuggestion] = Field(default_factory=list)
