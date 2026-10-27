# 客服 Agent RAG 管道生产级差距分析与优化路线图

> 范围：切片、数据清洗、检索、精排、生成全链路。约束：保持零依赖单机架构（SQLite + 本地 Chroma + 进程内 BM25/缓存）。交付物：分析报告 + P0/P1/P2 分级路线图，先评审，不改代码。

---

## 一、总体结论

该 RAG 管道的**护栏体系已达到较高成熟度**（降级链路、证据核对、忠实度门槛、检索 trace、离线评测三件套、告警监控），明显优于常见 demo 级实现。**离生产级的核心差距不在"算法不够花哨"，而在五类工程问题**：

1. **一致性缺陷**：进程内状态（BM25/FAQ 钉住/回答缓存）在多 worker 部署下互相不可见；向量库与 DB 无对账机制。
2. **数据完整性缺陷**：DOCX 表格整体丢失；嵌入失败的切片永久降级 BM25；换 embedding 模型不触发重建。
3. **缓存正确性缺陷**：回答语义缓存不感知提示词变更。
4. **同步阻塞**：入库在请求路径同步执行；BM25 全量重建持锁期间查询被阻塞。
5. **质量闭环缺失**：评测靠手动跑、无 CI 门禁；线上拒答率/延迟无聚合看板；阈值未经线上数据校准。

---

## 二、现状盘点（全链路架构）

| 环节 | 实现 | 文件 |
|---|---|---|
| 文档解析 | PDF(pypdf)+DOCX(python-docx)+txt；页眉页脚去除；折行合并 | [ingest.py](../../backend/app/rag/ingest.py#L47-L189) |
| 切片 | FAQ 整体一块；文档按章节/空行/清单成块；相邻块 10-20% 重叠；无结构长段按相邻句嵌入相似度切分 | [ingest.py](../../backend/app/rag/ingest.py#L199-L401) |
| 嵌入/索引 | SiliconFlow bge-m3，批次 32；Chroma cosine 本地持久化 | [embeddings.py](../../backend/app/rag/embeddings.py) / [vectorstore.py](../../backend/app/rag/vectorstore.py) |
| 稀疏检索 | jieba + rank_bm25（Lucene 风格恒正 idf 修正），内存索引 | [bm25.py](../../backend/app/rag/bm25.py) |
| 查询改写 | LLM 指代消解 + 1 个同义改写（最多 3 路）；未命中后 broaden 放宽重试一次 | [rewrite.py](../../backend/app/rag/rewrite.py) |
| 检索融合 | 每路 dense(top20,≥0.3)+BM25(top20) → RRF(K=60) → fused[:20] → 分类屏蔽 → 精确 FAQ 钉住 | [pipeline.py](../../backend/app/rag/pipeline.py#L53-L135) |
| 精排 | bge-reranker-v2-m3 API；原句权重 1.0/改写 0.5 两路并发；阈值 0.35+相对比例 0.65 过滤 | [rerank.py](../../backend/app/rag/rerank.py) / [pipeline.py](../../backend/app/rag/pipeline.py#L439-L482) |
| 兜底规则 | 精排不可用时退回 dense 分差 0.08 / BM25 倍数 1.5x 规则 | [pipeline.py](../../backend/app/rag/pipeline.py#L485-L544) |
| 生成 | 提示词热更新（cs_agent.md）；上下文预算 800/2400 字符；JSON 契约；草稿预取 | [engine.py](../../backend/app/agent/engine.py) / [prompt.py](../../backend/app/agent/prompt.py) |
| 质量护栏 | LLM 相关性判定 → context_precision；evidence 数字/单位/极性核对；faithfulness≥0.6 双门槛；反思 reflect | [confidence.py](../../backend/app/agent/confidence.py) / [evidence.py](../../backend/app/agent/evidence.py) |
| 回答缓存 | 语义缓存：余弦≥0.92，500 条，进程内 | [cache.py](../../backend/app/rag/cache.py) |
| 评测 | hit@3/MRR@3 检索评测 + 规则忠实度回归 + 提示词契约回归 | [evals/](../../backend/evals/) |
| 可观测 | stage_ms 耗时打点；检索 trace 落 Message.extra；错误计数+webhook 告警 | [monitor.py](../../backend/app/core/monitor.py) |

---

## 三、差距分析（按环节）

### 3.1 数据清洗与文档解析

**现状**：PDF 用 pypdf 纯文本提取；DOCX 只遍历 `doc.paragraphs`（[ingest.py:59-78](../../backend/app/rag/ingest.py#L59-L78)）；页眉页脚仅去除"相邻页完全相同的行"（[ingest.py:147-169](../../backend/app/rag/ingest.py#L147-L169)）；上传端点仅校验"内容非空"（[knowledge.py:224-243](../../backend/app/api/knowledge.py#L224-L243)）。

| # | 差距 | 影响 | 生产级要求 |
|---|---|---|---|
| A1 | **DOCX 表格整体丢失**：`_parse_docx` 从未读取 `doc.tables`，价格表、银行名单、资料清单等表格内容全部不进知识库 | 财务代账场景核心信息恰好以表格为主，直接导致拒答/漏答转人工率升高 | 遍历 document body 元素流（段落+表格交错顺序），表格转 Markdown/行式文本 |
| A2 | **扫描版 PDF 无文本层时静默失败**：pypdf 提取为空 → 整篇空文档被 400 拒绝，但部分页有字部分空时，空页内容丢失无告警 | 用户以为入库成功，实际知识残缺 | 检测文本层占比，低于阈值时明确提示"疑似扫描件需 OCR"；可选集成 RapidOCR（本地依赖，仍是单机） |
| A3 | **无重复文档检测**：同名/同内容文档可反复上传，每次全量重建索引 | 索引膨胀；重复 chunk 干扰 RRF 融合与去重逻辑 | 标题+内容哈希查重，重复上传走"版本更新"而非新建 |
| A4 | **无清洗质检报告**：无乱码检测、超短文档告警、全半角/空白规范化、水印行清理 | 脏数据入库后排查困难 | 入库前产出清洗摘要（字符数、可疑字符比例、被裁剪行），随文档保存供后台核对 |
| A5 | **知识时效性无管理**：价格、政策类内容无有效期字段 | 过期价格继续被检索到并忠实输出（护栏只防"编"，不防"旧"） | KnowledgeDoc 增加 `effective_until`/`reviewed_at`，检索时对过期文档降权或告警 |

### 3.2 切片

**现状**：语义切片（章节标题/空行/编号清单）+ 相邻块 10-20% 重叠 + 嵌入相似度切分无结构长段（[ingest.py:199-401](../../backend/app/rag/ingest.py#L199-L401)）。`_MIN_CHUNK_LEN=10` 只有下限。

| # | 差距 | 影响 | 生产级要求 |
|---|---|---|---|
| B1 | **无最大块长上限**：无标题无空行的超长段在嵌入不可用时整块保留；即使相似度切分可用，其单"组"也可能极长 | 检索粒度差（命中即整段）；rerank 输入超长可能被 API 截断；上下文预算 800 字符靠生成端事后裁剪（[engine.py:1336](../../backend/app/agent/engine.py#L1336)），裁剪丢失的句子可能正是答案 | 设 MAX_CHUNK（建议 600-1000 字符），超限时在句子边界二次切分；rerank 候选统一截断（如前 512 字符） |
| B2 | **父子索引是死代码**：`ingest_document` 全部 `parent_id=None`（[ingest.py:473](../../backend/app/rag/ingest.py#L473)），`_resolve_parent`/`PARENT_CONTEXT_MAX=800`（[pipeline.py:633-658](../../backend/app/rag/pipeline.py#L633-L658)）永不触发；`_indexable` 的父块过滤同样空转 | 命中片段缺章节上下文时只能靠标题前缀兜底；维护者会误以为存在父子索引 | 二选一：删除死代码；或实现章节级父块（标题层级聚合），命中子块返回完整章节（≤800 字符时） |
| B3 | **切块策略无回归验证**：INDEX_VERSION 手工递增，切块改动靠肉眼 | 切片回归引入后无法察觉 | 切片单元测试覆盖典型形态（表格、多级清单、无结构长文），版本升级前后跑检索评测对比 |

### 3.3 嵌入与向量索引

**现状**：批次 32、失败重试 2 次（无退避间隔）后放弃（[embeddings.py:18-33](../../backend/app/rag/embeddings.py#L18-L33)）；失败批次"只保留关键词索引"（[ingest.py:549-578](../../backend/app/rag/ingest.py#L549-L578)）；Chroma 本地持久化。

| # | 差距 | 影响 | 生产级要求 |
|---|---|---|---|
| C1 | **嵌入失败无补偿机制**：上传时该批向量化失败 → 这些 chunk **永久**只有 BM25，直到手工 bump INDEX_VERSION | 向量召回静默缺失，检索质量劣化且不可见 | 失败批次落"待嵌入"状态，启动时/定时对账补偿（项目已有 queue worker 可承载） |
| C2 | **向量库与 DB 无一致性对账**：DB 有 chunk 而 Chroma 无向量（或反之，如 Chroma 目录损坏/误删）时无检测 | 召回率不可解释地下降；`reindex_all` 只在 INDEX_VERSION 变化时触发 | 启动对账：`SELECT count(*) FROM knowledge_chunks` vs `collection.count()`，差异超阈值自动触发重建并告警 |
| C3 | **embedding 模型/维度变更不触发重建**：INDEX_VERSION 只编码切块策略（"5"），换 `embedding_model`（如 bge-m3 → text-embedding-v3，维度不同）后旧向量继续存在 | 新旧维度混存 → 检索结果错误或报错 | INDEX_VERSION 或独立版本文件纳入 embedding 模型名，模型变更即全量重嵌入 |
| C4 | **嵌入重试无退避、无 429 识别**：立即连续重试 2 次 | 硅基流动限流时两次都失败，批量入库中断 | 指数退避（如 1s/4s），识别 429 单独处理 |

### 3.4 检索

**现状**：查询改写多路 → 每路 dense+BM25 → RRF → 分类屏蔽 → 精确 FAQ 钉住（进程内哈希 `_faq_pins`）→ 放宽重试。

| # | 差距 | 影响 | 生产级要求 |
|---|---|---|---|
| D1 | **多 worker 部署下进程内状态分裂（最高风险）**：BM25 索引（[bm25.py:28-30](../../backend/app/rag/bm25.py#L28-L30)）、FAQ 钉住缓存（[pipeline.py:174-175](../../backend/app/rag/pipeline.py#L174-L175)）、回答缓存（[cache.py:12-13](../../backend/app/rag/cache.py#L12-L13)）全部进程内。文档更新只在处理该请求的 worker 生效，其他 worker 继续用旧 BM25/旧缓存命中旧答案 | `uvicorn --workers N`（生产常规配置）下知识更新不可见、删除的文档仍被检索到、缓存失效不传播——**知识正确性事故** | 单机约束下的方案：DB 落"索引纪元"（epoch 表或 knowledge_docs.updated_at MAX），各 worker 短周期轮询对齐，检测到变化即本地重建 BM25/清缓存；或文档明确强制单 worker 并加启动断言 |
| D2 | **BM25 重建与查询共用一把锁**（[bm25.py:40-59](../../backend/app/rag/bm25.py#L40-L59)）：每次知识变更全量重建，tokenize 数万 chunk 期间所有检索请求排队 | 知识变更瞬间 P99 延迟尖刺 | 双缓冲（构建新索引后原子切换引用），重建不再持查询锁 |
| D3 | **同步入库阻塞请求路径**：上传接口 `await ingest.ingest_document()`（[knowledge.py:242](../../backend/app/api/knowledge.py#L242)），含解析+切片+嵌入+全量 BM25 重建 | 大文档上传请求超时；期间所有对话检索被 D2 的锁拖慢 | 入库走已有 task queue 异步化，上传立即返回"处理中"，后台完成后回写状态字段 |
| D4 | **阈值体系未经线上数据校准**：0.3/0.45/0.35/0.65/1.5x/0.08 六个阈值（[config.py:39-44](../../backend/app/config.py#L39-L44)）为静态默认值，无线上 passed_rate/reason 分布反馈校准 | 拒答率（转人工率）与漏答率不可控——客服场景最核心的运营指标 | 上线后按 reason 分桶统计（数据已有：Message.extra.retrieval.reason），用分布调参并沉淀到评测 |
| D5 | **查询嵌入无缓存**：同一高频问题每次重新调 embedding API | 无谓延迟+成本 | LRU 缓存 query→vector（进程内即可） |
| D6 | **jieba 无领域词典**：「ODI备案」「年审」「银行开户」等领域词依赖默认分词 | BM25 召回质量次优 | 维护领域自定义词典（一次性工作，写入 data/ 随项目走） |

### 3.5 精排

**现状**：外部 API 两路加权合并（[pipeline.py:439-465](../../backend/app/rag/pipeline.py#L439-L465)），失败退回保守分差规则。

| # | 差距 | 影响 | 生产级要求 |
|---|---|---|---|
| E1 | **精排候选全文直送 API，无长度截断** | 超长 chunk（见 B1）被 API 截断后精排只看前段，排序失真 | 送精排前统一截断（如 512 字符，保留标题） |
| E2 | **rerank 完全依赖外部 API，无本地兜底**：失败时退回的分差规则极其保守（`DENSE_SCORE_GAP=0.08` 要求第一名显著领先），实际效果是大量本可回答的问题转人工 | 外部服务抖动 → 拒答率瞬间飙升（虽有 3 次/5min 告警） | 可选本地轻量 reranker（bge-reranker-base CPU 推理，仍是单机零服务依赖）；或至少将降级规则的阈值独立成可调配置，故障时快速放宽 |
| E3 | **两路精排的加权平均未验证收益**：0.5 权重的改写句可能引入噪声 | 复杂度上升、收益未知 | 用离线评测（run_retrieval_eval）做消融对比后再保留 |

### 3.6 生成

**现状**：提示词热更新 + JSON 契约 + 草稿预取 + evidence 数字核对 + faithfulness 双门槛 + reflect 反思。这部分是全链路最强的一环。

| # | 差距 | 影响 | 生产级要求 |
|---|---|---|---|
| F1 | **回答语义缓存不感知提示词变更**：`cache.py` 的 version 只绑 `INDEX_VERSION`（切块版本），提示词文件热更新（mtime 变化，[prompt.py:16-24](../../backend/app/agent/prompt.py#L16-L24)）后，旧风格缓存回答继续命中发出 | 运营改了话术/规则，线上仍回复旧话术，且看似"热更新生效了" | 缓存条目 version 纳入 prompt 文件 mtime（或哈希），变更即失效 |
| F2 | **单条回复最多 5 次串行 LLM 调用**（rewrite → judge → draft → fallback → reflect），REPLY_BUDGET=45 秒（[engine.py:35](../../backend/app/agent/engine.py#L35)） | 社交私信场景用户等待体验差；预算被低价值环节吃掉（rewrite/broaden 可与首路检索并行） | 调用图审计：rewrite 与无改写首路检索并行化；judge 与 draft 已预取并行（好设计）；统计线上 P50/P95 决定裁剪哪一环 |
| F3 | **faithfulness 是 n-gram 重叠近似**（[confidence.py:138-200](../../backend/app/agent/confidence.py#L138-L200)）：4 字窗口重叠即认定"有依据" | 双向出错：同义改写的正确句被误杀（白白转人工）；表面重叠的幻觉被放过 | 保留代码版做实时门槛（成本低），周期性抽样用 LLM judge 离线校准近似误差，误差大再升级 |
| F4 | **知识库内容直接拼入提示词，无注入防护** | 恶意/不当文档（或被污染的对话历史）可注入指令 | 文档入库时清洗"指令样式"行；生成侧已有契约校验兜底，风险中低 |
| F5 | **无引用溯源展示给客服侧**（citations 存 extra 但用户端呈现未知） | 人工审核 AI 回复时无法快速核对出处 | 客服工作台展示命中资料卡片（数据已具备） |

### 3.7 评测与质量闭环

**现状**：三套评测（检索 hit@3/MRR@3、规则忠实度、提示词契约）+ missed_questions 表（含 suggested_answer 字段，已有闭环雏形）。

| # | 差距 | 影响 | 生产级要求 |
|---|---|---|---|
| G1 | **评测全部手动触发，无 CI 门禁**：阈值/切片/提示词改动后回归靠自觉 | 优化随时可能引入退化 | pytest 已有大量 RAG 测试（test_rag.py 1300+ 行），把 evals 脚本接入 pre-commit/CI（检索评测可加 `--smoke` 快速模式） |
| G2 | **检索评测的期望标注靠字符串精确/包含匹配**（[run_retrieval_eval.py:63-80](../../backend/evals/run_retrieval_eval.py#L63-L80)），多义跳过、标签噪声大 | hit@3 数字不可信，阈值调参依据不可靠 | 一次性人工（或 LLM 辅助）审校金标集，存为静态 fixture；评测切换到金标集 |
| G3 | **无端到端 RAGAS 式评测**：检索和生成各自评，无"最终答案正确率" | 各环节分数与业务结果脱节 | 抽样对话 + 人工标注终判，月度跑批 |
| G4 | **missed_questions 有表无流程**：转人工问题已记录，但缺"高频 missed → 转化 FAQ 入库"的运营闭环 | 知识库只靠人主动维护，召回率改进无抓手 | 建议周期（如每周）导出 missed 聚类报告，Top 问题补录 FAQ（表结构已支持 suggested_answer） |

### 3.8 可观测性与运维

**现状**：stage_ms 打点、检索 trace 落 Message.extra、错误计数告警（含 embedding/rerank failure）。SQLite 已开 WAL + busy_timeout（[database.py:20-28](../../backend/app/database.py#L20-L28)）。

| # | 差距 | 影响 | 生产级要求 |
|---|---|---|---|
| H1 | **无聚合指标**：耗时只在日志行、trace 只在消息 JSON 里，无 P50/P95、无 reason 分布、无 passed_rate/cache_hit 率趋势 | 问题靠 grep 日志发现，运营指标盲飞 | 轻量方案：内存滑动窗口聚合（复用 monitor 模式）+ /api/health/detail 已有快照入口，或启动时定期把 Message.extra.retrieval 聚合落一张统计表 |
| H2 | **告警缺质量类指标**：只有故障告警，无"拒答率突增/缓存命中率骤降/检索 passed 率异常" | 质量劣化静默发生 | 基于 H1 的聚合加阈值告警（现有 webhook 通道复用） |
| H3 | **备份一致性未覆盖 Chroma**：backup 模块备份 DB，chroma 目录与 DB 快照可能不一致 | 恢复后 C2 的对账问题被放大 | 备份时一同快照 chroma 目录，恢复后触发一次对账/重建 |

---

## 四、分级路线图

### P0 —— 正确性/一致性事故级（不修会出知识错误）

| 项 | 内容 | 方案要点（单机约束内） |
|---|---|---|
| P0-1 | 多 worker 状态分裂（D1） | DB 索引纪元 + worker 轮询对齐；短期先在部署文档强制 `--workers 1` 并加启动告警 |
| P0-2 | DOCX 表格丢失（A1） | `_parse_docx` 按 body 元素流遍历段落+表格，表格转文本行 |
| P0-3 | 嵌入失败无补偿 + 无对账（C1/C2） | 待嵌入补偿任务（复用现有 queue worker）+ 启动对账 |
| P0-4 | 缓存不感知提示词变更（F1） | 缓存 version 纳入 prompt mtime |
| P0-5 | 同步入库阻塞（D3，含 D2 锁阻塞） | 入库异步化到任务队列 + BM25 双缓冲切换 |

### P1 —— 质量/延迟显著改进

| 项 | 内容 |
|---|---|
| P1-1 | 扫描 PDF 检测与告警（A2），可选 OCR |
| P1-2 | 切片最大长度上限 + rerank 候选截断（B1/E1） |
| P1-3 | 父子索引：实现或删除（B2） |
| P1-4 | LLM 调用链延迟优化 + 线上延迟分位数（F2/H1） |
| P1-5 | 阈值线上校准：reason 分布统计先行（D4/H1） |
| P1-6 | 重复文档检测（A3） |
| P1-7 | 评测接 CI + 金标集审校（G1/G2） |
| P1-8 | jieba 领域词典（D6） |

### P2 —— 长期完善

| 项 | 内容 |
|---|---|
| P2-1 | 知识时效性字段与过期策略（A5） |
| P2-2 | 嵌入重试退避 + 429 处理（C4） |
| P2-3 | 查询嵌入缓存（D5） |
| P2-4 | faithfulness 的 LLM judge 离线校准（F3） |
| P2-5 | 本地轻量 reranker 兜底或降级阈值可调（E2/E3） |
| P2-6 | 提示词注入清洗（F4）、客服侧引用展示（F5） |
| P2-7 | missed_questions 运营闭环（G4）、质量类告警（H2）、备份覆盖 Chroma（H3） |
| P2-8 | 清洗质检报告（A4）、切片回归测试（B3）、模型变更触发重建（C3）、死代码清理 |

---

## 五、验证方式（路线图执行时）

1. **P0 项逐项验证**：多 worker 用 `uvicorn --workers 2` 起服务，更新一篇文档后双 worker 检索一致；DOCX 含表格样例上传后 chunks 可见表格内容；手工制造嵌入失败（错 key）后启动对账自动补齐；改提示词 mtime 后旧缓存不再命中；上传大文档接口立即返回、后台完成后可检索。
2. **回归**：`pytest`（backend/tests，重点 test_rag.py）+ `python -m evals.run_retrieval_eval --limit 100` 前后对比 hit@3/MRR@3 不退化。
3. **延迟**：压测典型问答 50 次，P95 与 stage_ms 分布对比。

## 六、执行说明

用户已确认本次交付为**分析报告 + 分级路线图，不改代码**。计划批准后，将把本报告结论整理为最终分析响应呈现；后续可按 P0 清单逐项立项实施。
