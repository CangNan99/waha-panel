# AI 产品图片识别、产品图片库与主动营销升级设计

**日期：** 2026-11-13  
**状态：** 方案已批准，设计文档待审核  
**范围：** 面板本地代码、SQLite 数据、产品图片库、聊天自动回复和营销自动化；不包含生产部署

## 1. 意图与结果

本次升级把现有的 GLM 图片理解、产品图片库和 AI 营销系统连接起来，完成三个连续能力：

1. 客户发送产品图片后，系统识别图片内容，匹配自己的产品目录，在高置信度且唯一匹配时自动发送对应产品图片和介绍；
2. 客户聊天过程中，AI 根据当前对话主动调用产品图片库，自动发送合适的产品图片和说明；
3. AI 营销系统从“一次扫描、发送一条文字”升级为可暂停、可恢复、可分阶段的主动沟通流程，并支持受控的产品图片发送。

“直接自动发送”是本设计的明确决策。管理员不需要逐条确认，但每次发送必须通过服务器侧的产品匹配、客户状态、人工接管、退订、冷却、每日上限、权限和幂等检查。

成功标准：

- 已知产品图片在高置信度和唯一匹配时，自动发送正确产品图，且同一消息最多发送一次；
- 相似、模糊或无法确认的产品不会被错误映射到其他产品；
- AI 能在聊天中根据产品兴趣调用图片工具，服务器不会接受任意 URL 或内部资产编号；
- 营销客户收到产品图片后，客户回复、退订或人工接管会立即停止当前营销序列；
- 旧的文字自动回复、翻译、聊天发送、产品图片库和已有发送账本继续可用；
- 客户图片、API Key、完整提示词和推理内容不写入普通日志。

## 2. 当前基础

当前 release/glm-multimodal-1.0.28 代码已经提供以下基础：

- panel/glm_service.py 已支持 GLM 文本、视觉请求和图片校验；
- panel/app.py 已在客户图片消息上读取媒体并调用视觉模型；
- panel/product_image_service.py 已支持 JPEG、PNG、WebP 校验、图片资产、产品名、SKU、别名、标签、主图、优先级和发送幂等；
- panel/product_image_page.py 已提供图片上传、标签管理、停用、主图和优先级管理；
- panel/app.py 已有 send_product_image 工具，当前主要通过客户当前消息中的精确产品名、SKU、别名或标签触发；
- panel/marketing_automation.py 已支持营销候选、空闲天数、退订、人工接管、冷却时间、每日上限和 ai_marketing_send_requests 发送账本；
- panel/migrations/014_product_image_library.sql、019_product_image_matching.sql 和 020_ai_marketing.sql 已建立现有图片库、匹配字段和营销账本。

现有缺口：

- 客户图片视觉结果没有与产品图片资产做可审计的产品级匹配；
- 图片识别只在视觉回复中使用，不会稳定触发产品库图片发送；
- 产品图片库是资产级结构，产品知识、产品版本和多张图片之间没有统一产品实体；
- send_product_image 的触发规则主要依赖文字明确提及产品；
- 营销系统只生成和发送文字，没有活动阶段、产品兴趣、图片发送和触达统计；
- 当前视觉测试按钮调用的 POST /api/settings/test-ai 路由仍需要补齐，不能作为本次升级的隐藏前置条件。

## 3. 目标、非目标和失败标准

### 目标

- 增加产品级目录，将产品知识、别名、SKU、版本和图片资产统一管理；
- 让 GLM 返回结构化视觉候选，由服务器完成最终产品和资产匹配；
- 支持客户图片触发产品图片自动发送；
- 支持聊天中的 AI 工具主动调用产品图片；
- 支持营销活动、客户阶段、产品兴趣、图片触达和停止条件；
- 复用现有 WAHA 媒体读取、ChatService、产品图片发送账本和管理员认证；
- 增加影子模式、逐会话开关和全局停止开关，支持分批启用；
- 使用追加式 SQLite 迁移，保留旧设置和旧数据。

### 非目标

- 不修改 WAHA API、WhatsApp 消息数据结构、Docker 网络、端口、域名或反向代理；
- 不实现视频、音频、OCR 专用流水线、训练或模型微调；
- 不把任意远程图片 URL 交给模型或 WhatsApp 发送接口；
- 不在本次首期引入独立向量数据库；
- 不强制所有会话使用 GLM，旧的 OpenAI 兼容语言能力继续保留；
- 不把供应商模型列表、价格和配额写死为数据库枚举；
- 不将原始客户图片、完整聊天内容或推理过程写入普通日志。

### 失败标准

- 模型把一个相似产品误判为库内产品后自动发错图片；
- 同一 webhook、网络重试或服务重启导致重复发送；
- 客户回复或退订后营销任务仍继续发送；
- AI 通过工具参数访问任意文件、URL 或其他会话的资产；
- 产品图片发送失败后被固定文案掩盖，或未知发送结果被盲目重试；
- 新迁移使旧的文字自动回复、翻译、发送或产品图片库不可用；
- 失败日志泄露客户图片、API Key、完整提示词或上游响应。

## 4. 方案比较与选定方案

### 方案 A：规则和文字匹配增强

继续依赖产品名称、SKU、别名、标签和图片中的文字识别结果，不做产品参考图片对比。

优点是改动小、成本低、迁移简单；缺点是对没有清晰文字的图片、相似产品和版本差异不可靠，无法完整满足“客户图片识别到自己的产品图片库”的目标。

### 方案 B：多模态识别、服务器二次匹配和受控自动发送（选定）

GLM-5.3-Flash 负责识别客户图片和返回候选产品；产品目录和图片资产提供约束；服务器完成唯一匹配、置信度决策、权限检查和发送。首期使用产品字段筛选和少量参考图片复核，不引入向量数据库。

该方案复用当前 GLMService、ProductImageService、ChatService 和发送账本，能同时覆盖客户图片识别、聊天主动图片和营销图片发送，并且可以通过影子模式逐步上线。

### 方案 C：视觉向量检索为核心

给所有产品图片建立向量索引，让客户图片与产品图片做近邻搜索，再由 GLM 解释和决策。

该方案适合较大的产品库，但会增加向量模型、索引、重建、备份和运行依赖。它作为方案 B 的后续增强保留，不作为本次首期基础。

## 5. 总体架构

~~~mermaid
flowchart LR
    A[客户文字或图片] --> B[WAHA 媒体读取与校验]
    B --> C[GLM 视觉理解]
    C --> D[结构化候选产品]
    D --> E[产品目录与参考图匹配]
    E --> F{策略与置信度}
    F -->|高且唯一| G[自动发送产品图和介绍]
    F -->|中等或有歧义| H[自动询问客户]
    F -->|低或未知| I[只回复文字]
    G --> J[产品图片发送账本]
    H --> K[客户对话状态]
    I --> K
    L[营销活动调度器] --> M[客户阶段和资格判断]
    M --> N[AI 文案与产品工具]
    N --> G
    N --> O[自动发送文字]
    O --> P[营销发送账本]
~~~

模块边界：

- GLMService：构造视觉请求、解析结果、处理超时和脱敏错误；
- ProductCatalogService：管理产品、版本、知识和资产关联；
- ProductImageService：保存图片、校验图片、解析可发送资产和维护图片发送幂等；
- PanelState：连接 webhook、视觉结果、产品策略和聊天发送；
- MarketingAutomationService：管理活动、客户阶段、下一次动作和营销发送；
- 页面模块：产品目录、图片库、AI 设置和营销活动管理；
- SQLite：保存配置、产品目录、匹配审计、营销阶段和发送账本。

## 6. 产品目录与图片库

### 6.1 产品目录

新增产品级实体，现有资产通过 product_id 关联：

~~~text
product_catalog
- product_id
- product_key
- product_name
- sku
- aliases_json
- category
- description
- selling_points
- usage_scenarios
- exclusion_scenarios
- language_content_json
- active
- allow_ai_image_send
- allow_marketing_image_send
- created_at
- updated_at
~~~

product_key 是服务器内部稳定标识。模型只能返回产品名称、SKU、别名或候选标签，服务器负责映射到 product_id。

### 6.2 图片资产扩展

在现有 product_image_assets 上追加：

~~~text
- product_id
- variant
- reference_role
- allow_visual_match
- allow_auto_send
- caption_template
~~~

reference_role 支持主图、界面图、包装图、套餐对比图和营销图。旧的 title、tags、product_name、sku、aliases_json 和 caption_template 继续保留。

### 6.3 兼容迁移

- 现有图片资产通过 product_name、SKU、别名和主标签尝试创建产品目录；
- 无法唯一归并的资产保留原有字段，并标记为待整理；
- 迁移不删除图片文件、不覆盖原有资产、不改变旧发送账本；
- 产品目录和图片资产都支持停用，不直接物理删除；
- 旧的 resolve_for_send 精确匹配继续可用，新的产品级匹配在其上增加能力。

### 6.4 管理页面

产品图片页面升级为产品目录管理页面，支持：

- 产品卡片编辑；
- 产品名称、SKU、别名和标签；
- 产品介绍、卖点、适用场景和多语言内容；
- 一产品多图片；
- 主图、备用图、参考图和营销图；
- 版本和变体；
- AI 自动发送和营销发送开关；
- 图片排序、停用、恢复和重复检测；
- 上传测试图片，查看匹配候选和原因，不触发客户发送；
- 统计产品图片发送次数和最近使用时间。

## 7. 客户图片识别和自动发送

### 7.1 输入处理

客户图片必须沿用现有受限媒体读取链路：

- 只接受当前会话和当前消息引用；
- 最大 10 MiB；
- 最大 40,000,000 像素；
- 只允许 JPEG、PNG、WebP；
- 下载和校验超时最多 15 秒；
- 原始图片只在内存中短时存在；
- 只保存图片 SHA-256，不保存客户原始图片。

caption、当前消息和历史对话都视为不可信数据，图片中出现的文字不能修改系统规则、发送限制或产品目录。

### 7.2 GLM 结构化识别

视觉提示要求模型只返回结构化结果，包括：

- match_status；
- 产品候选名称或 SKU；
- 版本候选；
- 置信度；
- 视觉证据；
- 客户意图；
- 是否需要澄清；
- 建议的产品动作。

模型不能返回内部路径、文件名、数据库 ID 或远程图片 URL。

### 7.3 服务器二次匹配

服务器按以下顺序匹配：

1. 产品名称、SKU、别名和标准化标签；
2. 当前会话已提到的产品；
3. 产品分类和版本；
4. 候选产品的参考图片；
5. 产品是否允许自动发送；
6. 是否存在唯一可发送主图。

产品图片库规模较小时，可将候选产品的主图和必要参考图一起发送给视觉模型复核。产品库扩大后，增加图片指纹或向量检索作为候选缩小器。

### 7.4 自动发送策略

初始策略：

- 置信度默认阈值：0.90；
- 自动发送必须只有一个唯一产品候选；
- 产品和图片必须处于启用状态；
- 当前客户消息必须表现出产品询问、识别、比较、试用或购买意图；
- 同一客户消息最多发送一张产品图；
- 图片默认附带产品介绍 caption；
- 同一产品在单客户冷却时间内不重复发送；
- 图片发送达到每日或单客户上限时只回复文字；
- 客户处于人工接管、退订、敏感业务或营销暂停状态时不发送；
- 低置信度和未知产品只回复文字；
- 中等置信度或多候选时自动询问客户确认。

阈值、冷却和上限都必须可配置。影子模式期间只记录决策，不调用 WhatsApp 图片发送。

### 7.5 匹配审计

新增 product_image_matches：

~~~text
product_image_matches
- id
- session_name
- chat_key_hmac
- message_key
- image_sha256
- match_status
- product_id
- asset_id
- confidence
- confidence_bucket
- decision
- reason_code
- created_at
~~~

只保存决策所需的摘要，不保存原始图片、完整提示词或完整模型响应。

## 8. 聊天内 AI 主动调用产品图片

现有 send_product_image 工具扩展为产品级动作：

~~~json
{
  "product_key": "iptv-premium",
  "variant": "premium",
  "caption": "这是 Premium 套餐的界面和频道示例。",
  "reason": "客户询问套餐效果"
}
~~~

服务器执行以下检查：

- product_key 能唯一映射到启用产品；
- 产品允许 AI 自动发送；
- 版本和图片资产匹配；
- 当前消息包含产品兴趣或图片需求；
- 没有人工接管、退订、冷却或限额阻止；
- 发送账本中没有相同消息的已处理请求；
- caption 不超过长度限制，不包含内部路径、URL 或敏感信息。

允许工具触发的场景：

- 客户询问外观、界面、包装或效果；
- 客户要求看图片或示例；
- 客户比较套餐；
- 客户表达试用、购买或进一步了解意图；
- 营销活动明确允许图片。

不满足条件时，服务器拒绝图片动作，AI 继续发送正常文字回复。

## 9. 营销自动化升级

### 9.1 活动模型

新增 marketing_campaigns：

~~~text
marketing_campaigns
- campaign_id
- name
- objective
- target_rules_json
- product_scope_json
- allow_image_send
- cadence_json
- max_touches
- quiet_hours_json
- language_policy
- active
- created_at
- updated_at
~~~

原有全局营销设置保留作为兼容默认值。新活动配置优先于旧的全局提示词。

### 9.2 客户阶段

新增 marketing_contact_states：

~~~text
marketing_contact_states
- campaign_id
- session_name
- chat_key_hmac
- stage
- interest_product_id
- last_contact_at
- last_reply_at
- next_action_at
- touch_count
- paused_reason
- opt_out
- updated_at
~~~

阶段包括：

- ELIGIBLE；
- CONTACTED；
- FOLLOW_UP_DUE；
- REPLIED；
- QUALIFIED；
- HUMAN_HANDOFF；
- PAUSED；
- OPTED_OUT；
- COMPLETED。

### 9.3 营销流程

~~~mermaid
stateDiagram-v2
    [*] --> ELIGIBLE
    ELIGIBLE --> CONTACTED: 首次触达
    CONTACTED --> FOLLOW_UP_DUE: 到达下一阶段
    FOLLOW_UP_DUE --> CONTACTED: 继续触达
    CONTACTED --> REPLIED: 客户回复
    REPLIED --> QUALIFIED: 识别到产品需求
    QUALIFIED --> HUMAN_HANDOFF: 需要人工
    REPLIED --> PAUSED: 转入正常 AI 对话
    ELIGIBLE --> OPTED_OUT: 客户退订
    CONTACTED --> OPTED_OUT: 客户退订
    CONTACTED --> PAUSED: 人工暂停
    FOLLOW_UP_DUE --> COMPLETED: 达到最大触达次数
~~~

### 9.4 图片营销策略

- 首次唤醒默认文字；
- 客户有产品兴趣后才允许营销图片；
- 每个客户每个活动周期最多发送一张产品图片；
- 产品图片必须来自当前活动的 product_scope；
- 客户回复后立即停止当前营销活动；
- 退订后永久抑制；
- 人工接管期间暂停；
- 超过安静时段、每日上限或客户冷却时间时跳过；
- 文字和图片使用统一的营销发送账本。

“直接自动发送”表示活动启用图片后不需要人工逐条确认，但发送仍然必须经过上述状态和策略判断。

### 9.5 营销事件和统计

新增 marketing_events：

~~~text
marketing_events
- campaign_id
- session_name
- chat_key_hmac
- event_type
- product_id
- asset_id
- message_ref
- reason_code
- created_at
~~~

后台统计：

- 扫描客户数；
- 合格客户数；
- 发送成功数；
- 图片发送数；
- 客户回复数；
- 退订数；
- 人工接管数；
- 失败和未知状态；
- 按活动和产品统计回复率。

## 10. API 和权限

新增或扩展接口：

- GET/POST/PUT /api/product-catalog；
- GET/PUT /api/product-catalog/{product_id}；
- GET/POST/PUT /api/product-images；
- POST /api/product-images/match-test；
- GET/PUT /api/product-image-settings；
- POST /api/settings/test-ai；
- POST /api/chat/sessions/{session}/vision；
- POST /api/chat/sessions/{session}/product-image；
- GET/POST/PUT /api/marketing/campaigns；
- GET /api/marketing/campaigns/{campaign_id}/contacts；
- POST /api/marketing/campaigns/{campaign_id}/pause；
- POST /api/marketing/campaigns/{campaign_id}/resume。

所有写接口继续使用管理员认证、CSRF 和会话校验。产品匹配测试可以由管理员上传图片，但只返回匹配结果，不发送给客户。

页面不能提交任意媒体 URL。客户图片必须通过受保护的 session、chat_ref 和 message_ref 解析后由服务端读取。

## 11. 安全、隐私和资源限制

- 新增产品和营销配置沿用管理员认证；
- API Key 继续使用 DataCipher 加密；
- 图片资产只能从面板私有数据卷读取；
- GLM 地址继续要求 HTTPS、公网解析、无账号密码；
- 原始客户图片不写入日志、不写入长期数据库；
- 图片识别只保存哈希和决策摘要；
- 图片中和客户 caption 中的指令视为不可信内容；
- 模型不能修改发送限制、活动状态或产品目录；
- 视觉和生成请求共用当前最多两个并发槽位；
- 媒体读取最多 15 秒，模型请求最多 30 秒；
- 上游错误、超时、限流、无效响应和图片校验失败都转换为脱敏错误；
- 明确发送失败可以重试，未知发送状态不能盲目重发；
- 全局产品图片发送开关可以立即停止所有自动图片发送；
- 营销图片、客户图片识别和聊天主动图片分别设置开关。

## 12. 文件和模块边界

预计修改或新增：

- panel/product_image_service.py：产品资产、产品级解析、匹配候选和图片发送账本；
- panel/product_image_page.py：产品目录和图片库管理页面；
- panel/glm_service.py：结构化视觉请求和响应校验；
- panel/app.py：自动回复、产品工具、API 路由、设置和策略；
- panel/marketing_automation.py：活动阶段、客户状态、图片营销和调度；
- panel/chat_service.py：客户状态、营销停止条件和发送幂等连接；
- panel/migrations/021_product_catalog.sql：产品目录和图片资产扩展；
- panel/migrations/022_product_image_matches.sql：视觉匹配审计；
- panel/migrations/023_marketing_campaigns.sql：活动、客户阶段和事件；
- tests/test_product_catalog.py：产品目录和迁移；
- tests/test_product_image_matching.py：视觉候选、置信度和去重；
- tests/test_product_image_integration.py：自动发图和发送账本；
- tests/test_marketing_automation.py：阶段、图片营销和停止条件；
- tests/test_glm_state.py：结构化视觉请求和错误映射；
- tests/frontend_syntax.test.mjs：页面内联脚本语法。

模块之间通过明确的产品 ID、资产 ID、客户会话引用和发送请求 ID 连接，模型不直接访问数据库或文件系统。

## 13. 测试和验收

### 数据和配置

1. 迁移可重复执行；
2. 旧图片资产、旧标签和旧营销设置保留；
3. 产品目录按会话和管理员权限隔离；
4. API Key 只返回 api_key_configured；
5. 未配置 PANEL_DATA_ENCRYPTION_KEY 时新 profile 和新营销密钥保存被拒绝。

### 视觉匹配

1. 高置信度唯一产品自动发送正确图片；
2. 多候选时不自动发送，返回澄清回复；
3. 低置信度、未知图片或非本产品不发送产品图；
4. 同一消息重复 webhook 不重复发送；
5. 同一产品处于冷却时不重复发送；
6. 图片下载失败、格式错误、超限和模型超时不发送固定误导文案；
7. 视觉模型返回非法 JSON、越权资产或远程 URL 时被拒绝。

### 聊天工具

1. 客户明确询问产品图片时自动调用正确资产；
2. 无产品意图时工具动作被服务器拒绝；
3. AI 无法传递任意路径或 URL；
4. 同一轮最多发送一张产品图片；
5. 发送未知状态不自动重试。

### 营销系统

1. 客户回复后当前营销序列立即暂停；
2. 客户退订后所有活动跳过；
3. 人工接管期间不发送；
4. 达到最大触达次数后停止；
5. 活动关闭或全局停止开关打开后不发送；
6. 营销文字和图片共享幂等和状态账本；
7. 服务重启后不会重复发送正在处理的消息；
8. 每日上限、客户冷却和安静时段有效。

### 本地验证

- 使用现有 Python 测试套件；
- 使用 scripts/check_frontend_syntax.mjs 检查所有页面脚本；
- 使用假的 GLM、WAHA 和发送客户端验证请求体和发送结果；
- 不连接真实客户会话；
- 不发送真实模型请求；
- 不输出真实 API Key、客户图片或完整聊天内容；
- 只有本地测试、迁移检查和只读运行状态验证通过后，才讨论构建镜像或部署。

## 14. 发布、备份和回滚

发布前备份：

- 面板 SQLite；
- panel_data 卷中的 product-images；
- secrets/；
- 当前 Compose 配置；
- 当前面板镜像标签和运行状态。

实现阶段只修改代码、迁移、测试和页面文件，不修改 WAHA API、网络、端口、域名或反向代理。

回滚顺序：

1. 关闭客户图片自动发送；
2. 关闭聊天主动产品图片；
3. 关闭营销图片；
4. 恢复旧面板代码或镜像；
5. 保留新增表和图片文件，不删除客户数据；
6. 检查旧版本的文字自动回复、翻译、聊天发送和旧图片库；
7. 只有在备份确认可读后，才考虑清理新增表或匹配记录。

## 15. 实施前的默认决定

本设计已经确定：

- 采用方案 B；
- 产品图片和营销图片直接自动发送；
- 高置信度、唯一匹配时自动发图；
- 中等置信度时询问确认；
- 低置信度时只回复文字；
- AI 不直接控制内部资产；
- 首期不引入向量数据库；
- 使用影子模式和分批启用；
- 保留现有文字流程、图片库和发送账本；
- 不执行生产部署。

实现计划需要继续明确的内容由构建者在不改变产品行为的前提下决定，包括页面字段布局、兼容旧资产的具体归并算法、测试假数据和内部函数命名。
