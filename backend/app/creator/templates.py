"""平台规格常量与提示词片段渲染。

新增平台时只需在 PLATFORM_SPECS 加一条 + PLATFORM_NAMES 加中文名。
"""

PLATFORM_SPECS: dict[str, dict] = {
    "xiaohongshu": {
        "title_max": 20,
        "body_max": 1000,
        "tag_max": 10,
        "spec_text": (
            "- 平台：小红书图文笔记\n"
            "- 标题：不超过 20 个字\n"
            "- 正文：不超过 1000 字，多分短段，适度 emoji\n"
            "- 话题标签：3~10 个，与内容强相关\n"
            "- 封面文案：一句话，不超过 15 字"
        ),
    },
    "douyin": {
        "title_max": 55,
        "body_max": 300,
        "tag_max": 5,
        "script_max_seconds": 60,
        "spec_text": (
            "- 平台：抖音短视频\n"
            "- 标题：不超过 55 个字，可带 #话题#\n"
            "- 口播脚本：60 秒以内（约 150~200 字），口语化有节奏\n"
            "- 视频简介（body）：不超过 300 字\n"
            "- 话题标签：2~5 个"
        ),
    },
}

PLATFORM_NAMES = {
    "douyin": "抖音",
    "xiaohongshu": "小红书",
}

CONTENT_TYPE_NAMES = {
    "note": "图文笔记",
    "video": "短视频",
}


def platform_spec_text(platform: str) -> str:
    return PLATFORM_SPECS.get(platform, {}).get("spec_text", "保持平台常规风格")


def clamp_draft(platform: str, draft: dict) -> dict:
    """按平台规格截断草稿字段（硬约束兜底，不完全信任 LLM 自控）。"""
    spec = PLATFORM_SPECS.get(platform, {})
    if not spec:
        return draft
    title_max = spec.get("title_max")
    if title_max and len(draft.get("title", "")) > title_max:
        draft["title"] = draft["title"][:title_max]
    body_max = spec.get("body_max")
    if body_max and len(draft.get("body", "")) > body_max:
        draft["body"] = draft["body"][:body_max]
    tag_max = spec.get("tag_max")
    if tag_max and isinstance(draft.get("tags"), list):
        draft["tags"] = [str(t).lstrip("#").strip() for t in draft["tags"] if str(t).strip()][:tag_max]
    return draft
