# 运维手册：监控告警 / 多模型热备 / 自动备份

## 一、监控告警

### 告警事件与阈值

| 事件 | 阈值 | 含义 |
|---|---|---|
| `llm_failure` | 5 分钟内 3 次 | LLM 调用失败（网络/服务异常），可能已切备用模型 |
| `webhook_failure` | 5 分钟内 5 次 | 入站消息处理失败（队列 worker 消费异常） |
| `tool_failure` | 5 分钟内 5 次 | 业务工具（查订单/物流/建工单）执行失败 |

### 接入告警通知

在 `backend/.env` 配置机器人 webhook 地址（钉钉/企微均兼容 text 消息格式）：

```bash
ALERT_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx
ALERT_COOLDOWN_SECONDS=600   # 同类告警冷却期，防刷屏
```

- 留空则只记日志（`ALERT ...` 级别 WARNING），不发网络请求
- 触发告警后可在日志搜索 `监控事件` 查看每次埋点明细

### 健康检查接口

```bash
GET /api/health          # 公开：各组件配置状态（true/false）
GET /api/health/detail   # 仅管理员 JWT：队列深度 + 组件状态 + 近 1h 错误计数
```

`/api/health/detail` 返回示例：

```json
{
  "status": "ok",
  "queue": { "pending": 0, "failed": 1 },
  "components": { "llm": true, "llm_fallback": true, "embedding": true, "...": "..." },
  "errors_last_hour": { "llm_failure": 2 }
}
```

**运维建议**：用 uptime 工具（如 Uptime Kuma）定时探测 `/api/health`；`queue.failed` 持续增长说明有消息处理异常，到设置页「队列运维」查看错误并重试，或查 `queue_tasks` 表的 `error` 字段。

## 二、多模型热备

主 LLM 故障（网络超时/5xx/鉴权失效）时自动切换备用模型，对话不中断：

```bash
# 主模型（默认 DeepSeek）
LLM_BASE_URL=https://api.deepseek.com/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=deepseek-chat

# 备用模型（任意 OpenAI 兼容服务，如通义/Kimi）
LLM_FALLBACK_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_FALLBACK_API_KEY=sk-yyy
LLM_FALLBACK_MODEL=qwen-plus
```

切换行为：
- 仅**硬失败**（网络/服务异常）触发切换；契约解析失败不切换（那是提示词问题，换模型无用）
- 切换事件计入 `llm_failure` 监控并触发告警
- 主备都失败 → 兜底话术 + 转人工，会话不丢

## 三、自动备份

```bash
BACKUP_DIR=./backups          # 备份目录
BACKUP_INTERVAL_HOURS=24      # 备份间隔
BACKUP_KEEP=7                 # 滚动保留份数
```

- 每次备份生成 `backup_YYYYMMDD_HHMMSS/` 目录，含 `app.db`（SQLite 安全备份 API，WAL 模式不丢数据）+ `chroma/`（向量库整目录拷贝）
- 超过 `BACKUP_KEEP` 份自动清理最旧的
- 生产环境建议把 `BACKUP_DIR` 指向挂载的对象存储/独立磁盘

**手动触发一次备份**（调试用）：

```powershell
cd backend
.venv\Scripts\Activate.ps1
python -c "from app.core.backup import do_backup; print(do_backup())"
```

**恢复**：停服 → 用备份的 `app.db` 替换 `data/app.db`、`chroma/` 替换 `data/chroma/` → 重启。

## 四、会话超时自动关闭

```bash
SESSION_TIMEOUT_MINUTES=30                          # AI 会话静默超时时间
SESSION_CLOSE_MESSAGE=或者您方便加下vx吗，把办理的流程和需要的资料发您vx上，您可以先接收参考下，后续有不清楚的也方便给您直接解答下您可以先接收参考下，后续  # 关闭前发送的结束语
SESSION_SWEEP_INTERVAL_SECONDS=300                  # 扫描周期
```

- 只自动关闭 **AI 接待中**的会话；pending（排队待人工）/ human（人工接待中）不关闭
- 关闭后用户再发消息会自动开新会话，历史记录保留在客户名下

## 五、入口内容安全

用户消息经过两级风险检测（`core/contentfilter.py` 的 `check_inbound`）：

| 级别 | 内置词库 | 系统行为 |
|---|---|---|
| `block` | 涉政/暴恐/违法 | 消息打标 `risk=block`，自动转人工，AI 不回复 |
| `warn` | 辱骂/骚扰 | 消息打标 `risk=warn`，正常 AI 接待 |

**自定义入口风险词**：调用违禁词 API（`POST /api/knowledge/banned-words`，需管理员），`direction` 传 `in`，`category` 填 `block` 或 `warn`。（`direction=out` 为 AI 出口违禁词，命中自动改写。）
