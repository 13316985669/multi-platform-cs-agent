# 抖音来客 RPA「回复用户消息」完整生产落地方案

> 目标：把「抖音来客商家后台会话中心」的私信，通过 RPA Worker 桥接进本系统，实现「用户消息秒读入库 → AI 生成回复 → Worker 整段粘贴秒发」的全自动闭环，并跑成可长期值守、可监控、可处置的生产级通道。
> 现状：代码层的「秒读秒回」改造已全部落地并通过测试（见本文「现状分析」）；本方案聚焦**把它真正部署上线**所需的全部步骤、配置、运维与风险管控。

---

## 一、现状分析（已就绪的部分）

### 1.1 代码改造已完成（无需再开发）

| 模块 | 已完成内容 | 位置 |
|---|---|---|
| Worker 读消息 | 预览驱动快读（空闲零点击、不来回切面板）、数据气泡 ID 作 msg_key、图片识别、自发送回读抑制、升级弹窗周期重查 | [douyin_laike_worker.py](../../workers/douyin_laike_worker.py#L305-L429) |
| Worker 发消息 | 整段 `fill` + Enter（不再逐字输入）、面板头像校验（防发错人）、页面操作锁（入站/出站串行） | [douyin_laike_worker.py](../../workers/douyin_laike_worker.py#L431-L515) |
| 后端 judge 快路径 | `rag_judge_skip_score=0.9` 高置信命中跳过相关性判定 LLM | [config.py](../../backend/app/config.py#L45-L47) |
| 频控规则 | `douyin_laike_rpa`（48h/6 条 + 每分钟 ≤6 条，保守占位） | [ratelimit.py](../../backend/app/core/ratelimit.py#L45-L48) |
| 账号驱动注册 | `douyin_laike` driver（platform=douyin，duty=dm） | [bindings.py](../../backend/app/bindings.py#L14-L17) |
| RPA 桥接协议 | incoming/outbox/ack/heartbeat/media/identity 全套接口 | [rpa.py](../../backend/app/api/rpa.py) |
| 队列与并发 | fast 车道并发 20、CAS 认领、会话锁串行、崩溃自恢复 | [queue.py](../../backend/app/core/queue.py) |

### 1.2 真实 DOM 已校准（2026-10-22 CDP 只读转储）

关键结论已回填到 worker 顶部注释与 `SELECTORS`（[douyin_laike_worker.py 第 21-40 行](../../workers/douyin_laike_worker.py#L21-L40)）：
- 消息气泡 `[data-message-watermark]` 携带平台消息 ID（`msg769...`），直接作 msg_key 幂等。
- 会话行 `lastMsgWrap` 是最后消息预览，作「哪些会话需要点开」的主信号。
- 文本用 `.csUI-Text`（避开「已读/未读」角标与三方尾注）。
- 回复框 `textarea[placeholder*='回复内容']` maxlength=500，无发送按钮，官方交互是 **Enter**。
- 面板身份靠「头像 URL 一致」兜底重名昵称。

### 1.3 待校准项（代码外，落地必做，见第六节）

- 来客官方回复窗口/频控规则（当前是保守占位值）。
- 来客后台「美洽」等三方自动回复是否已关闭（实测发现商家侧接入美洽在自动回）。
- 生产 headless 模式下的完整复测。
- ≥24h 长跑观察（内存/弹窗重查/会话列表虚拟化懒加载）。

---

## 二、架构与数据流（落地后的运行时全貌）

```mermaid
flowchart LR
    U[来客商家私信用户] -->|在抖音发消息| LC[life.douyin.com 会话中心]
    LC <-->|Playwright 轮询/点击| W[douyin_laike_worker.py<br/>preview 驱动读 + fill/Enter 发]
    W -->|POST /api/rpa/incoming| B[后端 rpa.py]
    B --> Q[queue fast 车道 → handle_inbound]
    Q --> ENG[Agent 引擎：rewrite→检索→(judge 快路径)→draft→护栏]
    ENG -->|RPA 模式写 outbox| OB[(rpa_outbox)]
    W -->|GET /api/rpa/outbox 每 2s 拉取| OB
    W -->|POST /api/rpa/ack| B
    W -->|POST /api/rpa/heartbeat 每 10s| B
```

- 平台侧只做**被动回复**，不做主动触达。
- 会话/统计/通道路由与抖店、企业号完全一致，仅频控规则与 Worker driver 不同。
- 消息不丢的三重保障：上报失败不 mark_seen 重试；queue 崩溃恢复 pending/processing；outbox 租约 60s 过期回滚。

---

## 三、落地前置条件（不满足不上线）

| # | 条件 | 说明 | 放行标准 |
|---|---|---|---|
| 1 | 来客商家账号 | 营业执照开通抖音来客，侧栏有「私信管理 → 会话中心」 | 能打开 `https://life.douyin.com/p/liteapp/leads_cs/chat/session` |
| 2 | **关闭来客自带自动回复 / 智能客服** | 否则用户收到双份回复（抢答） | 后台确认关闭 |
| 3 | **关闭「美洽」等三方客服应用自动回复**（2026-10-22 实测发现） | 包装成三方应用的自动回，`bot_enabled_flag` 在真实页无效，只能后台人工关 | 冒烟无第二份回复 |
| 4 | 后端与 Worker 网络互通（Worker 主动 pull，本机不开端口） | Pull 模式，NAT/防火墙无感 | Worker 能 `GET /api/health` |
| 5 | 已知晓合规红线 | RPA 违反平台用户协议，有封号风险，仅限自有账号低频拟人化 | 合规负责人确认 |

---

## 四、后端落地配置

在 [backend/.env](../../backend/.env.example) 补/改：

```ini
# RPA 通道开关与密钥（与 Worker 完全一致）
RPA_API_KEY=<python -c "import secrets;print(secrets.token_hex(16))">

# 抖音走 RPA 降级通道
DOUYIN_CHANNEL=rpa

# 抖音形态默认 = 来客（本地生活）——这是来客频控规则生效的关键
DOUYIN_ACCOUNT_TYPE=laike

# 生产必填基线
APP_ENV=production            # 不重置管理员密码
MOCK_ENABLED=false            # 关 Mock 注入
SECRET_KEY=<随机串>

# 建议
ALERT_WEBHOOK_URL=<钉钉/企微机器人>      # 掉线/连发失败告警
LLM_FALLBACK_*=<备用模型>                # 主 LLM 故障热备
```

要点：
- `DOUYIN_ACCOUNT_TYPE=laike` 是让 `douyin_laike_rpa` 频控规则生效的必要条件（账号页单独指定形态时以账号为准，不指定用此全局默认）。
- 若同一套后端还要跑其它抖音形态（抖店/企业号），**不要**把全局默认设成 laike，应到「账号」页逐账号指定形态。

---

## 五、Worker 落地部署

### 5.1 部署形态（二选一，按账号数量定）

| 形态 | 适用 | 启动方式 | 说明 |
|---|---|---|---|
| A. 单账号本地直跑 | 只有 1 个来客账号 | `python douyin_laike_worker.py` | `BROWSER_PROVIDER=local`，登录态存 `profiles/<account>/` |
| B. AdsPower 指纹浏览器 + fleet 调度 | 多账号/生产 | `.\run_fleet.ps1` | 「账号」页按绑定拉起 `run_assigned.py`，靠 `run_fleet.py` 对账/退避/拉活 |

> 决策：单账号走 A 足够；出现多账号同机、要隔离浏览器指纹、要做崩溃自愈时切 B。B 的崩溃自愈由 `run_fleet.py` supervisor 退避拉起（已测试覆盖），A 需额外接 Windows 计划任务/nssm 保活。

### 5.2 安装与登录（形态 A 主流程）

```powershell
cd workers
.\setup.ps1                    # venv + 依赖 + Chromium + 生成 .env.local 与随机 key
```

编辑 [workers/.env.local](../../workers/.env.example) 关键项：

```ini
BACKEND_URL=http://<后端地址>:8000
RPA_API_KEY=<与后端一致的密钥>
ACCOUNT=<来客店铺标识，如 my-laike-shop>
PLATFORM=douyin_laike
BROWSER_PROVIDER=local
HEADLESS=true                 # 生产 true；首次扫码登录时临时 false
POLL_INTERVAL=2
HEARTBEAT_INTERVAL=10
```

```powershell
# 首次登录引导（有头扫码，登录态落盘；识别为 online 即成功）
HEADLESS=false python login.py --platform douyin_laike

# 八项自检，全绿才能上线（选择器/登录/RPA key/后端连通等）
python doctor.py

# 正式启动
python douyin_laike_worker.py
```

### 5.3 选择器漂移自保

- 页面改版 → doctor「关键选择器在位」报 ✘ → 对照 `downloads/debug/` 最新 HTML 快照，只改 [douyin_laike_worker.py](../../workers/douyin_laike_worker.py) 顶部 `SELECTORS`，再跑 doctor + 冒烟。
- `HEADLESS=false` 打开同 profile 可看真实 DOM，不要凭猜改正式页结构。

---

## 六、真机校准与冒烟验收（落地最后一道关）

按顺序执行，全部通过才算「能长期值守」：

| # | 项目 | 步骤 | 通过标准 |
|---|---|---|---|
| 1 | 单用户常规收发 | 测试号发一条文字 | ≤3s 日志出现「已上报消息」；回复整段瞬间发出（无逐字）；平台侧可见 |
| 2 | 多用户突发 | 两个测试号 10s 内各发 2-3 条 | 全量入库不漏、每个未答问题都有回复、回复没发错人、频控日志正常 |
| 3 | 同用户连问 | 一用户快速连问 3 个问题 | 3 条回复按序生成发出，会话锁内不乱序 |
| 4 | 自发送去重 | AI 回复发出后查 `Message` 表 | 无重复 agent 消息（W9 生效） |
| 5 | judge 快路径 | 观察 backend `stage_ms` | 高置信命中 `judge_ms≈0` 命中率符合预期 |
| 6 | **来客频控校准** | 冒烟期持续观察 48h/6 + 分钟6 是否触顶或过松 | 调 `douyin_laike_rpa` 参数；确认无平台限流告警 |
| 7 | **美洽/双份回复确认** | 后台确认三方应用关闭 | 用户只收一份回复 |
| 8 | headless 复测 | `HEADLESS=true` 复跑一轮收发 | 选择器与交互在无头模式下仍稳定 |
| 9 | login_expired 告警链路 | 手动退出平台登录 | ≤90s 设置页卡片红 `login_expired` + 告警 |
| 10 | 长跑观察 | 连续运行 ≥24h | 无内存泄漏、弹窗重查生效、虚拟化长会话读取稳定 |

---

## 七、日常运维 Runbook

| 现象/状态 | 含义 | 处置 |
|---|---|---|
| 设置页卡片 `login_expired` | 平台登录态失效 | Worker 机 `python login.py --platform douyin_laike` 重扫 |
| `offline`（>90s 无心跳） | Worker 进程/网络问题 | 查进程、网络；形态 A 加保活 | 
| `selector_mismatch` | 页面改版 | 按 5.3 更新 SELECTORS |
| 发送 failed（连 3 次） | 会话关闭/附件/弹窗遮挡 | 设置页 RPA 卡片「重发 / 忽略」 |
| 用户收双份回复 | 自带机器人/美洽未关 | 后台关闭 |
| 身份异常（同用户多会话） | 改昵称未识别 | 后端 `rpa_identity_map` 手工合并 |

日常节奏：每日看一眼 RPA 卡片；每周「未命中问题」补知识库 + 查 `upload_dir/rpa/` 磁盘；按需轮换 `RPA_API_KEY`。

---

## 八、风险与回退

- **封号风险**：RPA 违反平台协议。缓解：仅自有账号、低频拟人化、只被动回复、拿官方权限即切 `DOUYIN_CHANNEL=api`。
- **多用户突发物理下限**：单页 RPA 读/发串行，N 个用户同时来消息时第 N 条最迟 ~7s 入库；超过 1 台单号负荷时用 fleet 多账号分片。
- **粘贴风控**：若真机发现瞬间填入+发送被清空，回退 `press_sequentially(delay=0)`（仍快一个量级）。
- **judge 跳过质量风险**：阈值 0.9 + ≤2 候选，faithfulness/证据核对护栏全保留；`rag_judge_skip_score=0` 一键关闭。
- **同会话补答排队**：连问 N 条串行生成，最迟 ~45s 发完（每问必答的设计取舍）。
- **频控误伤（占位未核实）**：48h/6 + 分钟6 是保守值，冒烟校准前不宜放量。

---

## 九、涉及文件与改动清单（本次只读，代码均已就绪）

| 文件 | 说明 |
|---|---|
| [douyin_laike_worker.py](../../workers/douyin_laike_worker.py) | 来客 Worker 主逻辑（已实现快读/锁/粘贴发送/幂等/去重） |
| [backend/app/config.py](../../backend/app/config.py) | 含 `rag_judge_skip_score` 与 `douyin_account_type` 等 |
| [backend/app/core/ratelimit.py](../../backend/app/core/ratelimit.py) | `douyin_laike_rpa` 频控（待真机校准） |
| [backend/app/bindings.py](../../backend/app/bindings.py) | `douyin_laike` driver 注册 |
| [backend/app/api/rpa.py](../../backend/app/api/rpa.py) | RPA 桥接协议全套接口 |
| [workers/run_fleet.py](../../workers/run_fleet.py) | 多账号 fleet 调度 supervisor |
| [docs/rpa-workers.md](../../docs/rpa-workers.md) | RPA 接入/运维/故障矩阵总纲 |

---

## 十、执行顺序（落地推进 Checklist）

1. 前置条件核销（账号、关美洽/自动回复、合规）→ 第六节第 7 项。
2. 后端 `.env` 配 `RPA_API_KEY` / `DOUYIN_CHANNEL=rpa` / `DOUYIN_ACCOUNT_TYPE=laike` → 重启。
3. Worker 装机 → `login.py --platform douyin_laike` → `doctor.py` 全绿。
4. 启动 Worker → 第六节第 1 项冒烟通过。
5. 第六节 2-5 项并发/幂等/快路径验证。
6. 第六节 6-8 项频控校准 + headless 复测。
7. 第六节 9-10 项告警链路 + 长跑观察。
8. 记录冒烟结果到 [rpa-workers.md](../../docs/rpa-workers.md) 的「冒烟记录」表。