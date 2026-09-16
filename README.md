# WAHA + 本地管理面板便携版

完整中文安装、配置、反向代理、备份、更新和故障排查请阅读：[INSTALL.zh-CN.md](INSTALL.zh-CN.md)。

本发布包包含两个可独立更新的服务：

- `devlikeapro/waha:latest-2026.8.2`
- `docker.io/cangnan88/waha-panel:1.0.2`

面板和 WAHA 通过 Docker 内部网络连接。默认只绑定本机端口：面板为 `127.0.0.1:3003`，WAHA 为 `127.0.0.1:3002`。数据保存在命名 Docker 卷中，分别为会话、媒体和 SQLite 面板数据。没有额外数据库服务。

## 安装

Linux/macOS 一键安装（默认从 GitHub `Cangnan99/waha-panel` 的 `main` 分支下载）：

```bash
curl -fsSL https://raw.githubusercontent.com/Cangnan99/waha-panel/main/bootstrap.sh | bash
```

也可以先下载仓库后执行本地脚本：

Linux/macOS：

```bash
chmod +x install.sh update.sh backup.sh
./install.sh
```

Windows PowerShell 7：

```powershell
pwsh -File .\install.ps1
```

首次安装会在本机生成 WAHA API 密钥、webhook 密钥、面板数据加密密钥和管理员初始密码。初始管理员密码只在安装脚本的首次成功输出中显示；不要把 `.env` 或 `secrets/` 目录上传到公共仓库。

`bootstrap.sh` 默认在当前目录创建 `waha-panel/`。需要指定目录或分支时，可在执行前设置 `WAHA_PANEL_INSTALL_DIR` 或 `WAHA_PANEL_REF`；安装目录已经存在时，直接进入该目录执行 `./install.sh`。

这是空白业务安装：不会带入现有 WhatsApp 会话、二维码、聊天、客户资料、AI 密钥、WooCommerce、PayPal、产品或订单数据。登录面板后可以创建会话并按会话填写设置。

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

## 备份

```bash
./backup.sh
```

```powershell
pwsh -File .\backup.ps1
```

备份结果位于 `backups/`，包含面板 SQLite、WAHA 会话和媒体卷的压缩文件。备份目录可能包含敏感业务数据，应限制访问权限。

## 反向代理

如需域名访问，只反代到面板端口 `127.0.0.1:3003`。WAHA API 端口 `3002` 默认不应公开。请在反向代理层启用 HTTPS，并确保代理不会记录 `Authorization`、APIKey 或密码字段。

## 赞助入口

默认开启。将 `.env` 中的 `PANEL_SPONSOR_ENABLED` 改为 `0` 后重建面板容器即可关闭，入口会出现在会话列表下方，图片地址由 `PANEL_SPONSOR_IMAGE_URL` 控制。默认地址为用户指定的公开图片地址。
