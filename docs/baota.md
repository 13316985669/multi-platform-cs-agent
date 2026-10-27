# 宝塔配置说明

在已有站点 `www.cndistribution.com` 上，把子目录 `/cs/` 反代到本机 Docker 里的客服系统。证书继续用这个站点现成的 SSL。不新建站点，不占用新域名，80 和 443 仍由宝塔 Nginx 监听。

做完后的地址：

- 工作台：`https://www.cndistribution.com/cs/`
- 健康检查：`https://www.cndistribution.com/cs/api/health`
- 知你快回：`https://www.cndistribution.com/cs/api/integrations/zhinikuaihui/reply`
- 平台 webhook：`https://www.cndistribution.com/cs/webhooks/{platform}`
- 抖音 OAuth：`https://www.cndistribution.com/cs/api/accounts/oauth/callback`
- 企微回调：`https://www.cndistribution.com/cs/api/wecom/callback`

总览和环境变量见 [deployment.md](deployment.md)。上线前逐项核对见 [deployment-checklist.md](deployment-checklist.md)。

## 1. 开始之前

确认这几件事已经成立：

- 宝塔里已经有网站 `www.cndistribution.com`，网站设置里的 SSL 已部署，并开启了强制 HTTPS。
- 这个网站现在的首页、业务页面保持原样。后面只往它的配置文件里加一段 `/cs/`。
- 安全里不要为 8080 添加放行。云厂商安全组同样不要放行 8080。容器只绑在 `127.0.0.1:8080`，公网流量都从 443 进来。

## 2. 安装 Docker

1. 登录宝塔，打开「软件商店」。
2. 搜索「Docker 管理器」，安装并启动。安装完成后面板左侧会出现「Docker」。
3. 打开「终端」，执行：

```bash
docker compose version
```

能打出版本号即可。若提示找不到命令，退出终端重新打开一次；仍没有，就在软件商店里重装 Docker 管理器。

## 3. 放置代码

代码放在网站根目录外面，例如：

```text
/www/cs-agent
```

网站根目录一般是 `/www/wwwroot/www.cndistribution.com`。客服系统不要放在这个目录下面，尤其不要放成 `/www/wwwroot/www.cndistribution.com/cs`。那个路径会被 Nginx 当成网站上的真实目录，`/cs/` 会列出文件或打开原站页面，反代不会生效。

在终端里：

```bash
mkdir -p /www/cs-agent
cd /www/cs-agent
```

把本仓库放到这个目录（git clone，或把压缩包解压到这里）。解压后应能看到 `docker-compose.yml`、`backend/`、`frontend/`。

## 4. 填写密钥并启动

仍在 `/www/cs-agent`：

```bash
cp backend/.env.example backend/.env
```

用宝塔「文件」打开 `/www/cs-agent/backend/.env`，至少改这三项：

| 变量 | 填什么 |
|---|---|
| `LLM_API_KEY` | LLM 密钥。留空时系统不会真正调用模型 |
| `ZHINI_REPLY_API_KEY` | 随机串，给知你快回插件做身份验证 |
| `SECRET_KEY` | 另一把随机串，用来签 JWT |

随机串在终端生成：

```bash
python3 -c "import secrets;print(secrets.token_hex(16))"
```

`SECRET_KEY` 再生成一次，两把不要相同。`SITE_DOMAIN` 保持 `www.cndistribution.com`，不要加 `https://`，也不要加 `/cs`。

然后构建并启动：

```bash
cd /www/cs-agent
docker compose up -d --build
```

首次构建要拉取镜像并安装依赖，可能要十几分钟。Compose 会强制这些值，`.env` 里写了也会被盖掉：

- `APP_ENV=production`：已修改的管理员密码不会在每次重启时被重置为 `admin123`
- `MOCK_ENABLED=false`
- `DOUYIN_CHANNEL=api`、`XHS_CHANNEL=api`
- `RPA_API_KEY` 置空。这套部署不走 RPA Worker

数据库、向量库、上传文件和自动备份都在 Docker 卷 `cs-data` 里。更新或重建容器时保留这个卷。

## 5. 确认容器已经起来

先在服务器本机看，这一步还没经过网站域名：

```bash
docker compose ps
curl -sS http://127.0.0.1:8080/api/health
```

`docker compose ps` 里 `cs-agent-frontend` 和 `cs-agent-backend` 都是 `running`。`curl` 返回的 JSON 里 `status` 为 `ok`，并且 `llm` 为 `true`。

`llm` 为 `false` 时，回到 `backend/.env` 检查 `LLM_API_KEY`，改完后执行：

```bash
docker compose up -d
```

本机 `curl` 不通时，先看日志，先不要改网站配置：

```bash
docker compose logs --tail 100 backend
docker compose logs --tail 50 frontend
```

## 6. 给现有站点加 `/cs/` 反代

用配置文件粘贴，不用「网站 → 反向代理」向导。向导生成的配置经常漏掉 `proxy_pass` 末尾的斜杠，并且默认打开缓存。少了斜杠时，浏览器的 `/cs/api/...` 会原样进容器，接口变成站点首页；开了缓存时，登录和健康检查会被缓存住。

1. 宝塔左侧「网站」。
2. 找到 `www.cndistribution.com`，点「设置」。
3. 左侧点「配置文件」。
4. 在 `server { ... }` 里面、`#REWRITE-END` 那一行的下面，粘贴下面整段。

80 和 443 如果在同一个 `server` 里，粘贴一次即可。强制 HTTPS 会把 80 跳到 443，真正处理请求的是 443。如果面板拆成两个 `server`，这段只加在 `listen 443` 的那个里面。

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

要点：

- `proxy_pass` 的地址以 `/` 结尾。宝塔收到 `/cs/api/health` 后，转给容器的是 `/api/health`。
- `^~` 让这段优先于宝塔自带的 js/css 过期缓存规则，否则 `/cs/assets/*.js` 可能被主站规则接走。
- `client_max_body_size 500m` 与读超时 300 秒要和容器里的 Nginx 一致。素材视频上限是 500MB，知识库和生成请求也会超过宝塔默认的 1MB 和 60 秒。
- 这段不要开 `proxy_cache`。

5. 点「保存」。宝塔会先执行 `nginx -t`。语法不对会拒绝保存，按提示改完再存。保存成功后配置已经重载。

伪静态里不要再写会接管 `/cs` 的 `rewrite`。已经用过「反向代理」向导的话，到「网站 → 设置 → 反向代理」里删掉指向 8080 的那条，避免和手写的 `location` 叠在一起。

## 7. 两层 Nginx 各管什么

```text
浏览器
  → 宝塔 Nginx（443，证书在这里）
      只匹配 /cs/，剥掉前缀后转到 127.0.0.1:8080
  → 容器里的 Nginx
      /api、/ws、/webhooks 转到后端
      其他路径当工作台页面
  → 后端 uvicorn，只在 Docker 网络内的 8000 端口
```

容器配置在 [frontend/nginx.conf](../frontend/nginx.conf)。宝塔如果没剥掉前缀、把 `/cs/api/...` 原样转进来，容器还会再剥一次。手写配置按上面的 `proxy_pass` 带斜杠即可。

页面和接口都在 `www.cndistribution.com` 上，`CORS_ORIGINS` 保持为空。

## 8. 浏览器验收

1. 打开 `https://www.cndistribution.com/`，确认原网站还是原来的页面。
2. 打开 `https://www.cndistribution.com/cs/`，应进入客服登录页，地址栏变成 `https://www.cndistribution.com/cs/login`。
3. 用 `admin` / `admin123` 登录，登录后立刻在设置里改掉密码。
4. 再打开 `https://www.cndistribution.com/cs/api/health`。`status` 为 `ok`，`llm` 为 `true`。
5. 登录后按 F12 看网络。应出现 `wss://www.cndistribution.com/cs/ws?token=`，状态是已连接。未登录时不应出现这条 WebSocket。

在服务器上也可以先看公网路径是否进了容器：

```bash
curl -sS https://www.cndistribution.com/cs/api/health
```

返回应和 `curl http://127.0.0.1:8080/api/health` 一致。

## 9. 知你快回

1. 回复来源选「使用自己的回复接口」。
2. 接口地址填 `https://www.cndistribution.com/cs/api/integrations/zhinikuaihui/reply`。
3. 身份验证填 `backend/.env` 里的同一把 `ZHINI_REPLY_API_KEY`。
4. 等待时间选 60 秒。
5. 点测试并保存。浏览器弹出该域名的访问授权时点允许。

工作台设置页里也会显示当前站点下的这条接口地址。

## 10. 更新和日志

代码更新后，在项目目录重建：

```bash
cd /www/cs-agent
docker compose up -d --build
```

看日志：

```bash
docker compose logs -f backend
docker compose logs -f frontend
```

`Ctrl+C` 只退出日志跟踪，不会停容器。

停机和启动：

```bash
docker compose stop
docker compose start
```

数据在卷 `cs-data`。`docker compose down` 可以停掉容器；加上 `-v` 会删掉数据库、向量库和上传文件。日常更新不要加 `-v`。

备份这个卷即可保留 SQLite、Chroma 和上传文件。卷的实际路径可在终端查看：

```bash
docker volume inspect cs-agent_cs-data
```

卷名以项目目录名为前缀。目录是 `/www/cs-agent` 时，卷名一般是 `cs-agent_cs-data`。

## 11. 常见故障

**本机 `curl http://127.0.0.1:8080/api/health` 返回 Nginx 的 502**

这是容器里的前端 Nginx 发出来的，说明它没有连上后端。先看进程是否还在：

```bash
docker compose ps
docker compose logs --tail 200 backend
```

`cs-agent-backend` 若是 `Restarting`，就是后端进程启动后立刻退出，8000 上没有服务。`docker compose up -d` 显示 Running 只表示容器刚被拉起，过几秒再执行一次 `docker compose ps`。日志里的 traceback 才是退出原因。改完依赖后需要 `docker compose up -d --build`，不能只 `up -d`。

**域名打开 `/cs/` 返回 502，但本机 curl 8080 已经是 JSON**

宝塔这段 `proxy_pass` 没保存上，或写到了只监听 80 的那个 `server` 里。本机 `curl http://127.0.0.1:8080/api/health` 应先返回 JSON，再查域名。

**打开 `/cs/` 仍是原网站，或变成目录列表**

网站根目录下存在名为 `cs` 的文件夹。把客服代码挪到 `/www/cs-agent`，删掉网站目录里的那个 `cs`。另外核对 `proxy_pass` 是否为 `http://127.0.0.1:8080/`，末尾有斜杠。

**登录请求 404，或 `/cs/api/health` 打开的是网站首页**

`/cs/api` 被主站接走了。确认配置里是 `location ^~ /cs/`，并且没有另一条反向代理用了不带结尾斜杠的 `proxy_pass http://127.0.0.1:8080`。

**登录页空白，控制台里 `/assets/...` 404**

静态资源跑到了网站根路径。生产镜像必须带 `VITE_BASE=/cs/` 构建。`frontend/Dockerfile` 里已经写了这个参数。在项目目录重新执行 `docker compose up -d --build`，然后强制刷新浏览器。正确的脚本地址形如 `/cs/assets/index-....js`。

**上传素材或知识库返回 413**

请求体在宝塔这一层被默认的 1MB 拦住了。确认手写的 `location ^~ /cs/` 里有 `client_max_body_size 500m`，保存后重载过 Nginx。

**能登录，但会话不刷新，WebSocket 立刻断开**

`location ^~ /cs/` 里要有 `Upgrade` 和 `Connection` 两行，并且 `proxy_read_timeout` 至少 300 秒。WebSocket 和页面走同一段 location，地址是 `/cs/ws`。

**改过的管理员密码，容器重启后又变回 `admin123`**

后端当时不是 `production`。用 Compose 启动时会强制 `APP_ENV=production`。确认容器是 `docker compose up` 拉起来的，而不是绕过 Compose 单独跑的后端镜像。可执行：

```bash
docker compose exec backend printenv APP_ENV
```

输出应为 `production`。

**`/cs/api/health` 里 `llm` 为 `false`**

`LLM_API_KEY` 为空或容器还是旧环境。改 `backend/.env` 后执行 `docker compose up -d`，再 curl 一次健康检查。
