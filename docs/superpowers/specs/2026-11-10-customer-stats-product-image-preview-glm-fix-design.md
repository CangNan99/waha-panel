# 客户统计、产品图片引用与 GLM 配置修复设计

日期：2026-11-10  
状态：待用户审阅；基于已确认的推荐路线  
基线：`release/glm-multimodal-1.0.26`  
范围：面板本地代码、数据库迁移、测试和页面资源；不包含生产部署

## 意图

让状态概览能按 WhatsApp 账号查看今日新进客户和累计客户；让产品图片自动引用在产品名、SKU、标签别名之间稳定命中且不会模糊猜图；让客户图片可以在聊天中打开私有预览窗口并放大缩小；修复 GLM 视觉理解配置填写完整 `/chat/completions` 地址时产生重复路径的 `Not found`。

## 目标

- 每个账号和当前统计范围显示 `new_today` 与 `total` 客户数量。
- “今日新客户”按 Asia/Shanghai 当天第一次收到的非群聊入站消息计算。
- “总客户”按账号内去重的客户聊天计算；不返回手机号、聊天 ID 或 HMAC。
- 产品图片按完整产品名、SKU、配置标签别名的规范化精确命中顺序解析；只在唯一启用主图时发送。
- 产品图片命中不唯一、图片停用、人工接管、会话断开、每日上限或发送结果未知时不重复发送。
- 客户图片在聊天窗口中可点击打开，支持放大、缩小、复位、拖动查看、Esc 关闭和移动端触摸缩放；预览请求继续走现有受保护媒体接口。
- GLM 视觉理解、视觉推理、语言和图像生成配置同时接受服务根地址与已带 endpoint 的地址，并统一生成单一正确请求路径。
- 产品图片导航和产品图片资产卡使用明确的图片图标。

## 非目标与边界

- 不修改 WAHA API、Docker 网络、端口、反向代理或生产容器。
- 不用实时拉取全部 WAHA 聊天作为客户统计主数据源；统计以面板收到并归档的入站消息为准。
- 不做模糊相似度、语言模型自由猜测或未配置的产品别名推断。
- 不把客户手机号、完整聊天 ID、图片二进制、API Key 或绝对路径写入统计响应、普通日志或前端状态。
- 不改变现有产品图片发送幂等、`UNKNOWN` 不重试、人工接管和每日上限规则。

## 方案与取舍

### 选定方案：本地首次来客记录 + 确定性产品图片解析

新增一个轻量客户统计表，按 `session_name + customer_key_hmac` 保存首次入站和最近入站时间。Webhook 在成功归档非群聊入站消息后幂等写入记录；监控服务只读聚合，状态页展示账号行和汇总卡片。

产品图片继续复用现有资产库和发送账本，在资产上增加规范化产品名、SKU、别名字段；解析时按完整产品名、SKU、别名顺序收集候选，最终仍交给已有优先级与唯一主图规则决定。这样不会因为模糊文本或历史上下文误发图片。

GLM 适配器在保存和请求两个边界都做 endpoint 规范化：识别并去掉末尾的 `/chat/completions` 或 `/images/generations`，再按能力追加一次正确后缀。旧的根地址行为保持不变。

聊天图片预览使用现有媒体代理 URL，不新增公开静态图片路径。预览窗口是页面内私有 `<dialog>`，图片缩放只作用于当前预览，不改变消息列表布局。

## 数据与接口

新增迁移 `018_customer_first_seen.sql`，创建：

```sql
CREATE TABLE IF NOT EXISTS customer_first_seen (
    session_name TEXT NOT NULL,
    customer_key_hmac TEXT NOT NULL,
    first_inbound_at INTEGER NOT NULL,
    last_inbound_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (session_name, customer_key_hmac)
);
CREATE INDEX IF NOT EXISTS idx_customer_first_seen_first
    ON customer_first_seen(session_name, first_inbound_at);
```

`MonitoringService.overview()` 新增：

```json
{
  "customer_counts": {"total": 0, "new_today": 0},
  "sessions": [
    {"customer_counts": {"total": 0, "new_today": 0}}
  ]
}
```

数据库不可用或统计表不可用时，客户统计字段返回 `null` 并加入脱敏的 `partial_errors`，不阻塞其余状态展示。

产品图片资产对外元数据增加安全字段：`product_name`、`sku`、`aliases`；不返回文件路径。已有标签、主图、优先级和发送账本保持兼容。

## 页面行为

- 状态概览顶部增加“今日新客户”和“总客户数”卡片；账号表增加对应两列，按范围切换时同步刷新。
- 产品图片导航使用图片图标；资产卡标题区显示同一图标和命中字段摘要。
- 聊天图片增加可点击样式和无障碍标签；打开预览后显示缩放控制、当前比例、关闭按钮和加载失败提示。
- 预览窗口支持 `+`、`−`、`100%` 复位、Esc 关闭、点击遮罩关闭，以及鼠标滚轮/触摸手势缩放；尊重 `prefers-reduced-motion`。

## 测试与验收

- 迁移重复执行不报错；首次来客记录按账号和客户 HMAC 隔离；同一客户重复入站不增加总数。
- Asia/Shanghai 午夜边界、历史数据、群聊、状态消息和数据库不可用场景均有测试。
- 产品名、SKU、别名命中；大小写、Unicode NFKC 和空白折叠命中；模糊子串、歧义候选、停用资产和重复 webhook 不发送。
- GLM 根地址和完整 `/chat/completions`、`/images/generations` 地址分别只发出一次正确 endpoint；API Key 不出现在错误文本和日志中。
- 聊天图片预览的 DOM、缩放状态、键盘关闭和脚本语法有前端测试。
- 运行相关 Python 单元测试、Node 前端语法测试、内联脚本检查、Python 编译检查和 `git diff --check`。

## 回滚

代码回滚到 1.0.26 即可；新增表只追加，不删除历史表和客户消息。若需要回到某个完整数据时间点，同时恢复 SQLite 与 `panel_data` 卷。此次实现阶段不执行镜像构建、推送、重启或公网验证。
