# 真实平台联调跟踪清单

代码里用 `# TODO: 确认实际接口地址` 标出的占位（精确计数：`backend/app` 22 处 + `workers` 18 处 = 40 处，不含 `.venv`）。本页按平台分组，列出**代码位置**与**联调验收标准**。未联调前对应通道会走 Mock / 日志降级 / RPA 占位选择器，功能可在 Mock 通道完整演示。

## 抖音开放平台

| 能力 | 代码位置 | 联调验收 |
|---|---|---|
| 评论 webhook 事件名 / 字段 | `adapters/douyin.py` `is_comment_event` / `normalize_comment`；`comments/douyin_api.py` 文件头 | 真实推送进入 `inbound_comment`，`platform_comment_id` 幂等不重复 |
| 评论列表 / 回复字段名 | `comments/douyin_api.py` 列表解析 | 轮询兜底能拉到新评并回复成功 |
| 顶层评论（首评引流） | `comments/douyin_api.py` `publish_comment` | 发布成功后 1–3 分钟视频下出现暗号评论；若账号体系不支持主动评论，自动走 RPA |
| 视频数据 API | `analytics/collector.py` `DOUYIN_ITEM_DATA_URL` | 回采写入 `PostStatSnapshot`（播放/点赞/评论/分享） |
| 用户数据 API | `analytics/collector.py` `DOUYIN_USER_DATA_URL` | 账号画像 `profile_json` 含粉丝/作品/获赞 |
| 分片上传序列（>20MB） | `publisher/channels/douyin_api.py` init/upload_part/complete | 大视频发布成功拿到 `item_id` |
| 视频审核状态查询 | `publisher/channels/douyin_api.py` | 审核中任务保持 publishing，通过后转 success |
| refresh_token 有效期 | `core/account_health.py` 按 30 天近似 | 授权后 `saved_at` 年龄预警窗口与控制台一致 |

## 小红书

| 能力 | 代码位置 | 联调验收 |
|---|---|---|
| 聚光评论推送 `msgTag` | `adapters/xiaohongshu.py` | 真实回调分流进 `inbound_comment`（当前先打日志观察） |

## 企业微信

| 能力 | 代码位置 | 联调验收 |
|---|---|---|
| `external_userid` 与平台客户映射 | `wecom/callback.py` | 加粉回调能归因到 24h 内同账号 `FunnelEvent.lead` |
| 一次性 state 活码 | `agent/tools.py` `PushWecomCodeTool`；`wecom/channel_code.py` 承接成员列表 | 每次推码生成独立 state，避免共享活码串客 |
| 私信外链屏蔽策略 | `agent/tools.py` 口令形态注释 | 抖音/小红书私信发出后用户能看到微信号/口令（外链被屏蔽时改口令） |

## RPA Worker（页面 URL + DOM 选择器均为占位）

首次联调一律先跑 `python doctor.py`，再按实际 DOM 改选择器。

| Worker | 文件 | 联调验收 |
|---|---|---|
| 小红书发布 / 数据 / 画像 | `workers/xhs_publish_worker.py` | 笔记发布回传 URL；`collect_stats` / `collect_account` 写入 outbox.result |
| 小红书评论 | `workers/xhs_comment_worker.py` | 采集新评 + 回复 + `post_first_comment` |
| 抖音企业号评论 | `workers/douyin_comment_worker.py` | 同上（企业号后台） |
| 热榜采集 | `workers/hot_collect_worker.py` | 按关键词导入灵感库，含标题/作者/URL/互动数 |

## 客服工具（非开放平台，接内部系统）

| 能力 | 代码位置 | 联调验收 |
|---|---|---|
| 查订单 | `agent/tools.py` 订单接口 TODO | 用户报订单号后 LLM 能读到真实状态 |
| 查物流 | `agent/tools.py` 快递接口 TODO | 返回在途节点，失败时 `ok=false` 转人工 |

## 联调原则

- 不编造官方路径：地址未确认前保持 TODO + Mock/降级，联调时只改标注处。
- 改选择器/字段名后补一条针对真实响应结构的回归测试（`respx` mock 真实 JSON）。
- 通过一项在本表对应行打勾，并删掉代码里那条 TODO。
