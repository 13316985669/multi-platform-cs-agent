# RPA Workers：平台后台自动化接入

无官方 API 资质时的降级通道：用 Playwright 自动化平台官方客服后台（抖音飞鸽 / 抖音企业号后台 / 抖音来客后台 / 小红书千帆），
把用户消息桥接到本系统后端，AI/人工回复经 outbox 由 Worker 发到平台。
抖音侧按账号形态选 driver：抖店商家用飞鸽，蓝V 企业号（无抖店）用企业号后台（e.douyin.com），本地生活商家用来客后台（life.douyin.com）。

> 合规提示：RPA 违反平台用户协议，仅限自有账号、低频拟人化使用，有封号风险。
> 有官方 API 资质时优先走官方通道（后端 `DOUYIN_CHANNEL=api`）。详见 `docs/rpa-workers.md`。

## 快速开始（跑起来就能用）

```bash
# Windows PowerShell
.\setup.ps1

# Linux / macOS
bash setup.sh
```

然后：

```bash
# 1. 编辑 .env.local：BACKEND_URL / RPA_API_KEY / ACCOUNT / PLATFORM
# 2. 首次登录引导（有头浏览器扫码，登录态落盘 profiles/<account>/）
python login.py
# 3. 自检（八项全绿才上线）
python doctor.py
# 4. 启动 Worker
python douyin_feige_worker.py       # 抖音飞鸽（抖店商家）
python douyin_enterprise_worker.py  # 抖音企业号后台（蓝V 无抖店，本期新增）
python douyin_laike_worker.py       # 抖音来客后台（本地生活商家，life.douyin.com，本期新增）
python xhs_ark_worker.py            # 小红书千帆
```

启动后消息收发全自动闭环，Worker 窗口不需要人盯——日常操作（接管会话、发图片、看状态）都在工作台进行，
详见 [docs/rpa-workers.md](../docs/rpa-workers.md) 第四节「启动后的日常使用」。

## 前置条件

1. **关闭平台自带自动化**：飞鸽「智能客服机器人」/ 企业号「自动回复、智能客服」（e.douyin.com → 消息管理）/ 千帆「自动回复」必须关闭或设为仅人工，
   否则用户会收到平台机器人与本系统 AI 的双份回复（doctor 第 8 项会检查）
2. 后端已配置 `RPA_API_KEY` 且与 `.env.local` 一致
3. 后端对应平台通道设为 RPA：`DOUYIN_CHANNEL=rpa` 或 `XHS_CHANNEL=rpa`
4. 企业号路径额外前提：蓝V 认证 + e.douyin.com 开通「客服管理」权限（1-3 个工作日审核，需营业执照 + 法人身份证）
5. 来客路径额外前提：来客商家账号（营业执照开通）+ life.douyin.com 开通「消息/客服管理」权限；后台自带自动回复（如有）须关闭

平台后台入口地址（内置默认，平台改地址时在 `.env.local` 配置 `PLATFORM_URL` 覆盖）：

- 抖音飞鸽（抖店商家）：`https://im.jinritemai.com/pc_seller_v2/main/workspace`
- 抖音企业号（蓝V 无抖店）：`https://e.douyin.com/`（消息管理页）
- 抖音来客（本地生活商家）：`https://life.douyin.com/`（消息/客服管理页）
- 小红书千帆私信：`https://ark.xiaohongshu.com/ark/message/platform/msg`

## 目录结构

```text
base_worker.py            公共框架（心跳/outbox 拉取/入站上报/媒体上下行/身份缓存/去重/自检）
douyin_feige_worker.py       抖音飞鸽后台驱动（抖店商家；选择器集中在文件头 SELECTORS）
douyin_enterprise_worker.py  抖音企业号后台驱动（蓝V 无抖店，e.douyin.com 消息管理；本期新增）
douyin_laike_worker.py       抖音来客后台驱动（本地生活商家，life.douyin.com 消息/客服管理；本期新增）
xhs_ark_worker.py            小红书千帆后台驱动
doctor.py                 上线前七项自检（退出码可接 CI）
login.py                  首次登录引导
fixtures/fake_feige.html  伪平台页面（无真实店铺也能端到端开发与测试）
tests/                    Worker 集成测试（对着 fixture 页面跑）
```

## 无真实店铺开发调试

```bash
playwright install chromium
pytest tests/ -v
```

测试用 `fixtures/fake_feige.html`（本地 HTML 模拟飞鸽 DOM；企业号对应 `fake_enterprise.html`，来客对应 `fake_laike.html`）+ 内存版 FakeBackend，
覆盖：页面自检、文本/图片入站上报、旁路消息识别、出站发送与气泡断言、失败回执。
**真实页面改版导致选择器失效时，应先更新 fixture 与 SELECTORS 并保证测试转绿。**

## Windows 常驻运行

用任务计划程序开机自启（示例）：

```powershell
$action = New-ScheduledTaskAction -Execute "D:\projects\multi-platform-cs-agent\workers\.venv\Scripts\python.exe" `
    -Argument "douyin_feige_worker.py" -WorkingDirectory "D:\projects\multi-platform-cs-agent\workers"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "rpa-douyin-worker" -Action $action -Trigger $trigger
```

生产建议 `HEADLESS=true`（新版无头模式，Windows 锁屏不影响）。

## 常见问题

| 现象 | 处理 |
|---|---|
| `ERR_CONNECTION_CLOSED` / 打不开平台后台 | 核对入口地址（见上文）；检查网络/代理；地址变更用 `PLATFORM_URL` 覆盖 |
| doctor 报「登录态有效 ✘」 | 重新运行 `python login.py` |
| doctor 报「关键选择器在位 ✘」 | 平台页面改版，按实际 DOM 更新 Worker 文件头 SELECTORS 与 fixture |
| 设置页 Worker 卡片红色 offline | 检查 Worker 进程是否存活、后端地址可达、心跳日志 |
| 用户收到双份回复 | 平台自带机器人未关闭，去平台后台关闭 |
