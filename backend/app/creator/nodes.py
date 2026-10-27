"""创作图节点：generate → compliance →（rewrite ↺）→ persist。

节点均为纯函数（CreatorState 进、部分状态更新出），不依赖图框架特性——
若未来回退 LangGraph，改为顺序调用这些函数即可，业务逻辑零改动。
"""
import logging
import os

from ..core import contentfilter
from ..database import SessionLocal
from ..models import ContentItem, ContentVersion
from .contracts import ComplianceResult, GeneratedVersion
from .llm import call_llm_json
from .state import CreatorState
from .templates import (CONTENT_TYPE_NAMES, PLATFORM_NAMES, clamp_draft,
                        platform_spec_text)

logger = logging.getLogger(__name__)

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "..", "prompts", "content_creator.md")
MAX_REVISIONS = 2  # 自动改写上限，超过转人工修改

# 一稿多版（Phase 7）：同一选题按不同风格生成备选版本，供 A/B 挑选
VARIANT_STYLES = ["真实测评风", "痛点提问风", "干货清单风"]

_VARIANT_DIRECTIVES = [
    "本版为第 1 版：「真实测评风」——第一人称真实体验分享，有细节、有感受，小缺点也敢说。",
    "本版为第 2 版：「痛点提问风」——标题与开头以目标人群的具体痛点提问切入，"
    "正文按「痛点 → 原因 → 解法」展开。",
    "本版为第 3 版：「干货清单风」——结构化清单体，分点罗列可落地的技巧/步骤/避坑，"
    "信息密度优先。",
]


def _render_variant_block(variant_index: int) -> str:
    """变体风格指令：注入提示词，让同选题的不同版本风格可区分（A/B 对比用）。"""
    idx = max(0, min(variant_index, len(_VARIANT_DIRECTIVES) - 1))
    directive = _VARIANT_DIRECTIVES[idx]
    if variant_index > 0:
        directive += "\n（同一选题的其他版本采用不同风格，请严格按本版风格创作，避免雷同）"
    return directive

_prompt_cache: dict = {"mtime": 0.0, "content": ""}


def _load_prompt() -> str:
    """按 mtime 热更新加载创作提示词（模式同 agent/prompt.py）。"""
    path = os.path.abspath(PROMPT_PATH)
    mtime = os.path.getmtime(path)
    if mtime != _prompt_cache["mtime"]:
        with open(path, "r", encoding="utf-8") as f:
            _prompt_cache["content"] = f.read()
        _prompt_cache["mtime"] = mtime
    return _prompt_cache["content"]


def _render_brand_style_block() -> str:
    """品牌语气块（Phase 7）：设置页配置的品牌风格指南注入提示词；空 = 不约束。"""
    from ..config import settings
    guide = (settings.brand_style_guide or "").strip()
    if not guide:
        return "（未配置品牌语气，按平台通用风格创作）"
    return f"【品牌语气要求】\n{guide}\n（以上品牌语气优先级高于通用风格，但不得违反硬性禁区）"


def _render_inspiration_block(inspiration: dict) -> str:
    """灵感参考块：注入爆款原文要点 + AI 拆解结论，引导仿写而非抄袭。"""
    if not inspiration:
        return "（无参考爆款，纯原创）"
    lines = [f"【爆款标题】{inspiration.get('title', '')}"]
    content = (inspiration.get("content_text") or "")[:500]
    if content:
        lines.append(f"【爆款正文摘录】{content}")
    analysis = inspiration.get("analysis") or {}
    if analysis:
        if analysis.get("title_formula"):
            lines.append(f"【标题公式】{analysis['title_formula']}")
        if analysis.get("structure"):
            lines.append(f"【内容结构】{analysis['structure']}")
        hooks = analysis.get("hooks") or []
        if hooks:
            lines.append("【开头钩子】" + "；".join(hooks[:3]))
        if analysis.get("selling_angle"):
            lines.append(f"【卖点切入】{analysis['selling_angle']}")
        points = analysis.get("reusable_points") or []
        if points:
            lines.append("【可复用要点】" + "；".join(points[:5]))
    lines.append("（参考其方法论与结构，但必须围绕给定选题原创表达，严禁抄袭原文句子）")
    return "\n".join(lines)


def _render_creator_prompt(state: CreatorState) -> str:
    platform = state["platform"]
    points = state.get("selling_points") or []
    return (
        _load_prompt()
        .replace("{{platform_name}}", PLATFORM_NAMES.get(platform, platform))
        .replace("{{content_type_name}}", CONTENT_TYPE_NAMES.get(state["content_type"], "图文笔记"))
        .replace("{{topic}}", state.get("topic", ""))
        .replace("{{selling_points}}", "\n".join(f"- {p}" for p in points) or "（无，自由发挥）")
        .replace("{{platform_spec}}", platform_spec_text(platform))
        .replace("{{inspiration}}", _render_inspiration_block(state.get("inspiration") or {}))
        .replace("{{variant_style}}", _render_variant_block(state.get("variant_index", 0)))
        .replace("{{brand_style}}", _render_brand_style_block())
    )


async def generate_node(state: CreatorState) -> dict:
    """LLM 生成平台版本草稿（契约校验 + 平台规格截断）。"""
    result = await call_llm_json(_render_creator_prompt(state), GeneratedVersion)
    if result is None:
        return {"error": "LLM 生成失败（未配置或多次重试失败）"}
    draft = clamp_draft(state["platform"], result.model_dump())
    return {"draft": draft, "error": ""}


def _scan_banned_words(text: str) -> list[dict]:
    """第一级合规：违禁词 + 引流词扫描（复用 contentfilter 词库，含 DB 词）。"""
    if not text:
        return []
    hits = [{"word": w, "reason": "命中违禁词库", "severity": "high"}
            for w in contentfilter.scan(text)]
    hits += [{"word": w, "reason": "命中引流词（公开内容零容忍）", "severity": "high"}
             for w in contentfilter.scan_drain(text)]
    return hits


async def check_text_compliance(platform: str, title: str, body: str,
                                script: str = "", tags: list[str] | None = None) -> dict:
    """双保险合规检测：违禁词扫描 + LLM 审核。供图节点与手动复检共用。"""
    full_text = "\n".join(t for t in [title, body, script, " ".join(tags or [])] if t)
    rule_hits = _scan_banned_words(full_text)

    prompt = (
        "你是内容合规审核员。请审核以下"
        f"{'小红书' if platform == 'xiaohongshu' else '抖音'}内容是否合规。\n\n"
        "审核要点：\n"
        "1. 广告法极限词（最好/第一/顶级/100%/绝对等）\n"
        "2. 功效承诺（治疗/根治/稳赚/无风险等）\n"
        "3. 站外引流词及变体（微信/加V/薇/vx/威信/二维码/手机号/私我发联系方式等）——零容忍\n"
        "4. 平台社区规范（夸大宣传/虚假数据/敏感话题）\n\n"
        "【标题】\n" + title + "\n\n【正文】\n" + body + "\n\n【口播脚本】\n" + script +
        "\n\n【话题标签】\n" + " ".join(tags or []) +
        "\n\n只输出 JSON：{\"passed\": true或false, "
        "\"hits\": [{\"word\": \"问题词\", \"reason\": \"原因\", \"severity\": \"high|medium|low\"}], "
        "\"suggestions\": [\"改写建议\"]}\n"
        "规则：命中引流词或极限词 severity 为 high 且 passed=false；"
        "仅风格建议时 passed=true 且 hits 为空。"
    )
    llm_result = await call_llm_json(prompt, ComplianceResult)

    if llm_result is None:
        # LLM 不可用：仅按词库结果判定（有 high 命中则失败，否则通过但标注降级）
        passed = not any(h["severity"] == "high" for h in rule_hits)
        report = {"passed": passed, "hits": rule_hits,
                  "suggestions": [] if passed else ["请修改违禁词后重新检测"],
                  "degraded": True}
        return report

    report = llm_result.model_dump()
    # 词库命中并入报告（去重）
    existing = {h["word"] for h in report["hits"]}
    for h in rule_hits:
        if h["word"] not in existing:
            report["hits"].append(h)
    if any(h.get("severity") == "high" for h in report["hits"]):
        report["passed"] = False
    return report


async def compliance_node(state: CreatorState) -> dict:
    """合规双审节点。"""
    draft = state.get("draft") or {}
    report = await check_text_compliance(
        state["platform"], draft.get("title", ""), draft.get("body", ""),
        draft.get("script", ""), draft.get("tags") or [],
    )
    return {"compliance_report": report}


async def rewrite_node(state: CreatorState) -> dict:
    """按合规意见定向改写（revision_count+1 由图装配层保证不超过上限）。"""
    draft = state.get("draft") or {}
    report = state.get("compliance_report") or {}
    prompt = (
        _render_creator_prompt(state)[:-len("现在，只输出你的 JSON：")] +  # 复用创作约束
        "\n## 六、改写任务\n\n"
        "以下是你上一版草稿，合规审核未通过，请按审核意见改写后重新输出完整 JSON：\n\n"
        f"【上一版草稿】\n{draft}\n\n"
        f"【审核命中的问题】\n{report.get('hits', [])}\n\n"
        f"【审核建议】\n{report.get('suggestions', [])}\n\n"
        "要求：彻底移除所有命中问题（尤其是引流词和极限词），保持主题与卖点不变。\n"
        "现在，只输出你的 JSON："
    )
    result = await call_llm_json(prompt, GeneratedVersion)
    if result is None:
        return {"revision_count": state.get("revision_count", 0) + 1}
    return {
        "draft": clamp_draft(state["platform"], result.model_dump()),
        "revision_count": state.get("revision_count", 0) + 1,
    }


async def persist_node(state: CreatorState) -> dict:
    """落库 ContentVersion。passed → passed；否则 failed（转人工修改）。"""
    draft = state.get("draft") or {}
    report = state.get("compliance_report") or {}
    status = "passed" if report.get("passed") else "failed"
    db = SessionLocal()
    try:
        version = ContentVersion(
            content_item_id=state["item_id"], platform=state["platform"],
            content_type=state["content_type"],
            title=draft.get("title", ""), body=draft.get("body", ""),
            tags=draft.get("tags") or [], script=draft.get("script", ""),
            cover_text=draft.get("cover_text", ""),
            compliance_status=status,
            compliance_report={"hits": report.get("hits", []),
                               "suggestions": report.get("suggestions", [])},
            variant_no=state.get("variant_index", 0) + 1,
        )
        db.add(version)
        # 注意：不自动流转 item.status —— 审批流（6-7）要求人工「提交审核」才进入 reviewing
        db.commit()
        db.refresh(version)
        # 查重报告（同平台近 90 天版本 SimHash 对比），发布前软拦截用
        try:
            from .dedup import check_duplicate, version_text
            version.dup_report = check_duplicate(db, version_text(version),
                                                 version.platform,
                                                 exclude_version_id=version.id,
                                                 exclude_item_id=version.content_item_id)
            db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("查重计算失败 version_id=%s（不阻断落库）", version.id)
        logger.info("创作版本落库 version_id=%s platform=%s compliance=%s",
                    version.id, version.platform, status)
        return {"version_id": version.id}
    finally:
        db.close()
