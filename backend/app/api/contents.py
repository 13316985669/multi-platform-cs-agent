"""内容创作 API：内容 CRUD、AI 生成（队列异步）、合规复检、审批流、素材库。"""
import hashlib
import hmac
import logging
import os
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..config import settings
from ..core import audit
from ..core.queue import enqueue
from ..database import get_db
from ..models import Agent, ContentItem, ContentVersion, Material
from ..schemas import (ContentItemDetail, ContentItemIn, ContentItemOut,
                       ContentReviewIn, ContentVersionOut, ContentVersionUpdate,
                       GenerateRequest, MaterialOut, PublishableVersionOut)
from .deps import get_current_agent, require_admin

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["contents"])

MATERIAL_MAX_BYTES = 500 * 1024 * 1024  # 500MB（视频素材）
MATERIAL_MIME_PREFIXES = ("image/", "video/", "audio/")


# ============ 内容 CRUD ============

@router.get("/contents", response_model=list[ContentItemOut])
def list_contents(status: str = "", _: Agent = Depends(get_current_agent),
                  db: Session = Depends(get_db)):
    q = db.query(ContentItem).filter(ContentItem.topic != "外部登记")
    if status:
        q = q.filter(ContentItem.status == status)
    return q.order_by(ContentItem.updated_at.desc()).limit(200).all()


@router.post("/contents", response_model=ContentItemOut)
def create_content(req: ContentItemIn, agent: Agent = Depends(get_current_agent),
                   db: Session = Depends(get_db)):
    item = ContentItem(title=req.title.strip(), topic=req.topic.strip(),
                       selling_points=[p.strip() for p in req.selling_points if p.strip()],
                       created_by=agent.id)
    db.add(item)
    db.commit()
    db.refresh(item)
    audit.log(db, agent, "content_create", target=f"content:{item.id}", detail=item.title)
    return item


@router.get("/contents/publishable", response_model=list[PublishableVersionOut])
def list_publishable(_: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """可发布版本列表（发布弹窗数据源）：审批通过 + 合规通过的版本平铺。

    一次 JOIN 查询直出，替代前端「列表 + 逐条详情」的 N+1 加载。
    注意：路由须位于 /contents/{item_id} 之前注册，否则 publishable 会被当作 item_id。
    """
    rows = (
        db.query(ContentVersion, ContentItem.title)
        .join(ContentItem, ContentItem.id == ContentVersion.content_item_id)
        .filter(ContentItem.status == "approved",
                ContentVersion.compliance_status == "passed")
        .order_by(ContentItem.updated_at.desc())
        .limit(200)
        .all()
    )
    return [PublishableVersionOut(
        version_id=v.id, item_id=v.content_item_id, item_title=item_title,
        platform=v.platform, content_type=v.content_type, variant_no=v.variant_no or 1,
        title=v.title or "", first_comment=v.first_comment or "",
    ) for v, item_title in rows]


@router.get("/contents/{item_id}", response_model=ContentItemDetail)
def get_content(item_id: int, _: Agent = Depends(get_current_agent),
                db: Session = Depends(get_db)):
    item = db.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(404, "内容不存在")
    out = ContentItemDetail.model_validate(item)
    out.versions = [ContentVersionOut.model_validate(v) for v in item.versions]
    return out


@router.delete("/contents/{item_id}")
def delete_content(item_id: int, agent: Agent = Depends(get_current_agent),
                   db: Session = Depends(get_db)):
    item = db.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(404, "内容不存在")
    db.delete(item)
    db.commit()
    audit.log(db, agent, "content_delete", target=f"content:{item_id}", detail=item.title)
    return {"ok": True}


# ============ AI 生成 / 合规复检（队列异步） ============

@router.post("/contents/{item_id}/generate")
def generate_content(item_id: int, req: GenerateRequest,
                     agent: Agent = Depends(get_current_agent),
                     db: Session = Depends(get_db)):
    """AI 生成平台版本：每个目标平台入队一次创作图运行（生成→合规→改写循环）。

    req.inspiration_id > 0 时绑定参考爆款（创作图会把拆解结论注入提示词）。
    """
    item = db.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(404, "内容不存在")
    if not req.platforms:
        raise HTTPException(400, "至少选择一个目标平台")
    if req.inspiration_id:
        from ..models import InspirationItem
        if db.get(InspirationItem, req.inspiration_id) is None:
            raise HTTPException(404, "参考爆款不存在")
        item.inspiration_id = req.inspiration_id
        db.commit()
    for platform in dict.fromkeys(req.platforms):  # 去重保序
        for variant_index in range(req.variants):  # 一稿多版：每平台 N 个风格变体
            enqueue("content_generate", {
                "item_id": item_id, "platform": platform, "content_type": req.content_type,
                "variant_index": variant_index,
            })
    audit.log(db, agent, "content_generate", target=f"content:{item_id}",
              detail=f"platforms={req.platforms} type={req.content_type} variants={req.variants}")
    return {"queued": len(set(req.platforms)) * req.variants}


@router.post("/contents/versions/{version_id}/check")
def check_version(version_id: int, _: Agent = Depends(get_current_agent),
                  db: Session = Depends(get_db)):
    """手动合规复检（编辑后）：入队异步执行，前端轮询结果。"""
    if db.get(ContentVersion, version_id) is None:
        raise HTTPException(404, "版本不存在")
    enqueue("compliance_check", {"version_id": version_id})
    return {"queued": True}


@router.post("/contents/versions/{version_id}/titles")
async def suggest_titles(version_id: int, _: Agent = Depends(get_current_agent),
                         db: Session = Depends(get_db)):
    """标题助手（Phase 7）：基于选题+正文由 LLM 出 10 个候选标题（标注公式/评分）。"""
    version = db.get(ContentVersion, version_id)
    if version is None:
        raise HTTPException(404, "版本不存在")
    item = db.get(ContentItem, version.content_item_id)
    from ..creator.contracts import TitleSuggestions
    from ..creator.llm import call_llm_json
    from ..creator.templates import PLATFORM_NAMES, platform_spec_text
    prompt = (
        f"你是{PLATFORM_NAMES.get(version.platform, version.platform)}爆款标题专家。\n\n"
        f"【选题】{(item.topic or item.title) if item else ''}\n"
        f"【卖点】{'、'.join(item.selling_points or []) if item else ''}\n"
        f"【当前标题】{version.title}\n"
        f"【正文摘要】{(version.body or '')[:300]}\n\n"
        f"【平台规格】\n{platform_spec_text(version.platform)}\n\n"
        "请输出 10 个候选标题，要求：\n"
        "1. 覆盖不同标题公式（数字清单/痛点提问/悬念反转/身份共鸣/对比冲突等），每个标注所用公式\n"
        "2. 严格遵守平台字数上限与社区规范：不用极限词、不夸大、不含任何联系方式\n"
        "3. 每个标题给 0-100 的爆款潜力评分（钩子强度/信息量/平台调性匹配度）\n\n"
        "只输出 JSON：{\"titles\": [{\"text\": \"标题\", \"formula\": \"公式\", \"score\": 85}]}"
    )
    result = await call_llm_json(prompt, TitleSuggestions)
    if result is None:
        raise HTTPException(503, "LLM 未配置或调用失败，请稍后重试")
    return {"titles": [t.model_dump() for t in result.titles]}


@router.post("/contents/versions/{version_id}/keywords")
async def suggest_keywords(version_id: int, _: Agent = Depends(get_current_agent),
                           db: Session = Depends(get_db)):
    """关键词埋词（Phase 7）：LLM 推荐品类热搜词，并检测当前版本标题/正文/标签覆盖。"""
    version = db.get(ContentVersion, version_id)
    if version is None:
        raise HTTPException(404, "版本不存在")
    item = db.get(ContentItem, version.content_item_id)
    from ..creator.contracts import KeywordSuggestions
    from ..creator.llm import call_llm_json
    from ..creator.templates import PLATFORM_NAMES
    prompt = (
        f"你是{PLATFORM_NAMES.get(version.platform, version.platform)}搜索 SEO 专家。\n\n"
        f"【选题】{(item.topic or item.title) if item else ''}\n"
        f"【卖点】{'、'.join(item.selling_points or []) if item else ''}\n"
        f"【当前标题】{version.title}\n"
        f"【正文摘要】{(version.body or '')[:300]}\n"
        f"【当前话题标签】{' '.join(version.tags or [])}\n\n"
        "请推荐 8 个该品类的热搜关键词（用户真实搜索词，优先长尾词），"
        "并标注热度（高/中/低）。要求与选题强相关、不含违禁词与联系方式。\n\n"
        "只输出 JSON：{\"keywords\": [{\"word\": \"关键词\", \"heat\": \"高\"}]}"
    )
    result = await call_llm_json(prompt, KeywordSuggestions)
    if result is None:
        raise HTTPException(503, "LLM 未配置或调用失败，请稍后重试")
    # 覆盖检测：标题/正文/标签文本中包含该词即算已覆盖
    full_text = f"{version.title}\n{version.body}\n{' '.join(version.tags or [])}"
    return {"keywords": [
        {"word": kw.word, "heat": kw.heat, "covered": bool(kw.word) and kw.word in full_text}
        for kw in result.keywords
    ]}


@router.put("/contents/versions/{version_id}", response_model=ContentVersionOut)
def update_version(version_id: int, req: ContentVersionUpdate,
                   _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """编辑版本内容。编辑后合规状态重置为 pending，需重新检测。"""
    version = db.get(ContentVersion, version_id)
    if version is None:
        raise HTTPException(404, "版本不存在")
    for field, value in req.model_dump(exclude_none=True).items():
        setattr(version, field, value)
    version.compliance_status = "pending"
    version.compliance_report = {}
    db.commit()
    db.refresh(version)
    return version


# ============ 审批流（draft → reviewing → approved；驳回回 draft） ============

@router.post("/contents/{item_id}/submit", response_model=ContentItemOut)
def submit_content(item_id: int, agent: Agent = Depends(get_current_agent),
                   db: Session = Depends(get_db)):
    """提交审核：draft → reviewing。要求至少有一个版本且全部通过合规检测。"""
    item = db.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(404, "内容不存在")
    if item.status not in ("draft",):
        raise HTTPException(400, f"当前状态（{item.status}）不可提交审核")
    if not item.versions:
        raise HTTPException(400, "还没有生成任何平台版本，请先生成")
    not_passed = [v for v in item.versions if v.compliance_status != "passed"]
    if not_passed:
        raise HTTPException(400, f"还有 {len(not_passed)} 个版本未通过合规检测，请先处理")
    item.status = "reviewing"
    db.commit()
    db.refresh(item)
    audit.log(db, agent, "content_submit", target=f"content:{item.id}", detail=item.title)
    return item


@router.post("/contents/{item_id}/review", response_model=ContentItemOut)
def review_content(item_id: int, req: ContentReviewIn,
                   agent: Agent = Depends(require_admin), db: Session = Depends(get_db)):
    """审批（仅管理员）：approve → approved；reject → draft + 驳回原因。"""
    item = db.get(ContentItem, item_id)
    if item is None:
        raise HTTPException(404, "内容不存在")
    if item.status != "reviewing":
        raise HTTPException(400, f"仅待审核（reviewing）状态可审批（当前 {item.status}）")
    if req.action == "approve":
        item.status = "approved"
    else:
        if not req.note.strip():
            raise HTTPException(400, "驳回必须填写原因（note）")
        item.status = "draft"
    item.review_note = req.note.strip()
    item.reviewed_by = agent.id
    item.reviewed_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    audit.log(db, agent, f"content_review_{req.action}", target=f"content:{item.id}",
              detail=req.note.strip()[:200] or item.title)
    return item


# ============ 素材库 ============

def _material_sign(material_id: int) -> str:
    """素材 URL 签名：HMAC(secret_key, "material:{id}") 截断 16 位。

    素材 URL 会被复制进素材包文本外发（PublishCalendar 导出），不能拼 JWT（等于泄露全权限 token）；
    签名 URL 免登录可访问但不可枚举/伪造，整型主键遍历拿不到 sign。
    """
    return hmac.new(settings.secret_key.encode("utf-8"),
                    f"material:{material_id}".encode("utf-8"),
                    hashlib.sha256).hexdigest()[:16]


def _material_url(m: Material) -> str:
    return f"/api/materials/{m.id}/file?sign={_material_sign(m.id)}"


@router.get("/materials", response_model=list[MaterialOut])
def list_materials(kind: str = "", _: Agent = Depends(get_current_agent),
                   db: Session = Depends(get_db)):
    q = db.query(Material)
    if kind:
        q = q.filter(Material.kind == kind)
    rows = q.order_by(Material.id.desc()).limit(200).all()
    return [MaterialOut(id=m.id, kind=m.kind, mime=m.mime, size=m.size,
                        duration_seconds=m.duration_seconds or 0,
                        url=_material_url(m), created_at=m.created_at) for m in rows]


@router.post("/materials", response_model=MaterialOut)
def upload_material(file: UploadFile, kind: str = "image",
                    _: Agent = Depends(get_current_agent), db: Session = Depends(get_db)):
    """上传素材（图片/视频/音频）。视频时长在图文成片时读取，此处不强制解析。"""
    mime = file.content_type or "application/octet-stream"
    if not mime.startswith(MATERIAL_MIME_PREFIXES):
        raise HTTPException(400, f"不支持的素材类型: {mime}")
    if kind not in ("image", "video", "audio"):
        kind = mime.split("/")[0] if mime.startswith(MATERIAL_MIME_PREFIXES) else "image"

    dir_path = os.path.join(settings.upload_dir, "materials")
    os.makedirs(dir_path, exist_ok=True)
    ext = os.path.splitext(file.filename or "")[1][:10] or ".bin"
    name = f"{uuid.uuid4().hex}{ext}"
    path = os.path.join(dir_path, name)

    data = file.file.read(MATERIAL_MAX_BYTES + 1)
    if len(data) > MATERIAL_MAX_BYTES:
        raise HTTPException(413, "素材超过 500MB 限制")
    with open(path, "wb") as f:
        f.write(data)

    m = Material(kind=kind, path=path, mime=mime, size=len(data))
    db.add(m)
    db.commit()
    db.refresh(m)
    return MaterialOut(id=m.id, kind=m.kind, mime=m.mime, size=m.size,
                       duration_seconds=0, url=_material_url(m), created_at=m.created_at)


@router.get("/materials/{material_id}/file")
def material_file(material_id: int, sign: str = "", db: Session = Depends(get_db)):
    """素材文件读取：HMAC 签名 URL 校验（免登录但不可枚举/伪造，签名由列表/上传接口下发）。"""
    from fastapi.responses import FileResponse
    if not sign or not hmac.compare_digest(sign, _material_sign(material_id)):
        raise HTTPException(403, "签名无效")
    m = db.get(Material, material_id)
    if m is None or not os.path.exists(m.path):
        raise HTTPException(404, "素材不存在")
    return FileResponse(m.path, media_type=m.mime)
