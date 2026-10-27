"""pydantic 请求/响应模型 + 统一消息模型 + AI 输出契约。"""
from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ============ 统一入站消息模型（适配器 normalize 的产物） ============

class InboundMessage(BaseModel):
    platform: str  # douyin | xiaohongshu | mock
    platform_user_id: str
    platform_conversation_id: str = ""
    platform_msg_id: str = ""  # 幂等键
    msg_type: str = "text"  # text | image | video | voice
    content: str = ""
    nickname: str = ""
    avatar: str = ""
    media_id: str = ""       # RPA 通道媒体文件 ID（图片/语音）
    sender_side: str = "user"  # user | agent（RPA 旁路消息：客服在平台后台直接发送的）
    account: str = ""        # RPA 店铺账号（多账号路由）
    raw_payload: dict[str, Any] = Field(default_factory=dict)


# ============ AI 输出契约（与 prompts/cs_agent.md 严格对应） ============

class LeadInfo(BaseModel):
    phone: str = ""
    wechat: str = ""
    note: str = ""


class ToolCall(BaseModel):
    """LLM 请求调用业务工具。"""
    name: str
    args: dict[str, Any] = Field(default_factory=dict)


class AgentReply(BaseModel):
    """LLM 必须输出的严格 JSON 契约。"""
    reply_messages: list[str] = Field(default_factory=list, max_length=3)
    intent: Literal["consult_price", "consult_feature", "complaint", "after_sale", "chitchat", "other"] = "other"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    handoff: bool = False
    handoff_reason: Literal["", "complaint", "sensitive", "explicit_human", "low_confidence", "out_of_scope"] = ""
    tags: list[str] = Field(default_factory=list)
    lead: LeadInfo = Field(default_factory=LeadInfo)
    quick_action: Literal["none", "send_price_card", "send_link"] = "none"
    tool_call: Optional[ToolCall] = None  # 需要调用业务工具时输出，执行后由系统回填结果再次生成


# ============ 认证 ============

class LoginRequest(BaseModel):
    username: str
    password: str


class AgentOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    status: str
    max_concurrent: Optional[int] = 20
    modules: Optional[list[str]] = None  # None = 不限制；管理员始终忽略

    class Config:
        from_attributes = True


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    agent: AgentOut


class AgentCreate(BaseModel):
    username: str
    password: str
    display_name: str
    role: str = "agent"
    max_concurrent: int = 20
    modules: Optional[list[str]] = None  # 客服缺省为客服组；管理员忽略


class AgentUpdate(BaseModel):
    max_concurrent: Optional[int] = None
    display_name: Optional[str] = None
    role: Optional[Literal["admin", "agent"]] = None
    password: Optional[str] = None
    modules: Optional[list[str]] = None


class AgentStatusUpdate(BaseModel):
    status: Literal["active", "resting", "offline"]


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


# ============ 会话 / 消息 ============

class CustomerOut(BaseModel):
    id: int
    platform: str
    platform_user_id: str
    nickname: str
    avatar: str
    tags: list[str] = []
    lead_phone: str = ""
    lead_wechat: str = ""
    lead_note: str = ""
    created_at: datetime

    class Config:
        from_attributes = True


class MessageOut(BaseModel):
    id: int
    conversation_id: int
    sender_type: str
    sender_id: Optional[int] = None
    msg_type: str
    content: str
    extra: dict[str, Any] = {}
    is_internal: bool = False
    bad_case: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class ConversationOut(BaseModel):
    id: int
    customer: CustomerOut
    platform: str
    mode: str
    status: str
    assignee_id: Optional[int] = None
    assignee_name: str = ""
    unread_count: int = 0
    summary: str = ""
    last_message_at: datetime
    created_at: datetime
    last_message: Optional[MessageOut] = None
    wait_seconds: int = 0
    awaiting_first_response: bool = False
    overdue: bool = False
    transferred_in: bool = False
    is_returning: bool = False

    class Config:
        from_attributes = True


class ConversationModeUpdate(BaseModel):
    mode: Literal["ai", "human", "pending"]


class AssignRequest(BaseModel):
    agent_id: int
    note: str = ""


class AgentMessageSend(BaseModel):
    content: str
    is_internal: bool = False
    msg_type: str = "text"   # text | image（人工发送图片时带 media_id）
    media_id: str = ""


class TagUpdate(BaseModel):
    tags: list[str]


class BadCaseMark(BaseModel):
    bad_case: bool = True
    note: str = ""  # 备注：哪里答得不好，导出 evals 时随 case 携带


# ============ 知识库 ============

class FaqCreate(BaseModel):
    title: str
    question: str
    answer: str
    category: str = "未分类"
    similar_questions: list[str] = []


class FaqUpdate(BaseModel):
    title: Optional[str] = None
    question: Optional[str] = None
    answer: Optional[str] = None
    category: Optional[str] = None
    similar_questions: Optional[list[str]] = None


class DocMetaUpdate(BaseModel):
    title: Optional[str] = None
    category: Optional[str] = None


class DocStatusUpdate(BaseModel):
    status: str  # active | disabled


class DocBatchIn(BaseModel):
    ids: list[int]
    action: Literal["active", "disabled", "delete"]


class DocBatchOut(BaseModel):
    updated: int
    failed_ids: list[int]


class KnowledgeChunkOut(BaseModel):
    id: int
    chunk_index: int
    parent_id: Optional[int] = None
    content: str


class KnowledgeDocOut(BaseModel):
    id: int
    title: str
    doc_type: str
    status: str
    version: int
    chunk_count: int = 0
    question: str = ""
    answer: str = ""
    category: str = "未分类"
    similar_questions: list[str] = []
    hit_count: int = 0
    created_at: datetime
    updated_at: datetime


class DialogImportOut(BaseModel):
    imported: int
    created: int
    updated: int
    skipped_invalid: int
    skipped_missed: int
    gap_count: int
    gap_titles: list[str]


class FaqPackOut(BaseModel):
    applied: bool
    created: int
    content_updated: int
    enabling: int
    disabling_active: int
    skipped_ambiguous: list[str]
    duplicates_overridden: list[str] = []


class RecallTestRequest(BaseModel):
    query: str


class RecallTestResult(BaseModel):
    rewritten_queries: list[str] = []
    hits: list[dict[str, Any]] = []
    rerank_top_score: Optional[float] = None
    threshold: float
    passed: bool
    degraded: bool = False  # 是否处于纯 BM25 降级模式


class LeadCaptureOut(BaseModel):
    id: int
    customer_id: int
    conversation_id: int
    platform: str
    contact: str
    contact_type: str
    nickname: str
    business: str
    created_at: datetime

    class Config:
        from_attributes = True


class MissedQuestionOut(BaseModel):
    id: int
    question: str
    platform: str
    count: int
    status: str
    suggested_answer: str = ""
    conversation_id: int | None = None
    created_at: datetime

    class Config:
        from_attributes = True


# ============ 快捷回复 / 违禁词 ============

class QuickReplyIn(BaseModel):
    title: str
    content: str
    personal: bool = False


class QuickReplyOut(BaseModel):
    id: int
    title: str
    content: str
    agent_id: Optional[int] = None

    class Config:
        from_attributes = True


class BannedWordIn(BaseModel):
    word: str
    category: str = "极限词"  # direction=in 时作为级别：block | warn
    direction: Literal["out", "in"] = "out"


class BannedWordOut(BaseModel):
    id: int
    word: str
    category: str
    direction: str = "out"

    class Config:
        from_attributes = True


# ============ Mock 通道 ============

class MockIncoming(BaseModel):
    platform: Literal["douyin", "xiaohongshu", "mock"] = "mock"
    user_id: str = "mock_user_001"
    nickname: str = "模拟用户"
    content: str
    msg_type: str = "text"


# ============ RPA 通道（Worker 桥接协议，见 docs/rpa-workers.md） ============

class RpaIncoming(BaseModel):
    """Worker 上报的入站消息。"""
    account: str
    platform: Literal["douyin", "xiaohongshu"] = "douyin"
    nickname: str = ""
    content: str = ""
    msg_type: str = "text"  # text | image | voice
    media_id: str = ""
    msg_id: str = ""
    conversation_id: str = ""
    sender_side: Literal["user", "agent"] = "user"
    prev_nickname: str = ""  # 改昵称识别（Worker 按会话位置连续性发现）


class RpaAck(BaseModel):
    outbox_id: int
    worker_id: str
    ok: bool = True
    error: str = ""
    platform_msg_id: str = ""


class RpaRelease(BaseModel):
    outbox_id: int
    worker_id: str


class RpaSelfIdentity(BaseModel):
    worker_id: str
    profile_id: str = ""
    account_name: str


class RpaIncomingComment(BaseModel):
    """Worker 上报的作品评论（评论采集通道）。"""
    account: str
    platform: Literal["douyin", "xiaohongshu"] = "xiaohongshu"
    post_url: str = ""               # 作品链接（用于关联 Post）
    platform_post_id: str = ""       # 平台作品 ID（可空，兜底按 post_url 匹配）
    comment_id: str                  # 平台评论 ID（幂等键）
    parent_comment_id: str = ""
    author_id: str = ""
    author_nickname: str = ""
    content: str = ""


class RpaHeartbeat(BaseModel):
    worker_id: str
    account: str
    platform: str
    status: str = "online"  # online | login_expired | selector_mismatch
    meta: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = False   # True 时只校验 key 不落库（doctor 自检用，避免误标 online 后又被判 offline 告警）


class RpaIdentityResolve(BaseModel):
    account: str
    platform: str
    nickname: str
    prev_nickname: str = ""


class RpaWorkerOut(BaseModel):
    worker_id: str
    account: str
    platform: str
    status: str
    meta: dict[str, Any] = {}
    last_heartbeat_at: datetime
    pending: int = 0
    failed: int = 0


# ============ 工单 ============

class TicketCreate(BaseModel):
    conversation_id: int
    type: Literal["refund", "logistics", "other"] = "other"
    title: str
    content: str = ""


class TicketOut(BaseModel):
    id: int
    ticket_no: str
    conversation_id: int
    customer_id: int
    type: str
    title: str
    content: str
    status: str
    assignee_id: Optional[int] = None
    assignee_name: str = ""
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class TicketStatusUpdate(BaseModel):
    status: Literal["open", "processing", "done"]


# ============ 统计 ============

class StatsOverview(BaseModel):
    today_conversations: int
    today_messages: int
    ai_reply_rate: float
    handoff_rate: float
    avg_first_response_seconds: float
    reply_within_3min_rate: float  # 今日会话中首响 ≤180s 的占比（对齐飞鸽店铺考核）
    today_tokens: int              # 今日 LLM token 总用量
    today_token_cost_yuan: float   # 今日估算成本（元，按 LLM_PRICE_* 单价）
    active_agents: int
    pending_conversations: int
    total_unread: int                       # 进行中会话的未读消息总数（导航徽标）
    platform_breakdown: dict[str, int]      # 今日会话按平台分布 {douyin: n, ...}
    today_comments: int = 0                 # 今日采集评论数（内容矩阵）
    today_leads: int = 0                    # 今日留资数（漏斗 lead 层）
    today_wecom_adds: int = 0               # 今日加企微数（漏斗 wecom 层）
    handoff_reasons_7d: dict[str, int] = {}
    retrieval_miss_rate_7d: float = 0.0
    bad_case_count_7d: int = 0
    send_failed_count_7d: int = 0


# ============ 内容矩阵平台 ============

class MatrixAccountIn(BaseModel):
    platform: Literal["douyin", "xiaohongshu"]
    account_name: str
    auth_type: Literal["api", "rpa"] = "api"
    rpa_account: str = ""           # auth_type=rpa 时必填（与 Worker .env.local 的 ACCOUNT 一致）
    daily_publish_limit: int = 5
    daily_comment_limit: int = 100
    group_name: str = ""
    queue_enabled: bool = False
    queue_slots: list[str] = Field(default_factory=list)  # 每日发布时段位，如 ["12:00","19:30"]
    douyin_account_type: str = ""  # 空=跟随全局默认；feige | enterprise | laike（仅抖音）


class MatrixAccountUpdate(BaseModel):
    account_name: Optional[str] = None
    status: Optional[Literal["active", "expired", "disabled"]] = None
    daily_publish_limit: Optional[int] = None
    daily_comment_limit: Optional[int] = None
    group_name: Optional[str] = None
    rpa_account: Optional[str] = None
    queue_enabled: Optional[bool] = None
    queue_slots: Optional[list[str]] = None
    douyin_account_type: Optional[str] = None  # 空=跟随全局默认；仅抖音账号生效


class AccountImportIn(BaseModel):
    """批量导入账号：每行 `平台,名称,auth_type,rpa_account,分组`（后三项可空）。"""
    lines: list[str] = Field(default_factory=list, max_length=200)


class MatrixAccountOut(BaseModel):
    id: int
    platform: str
    account_name: str
    auth_type: str
    open_id: str
    rpa_account: str
    status: str
    has_credentials: bool = False   # credentials_enc 绝不出参，只告知是否存在
    daily_publish_limit: int
    daily_comment_limit: int
    group_name: str
    douyin_account_type: str = ""  # 空=跟随全局默认；feige | enterprise | laike
    queue_enabled: bool = False
    queue_slots: list[str] = []
    today_published: int = 0        # 当日已发布数（额度用量展示）
    profile: dict[str, Any] = {}    # 账号画像（粉丝数/作品数/获赞数，采集循环回采）
    created_at: datetime

    class Config:
        from_attributes = True


class ContentItemIn(BaseModel):
    title: str
    topic: str = ""
    selling_points: list[str] = Field(default_factory=list)


class ContentItemOut(BaseModel):
    id: int
    title: str
    topic: str
    selling_points: list[str] = []
    status: str
    created_by: Optional[int] = None
    review_note: str = ""
    reviewed_by: int = 0
    reviewed_at: Optional[datetime] = None
    inspiration_id: int = 0
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ContentVersionOut(BaseModel):
    id: int
    content_item_id: int
    platform: str
    content_type: str
    title: str
    body: str
    tags: list[str] = []
    script: str = ""
    cover_text: str = ""
    material_ids: list[int] = []
    compliance_status: str
    compliance_report: dict[str, Any] = {}
    first_comment: str = ""
    dup_report: dict[str, Any] = {}
    variant_no: int = 1
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ContentItemDetail(ContentItemOut):
    versions: list[ContentVersionOut] = []


class ContentVersionUpdate(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[list[str]] = None
    script: Optional[str] = None
    cover_text: Optional[str] = None
    material_ids: Optional[list[int]] = None
    first_comment: Optional[str] = None


class GenerateRequest(BaseModel):
    platforms: list[Literal["douyin", "xiaohongshu"]]
    content_type: Literal["note", "video"] = "note"
    inspiration_id: int = 0  # 参考爆款（0 = 纯原创）
    variants: int = Field(default=1, ge=1, le=3)  # 一稿多版：每平台生成 N 个风格变体


class PublishableVersionOut(BaseModel):
    """发布弹窗可选版本（审批通过 + 合规通过的平铺列表，一次查询避免 N+1）。"""
    version_id: int
    item_id: int
    item_title: str
    platform: str
    content_type: str
    variant_no: int = 1
    title: str = ""
    first_comment: str = ""


class ContentReviewIn(BaseModel):
    action: Literal["approve", "reject"]
    note: str = ""


class ComplianceReport(BaseModel):
    status: Literal["pending", "passed", "failed"] = "pending"
    hits: list[dict[str, Any]] = []
    suggestions: list[str] = []


class MaterialOut(BaseModel):
    id: int
    kind: str
    mime: str
    size: int
    duration_seconds: float = 0
    url: str = ""
    created_at: datetime

    class Config:
        from_attributes = True


class PublishTaskIn(BaseModel):
    content_version_id: int
    account_ids: list[int]
    scheduled_at: Optional[datetime] = None  # 空 = 立即执行
    force: bool = False  # 查重软拦截确认（相似度>0.9 时必须显式确认）


class PublishTaskOut(BaseModel):
    id: int
    content_version_id: int
    account_id: int
    account_name: str = ""
    platform: str = ""
    scheduled_at: datetime
    status: str
    platform_post_id: str = ""
    post_url: str = ""
    error: str = ""
    retries: int = 0
    published_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class PostOut(BaseModel):
    id: int
    publish_task_id: int
    account_id: int
    account_name: str = ""
    platform: str
    platform_post_id: str
    url: str
    title: str
    stats_json: dict[str, Any] = {}
    created_at: datetime

    class Config:
        from_attributes = True


class PostCommentOut(BaseModel):
    id: int
    post_id: int
    post_title: str = ""
    platform: str
    platform_comment_id: str
    author_nickname: str
    content: str
    intent: str = ""
    status: str
    reply_content: str = ""
    replied_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class CommentReplyIn(BaseModel):
    content: str


class CommentRuleIn(BaseModel):
    platform: str = ""
    intent: str = ""
    keywords: list[str] = Field(default_factory=list)
    reply_templates: list[str] = Field(default_factory=list)
    guide_code: str = ""
    enabled: bool = True
    priority: int = 0


class CommentRuleOut(CommentRuleIn):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class FunnelOverview(BaseModel):
    stages: dict[str, int] = {}       # {comment: n, dm: n, lead: n, wecom: n}
    conversion: dict[str, float] = {}  # {dm_rate, lead_rate, wecom_rate}
    by_platform: dict[str, dict[str, int]] = {}
    by_account: dict[str, dict[str, int]] = {}


class FunnelEventOut(BaseModel):
    id: int
    stage: str
    platform: str
    account_id: int
    post_id: int
    customer_id: int
    guide_code: str
    created_at: datetime

    class Config:
        from_attributes = True


class AutoSwitchesOut(BaseModel):
    comment_auto_reply_enabled: bool
    publish_auto_enabled: bool


class AutoSwitchesIn(BaseModel):
    comment_auto_reply_enabled: Optional[bool] = None
    publish_auto_enabled: Optional[bool] = None


# ============ Phase 6：数据回采 / 排期 / 灵感库 ============

class PostStatPoint(BaseModel):
    play: int = 0
    digg: int = 0
    comment: int = 0
    share: int = 0
    collect: int = 0
    captured_at: datetime


class AnalyticsPostOut(BaseModel):
    id: int
    account_id: int
    account_name: str = ""
    platform: str
    title: str
    url: str
    stats: dict[str, Any] = {}       # 最新快照 {play, digg, comment, share, collect}
    stats_updated_at: Optional[datetime] = None
    created_at: datetime


class ContentRoiOut(BaseModel):
    """选题维度效果：聚合该内容所有平台版本的发布数据 + 漏斗事件。"""
    content_item_id: int
    title: str
    posts: int = 0
    play: int = 0
    digg: int = 0
    comment: int = 0
    funnel: dict[str, int] = {}      # {comment, dm, lead, wecom}


class AccountReportOut(BaseModel):
    """账号维度效果报表（Phase 7）：发布/互动/漏斗/成功率/健康度/画像。"""
    account_id: int
    account_name: str
    platform: str
    group_name: str = ""
    posts: int = 0
    play: int = 0
    digg: int = 0
    comment: int = 0
    publish_success_rate: Optional[float] = None  # 窗口内 success/(success+failed)，无样本为 None
    funnel: dict[str, int] = {}      # {lead, wecom}
    health_level: str = "good"
    health_score: int = 100
    profile: dict[str, Any] = {}     # {followers, works, liked, updated_at}


class RescheduleIn(BaseModel):
    scheduled_at: datetime


class EnqueueIn(BaseModel):
    content_version_id: int
    account_ids: list[int]
    force: bool = False  # 查重软拦截确认


class CalendarDayOut(BaseModel):
    date: str                        # YYYY-MM-DD
    tasks: list[PublishTaskOut] = []


class BestSlotsOut(BaseModel):
    account_id: int
    source: str                      # history 历史数据 | default 平台黄金时段
    slots: list[str] = []            # ["HH:MM", ...]


class InspirationItemIn(BaseModel):
    platform: Literal["douyin", "xiaohongshu"]
    source_url: str = ""
    author: str = ""
    title: str
    content_text: str = ""
    stats_json: dict[str, Any] = {}


class InspirationItemOut(BaseModel):
    id: int
    platform: str
    source: str
    source_url: str
    author: str
    title: str
    content_text: str
    keyword: str = ""
    stats_json: dict[str, Any] = {}
    analysis: dict[str, Any] = {}
    status: str
    created_at: datetime

    class Config:
        from_attributes = True
