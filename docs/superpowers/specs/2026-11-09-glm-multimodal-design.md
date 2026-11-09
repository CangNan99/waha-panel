# GLM 多模态模型接入设计

**日期：** 2026-11-09  
**状态：** 待确认设计  
**范围：** 面板本地代码与测试，不包含生产部署

## 1. 目标与背景

当前面板的会话自动回复和聊天管理 AI 都只发送纯文本 `chat/completions` 请求。WhatsApp 聊天页面虽然可以显示图片并发送图片，但图片不会进入自动回复或 AI 辅助流程，也没有图像生成入口。

本次为面板增加 GLM 多模态接入，完整覆盖四类能力：

- 语言模型：继续支持当前自动回复、翻译、营销建议和中文草稿优化；
- 视觉理解模型：读取 WhatsApp 图片和文字说明，生成描述或回答；
- 视觉推理模型：对图片进行带思考能力的分析，页面只展示最终答案；
- 图像生成模型：在当前聊天中根据文字生成图片，预览后可一键发送给客户。

智谱接口的对话补全和图像生成使用不同路径；实现会以官方文档为准，不把模型名、价格或可用参数写死为不可变业务规则。

## 2. 目标、非目标和失败标准

### 目标

1. 在每个会话的 AI 设置中配置四类 GLM 模型，API Key 只由后端保存和调用。
2. 自动回复遇到客户发来的图片消息时，若配置了视觉模型则必须读取并理解图片内容，结合图片说明和现有业务上下文自动生成回复并发送；没有视觉配置时保持现有“媒体消息不自动回复”的行为。
3. 聊天窗口对图片消息提供“视觉理解”和“视觉推理”操作，结果以可折叠的助手卡片展示，不自动发送。
4. 聊天输入区提供图像生成入口，生成结果可预览、取消或一键发送；发送沿用现有图片校验和幂等机制。
5. 保留现有 OpenAI 兼容配置和纯文本行为，旧数据库升级不丢失现有设置。
6. 通过单元测试、HTTP 级测试、前端脚本语法检查和本地集成验证证明请求体、权限、错误处理和回滚边界。

### 非目标

- 不修改 WAHA API、WhatsApp 会话数据结构、Docker 网络或反向代理；
- 不把图片、提示词、API Key、客户消息全文写入普通日志；
- 不实现视频理解、音频理解、OCR、训练或模型微调；
- 不强制所有会话使用 GLM，现有非 GLM 服务继续可用；
- 不把供应商当前模型列表、价格和配额做成需要频繁迁移的数据库枚举。

### 失败标准

- 视觉模型未配置时，媒体消息被错误地自动回复；
- 推理内容或 API Key 出现在浏览器响应、系统日志或错误信息中；
- 图像生成结果未经过面板图片校验就被发送；
- 一个会话的模型配置或图片引用可以被另一个会话访问；
- GLM 请求失败时误发固定文案、重复发送图片或破坏现有 AI 回退逻辑；
- 旧数据库迁移后现有自动回复和聊天功能不可用。

## 3. 约束与已确认选择

- 采用能力感知的 GLM 适配层，不重做全部 Provider 框架。
- 图像生成属于当前聊天会话上下文：结果在聊天管理页面显示，管理员确认后才能一键发送给当前 WhatsApp 客户。
- 每种能力使用独立配置槽位，允许一个会话同时配置语言、视觉理解、视觉推理和图像生成模型；API Key 可以相同，但各槽位独立保存模型、地址和推理设置。
- 现有 `session_settings` 中的 `ai_base_url`、`ai_model`、`ai_api_key` 继续作为语言模型的兼容回退。新配置优先使用能力槽位，旧字段只在语言槽位未配置时生效。
- 新增密钥使用现有 `DataCipher` 加密；旧字段不在本次范围内强制重加密，避免未经确认的密钥迁移。
- 所有外部模型地址继续要求 HTTPS、无账号密码、解析到公网地址；默认 GLM 地址建议为 `https://open.bigmodel.cn/api/paas/v4`，实际地址仍可配置。
- 图片输入复用现有 WAHA 媒体读取链路，最大 10 MiB、最大 40,000,000 像素，并限制为 JPEG、PNG、WebP。
- 图像生成结果只保存在进程内的短时缓存，使用不可猜测的引用标识；默认 10 分钟过期、单进程最多 32 个结果，服务重启后不保留。

实现决定：

- 自动回复同时存在 `vision_understanding` 和 `vision_reasoning` 时优先视觉推理；聊天页面的两个按钮仍然由管理员明确选择能力。
- 没有 `PANEL_DATA_ENCRYPTION_KEY` 时不创建或保存新的 GLM profile，新增 GLM 路由返回现有的 capability unavailable；旧的明文 `ai_*` 配置继续按当前行为可用。
- `openai_compatible` provider 只允许用于 `language` 槽位；三种非语言能力保存时必须使用 `glm`，避免出现未定义的图像 endpoint 契约。
- 生成图片引用使用 `AVAILABLE → RESERVED → CONSUMED` 状态。明确失败释放为 `AVAILABLE`，未知结果转为 `CONSUMED` 并禁止重发；同一 `client_request_id` 始终返回已有的发送状态。
- 本次保持面板现有同步 webhook 架构，但 GLM 媒体读取最多 15 秒、模型请求最多 30 秒、生成/视觉请求共用最多 2 个并发槽位；超时或上游错误只记录失败并返回，不进入固定文案回退，也不重复触发模型请求。
- `test-ai` 对语言模型发送最小文本、对视觉模型发送内置的 1×1 PNG fixture；对图像生成只校验 HTTPS 地址、模型名和密钥，不实际生成图片。页面明确显示“图像生成仅检查配置”。
- 自动回复视觉请求继承已有 persona、system prompt、知识库、会话上下文和订单工具；聊天页面的手动视觉理解/推理只使用管理员输入的提示和目标图片，不调用订单工具。

## 4. 方案比较

### 方案 A：只增加 GLM 预设

在现有服务地址、模型名和 API Key 字段旁增加 GLM 地址快捷填充。语言模型可以直接工作，视觉请求和图像生成仍需外部调用方自己拼装，不能满足面板内的完整需求。

### 方案 B：能力感知的 GLM 适配层（选定）

新增一个小型 GLM 适配模块，复用现有配置、认证、限流、图片校验和 WAHA 发送路径。按能力槽位保存四类模型，统一处理 OpenAI 兼容的多模态对话请求和 GLM 独立图像生成请求。改动集中，旧路径可保留，测试边界清晰。

### 方案 C：通用 Provider/能力框架

把现有 OpenAI 兼容、GLM 和未来供应商全部重构到统一注册表。长期扩展性更高，但会扩大迁移和回归范围，也会把本次 GLM 需求与未提出的供应商需求绑在一起。

## 5. 设计与接口

### 5.1 新增模块

新增 `panel/glm_service.py`，只负责 GLM 配置读取、请求构造、响应解析、图片校验和生成结果短时缓存，不负责 HTTP 路由或页面渲染。

核心接口：

- `GLMService.chat_text(profile, messages, tools=None) -> GLMTextResult`；
- `GLMService.chat_vision(profile, prompt, image_bytes, mime_type, reasoning=False, context=None, tools=None) -> GLMVisionResult`；
- `GLMService.generate_image(profile, prompt, size=None) -> GeneratedImage`；
- `GLMService.get_generated_image(reference) -> (mime_type, bytes)`；
- `GLMService.consume_generated_image(reference) -> (mime_type, bytes)`；
- `GLMService.test_connection(profile) -> dict`。

适配模块只接受已经通过会话和能力校验的 profile，不接受来自页面的任意 URL 或未经验证的媒体地址。所有上游响应都转换为面板自己的结果类型，错误只返回脱敏代码和用户可读消息。

### 5.2 配置槽位与数据库迁移

新增 `panel/migrations/007_glm_multimodal.sql`，迁移必须幂等，创建：

```sql
CREATE TABLE IF NOT EXISTS session_ai_profiles (
    session_name TEXT NOT NULL,
    capability TEXT NOT NULL CHECK (
        capability IN ('language', 'vision_understanding', 'vision_reasoning', 'image_generation')
    ),
    provider TEXT NOT NULL DEFAULT 'glm' CHECK (provider IN ('glm', 'openai_compatible')),
    base_url TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    api_key_ciphertext TEXT NOT NULL DEFAULT '',
    key_fingerprint TEXT NOT NULL DEFAULT '',
    reasoning_mode TEXT NOT NULL DEFAULT 'disabled' CHECK (
        reasoning_mode IN ('disabled', 'enabled', 'low', 'medium', 'high', 'max')
    ),
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_name, capability),
    FOREIGN KEY (session_name) REFERENCES managed_sessions(session_name) ON DELETE CASCADE
);
```

迁移只新增表和索引，不覆盖旧字段。`PanelState` 在启用加密服务后读取 profile；语言 profile 缺失时回退到旧的 `session_settings`。保存新 profile 时 API Key 为空表示保持旧值，勾选清除才删除。

全局 `translation_settings` 增加 `provider`、`reasoning_mode` 两列，翻译服务仍然只使用语言能力，不把聊天会话的图片或图像生成配置混入全局翻译设置。

### 5.3 后端 API

继续使用现有管理员认证、CSRF、会话校验和错误映射。

- `GET /api/settings?session=...`：返回四类 profile 的非敏感字段及 `api_key_configured`，不返回密钥；
- `POST /api/settings?session=...`：保存 profile，兼容现有自动回复字段；
- `POST /api/settings/test-ai?session=...&capability=...`：测试指定 profile，响应只包含成功状态和脱敏错误；
- `POST /api/chat/sessions/{session}/vision`：接收 `chat_ref`、`message_ref`、`prompt`、`mode`，读取并校验图片后调用视觉理解或视觉推理模型；
- `POST /api/chat/sessions/{session}/generate-image`：接收 `chat_ref`、`prompt`、可选 `size`，调用图像生成模型并返回短时 `image_ref`、媒体类型和过期时间；
- `GET /api/chat/sessions/{session}/generated-image?image_ref=...`：管理员读取短时生成图片预览；
- `POST /api/chat/sessions/{session}/send-generated-image`：接收 `chat_ref`、`image_ref`、`caption`、`client_request_id`，消费短时图片并走现有 `ChatService.send_image`；
- `POST /api/translation/settings` 与现有翻译接口：保持路径和结构兼容，仅增加 provider/reasoning 字段。

视觉接口的图片读取必须通过受保护的 `message_ref` 解析到当前会话，再调用 WAHA 的受限媒体读取；禁止页面直接提交任意 URL。生成图片的 `image_ref` 绑定会话和聊天 HMAC，不能跨会话复用。

### 5.4 自动回复行为

现有自动回复逻辑保持文本行为不变。Webhook 收到客户图片消息时，自动识别和回复是本次功能的强制路径：

1. 如果消息是图片且当前会话配置了视觉理解或视觉推理 profile，读取图片和 caption，构造带 `image_url` 内容的 GLM 请求，并自动生成、等待现有拟人延迟后发送回复；
2. 视觉推理 profile 使用启用思考/推理的请求参数，响应中的 `reasoning_content` 不写日志、不直接展示，只使用最终答案；
3. 如果视觉 profile 未配置、图片下载/校验失败或模型调用失败，保持原来的忽略媒体行为，不发送固定文案，也不将图片消息降级为文本自动回复；
4. 去重、人工接管、时间段、业务工具和发送确认逻辑继续沿用现有顺序；
5. 文本消息优先使用语言 profile，未配置新 profile 时回退旧的 `ai_*` 字段。

### 5.5 聊天页面行为

- 图片消息的操作区增加“视觉理解”和“视觉推理”，结果显示在现有助手卡片风格中，可折叠、可重试，不会自动发送；
- 顶部翻译设置保留，自动回复设置页增加四类模型槽位、能力说明、模型建议值和 Key 状态；
- 输入区增加“生成图片”入口，生成期间按钮禁用，结果以预览卡展示；
- 预览卡提供“重新生成”“移除”“发送给客户”，发送前必须经过现有人工发送确认、图片大小/格式校验和幂等请求；
- 实时消息刷新后保留当前生成预览和助手卡片，切换聊天或过期后清除引用；
- 页面不显示完整推理过程、密钥、上游错误原文或临时缓存路径。

## 6. 安全、隐私和资源限制

- 新 profile API Key 使用 `DataCipher` 加密，指纹只用于缓存/配置变化判断；
- 图片输入和生成结果严格限制大小、像素、媒体类型和缓存数量；
- GLM 地址只允许 HTTPS 公网地址，禁止内网、本机、账号密码和任意重定向；
- 生成结果若为 URL，后端只允许从已配置的 GLM 官方域名或同源地址拉取；不接受页面传入任意远程图片地址；
- 系统日志只记录能力、会话技术名、请求状态和错误代码，不记录提示词、图片、响应全文或 API Key；
- 自动回复中的图片内容属于客户数据，只有在已配置对应模型且满足现有自动回复开关、时间段和人工接管规则时才发送给第三方模型；
- 超时、限流、上游错误、解析错误和缓存过期都必须返回可重试的脱敏错误，不得触发未经确认的图片发送。

## 7. 测试与验收

### 后端测试

1. 迁移可重复执行，旧 `session_settings` 数据保留，语言 profile 缺失时正确回退；
2. profile 保存、清除 Key、跨会话隔离和隐藏 Key；
3. 语言、视觉理解、视觉推理三种对话请求生成正确的 `messages` 内容，图片使用受限 data URI，推理参数按能力发送；
4. 图像生成请求使用独立 endpoint，正确解析 URL/base64 两种响应，并拒绝无效或超限图片；
5. 视觉接口拒绝跨会话或过期的 chat/message 引用；
6. Webhook 图片消息在视觉 profile 存在时调用模型，在 profile 缺失或模型失败时不发送固定文案；
7. 生成图片引用绑定会话、短时过期、不能重复消费；
8. 发送生成图片沿用 `manual_send_requests`，覆盖成功、明确失败、结果未知和重复请求；
9. 旧的文本自动回复、翻译、聊天发送和多会话状态回归测试继续通过。

### 前端和集成验收

- 使用现有 `scripts/check_frontend_syntax.mjs` 检查 `panel/app.py`、`panel/chat_page.py` 的全部内联脚本；
- 使用 Python `unittest` 覆盖新增路由和服务结果；
- 本地启动面板，验证设置保存/读取、视觉请求失败提示、生成预览、发送确认和实时刷新保留状态；
- 不连接真实客户会话、不输出真实 API Key、不向生产服务发送模型请求；
- 只有本地测试和只读运行状态验证通过后，才讨论构建镜像或部署。

## 8. 发布、备份和回滚

实现阶段只修改代码、迁移和测试文件，不修改 Compose、WAHA 镜像、端口、域名或外部服务配置。发布前备份面板 SQLite、`secrets/` 和当前 Compose 配置，并记录旧面板镜像版本。

迁移只新增表和列，回滚优先是恢复旧面板代码/镜像并保留数据库；旧版本会忽略新增 profile 表和全局字段，不会删除现有自动回复设置。若需要彻底撤销新增配置，再在确认备份可读后清理新增表和临时缓存，不自动删除客户数据。

验收证据必须包含：测试命令与结果、迁移前后 schema 检查、面板本地 HTTP 响应、无敏感信息的日志摘录和前端语法检查结果。证据不足时不声明部署完成。

