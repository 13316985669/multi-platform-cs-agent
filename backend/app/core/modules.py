"""账号可见模块：清单、路径放行、WebSocket 事件过滤。

modules 为 None 表示不限制（旧账号）。管理员忽略勾选，始终可见全部。
路径按「页面要完成现有操作需要哪些请求」放行，写操作留在原模块。
"""
import re

MODULE_DEFS: tuple[tuple[str, str, str], ...] = (
    ("workbench", "对话", "cs"),
    ("knowledge", "知识库", "cs"),
    ("tickets", "工单", "cs"),
    ("leads", "留资", "cs"),
    ("dashboard", "数据", "cs"),
    ("accounts", "账号", "matrix"),
    ("studio", "创作", "matrix"),
    ("publish", "发布", "matrix"),
    ("comments", "评论", "matrix"),
    ("funnel", "漏斗", "matrix"),
    ("analytics", "效果", "matrix"),
    ("inspiration", "灵感", "matrix"),
    ("mock", "模拟", "matrix"),
    ("settings", "设置", "settings"),
)

MODULE_KEYS: frozenset[str] = frozenset(key for key, _label, _group in MODULE_DEFS)
CS_MODULE_KEYS: tuple[str, ...] = tuple(key for key, _label, group in MODULE_DEFS if group == "cs")

# 具备其中任一模块即可。未列入的路径不检查（登录、webhook、RPA worker、签名素材等）。
_OUTBOX_ACTION = re.compile(r"^/api/rpa/outbox/\d+/(retry|discard)$")
_MATERIAL_FILE = re.compile(r"^/api/materials/\d+/file$")

# 未列出的事件只推给不限制的连接，避免新事件把私信漏给矩阵账号。
WS_EVENT_MODULES: dict[str, frozenset[str]] = {
    "new_message": frozenset({"workbench"}),
    "conversation_update": frozenset({"workbench"}),
    "conversation_closed": frozenset({"workbench"}),
    "handoff": frozenset({"workbench"}),
    "ai_typing": frozenset({"workbench"}),
    "ticket_created": frozenset({"workbench", "tickets"}),
    "content_version_updated": frozenset({"studio"}),
    "comment_new": frozenset({"comments"}),
    "publish_task_updated": frozenset({"publish"}),
}


class ModuleSelectionError(ValueError):
    """勾选不合法：空列表或未知 key。"""


def normalize_modules(modules: list[str] | None) -> list[str]:
    """去重并保持顺序。空列表和未知 key 拒绝。"""
    if not modules:
        raise ModuleSelectionError("至少选择一个模块")
    unknown = [key for key in modules if key not in MODULE_KEYS]
    if unknown:
        raise ModuleSelectionError("未知模块")
    seen: list[str] = []
    for key in modules:
        if key not in seen:
            seen.append(key)
    order = {key: index for index, (key, _label, _group) in enumerate(MODULE_DEFS)}
    seen.sort(key=lambda key: order[key])
    return seen


def agent_can(agent, key: str) -> bool:
    """管理员和未配置模块的旧账号放行。"""
    if getattr(agent, "role", None) == "admin":
        return True
    modules = getattr(agent, "modules", None)
    if modules is None:
        return True
    return key in modules


def visible_modules(agent) -> set[str] | None:
    """None 表示接收全部实时事件。"""
    if getattr(agent, "role", None) == "admin" or getattr(agent, "modules", None) is None:
        return None
    return set(agent.modules)


def allow_ws_event(modules: set[str] | None, event: str) -> bool:
    if modules is None:
        return True
    needed = WS_EVENT_MODULES.get(event)
    if needed is None:
        return False
    return bool(modules & needed)


def _clean_path(path: str) -> str:
    path = path.split("?", 1)[0]
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def allowed_modules(method: str, path: str) -> list[str] | None:
    """返回可访问该请求的模块（满足其一即可）。None 表示不按模块拦截。"""
    method = method.upper()
    path = _clean_path(path)

    if path == "/api/knowledge/recall-test" and method == "POST":
        return ["workbench", "knowledge"]
    if path == "/api/knowledge/banned-words" or path.startswith("/api/knowledge/banned-words/"):
        return ["settings"]
    if path.startswith("/api/knowledge"):
        return ["knowledge"]

    if path == "/api/quick-replies" and method == "GET":
        return ["workbench", "settings"]
    if path == "/api/quick-replies" or path.startswith("/api/quick-replies/"):
        return ["settings"]

    if path == "/api/agents/me/status":
        return None
    if path == "/api/agents" and method == "GET":
        return ["workbench", "tickets", "settings"]
    if path == "/api/agents" or path.startswith("/api/agents/"):
        return ["settings"]

    if path == "/api/stats/overview" and method == "GET":
        return ["workbench", "dashboard"]
    if path.startswith("/api/stats"):
        return ["dashboard"]

    if path.startswith("/api/tickets/by-conversation/"):
        return ["workbench", "tickets"]
    if path == "/api/tickets" and method == "POST":
        return ["workbench", "tickets"]
    if path.startswith("/api/tickets"):
        return ["tickets"]

    if path == "/api/accounts/oauth/callback":
        return None
    if path == "/api/accounts" and method == "GET":
        return ["accounts", "publish"]
    if path.startswith("/api/accounts"):
        return ["accounts"]

    if path == "/api/contents/publishable" and method == "GET":
        return ["studio", "publish"]
    if path.startswith("/api/contents"):
        return ["studio"]
    if method == "GET" and _MATERIAL_FILE.match(path):
        return None
    if path.startswith("/api/materials"):
        return ["studio"]

    if path.startswith("/api/publish") or path == "/api/posts" or path.startswith("/api/posts/"):
        return ["publish"]

    if path.startswith("/api/comments"):
        return ["comments"]
    if path.startswith("/api/funnel"):
        return ["funnel"]
    if path.startswith("/api/analytics"):
        return ["analytics"]
    if path.startswith("/api/inspiration"):
        return ["inspiration"]
    if path.startswith("/api/leads"):
        return ["leads"]

    if path.startswith("/api/conversations") and method == "GET":
        return ["workbench", "mock"]
    if path.startswith("/api/conversations"):
        return ["workbench"]
    if path.startswith("/api/messages") and method == "GET":
        return ["workbench", "mock"]
    if path.startswith("/api/messages"):
        return ["workbench"]

    if path == "/api/media" and method == "POST":
        return ["workbench"]
    # GET /api/media/{id} 用查询参数 token，在接口内校验对话权限。

    if path == "/api/rpa/replies":
        return ["accounts"]
    if path in ("/api/rpa/workers", "/api/rpa/outbox/failed") or _OUTBOX_ACTION.match(path):
        return ["settings"]

    if path.startswith("/api/settings") or path.startswith("/api/evals") or path.startswith("/api/queue"):
        return ["settings"]
    if path.startswith("/api/account-bindings"):
        return ["accounts"]
    if path == "/api/health/detail":
        return ["settings"]
    return None
