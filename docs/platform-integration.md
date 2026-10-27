# 平台接入指引（抖音 / 小红书）

系统默认运行在 **Mock 通道**下，无需任何资质即可体验全流程。真实平台接入需要企业资质，按下述步骤操作。

## 一、抖音私信接入

### 政策现状（2026 年核实，重要）

**抖音私信消息管理权限的新申请通道已关闭。** 官方文档原文：「私信能力已不支持新增准入，相关能力申请不用提单」。已获得权限的存量应用不受影响，可继续正常使用。

当前仅存两条准入路径（见官方[「线索业务」ISV管理及IM接口开放规范](https://developer.open-douyin.com/docs/resource/zh-CN/dop/operation-standard/platform-capabilities/isv-im-standards)）：

| 路径 | 准入条件 |
|---|---|
| 第三方 ISV 应用 | 应用已入驻开放平台并上线为「正式应用」+ 移动/网站应用类型 + 面向经营者的 ToB SaaS 且以「智能客服（私信接待）」为主功能（ToC 类、内容工具类不准入）+ 提交 BRD 审核 + **服务客户的线索广告日耗 ≥ 10 万元** |
| 经营者自研应用 | 正式应用 + 移动/网站应用类型 + 企业号自研能力 + **公司线索广告日耗 ≥ 2 万元** |

持续考核机制（接入后仍可能被回收权限）：
- 连续 3 个月授权客户线索广告私信日消耗未达准入门槛
- 连续 3 个月私信接待水平低于行业平均水准
- 绕过平台风控或违反平台规则（可即刻回收）

### 手机号脱敏与「私信消息解码」权限

自 7 月 6 日起，抖音对私信中的手机号/固话**强制星号脱敏**（如 `138****5678`）：

- 即使已有私信权限，也必须额外在「控制台 - 能力管理 - 能力实验室」申请**「私信消息解码」权限**，否则只能收到脱敏后的留资信息
- 申请后**商家需重新扫码授权**（授权弹窗会新增「获取私信脱敏消息内容」权限项），否则依然只能拿到星号号码
- 合规红线：**严禁自研解密逻辑绕过脱敏**，必须走抖音官方解码接口，私自解密用户隐私信息会触发平台处罚
- 对本系统的影响：未完成解码授权时，抖音渠道的 `Customer.lead_phone` 只能存脱敏号码，AI 留资提取功能对该渠道部分受限

### 达不到门槛的替代方案

- **小程序商家**：可使用官方「抖音 IM 客服」能力，无需第三方私信 OpenAPI
- **普通企业号（无抖店、无线索广告）**：走「企业号后台 RPA」路径（见下文 RPA 降级通道），无需等待广告日耗达标；企业私信 OpenAPI 已「内测结束暂不开放」，不用考虑
- 本系统抖音适配器已按官方 OpenAPI 完整实现，获得权限后配置凭证即可启用，无需改代码

### 资质要求（获得权限的前提下）
- 抖音企业号（蓝 V 认证）
- 抖音开放平台开发者账号，创建「移动/网站应用」
- 私信能力（`im.direct_message` scope）+ 私信消息解码权限（能力实验室申请）

### 接入步骤（获得权限后适用）
1. 登录 [抖音开放平台](https://developer.open-douyin.com/)，创建应用，获取 `client_key` / `client_secret`
2. 申请 `im.direct_message` 权限并通过审核；在能力实验室申请「私信消息解码」权限
3. 企业号授权应用（OAuth），获取 `access_token`；申请解码权限后需商家重新扫码授权
4. 控制台「设置 - 开发配置 - Webhooks」配置回调地址：
   ```
   https://www.cndistribution.com/cs/webhooks/douyin
   ```
   订阅事件：`im_receive_msg`（接收私信）
5. 填写 `backend/.env`：
   ```
   DOUYIN_CLIENT_KEY=xxx
   DOUYIN_CLIENT_SECRET=xxx
   DOUYIN_ACCESS_TOKEN=xxx
   ```
6. 重启后端，访问 `/api/health` 确认 `douyin: true`

### 频控规则（系统已内置遵守）
- 用户发来一条消息后，24 小时内最多回复 6 条（`msg_id` 有效期 24h）
- 「用户进入会话页」事件 30 秒内最多发 3 条，单日最多 3 次
- 超限系统自动转人工并在工作台提醒，不会硬发

### 消息类型
- 接收：text / image / video / emoji / 留资卡片
- 发送：text（非互关用户仅支持文字 + 消息卡片）

### RPA 降级通道（无 API 资质时）

达不到上述广告日耗门槛时，可用 RPA Worker 自动化抖音官方客服后台完成收发（文本/图片/语音双向）。抖音多形态可以并存：账号页按账号指定企业号、来客或飞鸽，未指定的账号用全站默认。按账号形态三选一：

| 账号形态 | 目标后台 | Worker | 前置条件 |
|---|---|---|---|
| 抖店商家 | 飞鸽客服工作台 `https://im.jinritemai.com/pc_seller_v2/main/workspace` | `douyin_feige_worker.py` | 抖店主账号或客服子账号；关闭飞鸽「智能客服机器人」 |
| 蓝V 企业号（无抖店） | 企业服务中心 `https://e.douyin.com/` 消息管理页 | `douyin_enterprise_worker.py`（本期新增） | 蓝V 认证（600 元/年）；e.douyin.com 开通「客服管理」权限（1-3 个工作日审核）；关闭企业号「自动回复/智能客服」 |
| 来客商家（本地生活） | 来客会话中心 `https://life.douyin.com/p/liteapp/leads_cs/chat/session` | `douyin_laike_worker.py` | 来客商家账号（营业执照开通）；侧栏「私信管理 → 会话中心」 |

```
# backend/.env
RPA_API_KEY=<与 Worker 一致的随机密钥>
DOUYIN_CHANNEL=rpa
```

然后在商家电脑运行 `workers/` 下对应 Worker（setup → login → doctor → 启动）。

**频控差异**：企业号官方规则为「用户回复后 48h 内可发 6 条；主动触达 1 小时 ≤40 人、1 天 ≤100 人」（与抖店 24h/6 条不同），后端 `douyin_enterprise_rpa` 规则已内置对齐。来客（本地生活 IM）官方回复窗口规则未经真账号核实，后端 `douyin_laike_rpa` 初期保守对齐企业号（48h/6 + 分钟 6），冒烟核实后校准。

**留资提示**：RPA 路径下商家后台展示的联系方式为明文（API 路径则强制星号脱敏，需额外申请解码权限），线索收集场景反而更完整——但务必合规使用。

完整步骤、协议与故障排查见 [rpa-workers.md](rpa-workers.md)。

> 合规提示：RPA 违反平台用户协议，有封号风险，仅限自有账号低频使用；获得官方权限后应切回 `DOUYIN_CHANNEL=api`。

## 二、小红书私信接入

### 资质要求
- 小红书企业专业号（蓝 V 认证），个人号无法接入
- 审核需营业执照等资质，约 3~7 个工作日

### 接入步骤
1. 完成专业号蓝 V 认证
2. 登录小红书商业开放平台，创建服务商应用，获取 `appId` / `appSecret`
3. 登录聚光平台 →「工具 - 三方客服管理」→ 授权你的应用（勾选：私信消息读取、私信消息回复、用户基础信息读取）
4. 配置消息推送回调地址：
   ```
   https://www.cndistribution.com/cs/webhooks/xiaohongshu
   ```
5. OAuth 换取 `accessToken` / `refreshToken`（ark 网关 `oauth.getAccessToken`），可用内置脚本完成并落库：
   ```
   cd backend
   python scripts/xhs_oauth.py exchange --code <授权code> --write-env
   ```
   或手动填写 `backend/.env`：
   ```
   XHS_APP_ID=xxx
   XHS_APP_SECRET=xxx
   XHS_ACCESS_TOKEN=xxx
   XHS_REFRESH_TOKEN=xxx
   ```
6. 重启后端，访问 `/api/health` 确认 `xiaohongshu: true`

### 说明

- Token 自动续期、Webhook ACK 差异化（ark 要求 `{"success": true}`）、API 失败自动降级 RPA 等
  生产化机制均已内置，无需改代码
- 无资质时的 RPA 降级通道：配置 `RPA_API_KEY` + `XHS_CHANNEL=rpa`，运行 `workers/xhs_ark_worker.py`
  （前置条件：关闭千帆「自动回复」）

> 完整操作手册（平台侧 step-by-step、联调清单、FAQ、故障排查矩阵、技术契约）见
> [小红书接入手册](xiaohongshu-setup.md)。

## 三、从 Mock 切换到真实平台

Mock 通道与真实平台共用同一套会话/Agent/RAG 管线，切换无需改代码：

1. 按上文配置平台凭证
2. 用平台真实账号给企业号发私信
3. 在工作台「对话」页即可看到真实会话（平台 badge 区分来源）
4. Mock 面板（导航栏 🧪）仍可继续用于测试

## 四、新增平台（扩展）

实现 `backend/app/adapters/base.py` 的三个方法，注册到 `adapters/__init__.py` 即可：

```python
class MyAdapter(PlatformAdapter):
    platform = "my_platform"
    async def verify_webhook(self, headers, body) -> bool: ...
    async def normalize(self, payload) -> InboundMessage | None: ...
    async def send(self, platform_conversation_id, platform_user_id, content, msg_type="text", media_id="") -> str: ...
```

### 发布通道扩展（PublishChannel）

私信适配器之外，内容发布走独立的通道抽象 `backend/app/publisher/channels/base.py`：

```python
class MyPublishChannel(PublishChannel):
    is_async = False  # True 表示异步入 outbox，由 reconcile_async_tasks 对账
    async def publish(self, db, task, account, version) -> tuple[str, str]:
        ...  # 返回 (platform_post_id, url)；同步通道直接返回，异步通道返回 ("", "")
```

在 `channels/__init__.py` 的 `get_channel()` 注册 `(platform, auth_type)` 路由即可。评论通道同理：在 `comments/sender.py` 增加平台分支（API 直发或写 `rpa_outbox`）。

## 五、抖音内容发布接入（video.create）

### 申请条件与授权流程

1. 抖音开放平台创建「移动/网站应用」，申请 `video.create` 能力（视频发布需企业资质 + 应用审核）
2. 后端配置：`DOUYIN_CLIENT_KEY` / `DOUYIN_CLIENT_SECRET` / `DOUYIN_OAUTH_REDIRECT_URI`（回调地址需与开放平台后台一致）
3. 前端「账号」页新建抖音 API 账号 → 点「去授权」跳转 OAuth → 回调自动换取 `access_token`/`refresh_token`（Fernet 加密落库，有效期 30 天，系统自动刷新）

### 发布链路与限制

- 链路：`publisher/channels/douyin_api.py`：上传视频（`video/upload`，≤128MB 直传）→ `video/create` 创建 → 返回 item_id 与分享链接
- **每日上限**：单账号每日发布 ≤75 条（开放平台硬限制），系统在账号日限额之外兜底
- **审核期**：新创建视频进入平台审核，审核期间不可见；审核状态查询接口 `TODO: 确认实际接口地址`（联调阶段补轮询）
- 分片上传序列（>128MB 大视频）：`TODO: 确认实际接口地址`

### 联调清单

1. OAuth 回调拿到 token 并在「账号」页显示「已授权」
2. 发布一条 9:16 短视频 → 抖音 App 可见（或显示审核中）
3. token 过期后自动刷新（把 `expires_at` 改到过去再发一条验证）
4. 日限额触发后任务自动推迟到次日 9 点（「发布」页可见 deferred 状态）

## 六、抖音评论管理接入（item.comment）

### 申请与限制

- 申请 `item.comment` 能力（评论列表/回复），企业号可用
- **图集（图文）作品不支持评论 API**，仅视频作品可管理
- 评论事件 webhook 的事件名/字段：`TODO: 确认实际接口地址`（当前按 `item_comment` 事件 + content JSON 解析，联调校准）

### 双通道采集与兜底切换

- **Webhook 优先**：`webhooks.py` 识别评论事件 → 入队 `inbound_comment`
- **轮询兜底**：`comments/douyin_api.py` 的 `comment_polling_loop` 每 5 分钟拉取已发布作品的评论增量（按 `posts` 表驱动），webhook 不可用时自动补位
- **企业号 RPA 兜底**：无 `item.comment` 权限时，把账号 `auth_type` 改为 `rpa` 并运行 `workers/douyin_comment_worker.py`（e.douyin.com 评论管理页自动化），回复通道自动切换为 RPA outbox，无需改业务代码

### 顶层评论发布（首评引流）

- 接口：`item/comment/publish/`（`comments/douyin_api.py` 的 `publish_comment`），`TODO: 确认实际接口地址`（部分账号体系仅支持回复、不支持主动评论，联调确认；不支持时首评自动走 RPA 通道）
- 触发：发布成功且内容版本配置了首评话术 → 延迟 1-3 分钟自动发顶层评论（`{code}` 替换为最高优先级规则的暗号）
- 频控：与评论回复共用账号日限额与小时窗口（`comments/first_comment.py`）

### 视频数据回采（作品数据）

- 接口：`/api/douyin/v1/video/data/`（`analytics/collector.py`），`TODO: 确认实际接口地址`（需单独申请数据权限，返回字段名以联调为准）
- 策略：每 4 小时回采 7 天内发布作品的播放/点赞/评论/分享/收藏，写 `post_stat_snapshots` 时间序列；token 过期自动刷新一次
- 小红书无公开数据 API：走 RPA 回采（outbox `msg_type=collect_stats`，由 `xhs_publish_worker` 执行）

## 七、企业微信接入（加粉承接）

### 自建应用与客户联系权限

1. 企业微信管理后台 → 应用管理 → 创建自建应用
2. 开启「客户联系」权限（externalcontact），记录 `corpid` 与应用 `secret`
3. 后端配置：`WECOM_CORP_ID` / `WECOM_SECRET` / `WECOM_AGENT_USER_ID`（活码承接成员 UserID）

### 回调 URL 验证

1. 配置 `WECOM_TOKEN` / `WECOM_ENCODING_AES_KEY`（企微后台「接收消息」处生成）
2. 企微后台回调 URL 填 `https://<公网域名>/api/wecom/callback`（必须 HTTPS）
3. 保存时企微发起 GET 验证：本系统验签 + 解密 echostr 原样返回（`wecom/callback.py`），保存即通过

### 渠道活码与 state 归因约定

- 「漏斗」页创建活码：系统生成全局唯一 `state`（16 位 hex），调 `add_contact_way` API 建码（`skip_verify=true` 免验证）；企微未配置时可手工录入后台已建活码的二维码链接
- 用户扫码加粉 → 企微 POST 回调 `change_external_contact/add_external_contact` → 按 `State` 匹配活码 → 写 `FunnelEvent(wecom)` 并归因到绑定账号/内容
- **欢迎语 20s 窗口**：回调里的 `WelcomeCode` 仅 20 秒有效，系统在回调处理内同步调用 `send_welcome_msg`（文案取 `WECOM_WELCOME_TEXT`），超时仅告警不阻断归因
- 客户匹配为近似归因（该活码绑定账号 24h 内最近留资客户），精准方案（一次性 state 口令）见代码 TODO

### 私信侧推送

AI 客服在用户明确愿意加微信时调用 `push_wecom_code` 工具：按客户来源平台选活码 → 返回口令文本并写 `FunnelEvent(lead)`；企微未配置时工具返回 ok=false，LLM 自动改口引导留资入库，由人工后续添加。

## 知你快回网页版接入

知你快回 1.7.0 在抖音网页版、小红书网页版和私信通里读取私信。回复来源选「使用自己的回复接口」后，插件把消息 `POST` 到本系统，等待回复正文，再由插件发到网页。本系统只入库和生成，不走开放平台，也不写 RPA outbox。同一账号不要同时开 RPA Worker 或平台自带自动回复。

接口地址必须是公网 HTTPS，插件拒绝 `http://localhost` 和带账号密码的 URL：

`https://www.cndistribution.com/cs/api/integrations/zhinikuaihui/reply`

1. 后端 `.env` 设置 `ZHINI_REPLY_API_KEY`（随机字符串，不能留空）。
2. 在抖音或小红书网页私信页打开知你快回，回复内容来源选「使用自己的回复接口」。
3. 接口地址填上面的完整 URL。身份验证选 `Authorization: Bearer` 或 `X-API-Key`，密钥填同一个 `ZHINI_REPLY_API_KEY`。
4. 「最多等待回复」改为 60 秒。检索和模型经常超过插件默认的 15 秒。
5. 点测试并保存。浏览器弹出该域名的访问授权时点允许。状态变为「回复接口已连接」后才会在新消息里调用本接口。
6. 自动回复里打开对应平台（小红书网页版、私信通或抖音）。聊天页保持打开。小红书网页私信入口还要插件自己的授权有效。

连接测试不会写入客户和会话。真实私信写入工作台「对话」页。插件决定不发送时，接口返回 `action: skip`。工作台里事后手打的消息不会自动出现在网页上，要在插件打开的聊天页里发。
