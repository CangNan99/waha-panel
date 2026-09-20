# WAHA + 本地管理面板安装与使用指南

本指南适用于 GitHub 仓库 `CangNan99/waha-panel` 的便携发布版。它安装两个可以独立更新的 Docker 服务：

- WAHA：`devlikeapro/waha:latest-2026.8.2`
- 管理面板：`docker.io/cangnan88/waha-panel:1.0.5`

面板默认仅监听服务器本机，不会把管理端口直接暴露到公网。面板使用 SQLite，不部署额外数据库服务；数据通过 Docker 命名卷持久化保存。

## 1. 安装前确认

### Linux 或 macOS

需要具备：

- 已启动的 Docker Engine；
- Docker Compose v2，可执行 `docker compose version`；
- Bash、`curl`、`tar`、`openssl`；
- 更新脚本还需要 `python3`；
- 当前用户有权访问 Docker，或能使用 `sudo`。

服务器需要能够访问 GitHub 和 Docker Hub。若服务器使用代理或 VPN，代理必须同时对 Docker daemon 生效；宿主机终端能联网不代表 Docker daemon 一定能联网。

### Windows

需要安装并启动 Docker Desktop，建议启用 WSL2 后端，并安装 PowerShell 7。确认下面命令可以正常执行：

```powershell
docker compose version
pwsh --version
```

不要把生产环境的 `.env`、`secrets`、备份包或数据库文件放进公开 Git 仓库。

## 2. 一键安装

### Linux 或 macOS

在准备安装的目录执行：

```bash
curl -fsSL https://raw.githubusercontent.com/CangNan99/waha-panel/main/bootstrap.sh | bash
```

脚本会在当前目录创建 `waha-panel/`，下载 GitHub `main` 分支内容，然后执行 `install.sh`。安装目录已经存在时，脚本会停止，以免覆盖现有文件；此时应进入原目录执行安装脚本：

```bash
cd waha-panel
./install.sh
```

需要指定安装目录或分支时，先导出变量再运行：

```bash
export WAHA_PANEL_INSTALL_DIR=/opt/waha-panel
export WAHA_PANEL_REF=main
curl -fsSL https://raw.githubusercontent.com/CangNan99/waha-panel/main/bootstrap.sh | bash
```

首次安装会自动完成以下工作：

1. 检查 Docker Compose 和 OpenSSL；
2. 创建权限受限的 `secrets/` 目录；
3. 生成 WAHA API 密钥、webhook 密钥、面板数据加密密钥和管理员初始密码；
4. 拉取 WAHA 与面板镜像；
5. 启动两个服务，并最长等待 90 秒确认面板响应。

### Windows PowerShell 7

先获取仓库：

```powershell
git clone --branch main --depth 1 https://github.com/CangNan99/waha-panel.git waha-panel
Set-Location -LiteralPath .\waha-panel
```

再使用 PowerShell 7 安装：

```powershell
pwsh -File .\install.ps1
```

也可以把仓库 ZIP 下载并解压后，在解压目录执行同一条 `pwsh -File .\install.ps1`。不要在 Windows PowerShell 5.1 中替代 PowerShell 7 执行。

## 3. 安装完成后的地址与凭据

安装成功时脚本会显示：

- 面板：`http://127.0.0.1:3003/`
- WAHA 原生 Dashboard/API：`http://127.0.0.1:3002/`
- 面板初始管理员账号：`admin`
- 面板初始管理员密码：仅在首次成功安装输出中显示一次

面板和 WAHA 的管理员密码是两套独立凭据。WAHA Dashboard 的账号信息保存在本机受保护的 `secrets/waha_credentials` 文件中；该文件只应在服务器本机私下读取，不要复制到聊天、日志、截图、工单或 GitHub。

浏览器第一次访问面板时会出现 HTTP Basic Auth 登录提示。输入面板管理员账号和密码即可进入。安装脚本再次运行时不会重新生成已有 `.env` 或管理员账号，原有凭据会保留。

如果初始面板密码丢失，不要删除 Docker 卷或重新初始化整个目录。使用已经登录的面板，在右上角“管理员”中新增账号或修改密码；没有任何可用管理员时，应先备份，再按现场维护流程恢复管理员访问。

## 4. 检查服务和持久化

在安装目录执行：

```bash
docker compose ps
docker volume ls --filter name=waha-release_
docker network ls --filter name=waha-release-internal
```

正常情况下应看到 `waha` 和 `waha-panel` 两个容器处于运行状态，以及下面三个卷：

- `waha-release_sessions`：WAHA 会话认证状态；
- `waha-release_media`：WhatsApp 媒体文件；
- `waha-release_panel_data`：面板 SQLite、知识库、聊天记录和系统记录。

发布包默认创建内部网络 `waha-release-internal`。面板在容器内通过 `http://waha:3000` 访问 WAHA，宿主机端口只是映射入口。

验证重启后数据仍可读取：

```bash
docker compose restart
docker compose ps
```

重新打开面板，确认页面显示“数据库正常”，并检查会话列表、设置和系统记录仍然存在。`docker compose restart` 不会删除命名卷。

Windows PowerShell 7 对应命令：

```powershell
docker compose ps
docker volume ls --filter name=waha-release_
docker compose restart
docker compose ps
```

不要使用 `docker compose down -v` 做普通重启或更新，它可能删除持久化卷。

## 5. 创建并连接 WhatsApp 会话

### 创建会话

1. 打开面板首页 `/`。
2. 左侧“会话”区域默认会显示 `default`。点击“创建 / 启动”，或点击“新建”创建其他会话。
3. 新建会话时填写技术名称和显示名称。技术名称只能使用字母、数字、点、下划线和短横线，长度为 1 至 64 个字符；显示名称可以之后编辑。
4. 每个会话都可以单独启动、停止、重启、查看二维码和打开聊天管理。

创建会话时，面板会自动写入内部 webhook 地址和来源校验配置。建议始终从面板创建新会话，不要直接在 WAHA 中创建后再期待面板自动补齐全部配置。

### 使用二维码连接

1. 在左侧选择目标会话。
2. 点击“启动”或“创建 / 启动”。
3. 会话状态变为“等待扫码”后，点击“刷新二维码”。
4. 在手机 WhatsApp 中打开“已关联设备”，选择“关联设备”，扫描面板显示的二维码。
5. 等待状态变为“已连接”。

二维码只由面板代理给当前已认证的管理员浏览器，浏览器不会获得 WAHA API 密钥。会话已经连接时，二维码可能暂不可用，这是正常现象。

### 使用手机号配对码

二维码无法使用时，可以在当前会话的“手机号配对码”区域输入包含国家/地区码的手机号，例如 `8613812345678`，再点击“获取配对码”。然后按照手机 WhatsApp 中“使用手机号关联”或类似入口的提示输入配对码。

这里显示的是 WhatsApp 的设备配对码，不是短信验证码。手机号只能填写数字和国家/地区码，不能填写订单号或带空格的展示文本。配对码通常有时效，应生成后尽快在手机端输入。

## 6. 多会话和会话隔离

每个会话拥有独立的：

- WAHA 技术会话和连接状态；
- 显示名称；
- 自动回复开关、全时段开关和每周时间段；
- 固定回复文案、人设、系统提示词和知识库；
- AI 模型地址、模型名和 API Key；
- 聊天记录、人工接管状态和系统处理记录。

切换左侧会话后，点击“设置此会话”进入对应设置。不要把一个会话的 AI Key 或知识库误填到另一个会话。

## 7. 自动回复设置

点击首页“自动回复设置”，或在多会话首页点击当前会话的“设置此会话”。设置保存到现有 SQLite 数据库。

### 开关和时间

- “自动回复”默认关闭。关闭时不会自动发送消息；
- “全局总开关/全时段”打开后，当前会话全天回复，并覆盖每周时间段；
- 未打开全时段时，只在启用的每周时间段内回复；
- 时间统一按 `Asia/Shanghai` 判断；
- 结束时间早于开始时间表示跨天，例如 `20:00 → 11:00` 覆盖当天晚上到次日早上；
- 修改后必须点击“保存全部设置”。

### 固定文案与 AI

填写固定回复文案后，未配置完整 AI 模型，或 AI 调用失败时，系统使用固定文案作为回复或兜底回复。AI 配置需要同时填写：

- 服务地址：OpenAI 兼容 API 的基础地址，例如 `https://example.com/v1`；
- 模型名：服务端实际支持的模型名称；
- API Key：只在表单提交时发送给后端。

API Key 只在后端持久化并由后端调用 AI，不会在设置回显、二维码地址或普通日志中显示。面板 SQLite 及其备份仍应按敏感文件保护。API Key 输入框留空表示保持已保存的 Key；勾选“清除已保存的 API Key”才会删除它。不要把 API Key 写进知识库、人设、系统提示词、截图或公共文档。

当前版本会读取当前客户最近的对话上下文和该会话知识库，再将人设、系统提示词等作为 AI 请求上下文。自动回复还具有随机拟人延迟：冷对话发送前约 20 至 100 秒，热对话发送前约 1 至 5 秒，随后使用约 6 至 15 秒的随机打字延迟。

### 知识库

知识库支持两种方式：

- 在页面粘贴文字；
- 上传 `.txt` 或 `.md` 文件。

资料会保存到面板 SQLite，并按会话隔离。页面可以查看资料摘要、查看内容和删除资料。只放业务说明、产品资料和客服规则；不要放密码、Token、支付密钥、客户完整敏感资料或与当前会话无关的机密内容。

### 自动回复处理范围

系统只处理别人发来的私聊文字消息，并会忽略：

- 自己发出的消息；
- 群聊；
- 状态消息；
- 图片、视频、音频、文件等媒体消息；
- 空消息；
- 已处理过的重复消息。

聊天管理中对某个客户执行“人工接管”后，该客户的 AI 自动回复会暂停；人工处理完毕后点击“恢复 AI 回复”。人工发送文字或图片也会暂停该客户的 AI 自动回复。

## 8. 聊天管理和翻译

在多会话首页选择会话后点击“聊天管理”。聊天管理页面提供最近聊天列表、消息上下文、人工接管、恢复 AI 回复、人工发送文字和图片等功能。

页面中的翻译与建议功能使用一套全局翻译 AI 配置，与每个会话的自动回复 AI 独立。点击聊天页面的“翻译设置”，填写服务地址、模型名和 API Key 后可以测试连接。翻译和生成建议时，当前消息或最近消息以及相关知识会发送到配置的第三方 AI 服务；翻译 Key 同样只由后端加密保存并隐藏回显。

生成的客户语言内容是可编辑建议，不会未经人工确认自动发送。生成建议时会读取当前客户的最近聊天上下文。

## 9. 可选：WooCommerce、WordPress 和 PayPal

订单查询功能默认关闭，不影响普通自动回复。需要启用时打开 `/commerce` 订单与支付管理页面，按页面字段填写并先执行“测试连接”。敏感凭据只支持替换，不会读取旧值或回显。

常见字段包括：

- WooCommerce 站点基础地址；
- WooCommerce Consumer Key 和 Consumer Secret；
- PayPal Sandbox 或 Live，以及 Client ID 和 Client Secret；
- WordPress 数据库主机、端口、数据库名、表前缀；
- WordPress 只读数据库账号和密码；
- 管理员 WhatsApp 号码，填写国家/地区码。

WordPress 与本发布包位于同一宝塔服务器时，面板容器中的 `127.0.0.1` 指向面板容器本身，不是宿主机，也不是 WordPress 容器。数据库主机应填写面板容器实际可访问的数据库容器名、同一 Docker 网络中的服务名，或服务器可路由的地址。不要为了方便把 MySQL 暴露到公网；应让面板容器所在网段最小范围访问 `3306`，并使用只读数据库账号。

启用订单查询后，AI 只会在需要查询订单或支付状态时调用订单工具。身份核对失败、查询不到结果或支付状态存在冲突时，系统会建立人工工单并通知管理员；完整订单地址、邮箱、电话和交易标识只在通过规定的客户身份核验后提供给 AI 使用。不要把这些资料写入普通日志或知识库。

## 10. 反向代理和域名访问

默认端口映射如下：

| 用途 | 宿主机监听 | 容器端口 | 建议 |
| --- | --- | --- | --- |
| 管理面板 | `127.0.0.1:3003` | `3001` | 可由同机 Nginx/宝塔反代 |
| WAHA Dashboard/API | `127.0.0.1:3002` | `3000` | 不要直接公开 |

在宝塔中创建反向代理站点时，将域名上游设置为 `http://127.0.0.1:3003`，为域名配置 HTTPS。不要把域名上游设置为 `http://waha-panel:3001`，因为这个容器名只在 Docker 内部网络可解析。

如果手动使用 Nginx，可参考下面的非敏感配置，并按实际域名修改：

```nginx
location / {
    proxy_pass http://127.0.0.1:3003;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

反向代理必须转发 `Authorization` 请求头，否则面板管理员认证会失败。HTTPS、Basic Auth 和面板的来源校验必须同时保留。不要把 WAHA 的 `3002` 端口一并代理到公网，也不要把 `/api/webhook` 当作无校验公开接口。

## 11. 检查更新和独立更新

首页的“检查更新”按钮只读取 Docker Hub 公开版本信息，不会自动下载镜像，不会重启服务。

### Linux 或 macOS

在安装目录执行：

```bash
./update.sh panel
./update.sh waha
```

也可以只更新其中一个组件。脚本会读取公开稳定标签、备份当前 `.env`，再只重建所选服务：

- `panel` 只更新 `waha-panel`；
- `waha` 只更新 `waha`；
- 另一个服务不会被重建；
- 三个 Docker 持久化卷不会被删除。

更新脚本会在安装目录留下 `.env.before-update-日期时间` 备份。该文件含有敏感配置，必须像 `.env` 一样保护，不能提交到 GitHub。

### Windows PowerShell 7

```powershell
pwsh -File .\update.ps1 -Component panel
pwsh -File .\update.ps1 -Component waha
```

更新前建议先执行备份，并在更新后检查：

```powershell
docker compose ps
docker compose logs --tail=100 waha-panel
docker compose logs --tail=100 waha
```

WAHA 会话和媒体卷会保留，但大版本更新前仍应先备份并阅读对应 WAHA 发布说明。

## 12. 备份

备份前确认 Docker 正常运行。为避免备份时 SQLite 或会话数据仍在写入，生产环境建议选择维护窗口，先停止服务再备份：

```bash
docker compose stop
./backup.sh
docker compose start
```

如果能够确认备份期间没有新消息或配置写入，也可以直接在安装目录执行：

```bash
./backup.sh
```

Windows PowerShell 7：

```powershell
docker compose stop
pwsh -File .\backup.ps1
docker compose start
```

备份会在 `backups/日期时间/` 生成三个压缩包：

- `panel_data.tar.gz`：SQLite、知识库、聊天和系统记录；
- `sessions.tar.gz`：WAHA 登录会话；
- `media.tar.gz`：媒体文件。

备份目录可能包含客户资料、聊天、会话凭据和 API 配置，应限制文件权限并保存到受控位置。备份同时依赖 `.env`、`secrets/` 和三个压缩包；不要只保存 SQLite 而丢失加密密钥或 WAHA 会话卷。

恢复前必须停止相关服务、确认备份来源和目标卷完全对应。发布包没有自动恢复脚本；不熟悉 Docker 卷恢复时，不要直接覆盖生产卷，应先复制到临时目录验证。

## 13. 可选：显示赞助入口

赞助入口默认开启。如需关闭，在安装目录的 `.env` 中设置：

```dotenv
PANEL_SPONSOR_ENABLED=0
PANEL_SPONSOR_IMAGE_URL=https://www.6spring.com/wp-content/uploads/2026/09/cangnan.jpg
```

再只重建面板容器：

```bash
docker compose up -d --force-recreate waha-panel
```

该设置不会改变 WAHA 会话、SQLite 或其他服务。

## 14. 卸载与清理

只停止并删除容器、保留数据：

```bash
docker compose down
```

确认已经完成备份且不再需要数据后，才考虑删除命名卷。默认发布包卷名如下：

```bash
docker volume rm waha-release_sessions waha-release_media waha-release_panel_data
```

删除卷会永久删除 WhatsApp 会话、媒体、SQLite、知识库、聊天和系统记录，通常无法撤销。不要把 `docker compose down -v` 用作日常重启、排错或更新命令。

## 15. 常见问题排查

### `docker compose pull` 失败

先检查 Docker daemon 是否运行，以及服务器是否能访问 GitHub 和 Docker Hub：

```bash
docker info
docker compose config --services
```

不要运行或粘贴完整的 `docker compose config` 输出，因为展开后的环境变量可能包含敏感值。网络代理必须配置给 Docker daemon；只配置当前终端代理可能仍然无效。

### 面板返回 401

这是面板管理员认证的预期行为。确认使用的是“面板初始管理员”密码，而不是 WAHA Dashboard 密码。若浏览器缓存了旧的 Basic Auth 凭据，退出浏览器或清除该站点凭据后重新登录。

### 面板或 WAHA 容器没有运行

查看最近日志，不要把包含密钥的环境文件作为排查材料发送出去：

```bash
docker compose ps
docker compose logs --tail=100 waha-panel
docker compose logs --tail=100 waha
```

如果面板提示 `WAHA_API_KEY 未配置`，检查 `.env` 和 `secrets/waha_credentials` 是否仍在安装目录中，文件是否被移动、改名或被空文件覆盖。不要删除 SQLite 卷来解决启动问题。

### 二维码不可用

确认目标会话已经“创建 / 启动”，并且状态不是“已连接”。等待状态进入“等待扫码”后再刷新二维码。如果 WAHA 不可用，先查看 `waha` 容器日志和首页 WAHA 状态。

### 手机号配对码失败

确认手机号含国家/地区码、只使用数字、长度有效，并且目标会话已启动。配对码不是短信验证码；如果手机端没有“使用手机号关联”入口，先更新 WhatsApp 客户端或使用二维码方式。

### 自动回复没有发送

按下面顺序检查：

1. 首页左侧选择的是正确会话；
2. 该会话已连接；
3. “自动回复”已开启，或“全时段”已开启；
4. 当前时间符合 `Asia/Shanghai` 下的时间段；
5. 当前客户没有处于“人工接管中”；
6. 消息是别人发来的私聊文字，而不是群聊、状态或媒体；
7. 消息没有被去重记录判定为重复；
8. 会话是从面板创建的，webhook 已写入；
9. 面板“系统记录”没有 webhook 来源校验或 WAHA 调用错误。

即使 AI 未配置，满足开关和时间条件时也应使用固定文案。若直接使用固定文案，检查 AI 地址、模型名和后端 API Key 状态；AI 未配置或调用失败时使用固定文案是设计行为。

### 反向代理出现 502、认证失败或实时聊天不更新

确认上游是宿主机的 `127.0.0.1:3003`，并保留 `Host`、`X-Forwarded-Proto` 和 `Authorization` 请求头。实时聊天需要关闭代理缓冲并提高读取超时。不要把容器内部地址 `waha-panel:3001` 填到宿主机宝塔反代中。

### 更新检查失败

首页更新检查依赖 Docker Hub 公开接口。检查服务器出口网络、DNS、TLS 和 Docker Hub 可用性；检查失败不会修改当前服务。可以在宿主机稍后重试，或在确认备份后按指定组件执行更新脚本。

## 16. 安全清单

- 保留默认本机绑定；只有在确有需要时通过 HTTPS 反代面板；
- 使用长度不少于 12 个字符的面板管理员密码，并在首次登录后修改或新增专用账号；
- `.env`、`secrets/`、`backups/`、SQLite 和 Docker 卷都视为敏感数据；
- 不在日志、截图、浏览器地址、知识库或 GitHub 中显示 API Key、密码、Token、webhook 密钥和数据库密码；
- WordPress 数据库使用只读账号，只允许面板容器所在网段访问必要的 `3306`；
- 不直接公开 WAHA `3002` 端口；
- 更新前备份，更新后确认两个容器和数据库状态；
- 删除卷前先确认备份可读，并明确这是不可逆的数据删除操作。

## 17. 发布资料

- GitHub：<https://github.com/CangNan99/waha-panel>
- 本安装引导脚本：<https://raw.githubusercontent.com/CangNan99/waha-panel/main/bootstrap.sh>
- 面板镜像：<https://hub.docker.com/r/cangnan88/waha-panel>
- WAHA 镜像：<https://hub.docker.com/r/devlikeapro/waha>

安装包是空白业务发布版，不包含任何现有 WhatsApp 会话、客户资料、订单、产品、WooCommerce、PayPal 或 AI API Key。完成安装后，再由管理员按实际业务逐项配置。
