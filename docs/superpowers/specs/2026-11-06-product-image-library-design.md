# 产品图片库与 AI 自动发图设计

日期：2026-11-06
状态：已获用户批准，等待执行计划和实施
适用版本：当前生产 1.0.24，实施版本建议为 1.0.25

## 目标

在现有 WAHA 管理面板中增加一个全局共享的产品图片库。管理员上传图片并绑定产品标签后，AI 可以在客户明确询问对应产品时，按严格标签命中规则直接把产品图片发送给当前客户。

图片库必须复用现有 SQLite、`panel_data` 持久化卷、图片校验、管理员认证、CSRF、聊天发送和系统日志能力；不新增对象存储、CDN 或公开图片 URL。

## 非目标和失败边界

- 本期不做 OCR 或视觉模型自动打标签。
- 本期不引入对象存储、图片 CDN 或外部图片服务。
- AI 不读取服务器路径、文件内容 URL 或客户身份字段。
- AI 不根据模糊兴趣、历史消息或知识库猜测产品图片。
- 没有严格标签命中、图片已停用、标签对应主图不唯一、会话不允许自动回复或发送结果未知时，不发送图片。
- 本期只自动发送一张主图，不做多图轮播。
- 产品价格、库存、优惠和产品承诺仍由现有业务数据和人工规则处理，不由图片库推断。

## 当前项目约束

- 面板使用 SQLite，数据通过 `panel_data` Docker 卷持久化；现有迁移由 `panel/app.py:init_db()` 顺序执行。
- `ChatService` 已有 JPG/PNG/WebP 校验、大小和像素限制、WAHA `sendImage`、聊天引用校验和人工发送幂等状态机。
- AI `_ai_reply()` 当前允许一次受控函数调用，工具执行后由后端完成发送；因此本期使用一个 `send_product_image` 工具，不实现“先搜索、再二次工具调用”的多轮协议。
- 当前 AI 工具包括 IPTV 和订单查询，工具参数必须由后端严格校验；产品图片工具遵循同样边界。
- 生产升级只替换面板容器，WAHA、会话卷、媒体卷和面板数据卷保持不变。

## 选定方案

采用面板内置产品图片库：图片二进制保存在 `/app/data/product-images`，SQLite 保存元数据、标签和发送审计。管理员通过新的产品图片库页面维护资产；AI 通过 `send_product_image` 按产品标签发送。

自动发送权限按用户要求直接开启，但保留一个管理员可关闭的全局杀开关和每日上限。没有图片资产时工具不会出现在 AI 请求中；有资产时只有严格命中、唯一主图和当前会话允许自动回复才会发送。

## 方案比较

- **面板内置图片库（选定）**：复用现有 SQLite、`panel_data` 卷、管理员认证和图片发送能力；不增加外部服务，备份和回滚边界清晰，适合当前图片规模。
- **把所有缩略图直接放入 AI 上下文**：初期代码少，但会增加模型输入成本和延迟，图片多时容易误选，无法可靠处理下架、主图和权限，不适合作为正式方案。
- **对象存储和 CDN**：更适合大量图片和多实例，但需要新增凭据、公开访问控制、缩略图、生命周期和网络故障处理，超出当前项目的必要范围。

选择第一种方案的原因是它能在现有面板数据卷和 WAHA 发送边界内完成全流程，同时把图片检索、发送和回滚都留在项目可验证的本地状态机里。

## 数据模型

新增迁移 `panel/migrations/014_product_image_library.sql`，创建以下表：

### `product_image_settings`

- `id INTEGER PRIMARY KEY CHECK (id = 1)`
- `auto_send_enabled INTEGER NOT NULL DEFAULT 1 CHECK (auto_send_enabled IN (0,1))`
- `daily_limit INTEGER NOT NULL DEFAULT 20 CHECK (daily_limit BETWEEN 1 AND 1000)`
- `updated_at INTEGER NOT NULL`

### `product_image_assets`

- `id TEXT PRIMARY KEY`
- `title TEXT NOT NULL`
- `caption_template TEXT NOT NULL DEFAULT ''`
- `relative_path TEXT NOT NULL UNIQUE`
- `original_filename TEXT NOT NULL`
- `mime_type TEXT NOT NULL CHECK (mime_type IN ('image/jpeg','image/png','image/webp'))`
- `byte_size INTEGER NOT NULL`
- `width INTEGER NOT NULL`
- `height INTEGER NOT NULL`
- `sha256 TEXT NOT NULL UNIQUE`
- `active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1))`
- `sort_order INTEGER NOT NULL DEFAULT 0`
- `created_at INTEGER NOT NULL`
- `updated_at INTEGER NOT NULL`

### `product_image_tags`

- `id INTEGER PRIMARY KEY AUTOINCREMENT`
- `tag_key TEXT NOT NULL UNIQUE`
- `display_name TEXT NOT NULL`
- `created_at INTEGER NOT NULL`
- `updated_at INTEGER NOT NULL`

### `product_image_asset_tags`

- `asset_id TEXT NOT NULL REFERENCES product_image_assets(id) ON DELETE CASCADE`
- `tag_id INTEGER NOT NULL REFERENCES product_image_tags(id) ON DELETE CASCADE`
- `priority INTEGER NOT NULL DEFAULT 100`
- `is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0,1))`
- `created_at INTEGER NOT NULL`
- `UNIQUE(asset_id, tag_id)`

### `product_image_send_requests`

- `request_id TEXT PRIMARY KEY`
- `session_name TEXT NOT NULL`
- `chat_key_hmac TEXT NOT NULL`
- `message_key TEXT NOT NULL`
- `asset_id TEXT NOT NULL REFERENCES product_image_assets(id)`
- `tag_key TEXT NOT NULL`
- `caption_hash TEXT NOT NULL`
- `payload_hash TEXT NOT NULL`
- `state TEXT NOT NULL CHECK (state IN ('PENDING','SENT','FAILED','UNKNOWN'))`
- `waha_message_id TEXT`
- `error_code TEXT`
- `created_at INTEGER NOT NULL`
- `updated_at INTEGER NOT NULL`
- `UNIQUE(session_name, chat_key_hmac, message_key, payload_hash)`

图片文件使用 `product-images/<sha256 前缀>/<asset_id>.<规范扩展名>` 的相对路径，先写临时文件再原子替换；重复 SHA256 上传返回已有资产，不静默覆盖标题或标签。

## 图片服务边界

新建 `panel/product_image_service.py`，负责数据库、文件和标签，不负责 HTTP 路由或 WAHA 调用。建议接口：

```python
class ProductImageService:
    def __init__(self, database_path, data_root, clock=None, logger=None): ...
    def settings_payload(self) -> dict: ...
    def save_settings(self, payload: dict) -> dict: ...
    def upload(self, filename, declared_type, data, title, tags, caption_template='') -> dict: ...
    def list_assets(self, tag='', include_inactive=False) -> dict: ...
    def get_asset(self, asset_id, include_inactive=False) -> dict: ...
    def preview(self, asset_id) -> tuple[str, bytes]: ...
    def update_asset(self, asset_id, payload: dict) -> dict: ...
    def disable_asset(self, asset_id) -> dict: ...
    def resolve_for_send(self, product_tag, variant='') -> dict: ...
```

`resolve_for_send()` 只返回启用资产的安全元数据和受控文件句柄信息。标签使用 Unicode NFKC、空白折叠和 `casefold()` 规范化；精确标签没有匹配时返回 `NOT_FOUND`，同一优先级存在多个主图时返回 `AMBIGUOUS`，不替 AI 选择图片。

图片验证沿用 `ChatService` 的 10 MB、4000 万像素、真实格式和解压炸弹限制，并且上传、预览、发送三处都重新验证文件存在性和哈希。

## 管理员页面和接口

新增 `panel/product_image_page.py` 生成管理员产品图片库页面，增加主导航入口或设置页入口。页面提供：

- 图片上传、标题、说明模板和多标签编辑。
- 图片预览、标签筛选、启用/停用、优先级和主图设置。
- 重复哈希提示、上传失败提示和软删除确认。
- 全局自动发送开关和每日上限。

`PanelHandler` 增加管理员认证、CSRF 保护的接口：

```text
GET    /api/product-images
POST   /api/product-images
GET    /api/product-images/{id}/preview
PUT    /api/product-images/{id}
DELETE /api/product-images/{id}        # 软删除/停用
GET    /api/product-tags
POST   /api/product-tags
PUT    /api/product-tags/{id}
DELETE /api/product-tags/{id}
GET    /api/product-image-settings
PUT    /api/product-image-settings
```

预览接口只允许管理员访问，响应设置 `Cache-Control: private, no-store`、`X-Content-Type-Options: nosniff`，不创建公开静态路径。

## AI 自动发送协议

当图片库启用且存在至少一张启用图片时，`_ai_reply()` 增加：

```json
{
  "type": "function",
  "function": {
    "name": "send_product_image",
    "parameters": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "product_tag": {"type": "string"},
        "variant": {"type": "string"},
        "caption": {"type": "string"}
      },
      "required": ["product_tag"]
    }
  }
}
```

`PanelState._execute_product_image()` 负责：

1. 验证单一工具调用、参数集合、标签长度、说明长度和当前客户消息。
2. 检查自动回复策略、人工接管、连接状态、图片库开关和每日上限。
3. 通过 `ProductImageService.resolve_for_send()` 得到唯一主图。
4. 以 `message_key + asset_id + caption_hash` 计算幂等键，在 `product_image_send_requests` 中预留。
5. 调用 `ChatService.send_automated_image()`；该方法负责读取已验证文件、通过 WAHA 发送、归档出站消息并写入发送状态。
6. `SENT` 时记录 `product_image.sent`；`UNKNOWN` 不自动重试，避免 WAHA 已发送但响应丢失造成重复图片。

系统提示规则要求 AI 只有在客户当前消息明确索要或询问对应产品时调用工具；没有严格标签时使用普通文字回复，不调用工具。

## 审计和隐私

系统日志事件：

```text
product_image.uploaded
product_image.updated
product_image.disabled
product_image.sent
product_image.not_found
product_image.ambiguous
product_image.send_failed
product_image.send_unknown
```

日志只记录脱敏资产 ID、规范化标签、状态和错误码，不记录手机号、完整聊天 ID、图片二进制、客户完整消息、密钥或服务器绝对路径。

## 测试和验收

- 合法 JPG/PNG/WebP 可上传；伪造 MIME、超大文件、超像素图片和路径穿越文件名被拒绝。
- 相同 SHA256 不重复占用空间。
- 标签精确命中唯一主图时只发送一次；重复 webhook、重复工具调用和刷新不会重复发送。
- 无匹配、停用图片、标签歧义、人工接管、会话断开、自动发图关闭和达到每日上限时都不发送。
- `UNKNOWN` 状态不会自动重试。
- 非管理员无法上传、修改、删除或预览图片。
- 数据库和 `panel_data` 卷恢复后，图片文件、元数据和标签仍然对应。
- 运行 Python 全量测试、前端语法测试、迁移幂等测试、Docker 构建检查和生产容器验证。

## 部署、回滚和版本

实现建议发布为 `waha-panel 1.0.25`。发布前备份 `.env`、Compose 覆盖配置、面板 SQLite、`panel_data` 卷和图片目录；只更新面板容器，保持 WAHA 不动。

回滚到 1.0.24 时只恢复面板镜像并重建面板容器。迁移只新增表，不删除历史数据；若要恢复某个时间点，必须同时恢复 SQLite 和 `product-images` 文件目录，避免出现孤立元数据或孤立文件。
