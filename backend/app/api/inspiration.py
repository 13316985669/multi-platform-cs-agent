"""爆款灵感库 API：手动录入 / RPA 热榜采集导入 / AI 拆解 / 一键仿写。

仿写链路：灵感 → AI 拆解（标题公式/结构/钩子）→ 建 ContentItem(inspiration_id)
→ 创作台生成时把拆解结论注入创作提示词（creator/nodes._render_creator_prompt）。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..core import audit
from ..creator.contracts import InspirationAnalysis
from ..creator.llm import call_llm_json
from ..database import get_db
from ..models import Agent, ContentItem, InspirationItem
from ..schemas import InspirationItemIn, InspirationItemOut
from .deps import get_current_agent
from .rpa import rpa_key_dep

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/inspiration", tags=["inspiration"])

ANALYZE_PROMPT = """你是爆款内容拆解专家。请拆解以下{platform_name}爆款内容，提炼可复用的创作方法论。

【标题】
{title}

【正文/口播文案】
{content}

【数据】
{stats}

只输出 JSON：
{{"title_formula": "标题公式（如：数字+痛点+悬念）",
 "structure": "内容结构（如：痛点引入→产品体验→效果对比→行动号召）",
 "hooks": ["开头钩子1", "钩子2"],
 "selling_angle": "卖点切入角度",
 "why_viral": "爆款原因一句话总结",
 "reusable_points": ["可复用要点1", "要点2"]}}
"""


@router.get("", response_model=list[InspirationItemOut])
def list_inspiration(platform: str = "", status: str = "", keyword: str = "",
                     limit: int = 100, _: Agent = Depends(get_current_agent),
                     db: Session = Depends(get_db)):
    q = db.query(InspirationItem)
    if platform:
        q = q.filter(InspirationItem.platform == platform)
    if status:
        q = q.filter(InspirationItem.status == status)
    if keyword:
        q = q.filter(InspirationItem.keyword == keyword)
    limit = max(1, min(limit, 500))
    return q.order_by(InspirationItem.id.desc()).limit(limit).all()


@router.post("", response_model=InspirationItemOut)
def create_inspiration(req: InspirationItemIn, agent: Agent = Depends(get_current_agent),
                       db: Session = Depends(get_db)):
    """手动录入爆款（粘贴链接 + 标题 + 正文）。"""
    item = InspirationItem(
        platform=req.platform, source="manual", source_url=req.source_url.strip(),
        author=req.author.strip(), title=req.title.strip(),
        content_text=req.content_text.strip(), stats_json=req.stats_json or {})
    db.add(item)
    db.commit()
    db.refresh(item)
    audit.log(db, agent, "inspiration_create", target=f"inspiration:{item.id}",
              detail=item.title[:60])
    return item


class RpaImportItem(BaseModel):
    source_url: str = ""
    author: str = ""
    title: str
    content_text: str = ""
    stats_json: dict = {}


class RpaImportIn(BaseModel):
    platform: str
    keyword: str = ""
    items: list[RpaImportItem]


@router.post("/import")
def import_inspiration(req: RpaImportIn, db: Session = Depends(get_db),
                       _: object = Depends(rpa_key_dep)):
    """RPA 热榜采集导入（Worker 鉴权）。按 source_url 去重幂等。"""
    if req.platform not in ("douyin", "xiaohongshu"):
        raise HTTPException(400, "platform 仅支持 douyin | xiaohongshu")
    created, skipped = 0, 0
    for raw in req.items[:200]:
        title = raw.title.strip()
        if not title:
            continue
        if raw.source_url:
            dup = db.query(InspirationItem).filter(
                InspirationItem.platform == req.platform,
                InspirationItem.source_url == raw.source_url).count()
            if dup:
                skipped += 1
                continue
        db.add(InspirationItem(
            platform=req.platform, source="rpa", source_url=raw.source_url.strip(),
            author=raw.author.strip(), title=title,
            content_text=raw.content_text.strip(), keyword=req.keyword.strip(),
            stats_json=raw.stats_json or {}))
        created += 1
    db.commit()
    logger.info("灵感导入 platform=%s keyword=%s created=%s skipped=%s",
                req.platform, req.keyword, created, skipped)
    return {"created": created, "skipped": skipped}


@router.post("/{item_id}/analyze", response_model=InspirationItemOut)
async def analyze_inspiration(item_id: int, agent: Agent = Depends(get_current_agent),
                              db: Session = Depends(get_db)):
    """AI 拆解：提炼标题公式/结构/钩子，结果存 analysis，状态置 analyzed。"""
    item = db.get(InspirationItem, item_id)
    if item is None:
        raise HTTPException(404, "灵感不存在")
    platform_name = "小红书" if item.platform == "xiaohongshu" else "抖音"
    stats_text = "，".join(f"{k}={v}" for k, v in (item.stats_json or {}).items()) or "未知"
    prompt = (ANALYZE_PROMPT
              .replace("{platform_name}", platform_name)
              .replace("{title}", item.title or "")
              .replace("{content}", (item.content_text or "")[:2000] or "（未提供正文）")
              .replace("{stats}", stats_text))
    result = await call_llm_json(prompt, InspirationAnalysis)
    if result is None:
        raise HTTPException(502, "AI 拆解失败（LLM 未配置或多次重试失败），请稍后重试")
    item.analysis = result.model_dump()
    item.status = "analyzed"
    db.commit()
    db.refresh(item)
    audit.log(db, agent, "inspiration_analyze", target=f"inspiration:{item.id}")
    return item


@router.post("/{item_id}/imitate")
def imitate_inspiration(item_id: int, agent: Agent = Depends(get_current_agent),
                        db: Session = Depends(get_db)):
    """一键仿写：以灵感为参考创建 ContentItem（draft），跳转创作台生成。

    未拆解的灵感会自动先拆解（拆解结论注入创作提示词，仿写质量显著更好）。
    """
    item = db.get(InspirationItem, item_id)
    if item is None:
        raise HTTPException(404, "灵感不存在")
    content = ContentItem(
        title=f"仿写：{(item.title or '')[:40]}",
        topic=item.title or "",
        selling_points=[],
        status="draft",
        created_by=agent.id,
        inspiration_id=item.id,
    )
    db.add(content)
    item.status = "used"
    db.commit()
    db.refresh(content)
    audit.log(db, agent, "inspiration_imitate", target=f"inspiration:{item.id}",
              detail=f"-> content:{content.id}")
    return {"content_item_id": content.id, "analyzed": bool(item.analysis)}


@router.delete("/{item_id}")
def delete_inspiration(item_id: int, agent: Agent = Depends(get_current_agent),
                       db: Session = Depends(get_db)):
    item = db.get(InspirationItem, item_id)
    if item is None:
        raise HTTPException(404, "灵感不存在")
    db.delete(item)
    db.commit()
    audit.log(db, agent, "inspiration_delete", target=f"inspiration:{item_id}",
              detail=(item.title or "")[:60])
    return {"ok": True}
