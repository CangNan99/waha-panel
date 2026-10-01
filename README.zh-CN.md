# WhatsAPP AI管理面板

基于 WAHA + Docker 的 WhatsApp 多会话 AI 管理面板，支持 AI 自动回复、聊天管理、人工接管以及图片和文字发送。通过一键安装脚本即可完成环境准备、配置生成和服务启动。

## 一键搭建

### Linux/macOS

在终端执行以下命令，脚本会从 GitHub `main` 分支下载并启动面板：

```bash
curl -fsSL https://raw.githubusercontent.com/CangNan99/waha-panel/main/bootstrap.sh | bash
```

### Windows

请先安装 Docker Desktop 和 PowerShell 7，然后执行：

```powershell
git clone --branch main --depth 1 https://github.com/CangNan99/waha-panel.git waha-panel
Set-Location -LiteralPath .\waha-panel
pwsh -File .\install.ps1
```

首次安装会自动生成 WAHA API 密钥、webhook 密钥、面板数据加密密钥和管理员初始密码。初始管理员密码只在安装脚本的首次成功输出中显示；不要把 `.env` 或 `secrets/` 目录上传到公共仓库。

这是空白业务安装，不会带入现有 WhatsApp 会话、二维码、聊天、客户资料、AI 密钥、WooCommerce、PayPal、产品或订单数据。

## 安装完成后

1. 打开面板：<http://127.0.0.1:3003/>
2. 使用安装脚本首次输出的管理员账号和密码登录。
3. 创建 WhatsApp 会话并扫码连接。
4. 进入聊天管理，配置 AI 自动回复或进行人工接管。

首次认证成功后，同一浏览器配置文件会保持 7 天登录状态；关闭并重新打开浏览器不会立即要求重新登录。清除该站点 Cookie、换浏览器或换设备后需要重新认证。

面板和 WAHA 通过 Docker 内部网络连接。默认只绑定本机端口：面板为 `127.0.0.1:3003`，WAHA 为 `127.0.0.1:3002`。数据保存在命名 Docker 卷中，分别为会话、媒体和 SQLite 面板数据，没有额外数据库服务。

## 主要能力

- 多 WhatsApp 会话统一管理
- AI 自动回复、摘要和标签
- 按客户独立保存的加密动态记忆（在自动回复设置中开启）
- 聊天列表、消息收发和人工接管
- 当前聊天的阿里云机器翻译双行显示（客户原文/客服原文 + 中文）
- 打开聊天时定位最新消息，新消息到达时自动跟随
- 图片、文字等消息发送
- Docker 卷持久化、备份和独立更新
- 本地端口部署，可配合 Nginx 反向代理和 HTTPS 使用

## 客户记忆与当前聊天翻译

在“自动回复设置”中开启“启用客户动态记忆”后，面板会按会话和客户分别保存加密的对话摘要、客户偏好、未解决事项，以及有效的人工回复风格。记忆会在收到新消息或产生回复后排队迭代，后续 AI 自动回复会读取已保存的摘要；人工消息和 AI 消息都会进入记忆整理范围。关闭开关会停止使用和更新记忆，但不会删除已有内容，需要在对应客户的记忆操作中清除。

聊天页面顶部的“翻译设置”用于配置阿里云机器翻译。翻译开关按客户独立保存，默认关闭；在聊天顶部点击“全局翻译：关闭”后，只处理当前打开的聊天，并将非中文的客户、AI 或人工消息显示为原文和中文译文两行。切换客户、返回最近聊天列表或关闭页面时会停止当前翻译请求，未打开的聊天不会发送到翻译服务。销售辅助 AI 的设置仍单独保留，不会替代阿里云机器翻译。

## 更新

更新检查按钮只读取 Docker Hub 的公开稳定标签，不会自动修改服务。宿主机执行以下命令即可单独更新：

```bash
./update.sh panel
./update.sh waha
```

PowerShell 7：

```powershell
pwsh -File .\update.ps1 -Component panel
pwsh -File .\update.ps1 -Component waha
```

脚本会保留另一个服务和所有持久化卷。WAHA 更新会重建 WAHA 容器，面板更新只重建面板容器。更新前会保留 `.env` 备份。

## 历次更新记录

后续版本按新到旧追加到表格顶部，完整说明见对应发布文档。

| 版本 | 更新内容 | 详细说明 |
| --- | --- | --- |
| 1.0.12 | 增加按客户动态记忆、阿里云当前聊天双行翻译、最近聊天滚动位置保持和翻译/人工接管状态呼吸动画。 | [docs/releases/1.0.12.md](docs/releases/1.0.12.md) |
| 1.0.11 | 过滤无内容空消息气泡；打开聊天定位最新消息并在底部跟随新消息；增加固定 7 天管理员登录状态、退出和会话撤销。 | [docs/releases/1.0.11.md](docs/releases/1.0.11.md) |
| 1.0.10 | 修复聊天消息实时更新时拖拽导致的频闪和滚动位置被旧请求覆盖；按消息引用复用列表节点；优化二维码待刷新状态的白色压花磨砂层、不可扫描虚化定位块和中心提示卡显影。 | [docs/releases/1.0.10.md](docs/releases/1.0.10.md) |
| 1.0.9 | 修复管理员新增账号失败；增加当前 WhatsApp 账号头像代理与缓存；修复移动端顶部布局、头像闪烁，并区分客服/客户翻译来源。 | [docs/releases/1.0.9.md](docs/releases/1.0.9.md) |
| 1.0.8 | 修复 WhatsApp CDN 头像加载；将二维码未刷新占位改为磨砂纹理层；更新 WAHA 至 `latest-2026.9.1`。 | [docs/releases/1.0.8.md](docs/releases/1.0.8.md) |
| 1.0.7 | 优化客户备注、标签和头像展示；增加双向上下文、媒体消息开关、移动端聊天布局与扫码/手机号配对动效；统一面板品牌。 | [docs/releases/1.0.7.md](docs/releases/1.0.7.md) |
| 1.0.6 | 修复并发认证导致 SQLite 锁定；合并相同凭据的并发认证请求，并增加有上限、带过期时间的摘要缓存。 | [docs/releases/1.0.6.md](docs/releases/1.0.6.md) |
| 1.0.5 | 面板内存上限提升至 512 MiB；增加 Argon2 并发控制和渐进式重哈希，降低多对话页面的瞬时内存压力。 | [docs/releases/1.0.5.md](docs/releases/1.0.5.md) |
| 1.0.4 | 面板容器增加 256 MiB 内存限制，修复低内存环境下 Argon2 管理员认证失败。 | [docs/releases/1.0.4.md](docs/releases/1.0.4.md) |
| 1.0.3 | 新增多会话聊天管理、跟进任务和二维码状态保护。 | [docs/releases/1.0.3.md](docs/releases/1.0.3.md) |

## 备份与数据

```bash
./backup.sh
```

```powershell
pwsh -File .\backup.ps1
```

备份结果位于 `backups/`，包含面板 SQLite、WAHA 会话和媒体卷的压缩文件。备份目录可能包含敏感业务数据，应限制访问权限。

## 详细文档

- [中文安装与运维文档](INSTALL.zh-CN.md)
- [GitHub 仓库](https://github.com/CangNan99/waha-panel)
- [Docker Hub：面板镜像](https://hub.docker.com/r/cangnan88/waha-panel)
- [Docker Hub：WAHA 镜像](https://hub.docker.com/r/devlikeapro/waha)

当前镜像版本：

- `devlikeapro/waha:latest-2026.9.1`
- `docker.io/cangnan88/waha-panel:1.0.12`

如需域名访问，只反代到面板端口 `127.0.0.1:3003`。WAHA API 端口 `3002` 默认不应公开。请在反向代理层启用 HTTPS，并确保代理不会记录 `Authorization`、APIKey 或密码字段。
