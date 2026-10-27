# 部署文档

## 方式一：Docker Compose（推荐，知你快回）

证书和反向代理由宝塔做。这套编排只起后端和前端，不启动 Caddy，也不启动 RPA Worker。前端只监听本机 `127.0.0.1:8080`，不要把 8080 对公网开放。数据库、向量库、上传文件和自动备份都在卷 `cs-data` 里。面板上的点击顺序、配置文件粘贴位置和故障对照见 [宝塔配置说明](baota.md)。

不新建站点，也不占用新域名。现有网站 `www.cndistribution.com` 保持原样，只把子目录 `/cs/` 反代到客服系统。生产镜像按 `VITE_BASE=/cs/` 构建，浏览器里的页面、接口和 WebSocket 都在这个前缀下。

```bash
cp backend/.env.example backend/.env
```

编辑 `backend/.env`，至少填这三项：

- `LLM_API_KEY`
- `ZHINI_REPLY_API_KEY`：随机串，`python -c "import secrets;print(secrets.token_hex(16))"`
- `SECRET_KEY`：换成另一把随机串

`SITE_DOMAIN` 只作备查，填 `www.cndistribution.com`，不要带 `https://` 和路径。然后启动：

```bash
docker compose up -d --build
```

Compose 会强制 `APP_ENV=production`。这样已修改的管理员密码不会在每次重启时被重置为 `admin123`。同时强制 `MOCK_ENABLED=false`、`DOUYIN_CHANNEL=api`、`XHS_CHANNEL=api`，并把 `RPA_API_KEY` 置空。`.env` 里即使写了 RPA 密钥，这套部署也不会走 RPA。

在宝塔里打开已有站点 `www.cndistribution.com` 的配置文件，加上下面这段。不要用面板新建一个整站反代，`proxy_pass` 末尾的斜杠负责剥掉 `/cs/`。`^~` 用来压过宝塔对 js/css 的正则规则。这段不要开缓存。上传上限和读超时要和容器里的 Nginx 一致，否则大文件或生成请求会在宝塔这一层被掐断。

```nginx
location = /cs {
    return 301 /cs/;
}
location ^~ /cs/ {
    client_max_body_size 500m;
    proxy_pass http://127.0.0.1:8080/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $http_connection;
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
}
```

改完后重载 Nginx。工作台：`https://www.cndistribution.com/cs/`。首次登录 `admin` / `admin123`，登录后立刻改掉。

知你快回插件：

1. 回复来源选「使用自己的回复接口」。
2. 接口地址填 `https://www.cndistribution.com/cs/api/integrations/zhinikuaihui/reply`。
3. 身份验证填同一把 `ZHINI_REPLY_API_KEY`。
4. 等待时间选 60 秒。
5. 点测试并保存。浏览器弹出该域名的访问授权时点允许。

对外地址：

- 工作台：`https://www.cndistribution.com/cs/`
- 平台 webhook：`https://www.cndistribution.com/cs/webhooks/{platform}`
- 抖音 OAuth：`https://www.cndistribution.com/cs/api/accounts/oauth/callback`
- 企微回调：`https://www.cndistribution.com/cs/api/wecom/callback`

页面和接口同源，`CORS_ORIGINS` 保持为空。

## 方式二：本地开发

### 后端（Windows PowerShell）

```powershell
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env     # 编辑配置
uvicorn app.main:app --reload --port 8000
```

### 前端

```powershell
cd frontend
npm install
npm run dev               # 不设 VITE_BASE，页面在 http://127.0.0.1:5173/，代理到 localhost:8000
```

## 环境变量清单

| 变量 | 必填 | 说明 |
|---|---|---|
| `LLM_API_KEY` | **是** | LLM 密钥（默认 DeepSeek） |
| `LLM_BASE_URL` / `LLM_MODEL` | 否 | 换其他 OpenAI 兼容模型时改 |
| `SECRET_KEY` | 生产必填 | JWT 签名密钥，改成随机串 |
| `EMBEDDING_API_KEY` | 否 | 不配置则 RAG 降级纯 BM25 |
| `RERANK_API_KEY` | 否 | 不配置则跳过精排 |
| `RAG_RERANK_THRESHOLD` | 否 | 无命中阈值，默认 0.35 |
| `DOUYIN_*` | 否 | 抖音凭证，见 platform-integration.md |
| `XHS_*` | 否 | 小红书凭证，见 platform-integration.md |
| `APP_ENV` | 生产由 Compose 强制 | `production` 时不重置已改的管理员密码。Compose 覆盖 `.env` |
| `DATABASE_URL` | 否 | 默认 SQLite。Compose 会覆盖 `.env`，换 PostgreSQL 必须改 Compose |
| `AI_GLOBALLY_ENABLED` | 否 | AI 总开关，默认 true；设置页可运行时切换（admin） |
| `CORS_ORIGINS` | 生产按部署填 | 跨域白名单（逗号分隔）。空 = 仅同源（nginx/vite 反代无需填）。禁止 `*` |
| `MOCK_ENABLED` | 生产必须 false | Mock 注入口 `/api/mock/incoming`。开发默认 true，生产关闭后 404 |
| `DOUYIN_ACCOUNT_TYPE` | 否 | 抖音形态的全局默认：feige / enterprise / laike。账号页单独指定后以账号为准 |
| `LLM_PRICE_INPUT_PER_1K` / `LLM_PRICE_OUTPUT_PER_1K` | 否 | token 单价（元/千），看板成本估算用 |
| `HUMANIZE_BASE_DELAY_MS` / `HUMANIZE_PER_CHAR_MS` | 否 | 拟人化发送节奏 |
| `SESSION_TIMEOUT_MINUTES` | 否 | AI 会话超时自动关闭，默认 30 分钟 |
| `SESSION_CLOSE_MESSAGE` | 否 | 超时关闭的结束语 |
| `ALERT_WEBHOOK_URL` | 生产建议 | 钉钉/企微机器人地址，异常告警推送 |
| `ALERT_COOLDOWN_SECONDS` | 否 | 同类告警冷却期，默认 600 秒 |
| `LLM_FALLBACK_*` | 生产建议 | 备用 LLM（BASE_URL/API_KEY/MODEL），主模型故障自动切换 |
| `BACKUP_DIR` / `BACKUP_INTERVAL_HOURS` / `BACKUP_KEEP` | 否 | 自动备份目录/间隔/保留份数 |

## 生产数据库（PostgreSQL）

SQLite 适合单机小流量（日咨询 < 500）。以下情况建议直接上 PostgreSQL：
队列任务高频读写、多客服并发操作、需要 Point-in-Time 恢复。

### 1. 起库（Docker 示例）

```bash
docker run -d --name cs-postgres \
  -e POSTGRES_PASSWORD=<强密码> -e POSTGRES_DB=cs_agent \
  -v cs-pgdata:/var/lib/postgresql/data -p 5432:5432 postgres:16
```

### 2. 配置

`docker-compose.yml` 里 backend 的 `environment.DATABASE_URL` 会覆盖 `backend/.env`。只改 `.env` 时，容器仍然使用卷上的 SQLite `sqlite:////data/app.db`。

换库时改 Compose 里的这一项，并在 `backend/requirements.txt` 加入 `psycopg2-binary` 后重新构建镜像。容器里的 `127.0.0.1` 是容器自己，不是宿主机，数据库地址要写成宿主机可达地址，或把 Postgres 放进同一个 Compose 网络。

```yaml
DATABASE_URL: postgresql+psycopg2://postgres:<强密码>@<数据库主机>:5432/cs_agent
```

首次启动 `init_db()` 自动建全量表（无需手动 DDL）。

### 3. 从 SQLite 迁移存量数据（可选）

```bash
# 导出 SQLite 数据 → 导入 Postgres（按外键依赖顺序）
sqlite3 backend/data/app.db .dump > dump.sql
# 手工清理 SQLite 方言（PRAGMA/AUTOINCREMENT 等）后：
psql -h 127.0.0.1 -U postgres -d cs_agent -f dump.sql
```

数据量大或结构复杂时建议用 pgloader：`pgloader sqlite://backend/data/app.db postgresql://postgres:<密码>@127.0.0.1/cs_agent`

### 4. 验证

```
cd backend && pytest tests/ -q   # 测试用例与数据库方言无关，全绿即兼容
```

注意：`_ensure_column` 轻量迁移仅在 SQLite 下生效；PostgreSQL 存量 schema 变更请引入 Alembic。

## 数据备份

系统默认每 24 小时自动备份到 `BACKUP_DIR`（SQLite 用安全备份 API，Chroma 整目录拷贝），滚动保留最近 7 份。

手动备份以下目录/卷即可完整备份：
- SQLite：`backend/data/app.db`（或 docker volume）
- Chroma 向量库：`backend/data/chroma/`
- 上传文档：`backend/data/uploads/`

## 生产部署 checklist

完整逐项清单见 [deployment-checklist.md](deployment-checklist.md)。最低要求：

1. `SECRET_KEY` 改为强随机串
2. 登录后台修改默认管理员密码（admin / admin123）
3. `MOCK_ENABLED=false`，`CORS_ORIGINS` 按部署填写（同源反代保持为空）
4. webhook 地址需要公网 HTTPS（由现有站点 `www.cndistribution.com` 的证书终止）
5. 抖音/小红书后台配置 webhook 为 `https://www.cndistribution.com/cs/webhooks/{platform}`
6. 数据库建议换 PostgreSQL（改 Compose 的 `DATABASE_URL` 并安装 `psycopg2-binary`，只改 `.env` 无效）

## 常见问题

**Q: pip install chromadb 很慢/失败？**
A: chromadb 依赖较多属正常；Windows 需 Python 3.11 64 位。失败可先装 `pip install chromadb --no-cache-dir`。

**Q: 启动后 AI 不回复？**
A: 访问 `/api/health` 检查 `llm: true`；查看后端日志。未配置 LLM 时系统会固定话术 + 转人工。

**Q: webhook 收不到平台消息？**
A: 检查回调地址公网可达、平台后台事件已订阅、`/api/health` 中平台凭证为 true。
