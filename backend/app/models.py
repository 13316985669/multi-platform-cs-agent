"""数据模型：客户/会话/消息/客服/知识库/未命中问题/快捷回复/违禁词/转人工事件/幂等表/任务队列表。"""
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Boolean, Float, ForeignKey, JSON, Index,
)
from sqlalchemy.orm import relationship

from .database import Base


class Agent(Base):
    """客服账号。"""
    __tablename__ = "agents"

    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(128), nullable=False)
    display_name = Column(String(64), nullable=False)
    role = Column(String(16), default="agent")  # admin | agent
    # None = 不限制（旧账号）；非空为可见模块 key 列表。管理员忽略此列。
    modules = Column(JSON, nullable=True)
    status = Column(String(16), default="offline")  # active(接待中) | resting(休息) | offline
    max_concurrent = Column(Integer, default=20)  # 接待上限；0 = 不自动分配，仍可接受转接
    created_at = Column(DateTime, default=datetime.utcnow)


class Customer(Base):
    """客户（平台用户）。"""
    __tablename__ = "customers"
    __table_args__ = (Index("ix_customers_platform_user", "platform", "platform_user_id", unique=True),)

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), nullable=False)  # douyin | xiaohongshu | mock
    platform_user_id = Column(String(64), nullable=False)
    nickname = Column(String(64), default="")
    avatar = Column(String(512), default="")
    tags = Column(JSON, default=list)
    lead_phone = Column(String(32), default="")
    lead_wechat = Column(String(64), default="")
    lead_note = Column(String(256), default="")
    wecom_added_at = Column(DateTime, nullable=True)  # 加企微成功时间（企微回调归因写入）
    source_guide_code = Column(String(32), default="")  # 来源暗号（评论引流归因）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    conversations = relationship("Conversation", back_populates="customer")


class LeadCapture(Base):
    """一次留资。同一会话同一号码不重复。"""
    __tablename__ = "lead_captures"
    __table_args__ = (
        Index("ix_lead_captures_conv_contact", "conversation_id", "contact", unique=True),
    )

    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    platform = Column(String(16), default="", index=True)
    contact = Column(String(64), nullable=False)
    contact_type = Column(String(16), default="phone")  # phone | wechat
    nickname = Column(String(64), default="")
    business = Column(String(64), default="")
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Conversation(Base):
    """会话。mode: ai(AI接待) | human(人工接待) | pending(排队待人工)；status: open | closed。"""
    __tablename__ = "conversations"

    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    platform = Column(String(16), nullable=False)
    platform_conversation_id = Column(String(64), default="")  # 平台侧会话 ID（如抖音 conversation_short_id）
    mode = Column(String(8), default="ai", index=True)
    status = Column(String(8), default="open", index=True)
    assignee_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    unread_count = Column(Integer, default=0)
    summary = Column(Text, default="")  # 滚动小结（长上下文压缩）
    summary_upto_message_id = Column(Integer, default=0)
    last_message_at = Column(DateTime, default=datetime.utcnow, index=True)
    lead_followups = Column(Integer, default=0)  # 沉默留资跟进已发次数，整段会话最多 2
    created_at = Column(DateTime, default=datetime.utcnow)
    closed_at = Column(DateTime, nullable=True)

    customer = relationship("Customer", back_populates="conversations")
    messages = relationship("Message", back_populates="conversation", order_by="Message.id")


class Message(Base):
    """消息。sender_type: user | ai | agent | system。platform_msg_id 唯一索引用于幂等。"""
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_platform_msg", "platform_msg_id", unique=True),)

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    sender_type = Column(String(8), nullable=False)
    sender_id = Column(Integer, nullable=True)  # agent 消息对应 Agent.id
    msg_type = Column(String(16), default="text")  # text | image | video | system
    content = Column(Text, default="")
    platform_msg_id = Column(String(64), nullable=True)
    extra = Column(JSON, default=dict)  # citations / intent / confidence / handoff_reason 等
    is_internal = Column(Boolean, default=False)  # 内部备注，仅团队可见
    bad_case = Column(Boolean, default=False)  # 客服标记「回答不佳」
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    conversation = relationship("Conversation", back_populates="messages")


class KnowledgeDoc(Base):
    """知识文档：faq（结构化问答对）或 file（上传文档）。"""
    __tablename__ = "knowledge_docs"

    id = Column(Integer, primary_key=True)
    title = Column(String(256), nullable=False)
    doc_type = Column(String(8), default="faq")  # faq | file
    content = Column(Text, default="")  # faq: "问：…\n答：…" 或 file 原文
    category = Column(String(64), default="未分类")
    similar_questions = Column(Text, default="[]")  # FAQ 相似问，JSON 字符串数组
    status = Column(String(16), default="active")  # active | disabled | archived
    hit_count = Column(Integer, default=0)  # AI 实际发出回复时引用次数
    version = Column(Integer, default=1)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chunks = relationship("KnowledgeChunk", back_populates="doc", cascade="all, delete-orphan")


class KnowledgeChunk(Base):
    """知识切片。父子索引：子块参与检索，parent_id 指向父块（FAQ 场景 parent 为空，自身即完整知识）。"""
    __tablename__ = "knowledge_chunks"

    id = Column(Integer, primary_key=True)
    doc_id = Column(Integer, ForeignKey("knowledge_docs.id"), nullable=False, index=True)
    parent_id = Column(Integer, nullable=True)
    chunk_index = Column(Integer, default=0)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    doc = relationship("KnowledgeDoc", back_populates="chunks")


class MissedQuestion(Base):
    """未命中问题沉淀：RAG 无命中时自动收集，供运营转 FAQ。"""
    __tablename__ = "missed_questions"

    id = Column(Integer, primary_key=True)
    question = Column(Text, nullable=False)
    platform = Column(String(16), default="")
    conversation_id = Column(Integer, nullable=True)
    count = Column(Integer, default=1)
    suggested_answer = Column(Text, default="")  # 人工纠正，待确认后才入库
    status = Column(String(16), default="pending")  # pending | resolved
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class QuickReply(Base):
    """快捷回复（口语化模板库）。agent_id 为空=团队模板；有值=仅该客服可见。"""
    __tablename__ = "quick_replies"

    id = Column(Integer, primary_key=True)
    title = Column(String(64), nullable=False)
    content = Column(Text, nullable=False)
    agent_id = Column(Integer, ForeignKey("agents.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BannedWord(Base):
    """违禁词库。direction=out：AI 出口违禁词（广告法极限词等）；direction=in：用户入口风险词（category 存级别 block|warn）。"""
    __tablename__ = "banned_words"

    id = Column(Integer, primary_key=True)
    word = Column(String(64), unique=True, nullable=False)
    category = Column(String(32), default="极限词")
    direction = Column(String(8), default="out")  # out | in
    created_at = Column(DateTime, default=datetime.utcnow)


class Ticket(Base):
    """工单：售后/物流等需要线下跟进的事项，可由 AI 工具或客服手动创建。"""
    __tablename__ = "tickets"

    id = Column(Integer, primary_key=True)
    ticket_no = Column(String(32), unique=True, nullable=False, index=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    type = Column(String(16), default="other")  # refund | logistics | other
    title = Column(String(128), nullable=False)
    content = Column(Text, default="")
    status = Column(String(16), default="open", index=True)  # open | processing | done
    assignee_id = Column(Integer, ForeignKey("agents.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HandoffEvent(Base):
    """转人工事件记录（用于统计转人工率）。"""
    __tablename__ = "handoff_events"

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, ForeignKey("conversations.id"), nullable=False)
    reason = Column(String(32), default="")
    from_mode = Column(String(8), default="ai")
    to_agent_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ProcessedEvent(Base):
    """幂等去重表：webhook 事件按 event_key 去重。"""
    __tablename__ = "processed_events"

    id = Column(Integer, primary_key=True)
    event_key = Column(String(128), unique=True, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class QueueTask(Base):
    """持久化任务队列：webhook 秒级 ACK 后由后台 worker 消费，崩溃重启可恢复。"""
    __tablename__ = "queue_tasks"

    id = Column(Integer, primary_key=True)
    task_type = Column(String(32), nullable=False)  # inbound_message
    payload = Column(JSON, nullable=False)
    status = Column(String(16), default="pending", index=True)  # pending | processing | done | failed
    retries = Column(Integer, default=0)
    error = Column(Text, default="")
    not_before = Column(DateTime, nullable=True)  # 延迟任务：到点才可被消费（如首评延迟 1-3 分钟）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PlatformToken(Base):
    """平台 OAuth token 持久化：刷新后落库，进程重启不丢失（目前用于小红书 ark 网关）。

    access_expires_at / refresh_expires_at 为 UTC 绝对时间，由平台返回的 expiresAt 解析；
    解析失败为 None 时发送路径按「无过期信息」处理（用到失效错误码再强制刷新）。
    """
    __tablename__ = "platform_tokens"

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), unique=True, nullable=False, index=True)
    access_token = Column(Text, default="")
    refresh_token = Column(Text, default="")
    access_expires_at = Column(DateTime, nullable=True)
    refresh_expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ============ RPA 通道（无官方 API 资质时的降级方案，见 docs/rpa-workers.md） ============

class RpaOutbox(Base):
    """RPA 出站队列：RPA 通道的待发消息，Worker 轮询拉取（按 account 隔离）并回执。"""
    __tablename__ = "rpa_outbox"
    __table_args__ = (Index("ix_rpa_outbox_status_account", "status", "account"),)

    id = Column(Integer, primary_key=True)
    account = Column(String(64), nullable=False, default="", index=True)  # 店铺账号（多账号路由）
    platform = Column(String(16), nullable=False)
    platform_conversation_id = Column(String(128), default="")  # 页面侧会话标识（不含 account 前缀）
    platform_user_id = Column(String(80), default="")
    content = Column(Text, default="")
    question = Column(Text, default="")  # 入队时记下的用户原话；答案在 content
    msg_type = Column(String(16), default="text")  # text | image | voice
    media_id = Column(String(64), default="")
    status = Column(String(16), default="pending", index=True)  # pending | leased | acked | failed
    worker_id = Column(String(64), default="")
    attempts = Column(Integer, default=0)
    error = Column(Text, default="")
    result = Column(String(512), default="")  # Worker 回执附带结果（如发布成功的笔记 URL）
    leased_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    acked_at = Column(DateTime, nullable=True)


class RpaWorker(Base):
    """RPA Worker 注册表：心跳与运行状态。"""
    __tablename__ = "rpa_workers"

    id = Column(Integer, primary_key=True)
    worker_id = Column(String(64), unique=True, nullable=False, index=True)
    account = Column(String(64), nullable=False, default="")
    platform = Column(String(16), nullable=False)
    status = Column(String(24), default="online")  # online | login_expired | selector_mismatch | offline
    meta = Column(JSON, default=dict)  # 版本 / Chrome / 统计等
    last_heartbeat_at = Column(DateTime, default=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)


class RpaMedia(Base):
    """RPA 媒体文件登记：文件本体存 upload_dir/rpa/。"""
    __tablename__ = "rpa_media"

    id = Column(String(64), primary_key=True)  # uuid hex
    kind = Column(String(16), default="image")  # image | voice
    path = Column(String(256), nullable=False)
    mime = Column(String(64), default="")
    size = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class RpaIdentityMap(Base):
    """RPA 平台身份映射：昵称 → 稳定内部 ID（用户改昵称不断会话）。"""
    __tablename__ = "rpa_identity_map"
    __table_args__ = (Index("ix_rpa_identity_unique", "account", "platform", "nickname", unique=True),)

    id = Column(Integer, primary_key=True)
    account = Column(String(64), nullable=False, default="")
    platform = Column(String(16), nullable=False)
    nickname = Column(String(128), nullable=False)
    stable_user_id = Column(String(80), nullable=False)
    first_seen_at = Column(DateTime, default=datetime.utcnow)
    last_seen_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LlmUsage(Base):
    """LLM 调用 token 用量：成本核算与用量趋势（每次 API 调用一条，含解析失败的重试）。"""
    __tablename__ = "llm_usage"
    __table_args__ = (Index("ix_llm_usage_created", "created_at"),)

    id = Column(Integer, primary_key=True)
    conversation_id = Column(Integer, default=0, index=True)
    model = Column(String(64), default="")
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class AppSetting(Base):
    """运行时开关落库。重启后从这里读回，避免只活在进程内存里。"""
    __tablename__ = "app_settings"

    key = Column(String(64), primary_key=True)
    value = Column(Text, default="")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AccountBinding(Base):
    """账号职责绑定：一个矩阵账号的私信、评论或发布，对应一个 Worker 和一个浏览器环境。"""
    __tablename__ = "account_bindings"
    __table_args__ = (
        Index("ix_account_bindings_account_duty", "account_id", "duty", unique=True),
    )

    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("matrix_accounts.id"), nullable=False, index=True)
    duty = Column(String(16), nullable=False)  # dm | comment | publish
    driver = Column(String(32), nullable=False)
    provider = Column(String(16), default="adspower")  # adspower | local
    adspower_profile_id = Column(String(64), default="")
    worker_id = Column(String(64), default="", index=True)
    platform_url = Column(String(256), default="")
    auth_status = Column(String(24), default="pending_login")
    # unbound | pending_login | authorized | login_expired | disabled
    reopen_requested = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuditLog(Base):
    """操作审计日志：接管/释放会话、知识库变更、设置变更等敏感操作留痕。"""
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_logs_created", "created_at"),)

    id = Column(Integer, primary_key=True)
    agent_id = Column(Integer, default=0, index=True)  # 0 = 系统操作
    agent_name = Column(String(64), default="")        # 冗余展示名（客服被删后仍可读）
    action = Column(String(48), nullable=False, index=True)  # takeover | release | kb_create | ...
    target = Column(String(128), default="")           # 操作对象（如 conversation:12 / doc:5）
    detail = Column(String(500), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


# ============ 内容矩阵平台（创作 → 发布 → 引流 → 承接） ============

class MatrixAccount(Base):
    """矩阵账号：一个平台下的一个运营账号。

    auth_type=api：走官方开放平台，credentials_enc 存 Fernet 加密 JSON
    （{"access_token","refresh_token","expires_at"}，见 core/crypto.py）；
    auth_type=rpa：走 RPA Worker，rpa_account 与 rpa_workers.account 对应路由。
    """
    __tablename__ = "matrix_accounts"
    __table_args__ = (Index("ix_matrix_accounts_platform_openid", "platform", "open_id", unique=True),)

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), nullable=False)  # douyin | xiaohongshu
    account_name = Column(String(64), nullable=False)
    auth_type = Column(String(8), default="api")  # api | rpa
    open_id = Column(String(64), default="")  # 抖音授权账号 open_id（RPA 账号为空字符串）
    credentials_enc = Column(Text, default="")  # Fernet 加密凭证 JSON，绝不明文出参
    rpa_account = Column(String(64), default="")  # RPA 通道账号标识（auth_type=rpa 时使用）
    douyin_account_type = Column(String(16), default="")  # 抖音账号形态：空=跟随全局默认 | feige | enterprise | laike
    status = Column(String(16), default="active")  # active | expired | disabled
    daily_publish_limit = Column(Integer, default=5)
    daily_comment_limit = Column(Integer, default=100)
    group_name = Column(String(32), default="")
    queue_enabled = Column(Boolean, default=False)  # 发布队列模式：内容入队自动占时段坑
    queue_slots = Column(JSON, default=list)  # 每日发布时段位，如 ["12:00", "19:30"]
    profile_json = Column(JSON, default=dict)  # 账号画像回采 {followers, works, liked, updated_at}
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ContentItem(Base):
    """内容主体：一次创作的主题与卖点。status: draft | reviewing | approved | archived。"""
    __tablename__ = "content_items"

    id = Column(Integer, primary_key=True)
    title = Column(String(256), nullable=False)
    topic = Column(String(256), default="")
    selling_points = Column(JSON, default=list)
    status = Column(String(16), default="draft", index=True)
    created_by = Column(Integer, ForeignKey("agents.id"), nullable=True)
    review_note = Column(String(512), default="")  # 审批意见（驳回原因/通过备注）
    reviewed_by = Column(Integer, default=0)  # 审批人 agent id
    reviewed_at = Column(DateTime, nullable=True)
    inspiration_id = Column(Integer, default=0)  # 来源灵感（0 = 原创选题）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    versions = relationship("ContentVersion", back_populates="item",
                            cascade="all, delete-orphan", order_by="ContentVersion.id")


class ContentVersion(Base):
    """平台适配版本：一稿多投时每个平台一版，各自带合规检测结果。"""
    __tablename__ = "content_versions"

    id = Column(Integer, primary_key=True)
    content_item_id = Column(Integer, ForeignKey("content_items.id"), nullable=False, index=True)
    platform = Column(String(16), nullable=False)
    content_type = Column(String(8), default="note")  # note 图文 | video 视频
    title = Column(String(256), default="")
    body = Column(Text, default="")
    tags = Column(JSON, default=list)  # 话题标签
    script = Column(Text, default="")  # 口播脚本（视频）
    cover_text = Column(String(256), default="")  # 封面文案
    material_ids = Column(JSON, default=list)  # 关联素材 ID 列表
    compliance_status = Column(String(16), default="pending")  # pending | passed | failed
    compliance_report = Column(JSON, default=dict)  # {hits:[...], suggestions:[...]}
    first_comment = Column(String(512), default="")  # 首评引流话术（{code} 占位暗号，空=不发首评）
    dup_report = Column(JSON, default=dict)  # 查重报告 {max_similarity, similar_version_id, checked_at}
    variant_no = Column(Integer, default=1)  # 一稿多版序号（1=第 1 版，风格见 creator/nodes.VARIANT_STYLES）
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    item = relationship("ContentItem", back_populates="versions")


class Material(Base):
    """素材库：图片/视频/音频文件，本体存 upload_dir/materials/。"""
    __tablename__ = "materials"

    id = Column(Integer, primary_key=True)
    kind = Column(String(16), default="image")  # image | video | audio
    path = Column(String(256), nullable=False)
    mime = Column(String(64), default="")
    size = Column(Integer, default=0)
    duration_seconds = Column(Float, default=0)  # 视频/音频时长（图片为 0）
    created_at = Column(DateTime, default=datetime.utcnow)


class PublishTask(Base):
    """发布任务：一个内容版本 × 一个账号 × 一个计划时间。

    状态机：pending → publishing → success | failed（retries<3 可回 pending 重试）；
    cancelled 为人工取消终态。
    """
    __tablename__ = "publish_tasks"

    id = Column(Integer, primary_key=True)
    content_version_id = Column(Integer, ForeignKey("content_versions.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("matrix_accounts.id"), nullable=False)
    scheduled_at = Column(DateTime, nullable=False, index=True)
    status = Column(String(16), default="pending", index=True)
    platform_post_id = Column(String(128), default="")
    post_url = Column(String(512), default="")
    error = Column(Text, default="")
    retries = Column(Integer, default=0)
    published_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Post(Base):
    """已发布作品：发布成功后落库，关联账号与内容版本，stats_json 存数据快照。"""
    __tablename__ = "posts"

    id = Column(Integer, primary_key=True)
    publish_task_id = Column(Integer, ForeignKey("publish_tasks.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("matrix_accounts.id"), nullable=False)
    platform = Column(String(16), nullable=False)
    platform_post_id = Column(String(128), default="", index=True)
    url = Column(String(512), default="")
    title = Column(String(256), default="")
    stats_json = Column(JSON, default=dict)  # {play, digg, comment, ...} 数据回采快照
    stats_updated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class PostComment(Base):
    """作品评论：采集入库后由评论引擎处理。

    status: pending 待处理 | replied 已自动回复 | manual 已人工回复 | skipped 跳过（差评/广告等转人工）。
    platform_comment_id 唯一索引用于幂等。
    """
    __tablename__ = "post_comments"

    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False, index=True)
    platform = Column(String(16), nullable=False)
    platform_comment_id = Column(String(128), nullable=False)
    parent_comment_id = Column(String(128), default="")
    author_id = Column(String(64), default="")
    author_nickname = Column(String(128), default="")
    content = Column(Text, default="")
    intent = Column(String(32), default="")  # consult | price | praise | complaint | spam | irrelevant
    status = Column(String(16), default="pending", index=True)
    reply_content = Column(Text, default="")
    replied_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (Index("ix_post_comments_platform_cid", "platform_comment_id", unique=True),)


class CommentRule(Base):
    """评论自动回复规则：intent/关键词命中 → 模板随机回复（含暗号引导私信）。"""
    __tablename__ = "comment_rules"

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), default="")  # 空 = 全平台通用
    intent = Column(String(32), default="")  # 命中意图（空 = 仅按关键词）
    keywords = Column(JSON, default=list)  # 命中关键词（空 = 仅按意图）
    reply_templates = Column(JSON, default=list)  # 回复模板池（随机选取，{code} 变量替换暗号）
    guide_code = Column(String(32), default="")  # 暗号（用户私信该词触发归因）
    enabled = Column(Boolean, default=True)
    priority = Column(Integer, default=0)  # 数值越大越优先
    created_at = Column(DateTime, default=datetime.utcnow)


class FunnelEvent(Base):
    """引流漏斗事件：comment → dm → lead → wecom 全链追踪。"""
    __tablename__ = "funnel_events"
    __table_args__ = (Index("ix_funnel_events_created", "created_at"),)

    id = Column(Integer, primary_key=True)
    stage = Column(String(16), nullable=False, index=True)  # comment | dm | lead | wecom
    platform = Column(String(16), default="")
    account_id = Column(Integer, default=0)
    post_id = Column(Integer, default=0)
    comment_id = Column(Integer, default=0)
    customer_id = Column(Integer, default=0)
    guide_code = Column(String(32), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class WecomChannelCode(Base):
    """企微渠道活码：state 编码归因信息（≤30 字符），加粉回调按 state 反查归因。"""
    __tablename__ = "wecom_channel_codes"

    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)
    config_id = Column(String(64), default="")  # 企微返回的 config_id
    qr_url = Column(String(512), default="")  # 活码二维码图片地址
    state = Column(String(32), unique=True, nullable=False)
    bound_content_id = Column(Integer, default=0)  # 归因到内容（0 = 不绑定）
    bound_account_id = Column(Integer, default=0)  # 归因到账号（0 = 不绑定）
    created_at = Column(DateTime, default=datetime.utcnow)


class PostStatSnapshot(Base):
    """作品数据快照：每次回采一行，时间序列供趋势曲线与最佳时段分析。"""
    __tablename__ = "post_stat_snapshots"
    __table_args__ = (Index("ix_post_stat_snapshots_post", "post_id", "captured_at"),)

    id = Column(Integer, primary_key=True)
    post_id = Column(Integer, ForeignKey("posts.id"), nullable=False)
    play = Column(Integer, default=0)     # 播放/阅读
    digg = Column(Integer, default=0)     # 点赞
    comment = Column(Integer, default=0)  # 评论
    share = Column(Integer, default=0)    # 分享
    collect = Column(Integer, default=0)  # 收藏
    captured_at = Column(DateTime, default=datetime.utcnow)


class InspirationItem(Base):
    """爆款灵感库：手动录入或 RPA 热榜采集的爆款内容，供 AI 拆解与仿写。

    status: new 新采集 | analyzed 已拆解 | used 已仿写。
    """
    __tablename__ = "inspiration_items"
    __table_args__ = (Index("ix_inspiration_platform_created", "platform", "created_at"),)

    id = Column(Integer, primary_key=True)
    platform = Column(String(16), nullable=False)  # douyin | xiaohongshu
    source = Column(String(8), default="manual")  # manual 手动录入 | rpa 热榜采集
    source_url = Column(String(512), default="")
    author = Column(String(128), default="")
    title = Column(String(256), default="")
    content_text = Column(Text, default="")  # 正文/口播文案（手动录入时可填）
    keyword = Column(String(64), default="")  # 采集关键词（RPA 来源）
    stats_json = Column(JSON, default=dict)  # {play, digg, ...} 采集时快照
    analysis = Column(JSON, default=dict)  # AI 拆解结果 {title_formula, structure, hooks, ...}
    status = Column(String(16), default="new", index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
