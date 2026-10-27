# 提示词设计说明

提示词文件：[backend/app/prompts/cs_agent.md](../backend/app/prompts/cs_agent.md)，支持**热更新**（按文件 mtime 自动重载，改完即生效）。

## 设计理念：「可执行」的提示词

普通提示词让 LLM 输出自由文本，只能看不能用。本系统的提示词约束 LLM **只输出严格 JSON 契约**，后端解析后直接执行动作（发回复/转人工/打标签/留资），提示词由此成为系统的一个「可执行组件」。

## JSON 输出契约逐字段说明

```json
{
  "reply_messages": ["第一条短消息", "第二条（可选）", "第三条（可选，最多3条）"],
  "intent": "consult_price | consult_feature | complaint | after_sale | chitchat | other",
  "confidence": 0.0,
  "handoff": false,
  "handoff_reason": "complaint | sensitive | explicit_human | low_confidence | out_of_scope",
  "tags": ["高意向", "询价"],
  "lead": { "phone": "", "wechat": "", "note": "" },
  "quick_action": "none | send_price_card | send_link",
  "tool_call": null
}
```

| 字段 | 说明 | 后端动作 |
|---|---|---|
| `reply_messages` | 分条短消息数组，模拟真人分开发送 | 逐条发送 + 打字延迟 |
| `intent` | 意图枚举（六选一） | 存入消息 extra，用于统计 |
| `confidence` | 0~1 自评置信度 | <0.6 连续两次应触发转人工（提示词规则） |
| `handoff` | 是否转人工 | true 时切会话到 pending 队列并自动分配 |
| `handoff_reason` | 转人工原因枚举 | 记录 HandoffEvent，用于看板统计 |
| `tags` | 客户标签 | 合并到 customer.tags，工作台展示 |
| `lead` | 留资信息 | 写入 customer.lead_*，日志脱敏 |
| `quick_action` | 预留的卡片动作 | 当前仅 none 生效 |
| `tool_call` | 请求调用业务工具 `{name, args}`，不需要时为 null | 执行工具 → 结果回填 → 二次生成（最多 2 次） |

契约校验代码：`backend/app/agent/engine.py` 的 `_parse_contract()`，pydantic 校验失败自动带错误反馈重试 1 次。

## 工具调用（function calling）契约

提示词第九章通过 `{{tools_section}}` 自动注入当前可用工具清单（来自 `agent/tools.py` 的 `TOOL_REGISTRY`）。

**两阶段流程**：LLM 首轮输出 `tool_call` → 后端执行工具 → 把 `{"ok", "data", "error"}` 结果以「工具调用结果」段落回填到提示词 → LLM 二次生成最终回复。最多 2 次工具调用（支持 query_order → query_logistics 链式调用），达到上限后提示词强制要求直接输出最终回复。

**工具失败降级**：`ok=false` 时提示词要求安抚用户并转人工；未注册工具/执行异常由 `execute_tool` 统一兜底，绝不抛出。

**新增工具**：继承 `Tool` 基类实现 `run()` 并 `register()`，提示词清单自动更新，无需改模板。

## AI 身份合规（重要）

《生成式人工智能服务管理暂行办法》与平台规则均要求 AI 客服**明示身份**。因此提示词的身份策略是：

- **不主动暴露**（保持自然对话体验）
- **被问及时必须大方承认**「智能客服」身份，严禁否认（旧版「否认是机器人」的话术已移除，有封号/合规风险）
- 承认的同时强调能解决问题，例如：「我是赢态财务的智能客服小赢，常见的注册开户问题我都能先回，拿不准的帮你转同事」

回归用例：`evals/cases.json` id=14（期望回复包含「智能客服」且不包含「真人/不是机器人」）。

## 口语化风格指南（「不死板」的核心）

提示词第二章，每条规则都配正反例：

1. **短句分条**：一条 ≤40 字，长回复拆 2~3 条
2. **自然语气词**：呢/哈/呀/哦，每条最多 1 个 emoji
3. **先共情再办事**：用户有情绪时第一条先安抚
4. **禁用机器人腔**：不说「很高兴为您服务」等模板话
5. **会接话闲聊**：不强行推销
6. **承认不确定**：不知道就说要确认，触发转人工

执行层配合：`agent/humanize.py` 按字数模拟打字延迟逐条发送。

## 转人工硬规则

满足任一条件必须 `handoff=true`：

| 场景 | handoff_reason |
|---|---|
| 用户明确要求人工 | explicit_human |
| 投诉/辱骂/负面情绪 | complaint |
| 退款/赔偿/法务/发票 | out_of_scope |
| 知识库无命中或没把握 | low_confidence |
| 政治敏感话题 | sensitive |

## 模板变量

| 变量 | 注入内容 |
|---|---|
| `{{platform}}` | 平台标识（douyin/xiaohongshu/mock） |
| `{{platform_style}}` | 平台风格说明（抖音活泼/小红书种草感） |
| `{{knowledge_context}}` | RAG 检索到的带引用知识片段，无命中时为「无匹配资料」 |
| `{{history}}` | 对话历史（最近 10 条原文 + 滚动小结） |
| `{{user_message}}` | 用户最新消息 |
| `{{tools_section}}` | 可用业务工具清单（自动从工具注册表生成） |
| `{{current_time}}` | 当前时间 |

## 如何安全地修改提示词

1. 直接编辑 `cs_agent.md`，保存即生效（热更新）
2. **必须跑回归**：
   ```powershell
   cd backend
   .venv\Scripts\Activate.ps1
   python -m evals.run_evals
   ```
3. 回归集（`evals/cases.json`，24 条用例）覆盖：知识命中、投诉转人工、越界问题、闲聊、留资、AI 身份合规、工具调用触发、口语化检查（单条长度、禁用机器人腔）等关键行为
4. 通过率下降时对比失败用例的「实际输出」，定位是提示词改动引起的行为漂移
5. 新增业务场景时，同步在 cases.json 补一条用例
