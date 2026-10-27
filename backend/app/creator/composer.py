"""图文成片（档 2）：素材图轮播 + 字幕烧录 + TTS 配音 → 竖屏 mp4。

实现：subprocess 调 ffmpeg（settings.ffmpeg_bin），非 0 返回码抛异常带 stderr。
TTS 走 OpenAI 兼容 /audio/speech（tts_* 配置，未配置则静默无音轨）。
输出规格：1080x1920 竖屏，每图 3s（Ken Burns 缩放），总长 ≤60s。
"""
import asyncio
import logging
import os
import re
import tempfile
import uuid

import httpx

from ..config import settings
from ..database import SessionLocal
from ..models import ContentVersion, Material

logger = logging.getLogger(__name__)

IMAGE_SECONDS = 3
MAX_DURATION_SECONDS = 60
VIDEO_SIZE = "1080:1920"


# ============ 素材规格校验 ============

def validate_material(platform: str, material: Material) -> list[str]:
    """单素材平台规格校验，返回问题列表（空 = 合规）。"""
    problems = []
    if platform == "douyin":
        if material.kind == "video":
            if material.mime not in ("video/mp4", "video/quicktime"):
                problems.append(f"抖音视频仅支持 mp4/mov（当前 {material.mime}）")
            if material.duration_seconds and material.duration_seconds > 15 * 60:
                problems.append("抖音视频不能超过 15 分钟")
    elif platform == "xiaohongshu":
        if material.kind == "video" and material.duration_seconds \
                and material.duration_seconds > 15 * 60:
            problems.append("小红书视频不能超过 15 分钟")
    return problems


def validate_version_materials(platform: str, version: ContentVersion, db) -> list[str]:
    """版本级素材校验（如小红书图文 ≤9 张）。"""
    problems = []
    materials = [db.get(Material, mid) for mid in (version.material_ids or [])]
    materials = [m for m in materials if m is not None]
    if platform == "xiaohongshu" and version.content_type == "note":
        images = [m for m in materials if m.kind == "image"]
        if len(images) > 9:
            problems.append(f"小红书图文最多 9 张图（当前 {len(images)} 张）")
        if not images:
            problems.append("小红书图文至少需要 1 张图片素材")
    if platform == "douyin" and version.content_type == "video":
        videos = [m for m in materials if m.kind == "video"]
        if not videos:
            problems.append("抖音视频内容需要视频素材（可先用图文成片生成）")
    for m in materials:
        problems.extend(validate_material(platform, m))
    return problems


# ============ TTS ============

async def synthesize_tts(text: str, dest_path: str) -> bool:
    """TTS 配音（OpenAI 兼容 /audio/speech）。未配置或失败返回 False（静默无音轨）。"""
    if not settings.tts_configured or not text.strip():
        return False
    base_url = settings.tts_base_url or settings.llm_base_url
    api_key = settings.tts_api_key or settings.llm_api_key
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{base_url.rstrip('/')}/audio/speech",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"model": settings.tts_model, "voice": settings.tts_voice,
                          "input": text[:2000]},
                )
                resp.raise_for_status()
                with open(dest_path, "wb") as f:
                    f.write(resp.content)
                return True
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            logger.warning("TTS 合成失败（第 %d/3 次）: %s", attempt + 1, exc)
            await asyncio.sleep(2 ** attempt)
    logger.warning("TTS 重试 3 次失败，成片无配音: %s", last_exc)
    return False


# ============ 字幕 ============

def _split_sentences(script: str) -> list[str]:
    parts = re.split(r"[。！？!?\n]+", script)
    return [p.strip() for p in parts if p.strip()]


def _write_srt(sentences: list[str], total_seconds: float, dest_path: str):
    """按句均分时长生成 SRT 字幕。"""
    if not sentences:
        return
    per = total_seconds / len(sentences)

    def _ts(t: float) -> str:
        ms = int(t * 1000)
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, sentence in enumerate(sentences):
        lines.append(f"{i + 1}\n{_ts(i * per)} --> {_ts((i + 1) * per)}\n{sentence}\n")
    with open(dest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ============ ffmpeg ============

async def _run_ffmpeg(args: list[str]) -> None:
    """执行 ffmpeg，非 0 返回码抛异常（带 stderr 尾部）。"""
    proc = await asyncio.create_subprocess_exec(
        settings.ffmpeg_bin, *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = (stderr or b"").decode("utf-8", errors="replace")[-800:]
        raise RuntimeError(f"ffmpeg 执行失败（code={proc.returncode}）: {tail}")


def _srt_filter_path(path: str) -> str:
    """subtitles 滤镜的 Windows 路径转义（盘符冒号需转义，反斜杠改正斜杠）。"""
    p = path.replace("\\", "/")
    return p.replace(":", "\\:")


def _build_compose_args(image_paths: list[str], srt_path: str | None,
                        audio_path: str | None, out_path: str) -> list[str]:
    args: list[str] = ["-y"]
    for p in image_paths:
        args += ["-loop", "1", "-t", str(IMAGE_SECONDS), "-i", p]
    audio_index = None
    if audio_path:
        audio_index = len(image_paths)
        args += ["-i", audio_path]

    # 每图：缩放裁满竖屏 + Ken Burns 缓推（zoompan，25fps × 3s = 75 帧）
    filters = []
    for i in range(len(image_paths)):
        filters.append(
            f"[{i}:v]scale={VIDEO_SIZE}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_SIZE},fps=25,"
            f"zoompan=z='min(zoom+0.0015,1.15)':d={IMAGE_SECONDS * 25}:s=1080x1920[v{i}]")
    concat_in = "".join(f"[v{i}]" for i in range(len(image_paths)))
    filters.append(f"{concat_in}concat=n={len(image_paths)}:v=1:a=0[vcat]")
    if srt_path:
        filters.append(
            f"[vcat]subtitles='{_srt_filter_path(srt_path)}':"
            "force_style='FontName=Microsoft YaHei,FontSize=14,PrimaryColour=&HFFFFFF,"
            "OutlineColour=&H80000000,BorderStyle=1,Outline=2,Alignment=2,MarginV=120'[vout]")
        v_out = "[vout]"
    else:
        v_out = "[vcat]"

    args += ["-filter_complex", ";".join(filters), "-map", v_out]
    if audio_index is not None:
        args += ["-map", f"{audio_index}:a", "-c:a", "aac", "-shortest"]
    args += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(MAX_DURATION_SECONDS),
             out_path]
    return args


async def compose_video(version_id: int) -> Material:
    """图文成片入口：读版本素材图 + 口播脚本 → 合成竖屏 mp4 → 落素材库并关联回版本。"""
    db = SessionLocal()
    try:
        version = db.get(ContentVersion, version_id)
        if version is None:
            raise RuntimeError(f"内容版本不存在: {version_id}")
        images = [db.get(Material, mid) for mid in (version.material_ids or [])]
        images = [m for m in images if m is not None and m.kind == "image"
                  and os.path.exists(m.path)]
        if not images:
            raise RuntimeError("图文成片需要至少 1 张图片素材")
        if len(images) * IMAGE_SECONDS > MAX_DURATION_SECONDS:
            images = images[: MAX_DURATION_SECONDS // IMAGE_SECONDS]
            logger.warning("图片过多，截断到 %d 张（60s 上限）", len(images))

        work_dir = tempfile.mkdtemp(prefix="compose_")
        audio_path = os.path.join(work_dir, "tts.mp3")
        has_audio = await synthesize_tts(version.script or version.body, audio_path)

        sentences = _split_sentences(version.script or "")
        srt_path = None
        if sentences:
            srt_path = os.path.join(work_dir, "subtitles.srt")
            _write_srt(sentences, len(images) * IMAGE_SECONDS, srt_path)

        out_dir = os.path.join(settings.upload_dir, "materials")
        os.makedirs(out_dir, exist_ok=True)
        out_path = os.path.join(out_dir, f"composed_{uuid.uuid4().hex}.mp4")

        args = _build_compose_args([m.path for m in images], srt_path,
                                   audio_path if has_audio else None, out_path)
        try:
            await _run_ffmpeg(args)
        except RuntimeError:
            if not srt_path:
                raise
            # 字幕烧录失败（ffmpeg 无 libass/字体问题）→ 降级无字幕重试
            logger.warning("字幕烧录失败，降级为无字幕成片", exc_info=True)
            args = _build_compose_args([m.path for m in images], None,
                                       audio_path if has_audio else None, out_path)
            await _run_ffmpeg(args)

        material = Material(
            kind="video", path=out_path, mime="video/mp4",
            size=os.path.getsize(out_path),
            duration_seconds=float(len(images) * IMAGE_SECONDS),
        )
        db.add(material)
        db.flush()
        # 成片关联回版本（追加到素材列表，供发布通道取用）
        ids = list(version.material_ids or [])
        ids.append(material.id)
        version.material_ids = ids
        db.commit()
        db.refresh(material)
        logger.info("图文成片完成 version_id=%s material_id=%s", version_id, material.id)
        return material
    finally:
        db.close()
