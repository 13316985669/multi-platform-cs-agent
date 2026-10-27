# 多平台 AI 客服 Agent

对接 **抖音 / 小红书** 私信的 AI 客服系统：RAG 知识库驱动的 AI 自动接待 + 参考美洽的三栏人工客服工作台。

## 功能特性

- **多平台接入**：平台适配器架构（抖音 / 小红书 / 内置 Mock 模拟通道），webhook 秒级 ACK + 异步队列处理
- **可执行严格提示词**：LLM 只输出结构化 JSON 契约（回复/意图/置信度/转人工/标签/留资），后端解析执行
- **口语化人格回复**：分条短消息 + 打字延迟模拟，像真人客服一样聊天，不死板
- **市面最优方案 RAG**：查询改写（指代消解）→ 向量(bge-m3) + BM25 混合召回 → RRF 融合 → bge-reranker 精排 → 阈值兜底防幻觉
- **知识运营闭环**：未命中问题自动沉淀、召回测试台、bad case 标记回溯（可导出 evals 回归用例）
- **出口守卫**：广告法违禁词过滤、平台频控遵守（超限自动转人工）、留资脱敏
- **人工工作台**：美洽式三栏布局、会话自动分配、AI 辅助建议、快捷回复、数据看板
- **生产运维**：AI 全局熔断开关、RPA outbox 失败重发/忽略、操作审计日志、token 用量与成本看板、3 分钟回复率考核指标

## 架构

```
抖音/小红书/Mock ──webhook──> 接收层(秒级ACK) ──> 异步队列(幂等去重)
                                                      │
                              会话管理 <── Agent 引擎 <── RAG 检索管线
                                  │           │              │
                                  │       提示词+LLM    向量库+BM25+Rerank
                                  ▼           │
                            出口守卫(违禁词/频控) ──> 适配器.send ──> 平台
                                  │
                            WebSocket ──> React 三栏工作台
```

## 快速启动

### 方式一：本地开发（Windows PowerShell）

```powershell
# 1. 后端（Python 3.11+）
cd backend
python -m venv .venv
.venv\Scripts\Activate.ps1        # 激活虚拟环境
pip install -r requirements.txt
copy .env.example .env            # 编辑 .env 填入 LLM_API_KEY 等
uvicorn app.main:app --reload --port 8000

# 2. 前端（Node 18+，新开一个终端）
cd frontend
npm install
npm run dev
```

打开 http://localhost:5173 ，默认管理员账号 `admin / admin123`（首次启动自动创建，请登录后在设置页修改）。

API 文档（Swagger UI）：http://localhost:8000/docs

### 方式二：Docker Compose

在现有站点 `www.cndistribution.com` 上把子目录 `/cs/` 反代到本机应用，证书仍由宝塔终止：

```bash
cp backend/.env.example backend/.env   # 填 LLM_API_KEY、ZHINI_REPLY_API_KEY、SECRET_KEY
docker compose up -d --build
```

前端只监听本机 `127.0.0.1:8080`。工作台是 `https://www.cndistribution.com/cs/`。面板操作见 [宝塔配置说明](docs/baota.md)，插件地址和等待时间见 [部署文档](docs/deployment.md)。这套编排不启动 RPA，也不占用 80 和 443。

## 目录结构

```
├── backend/                # FastAPI 后端
│   ├── app/
│   │   ├── core/           # 队列/幂等/频控/违禁词/JWT/ASR
│   │   ├── adapters/       # 平台适配器（mock/douyin/xiaohongshu/rpa）
│   │   ├── rag/            # RAG：入库/向量/BM25/rerank/改写/管线
│   │   ├── agent/          # Agent 引擎/提示词/上下文/拟人化发送
│   │   ├── prompts/        # 严格提示词（cs_agent.md，热更新）
│   │   └── api/            # REST + WebSocket + RPA 桥接
│   ├── tests/              # pytest
│   └── evals/              # 回归测试集
├── frontend/               # React + Vite + Tailwind 工作台
├── workers/                # RPA Workers（飞鸽/企业号/千帆后台自动化，降级接入通道）
├── docs/                   # 项目文档
└── docker-compose.yml
```

## 文档

- [架构设计](docs/architecture.md)
- [提示词设计](docs/prompt-design.md)
- [平台接入指引（抖音/小红书）](docs/platform-integration.md)
- [小红书接入手册](docs/xiaohongshu-setup.md)
- [RPA Workers 接入指南（无 API 资质时的降级通道）](docs/rpa-workers.md)
- [知识库运营手册](docs/rag-guide.md)
- [部署文档](docs/deployment.md)
- [运维手册（监控/热备/备份）](docs/operations.md)
- [客服使用手册](docs/workbench-manual.md)

## 最小配置

只有 LLM 是必填项（`LLM_API_KEY`）。不配置 Embedding/Rerank 时 RAG 自动降级为纯 BM25；不配置平台凭证时发送动作降级为日志输出，可用内置 Mock 通道完整体验全流程。

## 知你快回得接口
https://www.cndistribution.com/cs/api/integrations/zhinikuaihui/reply