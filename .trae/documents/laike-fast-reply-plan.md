# 抖音来客 Worker「最新消息秒读 + 整段粘贴秒发」优化计划

> 需求：1) 用户最新消息快速读入库触发回复；2) 用真实页面（已打开，两个用户来回切换）校准；3) 盘点来客通道不完善处并修到「有新消息立刻读、立刻回」；4) 输入框逐字输入改为整段 AI 回复直接填入 + 点击发送。
> 范围（用户已确认）：Worker + 后端都改；可短暂停掉 Worker 跑真实 DOM 检查脚本。

---

## 一、现状链路与延迟分解（回答需求 1）

用户消息 → AI 回复送达的完整链路（文件均为真实引用）：

| 阶段 | 现状 | 耗时估算 |
|---|---|---|
| 1. Worker 入站扫描 | [base_worker.py:683-697](../../workers/base_worker.py#L683-L697) 每 `POLL_INTERVAL=2s` 一轮 → [douyin_laike_worker.py:230-253](../../workers/douyin_laike_worker.py#L230-L253) `_read_workspace` **每轮都点击**顶部 3 个会话 + 所有未读会话，切换面板后读全部气泡 | 2s + 每会话 ~0.3-2.3s（点击 300ms + 面板名等待最多 2s + 全量气泡遍历） |
| 2. 上报入库 | POST `/api/rpa/incoming` → `enqueue("inbound_message")` fast 车道（[rpa.py:88-110](../../backend/app/api/rpa.py#L88-L110)、[queue.py:31](../../backend/app/core/queue.py#L31)） | <1s |
| 3. AI 生成 | `handle_inbound` → [engine.py:321](../../backend/app/agent/engine.py#L321) `process_ai_reply`：快路径已有（问候/留资/精确 FAQ/语义缓存直发）；慢路径 = rewrite（有历史或问价格时 1 次 LLM）→ 检索 → judge LLM → draft LLM（与 judge 并行预取）→ 证据核对（代码）→ reflect（有条件） | 典型 10-25s，预算上限 45s |
| 4. Worker 出站拉取 | [base_worker.py:645-658](../../workers/base_worker.py#L645-L658) 每 2s 拉 outbox | ≤2s |
| 5 | 页面发送 | [douyin_laike_worker.py:329-352](../../workers/douyin_laike_worker.py#L329-L352) `_send_workspace`：点开会话 → **逐字输入**（`type_humanlike` 50-130ms/字符）→ Enter | 200 字回复要 10-26s |
**结论**：Worker 两端（读+发）合计可优化 5~30s/条；后端生成是大头，其中 **judge 是唯一可安全跳过的 LLM 调用**（rewrite 已有跳过条件 [rewrite.py:57-65](../../backend/app/rag/rewrite.py#L57-L65)，draft 已与 judge 并行）。

---

## 一之二、多用户同时发消息（突发并发）全链路推演

以「5 个用户在同几秒内各发 1 条消息」为例，逐环节核对现有保护与瓶颈：

| 环节 | 现状 | 突发下的表现 |
|---|---|---|
| Worker 读消息 | 单页面只能串行点开各会话（页面锁后仍串行） | 列表按最新活跃排序，按序点开 5 个未读会话 ≈ 5×1s（优化后）≈ 5-7s 读完全部；**第 5 个用户的消息最迟 ~7s 入库**。这是单页 RPA 的物理下限，无法并行 |
| 上报入库 | 每读完一个会话立即上报（`_report_one` 逐条 HTTP） | 5 会话互不影响，随读随入 |
| 后端生成 | **fast 车道并发默认 20**（[queue.py:7-9](../../backend/app/core/queue.py#L7-L9) `QUEUE_FAST_CONCURRENCY=20`，CAS 认领防双取）+ **同会话 convlock 串行**（[services.py:170-176](../../backend/app/services.py#L170-L176)） | 5 个**不同**会话的生成完全并行，各自 10-15s，不排队 |
| 同一用户连发多条 | 补答收敛机制：`_unanswered_user_message_ids`（[engine.py:279-313](../../backend/app/agent/engine.py#L279-L313) 显式配对 + 位置配对）按序逐条补答 | 用户连问 3 个问题 → 3 次串行生成 3 条回复（会话锁内），每条 10-15s，共 30-45s 排队 |
| 首次打开老会话（大量历史气泡） | 历史气泡里的客服回复天然构成「位置配对」，只有末尾未被回复的用户消息才算未答 | 不会触发 N 条回复，只答末尾未答的；安全 |
| 平台频控 | 后端 `douyin_laike_rpa` 规则（保守对齐企业号：48h 内 6 条 + 每分钟 ≤6 条，未经真账号核实） | 突发多气泡回复（1 轮最多 3 气泡）会触顶限流，回复被延迟窗口释放；**真机校准此规则是落地必做项** |
| Worker 发消息 | 页面锁串行：点会话→校验面板→粘贴→点发送→确认 ≈ 1-2s/条 | 5 条回复 5-10s 内发完，频控允许时即时 |
| 后端宕机/上报失败 | 上报失败不 mark_seen，下一轮重试；outbox 租约过期回收（`_reap_expired_leases`） | 消息不丢 |

**突发场景的额外发现（本计划补齐）**：
- **W9 自发送回读抑制**：worker 发出的回复气泡会被下一轮扫描以 `sender_side=agent` 旁路上报，与 outbox 落库的那条形成**双份历史**（污染会话上下文与 RAG 历史提示词）。worker 内存维护「最近发送内容指纹（TTL 5 分钟）」，读面板时跳过匹配的 agent 气泡；人工旁路消息不受影响。
- **W10 升级弹窗周期重查**：`dismiss_blockers` 目前每页只查一次（[douyin_laike_worker.py:135-137](../../workers/douyin_laike_worker.py#L135-L137)），长会话中途弹窗会挡住点击导致读写连环失败。改为每 5 分钟重查一次。

---

## 二、差距清单（回答需求 3）

### Worker 侧（douyin_laike_worker.py）

| # | 问题 | 位置 | 影响 |
|---|---|---|---|
| W1 | **空闲也每轮点击前 3 会话**（SCAN_RECURRENT 扫描旁路消息），两个用户面板每 2 秒来回切换 | `_read_workspace` L236-253 | 面板狂闪（用户看到的现象）；每轮多花 1-5s；与人工操作互相干扰 |
| W2 | **入站/出站协程同页无锁并发**：出站粘贴输入到一半，入站协程点击别的会话 → 文本进错会话或发送失败 | base_worker L573-577 gather 两循环；LaikeDriver 无锁 | 偶发把回复发错人（正确性事故） |
| W3 | **逐字输入**：`type_humanlike`（L346）50-130ms/字符 | `_send_workspace` | 每条回复 10-26s 纯打字时间（需求 4） |
| W4 | **发送只按昵称 `filter(has_text)` 定位且不校验面板**：重名昵称点错行时无兜底 | L334-337 | 回复发错用户 |
| W5 | **msg_key 是纯内容哈希**：同会话同侧发两条一模一样的消息（用户连发两个"在吗"）第二条被去重丢弃 | `_read_workspace_messages` L287-288 | 漏读用户消息 → 不回复 |
| W6 | **图片/语音消息被跳过**：真实面板路径只读文本节点，图片气泡 `continue` 掉 | L281-283 | 用户发图片 → 系统无感知，不回复 |
| W7 | conv_key=昵称：昵称改了/重名时读发两侧都错位 | L289-291 | 会话身份脆弱（真实页若有稳定 ID 可换） |
| W8 | 未读红点选择器 `[class*='unread']` 大小写双写猜测，未经真实 DOM 校验 | SELECTORS L62 | 红点漏检 → 新消息不打开 |
| W9 | **自发送回读造成双份历史**：worker 发出的回复气泡下一轮被当旁路消息上报，与 outbox 落库的回复重复入库 | `_read_workspace_messages` 无差别读 agent 气泡 | 会话上下文/RAG 历史提示词被重复回复污染 |
| W10 | **升级弹窗只查一次**：`dismiss_blockers` 每页仅首查，长会话中途弹窗挡住点击 → 读写连环失败 | L135-137 | 跑几小时后突然集体超时 |

### 后端侧

| # | 问题 | 影响 |
|---|---|---|
| B1 | judge（相关性判定 LLM）在检索通过时必跑，即使 rerank 分数极高（如 0.95 的精确命中）也要花 3-8s | 高置信命中也慢 |
| B2 | `rerank_top_score` 已在 RetrievalResult（[pipeline.py:32](../../backend/app/rag/pipeline.py#L32)）但引擎未利用它做快路径 | 浪费现成信号 |

---

## 三、实施方案

### 阶段 0：真实页面 DOM 检查（需求 2，先行）

1. 协调用户停掉正在运行的 laike Worker（释放 profile 锁）。
2. 临时脚本 `workers/_inspect_laike.py`（用后删除）：复用 `browser_session.open_session` + 同一 profile（非 headless）→ 打开 `LAIKE_URL` → 关升级弹窗 → 转储到 `workers/downloads/debug/laike_inspect/`：
   - 会话列表容器前 3 个 item 的 outerHTML（找稳定会话标识、未读红点、最后一条消息预览文本节点）
   - 点开第一个会话后面板名节点、前 3 个消息气泡 HTML（找消息 ID/时间戳属性、图片/语音节点、左右侧标识）
   - 回复输入框 + 工具栏 HTML（找发送按钮）
3. 依据转储结果确定并回填 SELECTORS：`workspace_conv_id`（若有 data-* 稳定标识）、`workspace_preview`（列表预览文本）、`workspace_send_button`、修正 `workspace_unread`、图片/语音节点选择器。
4. 若真实页无稳定会话 ID：conv_key 维持昵称，但配合 W4 的面板校验兜底。

### 阶段 1：Worker 改造（douyin_laike_worker.py）

1. **W1 列表驱动的快读**：重写 `_read_workspace`：
   - 每轮先**不点击**读列表元数据（昵称/未读红点/预览文本）；
   - 实例内存维护 `self._conv_state: dict[conv_key, preview_hash]`；
   - 只有点击：有未读红点 ∨ 预览变化（旁路捕获）∨ 首次见到；预览节点不存在时回退旧逻辑；
   - SCAN_RECENT 兜底扫描降频：每 5 轮（约 10s）才扫顶部会话一次，防预览节点缺失时漏旁路。
   - 效果：无新消息时零点击、面板不再来回切换；有新消息时 ≤1 个轮询周期（2s）内点开读取。
2. **W2 页面操作锁**：LaikeDriver 惰性创建 `asyncio.Lock`（首次异步调用时建，避免进程启动早期绑 loop 问题）；`read_incoming` 与 `send_message` 全程持锁，串行化同页操作。
3. **W3+W4 整段粘贴 + 发送校验**：`_send_workspace` 改为：点行 → `_workspace_panel_ready` 校验面板昵称匹配（复用现有 L255-268）→ `fill(content)` 整段填入（真实框是 textarea，fill 触发 input 事件；若 DOM 检查发现是 contenteditable 则改用 evaluate 设文本 + 派发 input）→ 点击 `workspace_send_button`（阶段 0 确定；找不到按钮回退 Enter）→ 保留现有"右侧气泡 +1"发送确认（L342-352）。
4. **W5 msg_key 防重**：指纹加入前一条气泡内容：`f"{nickname}|{sender_side}|{prev_content}|{content}"`，相同内容相邻两条消息 key 不同；真实气泡若有稳定 ID 属性则优先用 ID（阶段 0 决定）。
5. **W6 图片/语音**：`_read_workspace_messages` 按阶段 0 发现的节点结构补图片/语音分支（参照 fixture 路径 L206-211 的写法），上报 `msg_type=image/voice` + media_url，走 base_worker 现成媒体上传链路。
6. **W9 自发送回读抑制**：`_send_workspace` 发送成功后，把内容指纹（`(conv_key, content)` 的 md5）存入实例内存 `self._sent_fingerprints`（TTL 5 分钟，条数上限 200）；`_read_workspace_messages` 读到 agent 气泡先查指纹命中则跳过上报。
7. **W10 弹窗周期重查**：`_upgrade_modal_page` 判断改为「同页且距上次检查 < 5 分钟才跳过」，长时间运行期间每 5 分钟重查一次弹窗。
8. `.env.local` 建议 `POLL_INTERVAL=1`（扫描已变轻，读发两端各省 1s；执行时与用户确认改不改）。

### 阶段 2：后端 judge 快路径（engine.py + config.py）

1. `config.py` 新增 `rag_judge_skip_score: float = 0.9`（0 表示关闭）。
2. `engine.py` L479 `if retrieval.passed:` 分支：满足 `retrieval.rerank_top_score >= settings.rag_judge_skip_score > 0` 且 `len(retrieval.contexts) <= 2` 且非 exact_faq 时，**不调 `_select_for_answer`**，合成同名结构 confidence_meta：`action="use"`、`contexts=retrieval.contexts`、`verdicts=每条 context 一个 use 判定`、`raw_precision=1.0`（以 `_select_for_answer` 实际返回契约为准，实现时先读该函数核对键名）。
3. 后续链路不变：draft 已预取（L470-478），judge 跳过后立刻取 draft；faithfulness 门槛（代码实现，L661-685）照跑，防幻觉护栏不减；gate_payload 非 None → reflect 自动不触发（L690 条件）。
4. 效果：高置信命中时生成耗时 ≈ rewrite(0，首句无指代时) + 检索 1-2s + draft 5-10s ≈ **8-12s**（原 15-25s）。

### 阶段 3：测试与真机验证

1. **Worker 单测**（workers/tests/test_laike_workspace.py 扩展，沿用现有 Node/Locator 假页）：
   - 无未读且预览未变 → 不点击；
   - 预览变化 → 打开并读取（旁路捕获）；
   - 首次见到 → 打开一次；
   - 相同内容相邻两条 → 生成不同 msg_key；
   - 发送前面板校验失败 → 抛错（不粘贴）；
   - 发送成功后的内容指纹 → 下一轮读到相同 agent 气泡不上报（W9）。
   - 现有测试 `test_unread_outside_top_is_opened_and_stuck_panel_is_skipped` 应继续通过。
2. **后端单测**（backend/tests/test_rag.py 或 engine 相关测试文件加用例）：分数达阈值 → 不调 judge LLM（mock 断言零调用）、faithfulness 门槛照跑；分数低于阈值 → judge 正常调用。
3. **真机验证**（用户重启 Worker，两个测试用户互发）：
   - **单用户常规**：发消息后 ≤3s 日志出现"已上报消息"；空闲时面板不再来回切换；回复整段瞬间出现并点发送按钮发出（无逐字过程）。
   - **多用户突发**：两个测试用户在 10 秒内各发 2-3 条消息 → 验证：所有消息全部入库不漏（msg_key 防重）、每个未答问题都有回复（补答收敛）、回复没有发错人（面板校验）、频控日志无异常刷屏；记录第 N 个用户从发消息到收到回复的实测耗时。
   - **同用户连问**：一个用户快速连问 3 个问题 → 3 条回复按序生成发出，会话锁内不乱序。
   - **自发送回读**：AI 回复发出后，检查后端 Message 表无重复 agent 消息（W9 生效）。
   - backend 日志 stage_ms：judge_ms≈0 的快路径命中率。

### 阶段 4：生产落地 RPA 完备性清单（回答需求「完全落地」的遗漏点）

已有能力（验证即可，不新开发）：
- **崩溃自愈**：run_fleet.py supervisor 拉起/退避（[run_fleet.py](../../workers/run_fleet.py)，测试已覆盖 crash backoff）；单进程部署需配外部守护（计划任务/nssm）——执行时与用户确认部署方式。
- **登录过期闭环**：`check_status` → login_expired → 暂停不崩溃；后端 sweeper 判 offline 告警（doctor.py 注释确认）；恢复走 `login.py`。真机验证项：手动退出登录看告警链路。
- **消息不丢**：上报失败不 mark_seen 重试；queue 任务崩溃重启自动恢复 pending/processing（[queue.py:12](../../backend/app/core/queue.py#L12)）；outbox 租约回收。
- **发送失败处置**：ack failed → workbox「失败回复」页 + 人工重发/丢弃（rpa.py `/outbox/failed`、`/retry`、`/discard` 已有）。
- **选择器漂移**：selector_mismatch 自动 dump 页面（base_worker `_dump_page`）+ doctor.py 第 6 项；上线前 doctor 全绿、每日定时 doctor 巡检（可接开机脚本/CI）。

本次补齐（前文已列）：页面操作锁（W2）、面板校验（W4）、弹窗周期重查（W10）、双份历史抑制（W9）。

真机校准项（代码外，落地必做）：
- **来客频控规则核实**：48h/6 条 + 每分钟 6 条为保守占位，冒烟期观察真实平台限制并调后端 `douyin_laike_rpa` 规则参数。
- **关闭来客自带智能客服**：`bot_enabled_flag` 检查在真实页无效（fixture 契约），必须在来客后台人工关闭，冒烟时确认无双份回复。
- **headless 验证**：选择器目前基于非无头页面采集；生产若切 `HEADLESS=new` 需复跑一轮真机验证。
- **长跑观察**：连续运行 ≥24h，确认无内存泄漏、弹窗重查生效、会话列表虚拟化（长会话气泡懒加载）下读取仍稳定。

---

## 四、改动文件清单

| 文件 | 改动 |
|---|---|
| [workers/douyin_laike_worker.py](../../workers/douyin_laike_worker.py) | 阶段 1 全部（SELECTORS 校准 + 快读 + 锁 + 粘贴发送 + msg_key + 图片/语音） |
| [backend/app/config.py](../../backend/app/config.py) | 新增 `rag_judge_skip_score` |
| [backend/app/agent/engine.py](../../backend/app/agent/engine.py) | judge 快路径（L479 分支） |
| [workers/tests/test_laike_workspace.py](../../workers/tests/test_laike_workspace.py) | 快读/msg_key/发送校验新用例 |
| [backend/tests/test_rag.py](../../backend/tests/test_rag.py)（或 engine 测试文件） | judge 跳过用例 |
| workers/_inspect_laike.py（临时） | 阶段 0 检查脚本，用后删除 |

不动：base_worker.py 共享逻辑（锁在 LaikeDriver 内实现）、fixture 路径（`_read_fixture`/fixture 发送流程保持测试契约）、rewrite 逻辑（已有跳过条件）、其他平台 worker。

## 五、风险与回退

- **多用户突发的物理下限**：单页 RPA 读/发均串行，5 用户同时来消息时第 5 个最迟 ~7s 入库、回复在频控内 5-10s 发完；若未来并发量级更大（>10 个活跃会话同时来消息），需要多 worker 多账号分片（fleet 已支持多绑定），单机单账号无需提前设计。
- **粘贴风控**：平台对瞬间填入+发送一般无特殊限制（人类也常粘贴）；若真机验证发现输入被清空/发送无效，回退为 `press_sequentially(delay=0)`（速度仍快一个量级）。
- **同会话补答排队**：用户连问 3 条 → 3 次串行生成（会话锁），第 3 条回复最迟 ~45s 后发出；这是设计取舍（每问必答优于只答最新），若用户希望「合并回答最新一条」可作为后续优化项。
- **judge 跳过质量风险**：阈值 0.9 + 仅 ≤2 个 context，faithfulness/证据核对护栏全保留；`rag_judge_skip_score=0` 一键关闭回退。
- **选择器漂移**：阶段 0 基于当日真实 DOM；页面改版时 doctor.py 第 6 项（关键选择器在位）会拦截。
- **预览节点缺失**：降频 SCAN_RECENT 兜底扫描仍在（每 10s），最坏情况旁路消息延迟 10s 上报，不影响用户消息（有红点必开）。
- **W9 指纹误伤**：人工客服在后台发的消息恰好与 AI 5 分钟内发的完全相同会被跳过一次上报——概率极低，且内容已在库；条数上限 200 + TTL 5min 控制误差面。
