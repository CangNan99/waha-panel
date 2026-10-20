# 全局翻译双引擎、IPTV 系统标签与回复延迟设计

日期：2026-10-21（Asia/Shanghai）  
适用分支：`release/1.0.17-iptv-mobile`  
基线提交：`8d9b7eb`  
状态：已获用户审批，等待实施计划评审

## 1. 目标与已确认范围

本次调整包含三个相互独立、可以分别回滚的能力：

1. 将聊天页“全局翻译”的阿里云调用从通用版切换为阿里云机器翻译专业版，并增加 AI 模型翻译选项，允许管理员手动切换。
2. IPTV 试用消息确认成功发送后，给当前客户自动添加只读系统标签 `IPTV测试过`。
3. 将自动回复延迟从固定常量改为按 WhatsApp 会话保存的可调滑杆。

本次默认理解：

- 全局翻译引擎选择是面板级单例配置，默认 `aliyun_professional`；客户是否开启全局翻译仍按当前聊天独立保存、默认关闭。
- 引擎切换只影响聊天消息的双行翻译显示；销售建议、草稿优化、总结、备注中文候选继续使用现有独立 AI 服务。
- 阿里云专业版场景默认使用 `communication`，因为当前产品面向 WhatsApp 客服和销售沟通。
- AI 翻译复用现有 `translation_settings` 中的地址、模型和密钥。
- IPTV 系统标签只在消息发送结果确认成功后写入；创建成功但发送失败或结果未知时不写入。
- 延迟滑杆保存为 `0..120` 的整数，默认 `100`；`100` 保持当前热/冷对话和输入模拟的延迟范围。
- 实施目标是当前工作树，不修改主分支 `main`，不覆盖已有未跟踪计划文档。

阿里云官方文档将 `Translate` 定义为机器翻译专业版文本接口，支持 `communication`、`social` 等场景；专业版单次请求限制小于 5000 字符，流量配额为 50 QPS。本设计继续使用 4800 字符安全上限，并在本地保留并发和速率保护。

## 2. 非目标与不变项

- 不修改 WAHA API、WhatsApp 原始消息结构、会话认证、Docker 网络和反向代理。
- 不将 IPTV 账号、密码、M3U 地址、AccessKey Secret、AI API Key 或客户原文写入日志、标签或普通 HTTP 响应。
- 不在专业版失败时自动回退到 AI 模型或通用版；当前引擎失败要显示明确错误并保留原文。
- 不批量重新翻译历史消息；服务端缓存继续按引擎隔离使用。
- 不删除已有 `MANUAL` 或 `AI` 客户标签；AI 总结更新不得覆盖 IPTV 系统标签。
- 不改变人工发送、人工接管、跟进任务的业务语义。
- 不在本轮自动发布镜像、重启远端容器、修改生产凭据或进行线上迁移。线上动作需要单独的发布授权。

## 3. 当前基线与问题定位

当前工作树已经具备：

- `panel/aliyun_translation_service.py`：阿里云通用版 `TranslateGeneral` 调用、加密缓存、批量翻译和配置接口。
- `panel/translation_service.py`：独立 AI 翻译、销售建议、草稿优化、跟进和总结能力。
- `panel/chat_page.py`：聊天顶部全局翻译开关，当前只连接阿里云机器翻译。
- `panel/migrations/010_iptv_trials.sql`：IPTV 供应商设置、试用记录和状态机。
- `panel/migrations/007_chat_engagement.sql`：客户标签表，`source` 当前只允许 `MANUAL` 和 `AI`。
- `panel/app.py`：自动回复延迟目前固定为热对话前置 1–5 秒、冷对话前置 20–100 秒、输入模拟 6–15 秒。

因此本次不重做既有翻译、IPTV 或标签流程，而是在现有服务边界上增加路由、系统标签表和会话设置。

## 4. 全局翻译双引擎

### 4.1 引擎选择

新增单例配置表 `global_translation_settings`：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `id` | `INTEGER PRIMARY KEY CHECK(id=1)` | 单例行 |
| `provider` | `aliyun_professional` / `ai_model` | 当前全局翻译引擎 |
| `scene` | 默认 `communication` | 阿里云专业版场景 |
| `updated_at` | Unix 秒 | 更新时间 |

迁移写入默认 `aliyun_professional`，但不要求阿里云凭据已经配置。这样升级不会偷偷修改凭据，也不会把原有翻译功能切换到 AI 费用链路。

### 4.2 服务边界

新增 `GlobalTranslationService`，负责：

- 读取和保存当前引擎；
- 返回引擎和配置状态，永不返回秘密；
- 按当前引擎分派批量翻译；
- 规范化 `READY`、`SKIPPED`、`FAILED` 三种结果；
- 给缓存指纹增加 provider、模型/场景信息，避免两个引擎复用错误译文；
- 在切换引擎时取消或拒绝过期请求结果。

阿里云服务继续负责凭据、SDK、专业版请求、限流和阿里云缓存；现有 AI 服务继续负责 AI 配置和结构化输出。`GlobalTranslationService` 不把两个服务的密钥合并到一张表。

### 4.3 阿里云专业版请求

`AliyunTranslationService._sdk_request` 改用官方 SDK 的专业版请求对象：

- `source_language=auto`；
- `target_language=zh`；
- `format_type=text`；
- `scene=communication`；
- `source_text` 使用已过滤的当前消息文本。

现有 4800 字符安全限制继续保留。SDK 错误转换为脱敏的业务错误码，至少覆盖配置缺失、权限未开通、余额/服务不可用、超长、限流和网络异常。

本地速率窗口不超过专业版 50 QPS，保留 5 个并发槽位。遇到限流时短退避；不自动改用另一个引擎。

### 4.4 AI 模型翻译

当 `provider=ai_model` 时，使用现有 `TranslationService.translate`，按消息来源传递 `customer` 或 `agent` 角色，从结构化结果读取 `chinese_translation`。现有 AI 服务的：

- HTTPS 地址校验；
- API Key 加密保存；
- IPTV 凭据过滤；
- 速率限制；
- JSON 校验和一次修复请求；
- 加密缓存；

全部保留。

AI 服务未配置时，接口返回 `TRANSLATION_NOT_CONFIGURED`，页面显示“AI 模型翻译未配置”，不自动切回阿里云。

### 4.5 HTTP 接口

新增：

- `GET /api/translation/preferences`：读取当前引擎和两个引擎的配置状态；
- `PUT /api/translation/preferences`：保存 `provider` 和受支持的场景；
- `POST /api/translation/preferences/test`：测试当前选择的引擎；

现有接口继续保留：

- `/api/translation/aliyun/settings`；
- `/api/translation/aliyun/test`；
- `/api/translation/aliyun/cache`；
- `/api/translation/settings`；
- `/api/translation/settings/test`。

当前聊天的 `/api/chat/sessions/{session}/machine-translations` 改为调用 `GlobalTranslationService`，请求格式保持兼容。服务端继续检查聊天引用、消息归属、批次数量、单条字符数和总字符数。

### 4.6 前端交互

聊天页翻译设置对话框增加：

- 当前引擎下拉选择；
- 阿里云专业版配置和测试状态；
- AI 模型配置和测试状态；
- 当前引擎的测试按钮；
- 清理当前引擎缓存按钮。

聊天顶部按钮显示当前引擎，例如：

- `全局翻译：阿里云专业版 · 关闭`；
- `全局翻译：AI 模型 · 开启`。

切换引擎时：

1. 终止当前 AbortController 请求；
2. 增加请求代次；
3. 清理当前页面中旧引擎结果；
4. 保留服务端的两类缓存；
5. 使用新引擎重新请求当前聊天。

缓存键必须包含 provider，避免同一消息在切换后显示错误译文。切换客户、关闭页面或聊天不可见时继续取消请求。

“销售辅助 AI”对话框仍然独立，文案明确说明它不决定全局消息翻译引擎。

## 5. IPTV 系统标签

### 5.1 数据模型

新增 `customer_system_labels` 表：

| 字段 | 说明 |
| --- | --- |
| `id` | 自增 ID |
| `session_name` | 技术会话名 |
| `chat_key_hmac` | 现有客户 HMAC |
| `label` | 系统标签文字 |
| `created_at` | 创建时间 |
| `updated_at` | 更新时间 |

使用 `(session_name, chat_key_hmac, label)` 唯一约束和对应索引。迁移只增加新表，不重建现有 `customer_labels`。

### 5.2 写入规则

在 IPTV 自动试用流程中，只有以下顺序完成后才写入：

1. `create_trial` 成功创建或复用账号；
2. `send_text` 成功返回；
3. `mark_delivery(trial_id, True)` 成功；
4. `ChatService.add_system_label_by_chat_id(session, chat_id, "IPTV测试过")` 幂等写入。

创建失败、结果未知、发送失败、发送前条件变化和取消均不得写入。重复发送或同一客户跨会话复用时，使用 `INSERT OR IGNORE`，保持一条系统标签。

标签写入失败不能撤销已经成功的 WhatsApp 消息；只记录脱敏告警，并允许后续同一成功事件再次补写。

### 5.3 读取和页面

`customer_labels`、`customer_label_records`、聊天列表摘要和客户详情合并返回：

- `manual`：可编辑；
- `ai`：只读；
- `system`：只读。

聊天页标签对话框增加“系统”分组。系统标签不经过现有手动标签 POST/PUT/DELETE 接口，也不能被 AI 总结刷新删除。

## 6. 回复延迟滑杆

### 6.1 存储

复用现有 `session_settings` 键值表，新增：

```text
auto_reply_delay_scale = 0..120
```

缺少该键的旧会话按 `100` 读取。默认值写入现有默认设置同步逻辑，不新增独立延迟表。

### 6.2 算法

令：

```text
ratio = auto_reply_delay_scale / 100
```

生成延迟：

```text
热对话前置等待：uniform(1 * ratio, 5 * ratio)
冷对话前置等待：uniform(20 * ratio, 100 * ratio)
输入模拟等待：uniform(6 * ratio, 15 * ratio)
```

当值为 100 时保持当前行为；值为 0 时所有这三段等待为 0。只影响标准 AI 自动回复路径，不改变人工发送、跟进任务和 IPTV 试用状态机。

### 6.3 设置页面

在“自动回复设置 → 回复规则”增加原生 range 控件：

- `min=0`；
- `max=120`；
- `step=5`；
- 默认 `100`；
- 实时显示数值和“即时 / 当前默认 / 偏慢”提示；
- 支持键盘、触控和 `aria-valuenow`；
- 按当前 WhatsApp 技术会话隔离。

服务端拒绝非法值，保存失败时前端恢复上一次服务端值。

## 7. 安全和隐私

- AccessKey Secret、AI API Key、IPTV 凭据继续只保存加密值；读取接口只返回布尔配置状态。
- 两个翻译引擎在发出第三方请求前都调用现有 IPTV 敏感信息过滤。
- 日志只记录 provider、业务错误码、会话技术名和请求状态，不记录客户原文、译文或供应商凭据。
- 系统标签只包含固定标签名，不包含试用账号、密码、播放地址、手机号或供应商返回内容。
- 机器翻译请求仍只允许管理员认证、CSRF 校验、当前聊天引用和当前会话范围。
- 引擎切换和延迟设置均为面板配置，不新增公网端口或外部服务。

## 8. 迁移、备份与回滚

实施前记录：

- 当前分支、提交和工作树差异；
- 当前面板镜像标识；
- 生效 Compose/环境配置的脱敏摘要；
- SQLite、面板配置和凭据的可恢复备份位置。

迁移 011 必须可重复执行，只创建新表、索引和默认单例配置，不删除已有表或数据。

代码回滚使用 1.0.17 面板镜像或变更前提交。除非确认数据库损坏，不用旧数据库覆盖升级后新增记录；系统标签和新设置在代码回退时保留，旧代码会忽略新增表。

本轮不执行远端 Docker 重启或生产发布。Docker Desktop Linux 引擎恢复后，另行验证容器状态、日志、健康接口、持久化和真实页面访问。

## 9. 测试与验收

### 9.1 翻译后端

- 专业版 SDK 使用 `TranslateRequest`、`auto -> zh`、`text`、`communication`；
- 专业版超长、权限、服务未开通、限流和网络错误映射正确；
- AI 引擎复用现有结构化输出和错误处理；
- provider 配置读写、默认值和非法值校验；
- 两个 provider 缓存互不污染；
- 切换引擎后过期响应不能写入当前客户；
- 两个 provider 都执行 IPTV 凭据过滤；
- 配置读取和日志无秘密回显。

### 9.2 IPTV 标签

- 成功发送后出现 `IPTV测试过`；
- 创建成功但发送失败不出现；
- 结果未知、取消和失败不出现；
- 重复成功事件不重复；
- 跨会话复用时当前客户能看到标签；
- AI 总结刷新、手动标签编辑和客户列表刷新不删除系统标签。

### 9.3 延迟设置

- 0、50、100、120 四个边界行为；
- 非法字符串、负数和超范围值；
- 两个技术会话互不影响；
- 值为 100 时与当前默认区间一致；
- 设置保存、重读和页面滑杆显示一致；
- 人工发送和跟进任务不受影响。

### 9.4 集成回归

- Python 全量单元测试；
- 前端脚本语法检查；
- 设置页、聊天页和 IPTV 设置页浏览器验收；
- 新鲜 SQLite 数据库迁移；
- 1.0.17 数据库升级迁移；
- `git diff --check`；
- 敏感字段扫描；
- Docker 运行环境恢复后，再执行容器和真实访问验收。

## 10. 实施顺序

在本设计获得确认后，先写入逐文件实施计划，再按以下顺序开发：

1. 基线、备份和迁移演练；
2. 专业版阿里云 SDK 调整及单元测试；
3. 全局翻译 provider 路由、API 和缓存隔离；
4. 翻译设置和聊天页手动切换；
5. IPTV 系统标签、成功发送钩子和页面展示；
6. 回复延迟存储、服务端算法和滑杆 UI；
7. 全量测试、迁移回归和文档更新；
8. 输出本地验证证据和未验证项；
9. 如需线上发布，另行提交发布清单并等待发布授权。

