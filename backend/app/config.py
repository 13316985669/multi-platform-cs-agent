"""全局配置：从 .env 读取，所有密钥/阈值集中管理。"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 基础
    app_name: str = "multi-platform-cs-agent"
    app_env: str = "development"  # production 时不重置已有管理员密码
    database_url: str = "sqlite:///./data/app.db"
    # AI 全局熔断开关：关闭时所有入站会话直接转人工（运行时也可经 /api/settings/ai-switch 切换）
    ai_globally_enabled: bool = True
    chroma_dir: str = "./data/chroma"
    upload_dir: str = "./data/uploads"
    secret_key: str = "change-me-to-a-random-string"
    access_token_expire_minutes: int = 720

    # 安全基线（Phase 8）
    cors_origins: str = ""       # 跨域白名单（逗号分隔；空 = 仅同源。经 nginx/vite 代理部署时无需配置）
    mock_enabled: bool = True    # Mock 通道开关（/api/mock/incoming；生产环境必须置 false）

    # LLM（OpenAI 兼容）
    llm_base_url: str = "https://api.deepseek.com/v1"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    # Token 单价（元/千 tokens，用于成本估算；0 = 不估算）
    llm_price_input_per_1k: float = 0.0
    llm_price_output_per_1k: float = 0.0

    # Embedding / Rerank（不配置则降级纯 BM25）
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"
    rerank_base_url: str = "https://api.siliconflow.cn/v1"
    rerank_api_key: str = ""
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rag_rerank_threshold: float = 0.35
    rag_dense_min_score: float = 0.3
    rag_dense_absolute_min: float = 0.45
    rag_relative_score_ratio: float = 0.65
    rag_degraded_score_gap: float = 1.5
    rag_dense_score_gap: float = 0.08
    # 高置信快路径：精排最高分 ≥ 该阈值且候选 ≤2 条时跳过 judge（相关性判定 LLM），
    # 省 3-8 秒/条；faithfulness/证据核对护栏不变。0 = 关闭快路径。
    rag_judge_skip_score: float = 0.9
    # 订单 / 物流真实接口。留空则工具返回未接入，不把演示数据说给用户。
    order_api_url: str = ""
    logistics_api_url: str = ""

    # 抖音开放平台
    douyin_client_key: str = ""
    douyin_client_secret: str = ""
    douyin_access_token: str = ""

    # 小红书开放平台
    xhs_app_id: str = ""
    xhs_app_secret: str = ""
    xhs_access_token: str = ""
    xhs_refresh_token: str = ""

    # 拟人化发送
    humanize_base_delay_ms: int = 800
    humanize_per_char_ms: int = 60

    # 队列并发：fast lane 同时处理的入站消息上限（数十用户同时问答的并发阀门；
    # 调大前先压测：LLM/embedding 供应商配额 + SQLite 写压力）
    queue_fast_concurrency: int = 20

    # 会话超时自动关闭（仅 AI 接待中的会话）
    session_timeout_minutes: int = 30
    session_close_message: str = "先不打扰您啦，有问题随时喊我哈~"
    session_sweep_interval_seconds: int = 300

    # 告警（钉钉/企微机器人 webhook 地址，留空则只记日志）
    alert_webhook_url: str = ""
    alert_cooldown_seconds: int = 600

    # 备用 LLM（主模型故障时热备切换，OpenAI 兼容）
    llm_fallback_base_url: str = ""
    llm_fallback_api_key: str = ""
    llm_fallback_model: str = ""

    # 自动备份
    backup_dir: str = "./backups"
    backup_interval_hours: int = 24
    backup_keep: int = 7

    # RPA 通道（无官方 API 资质时的降级方案，见 docs/rpa-workers.md）
    rpa_api_key: str = ""              # Worker 鉴权密钥（留空则 RPA 接口不可用）
    zhini_reply_api_key: str = ""      # 知你快回「自己的回复接口」密钥（留空则该接口不可用）
    douyin_channel: str = "api"        # api | rpa
    xhs_channel: str = "api"           # api | rpa
    douyin_account_type: str = "feige"  # feige（抖店商家）| enterprise（蓝V 企业号）| laike（来客商家，影响 RPA 频控规则）
    rpa_outbox_lease_seconds: int = 60
    rpa_worker_offline_seconds: int = 90
    rpa_media_retention_days: int = 30

    # 语音转写（OpenAI 兼容 /audio/transcriptions，留空禁用；如 Whisper / SenseVoice）
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model: str = ""

    # ============ 内容矩阵平台 ============
    # 抖音 OAuth（账号矩阵授权回调，需公网 HTTPS 地址，与开放平台控制台配置一致）
    douyin_oauth_redirect_uri: str = ""

    # 企业微信（渠道活码 + 加粉回调；留空则企微承接走「仅留资人工添加」降级）
    wecom_corp_id: str = ""
    wecom_secret: str = ""
    wecom_token: str = ""               # 回调 URL 验证 token
    wecom_encoding_aes_key: str = ""    # 回调消息加解密 key（43 位）
    wecom_welcome_text: str = "你好呀，我是你的专属顾问，有任何问题随时找我~"
    wecom_agent_user_id: str = ""       # 活码默认承接成员的 UserID

    # 自动化熔断开关（默认关闭，联调验证后再开；运行时也可经 /api/settings/auto-switches 切换）
    comment_auto_reply_enabled: bool = False
    publish_auto_enabled: bool = False

    # 品牌语气（Phase 7）：创作提示词注入的品牌风格指南；运行时也可经 /api/settings/brand-style 切换
    brand_style_guide: str = ""

    # 图文成片（Phase 2）：TTS（OpenAI 兼容 /audio/speech，留空则成片无配音）+ ffmpeg
    tts_base_url: str = ""
    tts_api_key: str = ""
    tts_model: str = ""
    tts_voice: str = "alloy"
    ffmpeg_bin: str = "ffmpeg"

    # 图片理解（OpenAI 兼容视觉模型，留空禁用；base_url/key 留空时复用主 LLM 配置）
    vision_base_url: str = ""
    vision_api_key: str = ""
    vision_model: str = ""

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def llm_fallback_configured(self) -> bool:
        return bool(self.llm_fallback_api_key and self.llm_fallback_model)

    @property
    def embedding_configured(self) -> bool:
        return bool(self.embedding_api_key)

    @property
    def rerank_configured(self) -> bool:
        return bool(self.rerank_api_key)

    @property
    def douyin_configured(self) -> bool:
        return bool(self.douyin_client_key and self.douyin_access_token)

    @property
    def xhs_configured(self) -> bool:
        return bool(self.xhs_app_id and self.xhs_access_token)

    @property
    def rpa_configured(self) -> bool:
        return bool(self.rpa_api_key)

    @property
    def zhini_configured(self) -> bool:
        return bool(self.zhini_reply_api_key)

    @property
    def asr_configured(self) -> bool:
        return bool(self.asr_base_url and self.asr_api_key and self.asr_model)

    @property
    def vision_configured(self) -> bool:
        # base_url/key 留空时复用主 LLM 配置，因此只需 vision_model + 任一可用 key
        return bool(self.vision_model and (self.vision_api_key or self.llm_api_key))

    @property
    def wecom_configured(self) -> bool:
        return bool(self.wecom_corp_id and self.wecom_secret)

    @property
    def wecom_callback_configured(self) -> bool:
        return bool(self.wecom_token and self.wecom_encoding_aes_key)

    @property
    def tts_configured(self) -> bool:
        # base_url/key 留空时复用主 LLM 配置，只需 tts_model + 任一可用 key
        return bool(self.tts_model and (self.tts_api_key or self.llm_api_key))


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
