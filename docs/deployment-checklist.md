# 生产上线 Checklist

部署步骤见 [deployment.md](deployment.md)。本页是上线前必须逐项确认的安全与运维清单（Phase 8）。

## 必改（不改则 JWT / 账号可被伪造）

- [ ] `SECRET_KEY` 改为强随机串（`python -c "import secrets; print(secrets.token_urlsafe(48))"`）。启动日志出现「SECRET_KEY 仍为默认值」即未改。
- [ ] 登录后台修改默认管理员密码（首次 `admin` / `admin123`）。启动日志出现「仍使用初始密码」即未改。

## 必关（开发开关）

- [ ] `MOCK_ENABLED=false`。`/api/mock/incoming` 无鉴权，开着任何人都能伪造入站消息驱动 AI。关闭后该端点返回 404。
- [ ] `CORS_ORIGINS`：经 nginx/vite 同源反代部署时保持为空（仅放行同源）。前后端分离时填前端源，如 `https://ops.example.com`，逗号分隔多源。**禁止** `*`。

## 必配（公网回调）

- [ ] 全站 HTTPS（证书由现有站点 `www.cndistribution.com` 终止）。WebSocket `?token=` 与媒体/素材签名 URL 都会进访问日志，明文 HTTP 等于泄密。
- [ ] 宝塔只反代 `/cs/` 到 `http://127.0.0.1:8080/`，`proxy_pass` 带结尾斜杠，`client_max_body_size 500m`，关闭该段缓存。
- [ ] 抖音 / 小红书 webhook：`https://www.cndistribution.com/cs/webhooks/{platform}`（需平台后台配置事件订阅）。
- [ ] 抖音 OAuth 回调：`DOUYIN_OAUTH_REDIRECT_URI=https://www.cndistribution.com/cs/api/accounts/oauth/callback`，与开放平台控制台一致。
- [ ] 企微回调：`WECOM_TOKEN` + `WECOM_ENCODING_AES_KEY`，回调 URL 为 `https://www.cndistribution.com/cs/api/wecom/callback`。
- [ ] RPA Worker：`RPA_API_KEY` 与 worker `.env` 一致；账号页确认心跳在线。

## 建议

- [ ] `ALERT_WEBHOOK_URL` 钉钉/企微机器人，队列最终失败、Worker 掉线、账号健康度会推送。
- [ ] `LLM_FALLBACK_*` 备用模型，主 LLM 故障自动切换。
- [ ] 备份目录挂载持久卷（`BACKUP_DIR`，默认每 24h，保留 7 份）。
- [ ] 日咨询 > 500 或多客服并发时换 PostgreSQL（见 deployment.md）；存量 schema 变更需 Alembic（`_ensure_column` 仅 SQLite）。
- [ ] 保持单 uvicorn 进程。水平扩容需先换 PG + 外部队列 + WS 广播层（见 architecture.md「单实例边界」）。

## 上线后冒烟

1. `https://www.cndistribution.com/cs/api/health` 返回 `ok`，`llm: true`。本机容器上等价于 `http://127.0.0.1:8080/api/health`。
2. 不带 token 访问 `/cs/api/health/detail` → 401；管理员 JWT → 200。
3. 浏览器未登录打开工作台时不应出现 `/cs/ws` 连接；登录后 `/cs/ws?token=` 握手成功。
4. `MOCK_ENABLED=false` 时 `POST /cs/api/mock/incoming` → 404。
5. 设置页「队列运维」能看到 pending/failed 计数；故意失败一条任务后可点重试。
6. 账号页健康度、创作台走完「生成 → 合规 → 提交 → 审批」后，发布弹窗能选到该版本。
