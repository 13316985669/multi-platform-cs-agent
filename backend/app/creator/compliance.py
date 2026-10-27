"""合规独立入口：编辑后手动复检（不走图）。"""
import logging

from ..database import SessionLocal
from ..models import ContentVersion
from .nodes import check_text_compliance

logger = logging.getLogger(__name__)


async def check_version(version_id: int) -> ContentVersion | None:
    """对单个版本执行合规双审并落库结果（供 /api/contents/versions/{id}/check 调用）。"""
    db = SessionLocal()
    try:
        version = db.get(ContentVersion, version_id)
        if version is None:
            return None
        report = await check_text_compliance(
            version.platform, version.title, version.body, version.script, version.tags or [],
        )
        version.compliance_status = "passed" if report.get("passed") else "failed"
        version.compliance_report = {"hits": report.get("hits", []),
                                     "suggestions": report.get("suggestions", [])}
        db.commit()
        db.refresh(version)
        logger.info("版本复检完成 version_id=%s status=%s", version.id, version.compliance_status)
        return version
    finally:
        db.close()
