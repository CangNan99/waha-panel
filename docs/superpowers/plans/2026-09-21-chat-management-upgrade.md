# WhatsAPP AI管理面板聊天管理升级实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不修改 WAHA 服务的前提下，完成客户备注/客户标签/头像、双向自动回复上下文、手机端布局、媒体触发设置、扫码揭示动效、手机号配对等待反馈和 WhatsAPP AI管理面板 品牌统一。

**Architecture:** 沿用现有 Python PanelState、ChatService、PanelHandler、SQLite 键值设置和内联 HTML/CSS/JavaScript。消息先经过统一的归档与去重管线，再经过自动回复门控；头像通过面板鉴权代理和有界缓存读取；二维码与手机号配对由浏览器端显式状态机驱动，真实凭证只在服务端校验成功后进入展示层。

**Tech Stack:** Python 3 标准库、sqlite3、现有 WAHA HTTP client、Pillow、Python unittest、Node.js node:test/vm；不新增运行时依赖、容器、队列或前端框架。

**Spec:** docs/superpowers/specs/2026-09-20-chat-management-upgrade-design.md

## Global Constraints

- 本次只修改管理面板；不修改 WAHA 镜像、WAHA session 数据结构、Webhook 协议、Docker Compose、Nginx、端口或外部 AI 服务接口。
- 上下文滑块范围为 5–50，默认 5；数值 N 表示客户 N 条 + 客服/自己 N 条，最多 100 条历史上下文。
- 图片、视频、音频、文件四类自动回复媒体选项默认全部关闭；ptt 归入音频，document 归入文件。
- 自己发出的消息进入上下文但永远不能触发自动回复；归档或回填失败时自动回复采用 fail-closed。
- 扫码待刷新/加载态必须是明确不可扫描的磨砂占位；真实二维码预加载并校验成功后才可显示，揭示过渡为 420ms ease-out。
- 手机号配对等待状态使用 180ms ease-out 状态过渡和轻量提示；配对码本身不闪烁、不跳动，失败后可重试。
- 媒体只以受限文字元数据进入 AI；本版本不上传原始图片、视频、音频或文件。
- 面板品牌显示精确文本 WhatsAPP AI管理面板；底层技术标签中的“WAHA”可以保留。
- 不把 API Key、密码、Webhook Secret、原始头像 URL、媒体下载 URL、二维码 payload 或客户隐私写入页面、普通日志或测试夹具。
- 优先复用现有 session_settings、conversation_messages、message_dedupe，不删除/重建历史表；任何新增迁移只能是幂等 additive migration。
- 所有修改使用 apply_patch；每个任务完成自己的测试和提交后再进入下一任务。

## File Structure

| 文件 | 责任 |
| --- | --- |
| `panel/app.py` | WAHA HTTP 适配、设置契约、双向消息归档、Webhook 自动回复门控、HTTP 路由，以及状态/设置页面模板 |
| `panel/chat_service.py` | 安全聊天引用、聊天概览元数据、头像缓存/代理数据、人工与自动发送后的出站归档通知 |
| `panel/chat_page.py` | 聊天列表和会话头部渲染、移动端主要操作/更多操作、对话框/toast/列表详情有限动效 |
| `panel/commerce_page.py` | 订单/支付管理页的面板品牌文案；不改变业务接口 |
| `tests/test_chat_upgrade.py` | 新设置、N 对 N 上下文、Webhook/媒体门控、历史回填、头像安全代理的集中回归测试 |
| `tests/test_chat_features.py` | 既有模板、QR、品牌、聊天概览与发布边界回归 |
| `tests/test_chat_engagement.py` | 既有标签、总结、发送幂等、跟进和人工接管回归 |
| `tests/frontend_syntax.test.mjs` | 内联 JavaScript 语法、过期请求隔离、页面 DOM/CSS/状态机契约 |

不新增产品模块：当前代码以 `app.py`/`chat_service.py`/`chat_page.py` 为既有所有权边界，本次在边界内增加小型私有函数，避免为一次性数据转换创建新子系统。

## Review Focus

1. **过期二维码响应/跨会话响应**：切换会话后旧请求晚到不能覆盖当前占位或释放当前按钮；由任务 6 的 Node vm 测试固定。
2. **缺失或重复消息 ID**：fromMe 消息必须只归档不触发，重复 Webhook 不能复制历史或回声回复；由任务 2 的 Python 测试固定。
3. **非法设置输入**：上下文滑块越界、损坏 JSON、未知媒体类型必须回退/拒绝而不破坏旧值；由任务 1 和任务 5 的 Python/API 测试固定。
4. **头像安全与 @lid**：完整聊天 ID、跨会话引用、非法外部 URL、缓存失败和并发请求必须安全降级；由任务 3 的 client/service/handler 测试固定。
5. **窄屏与辅助技术**：320–430 CSS 像素无横向溢出，人工/恢复 AI 始终可操作，prefers-reduced-motion 不出现旋转/弹跳；由任务 4、任务 6 的静态契约和视口验收固定。

---

### Task 1: 自动回复设置契约与 N 对 N 上下文

**Files:**
- Modify: panel/app.py:106-122（默认值与范围常量）
- Modify: panel/app.py:946-1035（settings_payload、save_settings）
- Modify: panel/app.py:1337-1362（_conversation_context）
- Create: tests/test_chat_upgrade.py（设置和上下文测试夹具）

**Interfaces:**
- Consumes: 现有 session_settings 键值表和 conversation_messages(session_name, chat_id, direction, message_id, content, created_at)。
- Produces: settings_payload(session) 返回 auto_reply_context_per_side: int 与 auto_reply_media_types: dict[str, bool]；_conversation_context(chat_id, exclude_message_id=None, session_name=DEFAULT_SESSION_NAME, per_side=None) 返回按时间升序的字符串。

- [ ] **Step 0: 建立所有升级测试共用的脱敏 fixture。**

~~~python
import base64
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from cryptography.fernet import Fernet
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from panel.app import PanelHandler, PanelState, WahaApiError, WahaClient, init_db
from panel.chat_service import ChatAccessError, ChatServiceError

class UpgradeClient:
    def __init__(self):
        self.messages = []
        self.send_calls = 0
        self.send_result = {"id": "wamid.sent"}
    def get_sessions(self):
        return [{"name": "default", "status": "WORKING"}, {"name": "sales", "status": "WORKING"}]
    def get_chats(self, _session, _limit, _offset):
        return [{"id": "chat-1@c.us", "name": "测试客户", "lastMessage": {}}]
    def get_messages(self, _session, _chat_id, limit, offset, _before=None, download_media=False):
        return self.messages[int(offset):int(offset) + int(limit)]
    def send_text(self, _session, _chat_id, _text):
        self.send_calls += 1
        return dict(self.send_result)

class UpgradeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.client = getattr(self, "client_class", UpgradeClient)()
        self.state = PanelState(
            self.database, self.client, "redacted-test-key",
            sleep_fn=lambda _seconds: None,
            uniform_fn=lambda _start, _end: 0,
            data_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        self.state.save_settings({"auto_reply_all_day": True}, "default")
        self.state.last_ai_context = ""
        def fake_ai_reply(_settings, _incoming, **kwargs):
            self.state.last_ai_context = kwargs.get("conversation_context", "")
            return "测试回复", "ai"
        self.state._ai_reply = fake_ai_reply
        self.chat_ref = self.state.chat.overview("default")["items"][0]["chat_ref"]
    def tearDown(self):
        self.state.stop_background_services()
        self.temp.cleanup()
    def write_setting(self, key, value, session="default"):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO session_settings(session_name,key,value,updated_at) VALUES (?,?,?,1) "
                "ON CONFLICT(session_name,key) DO UPDATE SET value=excluded.value",
                (session, key, value),
            )
    def event(self, body="", from_me=False, message_id="m-1", chat_id="chat-1@c.us",
              message_type="chat", has_media=False, is_group=False, **extra):
        message = {
            "id": message_id, "fromMe": from_me, "from": chat_id, "to": chat_id,
            "body": body, "type": message_type, "hasMedia": has_media, "isGroup": is_group,
        }
        message.update(extra)
        return json.dumps({"event": "message", "session": "default", "payload": message}).encode("utf-8")
    def insert_history(self, inbound, outbound):
        with sqlite3.connect(self.database) as connection:
            sequence = []
            for index in range(max(inbound, outbound)):
                if index < inbound: sequence.append(("inbound", f"in-{index}", f"in-{index}"))
                if index < outbound: sequence.append(("outbound", f"out-{index}", f"out-{index}"))
            connection.executemany(
                "INSERT INTO conversation_messages(session_name,chat_id,direction,message_id,content,created_at) "
                "VALUES ('default','chat-1@c.us',?,?,?,?)",
                [(direction, message_id, content, index + 1) for index, (direction, message_id, content) in enumerate(sequence)],
            )
    def messages_for(self, chat_id):
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(
                "SELECT direction,message_id,content,created_at FROM conversation_messages "
                "WHERE session_name='default' AND chat_id=? ORDER BY id", (chat_id,),
            )]
    def count_message_id(self, message_id):
        with sqlite3.connect(self.database) as connection:
            return connection.execute(
                "SELECT COUNT(*) FROM conversation_messages WHERE session_name='default' AND message_id=?",
                (message_id,),
            ).fetchone()[0]
    def count_direction(self, direction):
        return sum(item["direction"] == direction for item in self.messages_for("chat-1@c.us"))
    def message_content(self, message_id):
        with sqlite3.connect(self.database) as connection:
            row = connection.execute(
                "SELECT content FROM conversation_messages WHERE session_name='default' AND message_id=?",
                (message_id,),
            ).fetchone()
        return row[0] if row else ""
    def count_outbound_content(self, content):
        return sum(
            item["direction"] == "outbound" and item["content"] == content
            for item in self.messages_for("chat-1@c.us")
        )
    def dispatch_media(self, message_type, message_id):
        return self.state.handle_webhook(self.event(
            message_type=message_type, has_media=True, message_id=message_id,
        ), dispatch=False)
    def last_context_or_prompt(self):
        return self.state.last_ai_context
    def decode_message_ref(self, message_ref):
        return self.state.chat._decode_message("default", message_ref)[1]
    @staticmethod
    def history_fixture(inbound, outbound):
        items = []
        for index in range(max(inbound, outbound)):
            if index < inbound:
                items.append({"id": f"history-in-{index}", "timestamp": index * 2 + 1,
                              "fromMe": False, "from": "chat-1@c.us", "body": f"客户历史 {index}"})
            if index < outbound:
                items.append({"id": f"history-out-{index}", "timestamp": index * 2 + 2,
                              "fromMe": True, "to": "chat-1@c.us", "body": f"客服历史 {index}"})
        return items
~~~

- [ ] **Step 1: 写失败测试，锁定默认值、边界和双方历史。**

~~~python
class SettingsAndContextTests(UpgradeTestCase):
    def test_settings_default_and_context_are_per_side(self):
        payload = self.state.settings_payload("default")
        self.assertEqual(payload["auto_reply_context_per_side"], 5)
        self.assertEqual(
            payload["auto_reply_media_types"],
            {"image": False, "video": False, "audio": False, "file": False},
        )
        self.insert_history(inbound=6, outbound=6)
        context = self.state._conversation_context("chat-1@c.us", session_name="default", per_side=5)
        self.assertEqual(context.count("客户："), 5)
        self.assertEqual(context.count("客服："), 5)
        self.assertLess(context.index("客户：in-1"), context.index("客服：out-1"))
~~~

以下方法继续放入同一个 `SettingsAndContextTests` 类；读取损坏值安全回退，保存非法值抛出 `ValueError` 且不覆盖旧值：

~~~python
    def test_corrupt_stored_settings_fall_back_to_safe_defaults(self):
        self.write_setting("auto_reply_context_per_side", "not-a-number")
        self.write_setting("auto_reply_media_types", "{broken")
        payload = self.state.settings_payload("default")
        self.assertEqual(payload["auto_reply_context_per_side"], 5)
        self.assertEqual(payload["auto_reply_media_types"], {
            "image": False, "video": False, "audio": False, "file": False,
        })

    def test_invalid_settings_are_rejected_without_overwriting(self):
        before = self.state.settings_payload("default")
        with self.assertRaisesRegex(ValueError, "5-50"):
            self.state.save_settings({"auto_reply_context_per_side": 51}, "default")
        with self.assertRaisesRegex(ValueError, "媒体"):
            self.state.save_settings({"auto_reply_media_types": {"sticker": True}}, "default")
        after = self.state.settings_payload("default")
        self.assertEqual(after, before)
~~~

- [ ] **Step 2: 运行目标测试确认当前实现失败。**

~~~powershell
python -m unittest tests.test_chat_upgrade.SettingsAndContextTests -v
~~~

Expected: 新字段不存在或上下文仍按单一 20 条限制，测试失败。

- [ ] **Step 3: 添加归一化函数和默认设置。**

在 panel/app.py 定义明确范围和白名单：

~~~python
CONTEXT_PER_SIDE_MIN = 5
CONTEXT_PER_SIDE_MAX = 50
DEFAULT_AUTO_REPLY_MEDIA_TYPES = {
    "image": False,
    "video": False,
    "audio": False,
    "file": False,
}

def normalize_context_per_side(value, default=5):
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    if number < CONTEXT_PER_SIDE_MIN or number > CONTEXT_PER_SIDE_MAX:
        return default
    return number
~~~

normalize_media_types 只接受四个白名单键并把值转为布尔；DEFAULT_SETTINGS 存储 "5" 和 JSON 字符串。settings_payload 永远返回完整四键对象，不回传 ai_api_key。

~~~python
MEDIA_TYPE_KEYS = ("image", "video", "audio", "file")

def normalize_media_types(value, strict=False):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            value = {}
    if not isinstance(value, dict):
        if strict:
            raise ValueError("媒体处理选项格式不正确")
        value = {}
    unknown = set(value) - set(MEDIA_TYPE_KEYS)
    if strict and unknown:
        raise ValueError("包含不支持的媒体处理选项")
    return {key: as_bool(value.get(key, False)) for key in MEDIA_TYPE_KEYS}
~~~

`settings_payload` 对存储值调用宽容的归一化函数；`save_settings` 对用户输入使用严格分支，先验证所有新字段，再一次性写入，避免只保存一半：

~~~python
if "auto_reply_context_per_side" in payload:
    try:
        context_count = int(str(payload["auto_reply_context_per_side"]).strip())
    except (TypeError, ValueError) as error:
        raise ValueError("历史消息数必须为 5-50") from error
    if not CONTEXT_PER_SIDE_MIN <= context_count <= CONTEXT_PER_SIDE_MAX:
        raise ValueError("历史消息数必须为 5-50")
    updates["auto_reply_context_per_side"] = str(context_count)
if "auto_reply_media_types" in payload:
    updates["auto_reply_media_types"] = json.dumps(
        normalize_media_types(payload["auto_reply_media_types"], strict=True),
        ensure_ascii=False, sort_keys=True,
    )
~~~

- [ ] **Step 4: 将上下文查询改为按方向分别取 N 条再合并。**

先读取设置得到 N，再按方向各执行一条 `ORDER BY id DESC LIMIT ?` 子查询；把两组结果按 `(created_at, id)` 升序合并并排除 `exclude_message_id`。字符超过 `CONTEXT_CHARACTER_LIMIT` 时从合并列表最早一条开始丢弃，必要时只截断当前保留集合的最早一条，确保最新上下文优先；当前触发消息仍由 `incoming_text` 单独传入。

~~~python
def _conversation_context(self, chat_id, exclude_message_id=None,
                          session_name=DEFAULT_SESSION_NAME, per_side=None):
    limit = normalize_context_per_side(
        per_side if per_side is not None
        else self._settings(session_name).get("auto_reply_context_per_side", "5")
    )
    inbound = self._context_rows(session_name, chat_id, "inbound", limit, exclude_message_id)
    outbound = self._context_rows(session_name, chat_id, "outbound", limit, exclude_message_id)
    rows = sorted(inbound + outbound, key=lambda row: (row["created_at"], row["id"]))
    return self._bounded_context_text(rows, CONTEXT_CHARACTER_LIMIT)

def _context_rows(self, session_name, chat_id, direction, limit, exclude_message_id):
    with sqlite3.connect(self.database_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(
            "SELECT id,direction,message_id,content,created_at FROM conversation_messages "
            "WHERE session_name=? AND chat_id=? AND direction=? "
            "AND (? IS NULL OR message_id IS NULL OR message_id != ?) "
            "ORDER BY id DESC LIMIT ?",
            (normalize_session_name(session_name), chat_id, direction,
             exclude_message_id, exclude_message_id, limit),
        )]

@staticmethod
def _bounded_context_text(rows, character_limit):
    lines = [
        ("客户" if row["direction"] == "inbound" else "客服")
        + "：" + str(row["content"]).strip()
        for row in rows if str(row["content"] or "").strip()
    ]
    while len("\n".join(lines)) > character_limit and len(lines) > 1:
        lines.pop(0)
    if lines and len(lines[0]) > character_limit:
        lines[0] = lines[0][:character_limit - 1] + "…"
    return "\n".join(lines)
~~~

- [ ] **Step 5: 运行目标测试确认通过并提交。**

~~~powershell
python -m unittest tests.test_chat_upgrade.SettingsAndContextTests -v
git diff --check
git add panel/app.py tests/test_chat_upgrade.py
git commit -m "feat: add per-side auto reply context settings"
~~~

Expected: 设置默认/边界和 N 对 N 历史测试全部通过；只包含本任务文件。

### Task 2: 双向消息归档、媒体门控与出站回调

**Files:**
- Modify: panel/app.py:823-832,1573-1670（统一出站归档和 Webhook 管线）
- Modify: panel/chat_service.py:128-150,582-664,857-884（手动文字、自动文字、图片发送后的归档回调）
- Modify: tests/test_chat_upgrade.py（Webhook/媒体/去重测试）
- Modify: tests/test_chat_engagement.py（既有发送回归断言必要时更新）

**Interfaces:**
- Consumes: Task 1 的设置归一化和 _conversation_context。
- Produces: `ChatService(database_path, client, reference_codec, encryption_key, outbound_recorder=None)` 可选回调；回调签名为 `outbound_recorder(session_name, chat_id, message_id, content, created_at=None, request_id=None)`；`PanelState._archive_message(session_name, chat_id, direction, message_id, content, created_at=None)` 幂等写入 `message_dedupe` 与 `conversation_messages`。

- [ ] **Step 1: 写失败测试，覆盖 fromMe、重复 Webhook、出站上下文和四类媒体。**

~~~python
class WebhookArchiveTests(UpgradeTestCase):
    def test_from_me_is_archived_but_never_triggers(self):
        result = self.state.handle_webhook(self.event(body="人工内容", from_me=True, message_id="out-1"), dispatch=True)
        self.assertEqual(result["action"], "ignored")
        self.assertEqual(self.client.send_calls, 0)
        self.assertEqual(self.messages_for("chat-1@c.us")[0]["direction"], "outbound")

    def test_duplicate_inbound_is_not_archived_or_sent_twice(self):
        event = self.event(body="客户问题", message_id="in-1")
        self.state.handle_webhook(event, dispatch=True)
        self.state.handle_webhook(event, dispatch=True)
        self.assertEqual(self.client.send_calls, 1)
        self.assertEqual(self.count_message_id("in-1"), 1)

    def test_media_setting_controls_trigger_but_keeps_metadata(self):
        self.state.save_settings({"auto_reply_media_types": {"image": True}})
        result = self.state.handle_webhook(self.event(message_type="image", has_media=True, message_id="img-1"))
        self.assertEqual(result["action"], "replied")
        self.assertIn("图片", self.last_context_or_prompt())
~~~

以下方法继续放入同一个 `WebhookArchiveTests` 类，补齐门控、归类和回填：

~~~python
    def test_disabled_media_is_archived_without_triggering(self):
        result = self.state.handle_webhook(
            self.event(message_type="image", has_media=True, message_id="img-off"), dispatch=True
        )
        self.assertEqual(result["action"], "ignored")
        self.assertEqual(self.client.send_calls, 0)
        self.assertIn("图片", self.message_content("img-off"))

    def test_ptt_and_document_follow_audio_and_file_switches(self):
        self.state.save_settings({
            "auto_reply_enabled": True,
            "auto_reply_media_types": {"audio": True, "file": True},
        })
        self.assertEqual(self.dispatch_media("ptt", "ptt-1")["action"], "replied")
        self.assertEqual(self.dispatch_media("document", "doc-1")["action"], "replied")

    def test_group_status_and_missing_id_never_trigger(self):
        for event in (
            self.event(chat_id="group@g.us", message_id="g-1", is_group=True),
            self.event(chat_id="status@broadcast", message_id="s-1", message_type="status"),
            self.event(chat_id="chat-1@c.us", message_id=""),
        ):
            self.assertEqual(self.state.handle_webhook(event)["action"], "ignored")
        self.assertEqual(self.client.send_calls, 0)

    def test_panel_send_and_webhook_echo_store_one_outbound_row(self):
        result = self.state.chat.send_text(
            "default", self.chat_ref, "人工回复", "00000000-0000-0000-0000-000000000011"
        )
        self.state.handle_webhook(self.event(
            body="人工回复", from_me=True, message_id=self.decode_message_ref(result["message_ref"])
        ))
        self.assertEqual(self.count_outbound_content("人工回复"), 1)

    def test_missing_local_send_id_uses_request_id_but_webhook_without_id_is_ignored(self):
        self.client.send_result = {}
        self.state.chat.send_text(
            "default", self.chat_ref, "本地出站", "00000000-0000-0000-0000-000000000012"
        )
        self.assertEqual(self.count_message_id("local:00000000-0000-0000-0000-000000000012"), 1)
        self.assertEqual(self.state.handle_webhook(self.event(from_me=True, message_id=""))["action"], "ignored")

    def test_missing_local_history_backfills_both_directions_before_ai(self):
        self.client.messages = self.history_fixture(inbound=5, outbound=5)
        self.state.handle_webhook(self.event(body="现在的问题", message_id="current-1"), dispatch=False)
        self.assertEqual(self.count_direction("inbound"), 6)
        self.assertEqual(self.count_direction("outbound"), 5)
        self.assertIn("客服：", self.state.last_ai_context)
~~~

- [ ] **Step 2: 运行目标测试确认当前 fromMe 早退和媒体早退仍不满足需求。**

~~~powershell
python -m unittest tests.test_chat_upgrade.WebhookArchiveTests -v
~~~

Expected: 当前实现会在 fromMe/媒体进入归档前返回，至少一项测试失败。

- [ ] **Step 3: 实现统一归档函数和安全媒体标记。**

实现 _archive_message(session_name, chat_id, direction, message_id, content, created_at)：

1. 清理控制字符、限制内容长度。
2. 有真实消息 ID 时 INSERT OR IGNORE message_dedupe，只有首次插入才写 conversation_messages。
3. 面板发送无 ID 时只接受已验证的本地 request ID；Webhook 无 ID 直接返回不可触发结果。
4. 对入站/出站媒体生成形如 `[客户发送图片] 文件名=<sanitized-name> MIME=<safe-mime> 说明=<sanitized-caption>` 的安全文本，不下载二进制、不携带远程 URL。

~~~python
CONTEXT_FIELD_LIMIT = 512
MEDIA_LABELS = {"image": "图片", "video": "视频", "audio": "音频", "file": "文件"}

def sanitize_context_content(value, limit=65535):
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(value or "")).strip()[:limit]

def format_media_context(direction, kind, filename="", mime="", caption="", size=None):
    actor = "客服" if direction == "outbound" else "客户"
    parts = [f"[{actor}发送{MEDIA_LABELS[kind]}]"]
    if filename: parts.append("文件名=" + sanitize_context_content(filename, CONTEXT_FIELD_LIMIT))
    if mime: parts.append("MIME=" + sanitize_context_content(mime, 120))
    if isinstance(size, int) and 0 <= size <= 1024 * 1024 * 1024: parts.append(f"大小={size}")
    if caption: parts.append("说明=" + sanitize_context_content(caption, 4096))
    return " ".join(parts)

def _archive_message(self, session_name, chat_id, direction, message_id,
                     content, created_at=None):
    message_id = str(message_id or "").strip()
    text = sanitize_context_content(content)
    if not message_id or not text:
        return False
    connection = sqlite3.connect(self.database_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        inserted = connection.execute(
            "INSERT OR IGNORE INTO message_dedupe(session_name,message_id,received_at) VALUES (?,?,?)",
            (session_name, message_id, int(created_at or self.clock())),
        ).rowcount
        if inserted:
            connection.execute(
                "INSERT INTO conversation_messages(session_name,chat_id,direction,message_id,content,created_at) "
                "VALUES (?,?,?,?,?,?)",
                (session_name, chat_id, direction, message_id, text, int(created_at or self.clock())),
            )
        connection.commit()
        return bool(inserted)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
~~~

为 `ChatService` 增加可选 `outbound_recorder`，在 `send_text`、`send_image`、`send_automated_text` 成功后调用；`PanelState.enable_chat_services` 传入 `_record_outbound_message`。`PanelState._send_text` 也在成功返回后提取消息 ID 并归档，覆盖业务消息发送路径。删除当前 `handle_webhook` 发送后对 `_record_conversation_message(chat_id, "outbound", None, content, created_at, session_name)` 的直接调用，避免回调已经归档后再写一行无 ID 历史。

~~~python
def _record_outbound_message(self, session_name, chat_id, message_id,
                             content, created_at=None, request_id=None):
    archive_id = str(message_id or "").strip()
    if not archive_id and request_id:
        archive_id = "local:" + str(request_id)
    return self._archive_message(
        normalize_session_name(session_name), str(chat_id), "outbound",
        archive_id, content, created_at,
    )
~~~

`ChatService.send_text` 与 `send_automated_text` 把已发送文字作为 `content`；`send_image` 使用 `format_media_context("outbound", "image", filename=safe_name, mime=mimetype, caption=safe_caption)`。三个路径都把本次 `client_request_id` 作为 `request_id` 传给回调，只有 WAHA 响应缺少消息 ID 时才使用 `local:<request_id>`。

- [ ] **Step 4: 重排 handle_webhook 的门控顺序。**

按规范顺序执行：解析 → 归一化 → 去重/归档 → 判断 fromMe/群聊/状态 → 判断媒体白名单 → 人工接管/时间段/总开关 → 有界历史回填 → N 对 N 上下文 → AI → 发送 → 出站归档。自己发出的消息和归档失败路径均不得调用 `_ai_reply` 或发送函数；入站活动只由客户消息更新。

~~~python
MEDIA_KIND = {
    "image": "image", "video": "video", "audio": "audio",
    "ptt": "audio", "document": "file",
}

archived = self._archive_message(
    session_name, chat_id, "outbound" if from_me else "inbound",
    message_id, context_content, received_at,
)
if not archived:
    return {"action": "ignored", "reason": "重复或无法归档的消息", "session": session_name}
if from_me or is_group or is_status:
    return {"action": "ignored", "reason": "消息不符合自动回复范围", "session": session_name}
if media_kind and not media_types[media_kind]:
    return {"action": "ignored", "reason": "该媒体类型未启用", "session": session_name}
self._backfill_conversation_history(session_name, chat_id, per_side)
context = self._conversation_context(
    chat_id, exclude_message_id=message_id,
    session_name=session_name, per_side=per_side,
)
~~~

`_backfill_conversation_history` 仅在某一方向本地记录少于 N 条时调用 `get_messages(session_name, chat_id, limit=min(2*N, 100), offset=0, before=None, download_media=False)`，按时间从旧到新归档并复用 `_archive_message` 去重；WAHA 读取或归档任一步失败都抛出受控错误，使当前自动回复停止而不是带缺失上下文继续发送。

- [ ] **Step 5: 运行回归测试并提交。**

~~~powershell
python -m unittest tests.test_chat_upgrade.WebhookArchiveTests tests.test_chat_engagement.ChatServiceEngagementTests -v
git diff --check
git add panel/app.py panel/chat_service.py tests/test_chat_upgrade.py tests/test_chat_engagement.py
git commit -m "feat: archive bidirectional context and gate media replies"
~~~

### Task 3: 备注/标签摘要、头像适配器与安全代理

**Files:**
- Modify: panel/app.py:458-493,2494-2634（WAHA picture client 和头像 GET 路由）
- Modify: panel/chat_service.py:118-125,128-150,373-426（元数据批量读取、头像缓存、overview 字段）
- Modify: tests/test_chat_features.py（既有概览断言）
- Modify: tests/test_chat_upgrade.py（头像 client/service/handler 测试）

**Interfaces:**
- Consumes: 现有签名 chat_ref、Task 2 的会话验证规则。
- Produces: WahaClient.get_chat_picture(session_name, chat_id) -> (status, content_type, bytes)；ChatService.avatar(session, chat_ref) -> (status, content_type, bytes)；overview 返回 note、最多四个有序 labels、label_overflow 和面板内部头像 URL；新增 GET action avatar。

- [ ] **Step 1: 写失败测试，锁定 endpoint、@lid、缓存和无原始 URL。**

~~~python
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

class AvatarClient(UpgradeClient):
    def __init__(self):
        super().__init__()
        self.picture_mode = "ok"
        self.picture_response = (200, "image/png", PNG_1X1)
        self.picture_calls = []
        self.picture_call_count = 0
        self.active_picture_calls = 0
        self.max_active_picture_calls = 0
        self.picture_lock = threading.Lock()
    def get_chats(self, _session, _limit, _offset):
        return [{"id": "12345@lid", "name": "测试客户"}, {"id": "67890@c.us", "name": "第二客户"}]
    def get_chat_picture(self, session, chat_id):
        with self.picture_lock:
            self.picture_calls.append((session, chat_id))
            self.picture_call_count += 1
            self.active_picture_calls += 1
            self.max_active_picture_calls = max(self.max_active_picture_calls, self.active_picture_calls)
        try:
            time.sleep(.02)
            if self.picture_mode == "404":
                raise WahaApiError(404, "picture unavailable")
            return self.picture_response
        finally:
            with self.picture_lock:
                self.active_picture_calls -= 1

class AvatarAndMetadataTests(UpgradeTestCase):
    client_class = AvatarClient
    def setUp(self):
        super().setUp()
        items = self.state.chat.overview("default")["items"]
        self.item, self.other = items
        self.other_ref = self.other["chat_ref"]
        self.service = self.state.chat
    def run_parallel_avatar_requests(self, count):
        refs = [self.service._encode_chat("default", f"parallel-{index}@c.us") for index in range(count)]
        threads = [threading.Thread(target=self._ignore_avatar_error, args=(ref,)) for ref in refs]
        for thread in threads: thread.start()
        for thread in threads: thread.join(2)
    def _ignore_avatar_error(self, chat_ref):
        try: self.service.avatar("default", chat_ref)
        except ChatServiceError: pass

    def test_avatar_uses_full_chat_id_and_panel_proxy(self):
        item = self.service.overview("default")["items"][0]
        self.assertTrue(item["avatar_url"].startswith("/api/chat/sessions/default/avatar?"))
        self.assertNotIn("waha.example", item["avatar_url"])
        self.service.avatar("default", item["chat_ref"])
        self.assertEqual(self.client.picture_calls[-1][1], "12345@lid")

    def test_avatar_failure_returns_placeholder_without_breaking_overview(self):
        self.client.picture_mode = "404"
        self.assertRaises(ChatServiceError, self.service.avatar, "default", self.item["chat_ref"])
        self.assertTrue(self.service.overview("default")["items"])
~~~

同步更新 `tests/test_chat_features.py` 现有的 `ChatFeatureTests.test_avatar_and_customer_note_are_session_scoped`：不再断言 WAHA 原始头像 URL，改为断言 `avatar_url` 以 `/api/chat/sessions/default/avatar?chat_ref=` 开头的面板代理地址，并保留备注保存/读取的会话隔离断言。这样既固定浏览器不接触原始 URL，也不削弱原有备注回归覆盖。

以下方法继续放入同一个 `AvatarAndMetadataTests` 类，覆盖安全、缓存和并发：

~~~python
    def test_picture_json_url_must_stay_on_waha_origin(self):
        class Headers:
            @staticmethod
            def get_content_type(): return "application/json"
        class Response:
            status = 200
            headers = Headers()
            def __enter__(self): return self
            def __exit__(self, *_args): return False
            def read(self, _limit=-1):
                return b'{"data":{"url":"https://attacker.invalid/a.png"}}'
        client = WahaClient("http://waha:3000", "redacted", opener=lambda *_args, **_kwargs: Response())
        with self.assertRaisesRegex(ValueError, "不属于 WAHA"):
            client.get_chat_picture("default", "12345@lid")

    def test_avatar_rejects_oversized_or_non_image_body(self):
        cases = (
            (self.item["chat_ref"], "text/html", b"x"),
            (self.other_ref, "image/png", b"x" * (2 * 1024 * 1024 + 1)),
        )
        for chat_ref, content_type, body in cases:
            self.client.picture_response = (200, content_type, body)
            with self.assertRaises(ChatServiceError):
                self.service.avatar("default", chat_ref)

    def test_avatar_cache_and_negative_cache_bound_remote_calls(self):
        self.service.avatar("default", self.item["chat_ref"])
        self.service.avatar("default", self.item["chat_ref"])
        self.assertEqual(self.client.picture_call_count, 1)
        self.client.picture_mode = "404"
        with self.assertRaises(ChatServiceError):
            self.service.avatar("default", self.other_ref)
        with self.assertRaises(ChatServiceError):
            self.service.avatar("default", self.other_ref)
        self.assertEqual(self.client.picture_call_count, 2)

    def test_avatar_rejects_cross_session_reference(self):
        with self.assertRaises(ChatAccessError):
            self.service.avatar("sales", self.item["chat_ref"])

    def test_overview_orders_manual_labels_first_and_caps_visible_labels(self):
        for label in ("手动一", "手动二", "手动三"):
            self.service.add_manual_label("default", self.item["chat_ref"], label)
        self.service.save_summary_and_ai_labels(
            "default", self.item["chat_ref"], {"summary": "测试"}, ("AI一", "AI二", "AI三")
        )
        item = self.service.overview("default")["items"][0]
        self.assertEqual([entry["source"] for entry in item["labels"]], ["manual", "manual", "manual", "ai"])
        self.assertEqual(item["label_overflow"], 2)

    def test_avatar_concurrency_never_exceeds_four_remote_requests(self):
        self.run_parallel_avatar_requests(8)
        self.assertLessEqual(self.client.max_active_picture_calls, 4)
~~~

Handler 集成测试启动现有 `ThreadingHTTPServer` fixture：无 Basic 管理员认证请求头像端点预期 `401`，带认证的同会话引用返回 `200`、严格图片 Content-Type、`Cache-Control: private, no-store` 和 `X-Content-Type-Options: nosniff`。

~~~python
class AvatarRouteTests(AvatarAndMetadataTests):
    def setUp(self):
        super().setUp()
        self.state._admin_db_auth = False
        self.state.admin_username = "admin"
        self.state.admin_password = "test-password"
        PanelHandler.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PanelHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        super().tearDown()
    def test_avatar_route_requires_auth_and_returns_private_image(self):
        path = "/api/chat/sessions/default/avatar?chat_ref=" + quote(self.item["chat_ref"], safe="")
        with self.assertRaises(HTTPError) as denied:
            urlopen(self.url + path, timeout=3)
        self.assertEqual(denied.exception.code, 401)
        credential = base64.b64encode(b"admin:test-password").decode("ascii")
        request = Request(self.url + path, headers={"Authorization": "Basic " + credential})
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
~~~

- [ ] **Step 2: 运行目标测试确认当前 overview 返回原始头像 URL且没有代理路由。**

~~~powershell
python -m unittest tests.test_chat_upgrade.AvatarAndMetadataTests -v
~~~

- [ ] **Step 3: 添加 WAHA picture 适配器和有界缓存。**

在 WahaClient 增加 get_chat_picture，请求 /api/{session}/chats/{quote(chat_id)}/picture，兼容 JSON data.url 和图片 body；任何 URL 必须通过现有 WAHA origin 校验后再取回。ChatService 使用 256 项上限、24 小时成功 TTL、30 秒失败 TTL、最多 4 个并发请求和 2MB 响应上限；缓存键为 (session_name, full_chat_id)。

~~~python
MAX_AVATAR_BYTES = 2 * 1024 * 1024
AVATAR_CACHE_LIMIT = 256
AVATAR_TTL_SECONDS = 24 * 60 * 60
AVATAR_NEGATIVE_TTL_SECONDS = 30

def get_chat_picture(self, session_name, chat_id):
    session = self._session_path(session_name)
    chat = quote(str(chat_id), safe="")
    status, content_type, body = self.request_bytes(
        f"/api/{session}/chats/{chat}/picture", max_bytes=MAX_AVATAR_BYTES
    )
    if content_type.startswith("image/"):
        return status, content_type, body
    payload = json.loads(body.decode("utf-8"))
    picture_url = ((payload.get("data") or {}).get("url") or payload.get("url"))
    return self.get_media_bytes(picture_url, max_bytes=MAX_AVATAR_BYTES)
~~~

`request_bytes` 和 `get_media_bytes` 都使用 `response.read(max_bytes + 1)` 后拒绝超限内容；Pillow `Image.verify()` 验证字节确为 JPEG/PNG/WebP。`ChatService` 使用 `OrderedDict` 做 LRU 淘汰、`threading.BoundedSemaphore(4)` 限流，缓存项保存 `(expires_at, status, content_type, body_or_error)`；负缓存只保存公开错误码，不保存原始 URL/响应。

- [ ] **Step 4: 批量组装备注/标签并生成面板头像 URL。**

overview 读取一页 WAHA 聊天后，用一至两次 SQLite 查询按 chat_key_hmac 批量加载备注和标签；手动标签在前、AI 标签在后，最多四项并返回溢出数量。avatar_url 使用当前 session 的签名 chat_ref，不再把 WAHA 原始头像 URL 发给浏览器。

~~~python
labels = metadata[chat_key]["labels"]
visible_labels = labels[:4]
items.append({
    "chat_ref": chat_ref,
    "name": contact_name,
    "note": metadata[chat_key]["note"],
    "labels": visible_labels,
    "label_overflow": max(0, len(labels) - len(visible_labels)),
    "avatar_url": (
        f"/api/chat/sessions/{quote(name, safe='')}/avatar?"
        + urlencode({"chat_ref": chat_ref})
    ),
})
~~~

- [ ] **Step 5: 增加鉴权头像路由并验证响应头。**

在 chat_route 白名单加入 avatar；send_chat_get 要求 chat_ref，调用 chat.avatar 后用现有 send_media 返回 Cache-Control: private, no-store、X-Content-Type-Options: nosniff 和严格 Content-Type。错误只返回面板可读信息，不包含 API Key、远程 URL 或完整响应体。

- [ ] **Step 6: 运行测试并提交。**

~~~powershell
python -m unittest tests.test_chat_features tests.test_chat_upgrade.AvatarAndMetadataTests -v
git diff --check
git add panel/app.py panel/chat_service.py tests/test_chat_features.py tests/test_chat_upgrade.py
git commit -m "feat: proxy chat avatars and expose compact metadata"
~~~

### Task 4: 聊天列表/头部备注标签与移动端操作区

**Files:**
- Modify: panel/chat_page.py:24-75,69-120,197-230（CSS、模板和列表/会话渲染）
- Modify: tests/frontend_syntax.test.mjs（页面契约检查）

**Interfaces:**
- Consumes: Task 3 overview 的 note、labels、label_overflow、面板 avatar_url。
- Produces: renderChatIdentity(item, target)、renderLabelChips(labels, overflow)、可键盘操作的移动端 conversationMoreDialog；人工/恢复 AI 主操作在窄屏始终可见。

- [ ] **Step 1: 在 Node 测试中加入失败契约。**

~~~javascript
test('chat list renders note first and caps labels at four', () => {
  assert.match(chatSource, /item\.note \|\| item\.name \|\| item\.display_id/);
  assert.match(chatSource, /slice\(0, 4\)/);
  assert.match(chatSource, /label_overflow/);
  assert.match(chatSource, /conversationMoreDialog/);
});

test('mobile primary takeover actions remain visible', () => {
  assert.match(chatSource, /人工接管/);
  assert.match(chatSource, /恢复 AI 回复/);
  assert.doesNotMatch(chatSource, /takeover-actions \.state-pill\{display:none/);
});
~~~

- [ ] **Step 2: 运行前端语法和契约测试确认失败。**

~~~powershell
node --test tests/frontend_syntax.test.mjs
~~~

- [ ] **Step 3: 重组列表项的主行和次级行。**

updateChatButton 使用文本节点渲染：主行取 note || name || display_id；次级行有标签时插入最多四个紧凑 chip 和 +N，没有标签时插入 display_id。移除会占用主行的“有备注”额外 pill。setAvatar 只接受面板代理 URL，失败后回退首字母，不自动循环重试。

~~~javascript
function renderLabelChips(item) {
  const row = element('div', 'chat-labels');
  const labels = Array.isArray(item?.labels) ? item.labels.slice(0, 4) : [];
  for (const entry of labels) {
    const source = entry?.source === 'manual' ? 'manual' : 'ai';
    row.append(element('span', 'chat-label ' + source, entry?.label || ''));
  }
  const overflow = Math.max(0, Number(item?.label_overflow) || 0);
  if (overflow) row.append(element('span', 'chat-label overflow', '+' + overflow));
  return row;
}

function displayName(item) {
  return item?.note || item?.name || item?.display_id || '未知客户';
}

const secondary = (item.labels?.length || item.label_overflow)
  ? renderLabelChips(item)
  : element('div', 'chat-id', item.display_id);
~~~

- [ ] **Step 4: 重组会话头部并实现移动端“更多”。**

模板拆成 identity、primary actions、more actions 三组；桌面端保持单行，max-width:820px 改为两层 grid。人工接管/恢复 AI 回复和状态文字始终显示；备注、标签、总结、跟进放入带 Escape/焦点回收/点击关闭的 conversationMoreDialog。所有中间容器设置 min-width:0，chip 单行省略，触控目标至少 44px。

~~~html
<header class="conversation-head">
  <div class="conversation-title"><button class="icon-button mobile-back" id="mobileBack" type="button" aria-label="返回聊天列表">←</button><div class="avatar" id="conversationAvatar" aria-hidden="true">—</div><div class="conversation-identity"><div class="conversation-name" id="conversationName">请选择聊天</div><div class="conversation-meta" id="conversationMeta">从左侧列表选择客户</div><div class="chat-labels" id="conversationLabels" aria-label="客户标签"></div></div></div>
  <div class="conversation-primary-actions">
    <span class="state-pill" id="takeoverPill"><span class="state-dot"></span><span>AI 可回复</span></span>
    <button id="takeoverButton" type="button">人工接管</button>
    <button id="resumeButton" type="button" hidden>恢复 AI 回复</button>
  </div>
  <div class="conversation-more-actions">
    <button class="icon-button" id="moreButton" type="button" aria-haspopup="dialog" aria-label="更多客户操作" title="更多客户操作"><svg class="icon" aria-hidden="true" viewBox="0 0 24 24"><circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/></svg></button>
    <div class="desktop-secondary-actions">
      <button id="noteButton" type="button">客户备注</button>
      <button id="labelButton" type="button">标签</button>
      <button id="summaryButton" type="button">当前总结</button>
      <button id="followUpButton" type="button">跟进</button>
    </div>
  </div>
</header>
<dialog id="conversationMoreDialog" aria-labelledby="conversationMoreTitle">
  <div class="dialog-body" tabindex="-1">
    <div class="dialog-head"><div class="dialog-title" id="conversationMoreTitle">客户操作</div></div>
    <button type="button" data-mobile-action="note">客户备注</button>
    <button type="button" data-mobile-action="labels">标签</button>
    <button type="button" data-mobile-action="summary">当前总结</button>
    <button type="button" data-mobile-action="follow-up">跟进</button>
    <button type="button" id="closeConversationMore">关闭</button>
  </div>
</dialog>
~~~

~~~css
.conversation-primary-actions,.conversation-more-actions{display:flex;align-items:center;gap:8px;min-width:0}
.chat-labels{display:flex;align-items:center;gap:4px;min-width:0;overflow:hidden;margin-top:2px}
.chat-label{max-width:82px;padding:1px 5px;border-radius:5px;font-size:10px;line-height:17px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.chat-label.manual{background:var(--good-soft);color:var(--good)}
.chat-label.ai{background:var(--accent-soft);color:var(--accent)}
#moreButton{display:none}
@media(max-width:820px){
  .conversation-head{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-areas:"identity more" "primary primary";align-items:center}
  .conversation-title{grid-area:identity;min-width:0}
  .conversation-primary-actions{grid-area:primary;justify-content:space-between;width:100%}
  .conversation-more-actions{grid-area:more}
  #moreButton{display:inline-grid;min-width:44px;min-height:44px}
  .desktop-secondary-actions{display:none}
}
~~~

- [ ] **Step 5: 更新备注/标签保存后的局部渲染。**

保存备注或标签后更新 state.selected 与对应 state.chats 项，调用局部 renderChats()/renderConversationIdentity()，不清空消息堆栈、不改变消息滚动位置；实时 refresh 事件也使用同一路径。

~~~javascript
function syncSelectedMetadata(patch) {
  if (!state.selected) return;
  Object.assign(state.selected, patch);
  const listed = state.chats.find((item) => item.chat_ref === state.selected.chat_ref);
  if (listed) Object.assign(listed, patch);
  renderChats();
  renderConversationIdentity(state.selected);
}

function openConversationMore() {
  const dialog = $('conversationMoreDialog');
  dialog.showModal();
  dialog.querySelector('button')?.focus();
}

function closeConversationMore() {
  $('conversationMoreDialog').close();
  $('moreButton').focus();
}
const mobileActionTargets = {
  note: 'noteButton', labels: 'labelButton', summary: 'summaryButton', 'follow-up': 'followUpButton',
};
document.querySelectorAll('[data-mobile-action]').forEach((button) => button.addEventListener('click', () => {
  const target = $(mobileActionTargets[button.dataset.mobileAction]);
  $('conversationMoreDialog').close();
  target?.click();
}));
for (const id of ['noteDialog', 'labelDialog', 'summaryDialog', 'followUpDialog']) {
  $(id).addEventListener('close', () => {
    if (matchMedia('(max-width: 820px)').matches) $('moreButton').focus();
  });
}
~~~

- [ ] **Step 6: 实现动画审查保留的三处轻量动效。**

只实现 `find-animation-opportunities` 审查中确认有明确状态变化且不会增加认知负担的三处：对话框出现/关闭、Toast 消失、手机端列表与详情切换。二维码和配对码动效在任务 6 单独实现。动效只使用 opacity/transform，不动画化布局尺寸、颜色渐变或 `backdrop-filter`；关闭时先播放退出态再移除节点，Escape、点击遮罩和键盘焦点路径保持不变。

~~~css
dialog[data-motion="opening"] .dialog-body{animation:dialog-in 200ms ease-out both}
dialog[data-motion="closing"] .dialog-body{animation:dialog-out 200ms ease-out both}
@keyframes dialog-in{from{opacity:0;transform:scale(.97)}to{opacity:1;transform:scale(1)}}
@keyframes dialog-out{from{opacity:1;transform:scale(1)}to{opacity:0;transform:scale(.97)}}
.toast.closing{opacity:0;transform:translateY(6px);transition:opacity 200ms ease-out,transform 200ms ease-out}
.mobile-view-layer{transition:opacity 180ms ease-out,transform 180ms ease-out}
.mobile-view-layer.is-entering{opacity:0;transform:translateX(8px)}
@media(prefers-reduced-motion:reduce){
  dialog[data-motion] .dialog-body,.toast.closing,.mobile-view-layer{animation:none;transition:opacity 120ms ease-out}
  .mobile-view-layer.is-entering{transform:none}
}
~~~

~~~javascript
function closeWithMotion(dialog, restoreTarget) {
  if (!dialog.open) return;
  dialog.dataset.motion = 'closing';
  let finished = false;
  const finish = () => {
    if (finished) return;
    finished = true;
    dialog.removeEventListener('animationend', finish);
    dialog.close();
    delete dialog.dataset.motion;
    restoreTarget?.focus();
  };
  dialog.addEventListener('animationend', finish, {once:true});
  window.setTimeout(finish, matchMedia('(prefers-reduced-motion: reduce)').matches ? 140 : 220);
}
function dismissToast(toast) {
  toast.classList.add('closing');
  window.setTimeout(() => toast.remove(), matchMedia('(prefers-reduced-motion: reduce)').matches ? 130 : 220);
}
function setMobileView(view) {
  const next = view === 'conversation' ? $('conversationPane') : $('chatPane');
  const previous = view === 'conversation' ? $('chatPane') : $('conversationPane');
  previous.classList.remove('is-entering');
  next.hidden = false;
  next.classList.add('is-entering');
  requestAnimationFrame(() => next.classList.remove('is-entering'));
  requestAnimationFrame(() => { previous.hidden = true; });
}
~~~

Node 契约测试增加对话框 200ms、Toast 200ms、手机视图 180ms 数值和 `prefers-reduced-motion` 分支的断言，并验证关闭后焦点返回触发按钮；不以截图像素作为唯一断言。

- [ ] **Step 7: 运行测试并提交。**

~~~powershell
node --test tests/frontend_syntax.test.mjs
git diff --check
git add panel/chat_page.py tests/frontend_syntax.test.mjs
git commit -m "feat: refine chat identity and mobile actions"
~~~

### Task 5: 自动回复设置页滑块与媒体选项

**Files:**
- Modify: panel/app.py:2220-2305（设置页 HTML/CSS/JS）
- Modify: tests/test_chat_upgrade.py（设置 API 和页面契约）
- Modify: tests/frontend_syntax.test.mjs（设置脚本契约）

**Interfaces:**
- Consumes: Task 1 的 settings_payload/save_settings 字段契约。
- Produces: 设置页 contextPerSide range input、四个媒体 checkbox、collectMediaTypes() 和保存请求字段。

- [ ] **Step 1: 写失败测试，锁定表单范围和保存 payload。**

~~~javascript
test('settings page exposes bounded context slider and four media switches', () => {
  assert.match(appSource, /id="contextPerSide"[^>]*min="5"[^>]*max="50"/);
  for (const name of ['image', 'video', 'audio', 'file']) {
    assert.match(appSource, new RegExp('autoReplyMedia_' + name));
  }
  assert.match(appSource, /auto_reply_context_per_side/);
  assert.match(appSource, /auto_reply_media_types/);
});
~~~

在 `tests/test_chat_upgrade.py` 增加页面所依赖的往返测试，确保每个会话隔离：

~~~python
class SettingsApiTests(UpgradeTestCase):
    def test_context_and_media_settings_round_trip_per_session(self):
        saved = self.state.save_settings({
            "auto_reply_context_per_side": 12,
            "auto_reply_media_types": {
                "image": True, "video": False, "audio": True, "file": False,
            },
        }, "sales")
        self.assertEqual(saved["auto_reply_context_per_side"], 12)
        self.assertEqual(saved["auto_reply_media_types"]["image"], True)
        self.assertEqual(saved["auto_reply_media_types"]["audio"], True)
        self.assertEqual(self.state.settings_payload("default")["auto_reply_context_per_side"], 5)
~~~

- [ ] **Step 2: 实现设置页控件和可读状态。**

滑块显示当前数字并标注“客户 N 条 + 自己 N 条，默认 5 条”；媒体选项说明“只处理私聊客户消息，默认关闭”。loadSettings 从 API 填充，saveSettings 只发送白名单字段；保存失败恢复按钮可用并保留用户输入。

~~~html
<div class="field range-field">
  <label for="contextPerSide">每一方的历史消息数 <output id="contextPerSideValue">5</output></label>
  <input id="contextPerSide" type="range" min="5" max="50" step="1" value="5">
  <div class="section-note" id="contextExplanation">客户 5 条 + 自己 5 条，共最多 10 条历史上下文。</div>
</div>
<fieldset class="media-options">
  <legend>可触发自动回复的客户媒体</legend>
  <label><input id="autoReplyMedia_image" type="checkbox">图片</label>
  <label><input id="autoReplyMedia_video" type="checkbox">视频</label>
  <label><input id="autoReplyMedia_audio" type="checkbox">音频</label>
  <label><input id="autoReplyMedia_file" type="checkbox">文件</label>
</fieldset>
~~~

~~~javascript
const mediaTypeNames = ['image', 'video', 'audio', 'file'];
function renderContextCount() {
  const count = Math.max(5, Math.min(50, Number($('contextPerSide').value) || 5));
  $('contextPerSideValue').value = String(count);
  $('contextExplanation').textContent = `客户 ${count} 条 + 自己 ${count} 条，共最多 ${count * 2} 条历史上下文。`;
}
function collectMediaTypes() {
  return Object.fromEntries(mediaTypeNames.map((name) => [name, $('autoReplyMedia_' + name).checked]));
}
// loadSettings:
$('contextPerSide').value = String(data.auto_reply_context_per_side ?? 5);
for (const name of mediaTypeNames) $('autoReplyMedia_' + name).checked = Boolean(data.auto_reply_media_types?.[name]);
renderContextCount();
// saveSettings body:
body.auto_reply_context_per_side = Number($('contextPerSide').value);
body.auto_reply_media_types = collectMediaTypes();
~~~

- [ ] **Step 3: 加入窄屏布局与无敏感回显检查。**

媒体选项使用两列到一列的响应式布局；API Key 仍只显示配置状态。设置页面标题和返回链接使用新品牌。

~~~css
.range-field input[type=range]{width:100%;min-height:44px;accent-color:var(--blue)}
.media-options{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin:18px 0 0;padding:14px;border:1px solid var(--line);border-radius:7px}
.media-options legend{padding:0 5px;font-weight:700}
.media-options label{display:flex;align-items:center;gap:9px;min-height:44px;margin:0}
@media(max-width:420px){.media-options{grid-template-columns:1fr}}
~~~

- [ ] **Step 4: 运行测试并提交。**

~~~powershell
python -m unittest tests.test_chat_upgrade.SettingsApiTests -v
node --test tests/frontend_syntax.test.mjs
git diff --check
git add panel/app.py tests/test_chat_upgrade.py tests/frontend_syntax.test.mjs
git commit -m "feat: add context slider and media reply settings"
~~~

### Task 6: 扫码揭示、手机号配对等待与品牌统一

**Files:**
- Modify: panel/app.py:1949-2043（兼容旧控制台模板）
- Modify: panel/app.py:2079-2211（多会话控制台模板和内联脚本）
- Modify: panel/app.py:2220-2248（设置页标题/品牌）
- Modify: panel/chat_page.py:19,44（聊天管理页 title 和顶部品牌）
- Modify: panel/commerce_page.py:18,156（订单页 title 和产品品牌文案）
- Modify: tests/frontend_syntax.test.mjs（QR/配对状态机测试）
- Modify: tests/test_chat_features.py（品牌和模板契约）

**Interfaces:**
- Consumes: 现有 /api/qr、/api/sessions/{session}/qr、/api/session/pairing-code 和会话请求世代号。
- Produces: `setQrState(state, title, detail)`、`revealQrImage(blob, ownsRequest)`、`setPairingState(state, data)` 等前端状态函数；所有可见面板品牌为 `WhatsAPP AI管理面板`。

- [ ] **Step 1: 扩展现有 stale QR 测试为四态测试。**

~~~javascript
test('QR stays non-scannable until a validated image is ready', async () => {
  assert.match(declarations, /setQrState\('loading'/);
  assert.match(declarations, /setQrState\('ready'/);
  assert.match(declarations, /setQrState\('error'/);
  assert.match(declarations, /URL\.createObjectURL/);
  assert.match(declarations, /await image\.decode\(\)/);
  assert.match(declarations, /URL\.revokeObjectURL/);
  assert.match(declarations, /ownsRequest\(\)/);
  assert.match(appSource, /420ms ease-out/);
  assert.match(appSource, /legacyQrImageLayer/);
  assert.match(appSource, /requestPairingCode/);
});

test('pairing request exposes loading, ready and error states', () => {
  assert.match(declarations, /setPairingState\(['"]loading/);
  assert.match(declarations, /setPairingState\(['"]ready/);
  assert.match(declarations, /setPairingState\(['"]error/);
  assert.match(declarations, /aria-busy/);
});
~~~

另加模板断言：placeholder 不包含真实 QR payload，品牌标题包含 WhatsAPP AI管理面板，技术状态仍可出现 WAHA；在 `tests/test_chat_features.py` 的既有导入区加入 `html_page` 和 `from panel.commerce_page import commerce_page`，以便同时检查兼容旧控制台与订单页品牌。

在现有 `vm` fixture 中将 QR 容器拆为独立的 `qrImageLayer`、`qrFrosted`、`qrTitle`、`qrDetail` 节点；`document.createElement('img')` 返回含 `decode: async () => {}` 的对象，提供 `requestAnimationFrame: (callback) => callback()`，并给测试 URL 类增加 `revokeObjectURL(url)` 记录。把旧断言中的 `qrWrap.child` 改为 `qrImageLayer.child`，断言旧会话响应被废弃时调用 revoke、当前成功响应只保留一个活动 object URL；再补以下最小 fixture 代码，避免测试依赖真实浏览器：

~~~javascript
const qrImageLayer = {
  child: undefined,
  replaceChildren(child) { this.child = child; },
};
const qrFrosted = { dataset: {}, style: {}, textContent: '' };
const elements = new Map([
  ['qrButton', {disabled:false, setAttribute(){}}],
  ['qrWrap', {dataset:{}, setAttribute(){}}],
  ['qrImageLayer', qrImageLayer], ['qrFrosted', qrFrosted],
  ['qrTitle', {textContent:''}], ['qrDetail', {textContent:''}],
]);
class TestURL extends URL {
  static revoked = [];
  static createObjectURL(blob) { return 'blob:' + blob.id; }
  static revokeObjectURL(url) { TestURL.revoked.push(url); }
}
const imageNode = () => ({src:'', alt:'', decode: async () => {}});
const context = vm.createContext({
  document: {createElement: (tag) => tag === 'img' ? imageNode() : {}, getElementById: id => elements.get(id)},
  requestAnimationFrame: callback => callback(), URL: TestURL, matchMedia: () => ({matches:false}),
});
~~~

Python 模板测试放入仓库已有的 `QrAndReleaseTests`，不创建不存在的测试类：

~~~python
def test_panel_brand_and_qr_motion_contract(self):
    pages = (html_page(), multi_session_html_page())
    commerce = commerce_page()
    for page in pages:
        self.assertIn("WhatsAPP AI管理面板", page)
        self.assertIn("WAHA 服务", page)
        self.assertIn("data-state=", page)
        self.assertIn("420ms ease-out", page)
        self.assertIn("prefers-reduced-motion", page)
        self.assertNotIn("data:image/", page)
    self.assertIn("WhatsAPP AI管理面板", commerce)
~~~

同时把 `QrAndReleaseTests.test_qr_placeholder_is_frosted_non_scannable_and_stateful` 中的旧 `qrPlaceholder(state)` 断言改为 `setQrState('idle'|'loading'|'error')`、`qr-decoration`、`qrImageLayer` 和 `aria-busy` 断言；把 `test_qr_image_replaces_placeholder_only_after_image_response` 的替换目标改为 `$('qrImageLayer').replaceChildren(image)`，并保留“content-type 校验先于 DOM 替换”的顺序断言。

- [ ] **Step 2: 实现二维码双层 DOM 与安全占位。**

qrWrap 保持磨砂装饰层、说明层和真实图片层分离；占位图案由固定 CSS 装饰块组成，不使用真实 QR 图像。loadQr 开始时递增 generation、清理旧 object URL、显示 loading；只有响应为图片且 blob 预加载成功后才插入真实图片。通过 opacity .92→0、图片 opacity 0→1 和 scale(.985)→scale(1) 实现 420ms ease-out 揭示；失败回到 error 占位。

多会话模板使用 `qrWrap/qrImageLayer/qrFrosted`；兼容旧模板使用 `qrFrame/legacyQrImageLayer/legacyQrFrosted`，但两者复用完全相同的四态、预加载、object URL 释放和 reduced-motion 数值，不能保留旧模板当前的瞬间 `replaceChildren(image)` 路径。

~~~html
<div class="qr-wrap" id="qrWrap" data-state="idle" aria-live="polite" aria-busy="false">
  <div class="qr-image-layer" id="qrImageLayer"></div>
  <div class="qr-frosted" id="qrFrosted">
    <div class="qr-decoration" aria-hidden="true"></div>
    <div class="qr-frosted-copy"><strong id="qrTitle">二维码待刷新</strong><span id="qrDetail">点击刷新二维码</span></div>
  </div>
</div>
~~~

~~~css
.qr-wrap{position:relative;isolation:isolate;overflow:hidden}
.qr-image-layer,.qr-frosted{position:absolute;inset:0;display:grid;place-items:center}
.qr-image-layer img{opacity:0;transform:scale(.985);transition:opacity 420ms ease-out,transform 420ms ease-out}
.qr-wrap[data-state="ready"] .qr-image-layer img{opacity:1;transform:scale(1)}
.qr-frosted{opacity:.92;transition:opacity 420ms ease-out;backdrop-filter:blur(12px)}
.qr-wrap[data-state="ready"] .qr-frosted{opacity:0;pointer-events:none}
.qr-decoration{width:66%;aspect-ratio:1;background:repeating-linear-gradient(45deg,var(--ink) 0 7px,transparent 7px 17px);filter:blur(10px);opacity:.16}
~~~

~~~javascript
let qrObjectUrl = '';
function releaseQrObjectUrl(url = qrObjectUrl) {
  if (url) URL.revokeObjectURL(url);
  if (url === qrObjectUrl) qrObjectUrl = '';
}
function setQrState(state, title, detail) {
  const wrap = $('qrWrap');
  wrap.dataset.state = state;
  wrap.setAttribute('aria-busy', state === 'loading' ? 'true' : 'false');
  $('qrTitle').textContent = title;
  $('qrDetail').textContent = detail;
  if (state !== 'ready') {
    releaseQrObjectUrl();
    $('qrImageLayer').replaceChildren();
  }
}
async function revealQrImage(blob, ownsRequest) {
  const objectUrl = URL.createObjectURL(blob);
  const image = document.createElement('img');
  image.alt = '当前 WhatsApp 会话二维码';
  image.src = objectUrl;
  try { await image.decode(); } catch (error) { releaseQrObjectUrl(objectUrl); throw error; }
  if (!ownsRequest()) { releaseQrObjectUrl(objectUrl); return false; }
  releaseQrObjectUrl();
  qrObjectUrl = objectUrl;
  $('qrImageLayer').replaceChildren(image);
  requestAnimationFrame(() => setQrState('ready', '二维码已生成', '请使用手机 WhatsApp 扫描。'));
  return true;
}
async function loadQr() {
  const requestedSession = selected;
  const requestGeneration = ++qrRequestGeneration;
  const ownsRequest = () => selected === requestedSession && qrRequestGeneration === requestGeneration;
  setQrState('loading', '正在获取二维码', '请稍候，获取完成后会自动显示。');
  $('qrButton').disabled = true;
  try {
    const response = await fetch('/api/sessions/' + encodeURIComponent(requestedSession) + '/qr?ts=' + Date.now(), {cache:'no-store'});
    const type = response.headers.get('content-type') || '';
    if (!response.ok || !type.startsWith('image/')) {
      const data = type.includes('json') ? await response.json() : {};
      throw new Error(data.message || '二维码暂不可用');
    }
    await revealQrImage(await response.blob(), ownsRequest);
  } catch (error) {
    if (ownsRequest()) setQrState('error', '二维码获取失败', error.message || '请启动会话后重试。');
  } finally {
    if (ownsRequest()) $('qrButton').disabled = false;
  }
}
~~~

- [ ] **Step 3: 实现手机号配对等待状态。**

pairing 开始时清除旧码、按钮禁用并设置 aria-busy="true"，显示等待面板；请求中使用最多一个轻微 opacity 提示，不改变布局高度。成功以 220ms ease-out 淡入/上移展示码；失败隐藏旧码、设置错误状态、恢复可重试按钮。请求结果只接受当前请求，不用动画掩盖网络错误。

该状态机同时替换多会话模板的 `pairing()` 和兼容旧模板的 `requestPairingCode()`；两个页面都使用文本节点展示服务端 code，不用 `innerHTML` 拼接响应值。

~~~javascript
function makeElement(tag, className, text) {
  const node = document.createElement(tag);
  node.className = className;
  node.textContent = text;
  return node;
}
function setPairingState(state, data = {}) {
  const region = $('pairingCode');
  region.dataset.state = state;
  region.hidden = state === 'idle';
  region.setAttribute('aria-busy', state === 'loading' ? 'true' : 'false');
  if (state === 'loading') region.replaceChildren(makeElement('span', 'pairing-wait', '正在请求配对码，请稍候'));
  if (state === 'ready') {
    const label = makeElement('span', '', '请在手机 WhatsApp 中输入');
    const code = makeElement('strong', '', String(data.code || ''));
    region.replaceChildren(label, code);
  }
  if (state === 'error') region.replaceChildren(makeElement('span', 'pairing-error', data.message || '配对码获取失败，请重试'));
}

async function requestPairingCode() {
  const response = await mutateFetch(apiSession('/pairing-code'), {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({phone_number:$('phoneNumber').value}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.message || '配对码获取失败');
  return data;
}

async function pairing() {
  const generation = ++pairingRequestGeneration;
  $('pairingButton').disabled = true;
  $('pairingButton').setAttribute('aria-busy', 'true');
  setPairingState('loading');
  try {
    const data = await requestPairingCode();
    if (generation === pairingRequestGeneration) setPairingState('ready', data);
  } catch (error) {
    if (generation === pairingRequestGeneration) setPairingState('error', {message:error.message});
  } finally {
    if (generation === pairingRequestGeneration) {
      $('pairingButton').disabled = false;
      $('pairingButton').setAttribute('aria-busy', 'false');
    }
  }
}
~~~

- [ ] **Step 4: 统一品牌文字并保留 WAHA 技术标签。**

修改两个控制台模板、聊天管理页、订单页和设置页的 title、顶部品牌、必要的旧页面标题；将 WAHA 控制台/WAHA 本地控制台作为产品名的文本替换为 WhatsAPP AI管理面板，保留 WAHA 服务、WAHA 状态、WAHA / 技术会话等技术说明。不要修改 API 路径、镜像名、技术会话名或 Compose。

具体文案契约为：`panel/app.py` 的两个控制台模板 title/主标题精确使用 `WhatsAPP AI管理面板`；`panel/chat_page.py` 使用 `<title>聊天管理 · WhatsAPP AI管理面板</title>` 且顶部 `brand-title` 精确显示 `WhatsAPP AI管理面板`；`panel/commerce_page.py` 的 `<title>` 和页面主标题均包含 `WhatsAPP AI管理面板`；旧控制台的 `WAHA 服务`、`WAHA 状态`、`WAHA / 技术会话` 等仅作为技术说明保留。

- [ ] **Step 5: 加入 reduced-motion/transparency 规则并验证窄屏。**

普通模式保留二维码 420ms 和配对 180/220ms 过渡；prefers-reduced-motion 使用 160ms/120ms opacity-only；prefers-reduced-transparency 和不支持 backdrop-filter 使用不透明主题背景。320–430px 下按钮和配对码不水平溢出。

~~~css
.pairing-code{transition:opacity 180ms ease-out,transform 180ms ease-out}
.pairing-code[data-state="loading"] .pairing-wait{animation:pairing-wait 900ms ease-in-out infinite alternate}
.pairing-code[data-state="ready"]{animation:pairing-ready 220ms ease-out both}
@keyframes pairing-wait{from{opacity:.55}to{opacity:1}}
@keyframes pairing-ready{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
@media(prefers-reduced-motion:reduce){
  .qr-image-layer img,.qr-frosted{transition:opacity 160ms ease-out;transform:none}
  .pairing-code,.pairing-code[data-state]{animation:none;transition:opacity 120ms ease-out;transform:none}
}
@media(prefers-reduced-transparency:reduce){.qr-frosted{backdrop-filter:none;background:var(--surface)}}
~~~

- [ ] **Step 6: 运行测试并提交。**

~~~powershell
node --test tests/frontend_syntax.test.mjs
python -m unittest tests.test_chat_features.QrAndReleaseTests tests.test_chat_features.ChatPageRegressionTests -v
git diff --check
git add panel/app.py panel/chat_page.py panel/commerce_page.py tests/frontend_syntax.test.mjs tests/test_chat_features.py
git commit -m "feat: polish QR reveal pairing feedback and panel branding"
~~~

### Task 7: 全量回归、视口验收与发布前审查

**Files:**
- Modify: only files identified by failing tests, if a correction is required
- Test: tests/test_chat_upgrade.py, tests/test_chat_features.py, tests/test_chat_engagement.py, tests/frontend_syntax.test.mjs
- Review: docker-compose.yml, panel-release.json, waha-release.json, .env.example（只读确认未被修改）

**Interfaces:**
- Consumes: Tasks 1–6 的已提交接口和页面状态机。
- Produces: 可复核的测试报告、视口截图/记录、干净差异和最终实施提交列表；不自动发布 Docker 镜像或重启服务。

- [ ] **Step 1: 运行 Python 全量测试。**

~~~powershell
python -m unittest discover -s tests -p "test_*.py" -v
~~~

Expected: 既有测试和新增测试全部通过，任何失败先停在失败任务，不跳过。

- [ ] **Step 2: 运行前端语法与 Node 测试。**

~~~powershell
node --test tests/frontend_syntax.test.mjs
~~~

Expected: 所有 inline script 可解析，QR stale response、品牌、设置、移动端契约测试通过。

- [ ] **Step 3: 做静态敏感信息和范围检查。**

~~~powershell
rg -n "WAHA_API_KEY|PANEL_DATA_ENCRYPTION_KEY|Webhook Secret|api[_-]?key\s*[:=]" panel tests docs/superpowers
git diff --name-only HEAD~6..HEAD
git diff --check HEAD~6..HEAD
~~~

确认搜索结果只包含字段名/脱敏说明，不包含实际凭据；差异只落在面板代码、测试和计划/规范文件，不含 docker-compose.yml、WAHA release 元数据或生产数据。

- [ ] **Step 4: 在安全测试配置下做实际视口验收。**

使用本地测试管理员和脱敏 fixture 打开状态面板、聊天管理和设置页，检查 320/375/390/430px 及桌面宽度：

1. 备注覆盖主行，标签最多四个且在手机号下方。
2. 头像成功/失败都不阻塞列表。
3. 人工接管/恢复 AI 在窄屏可见。
4. QR 占位不能扫描，真实二维码只在响应校验完成后出现，刷新旧请求不能覆盖新会话。
5. 配对等待、成功和失败状态清楚可读。
6. reduced-motion/transparency 下没有跳动、旋转或布局抖动。

- [ ] **Step 5: 复核回滚路径并形成交付记录。**

记录每个任务的提交哈希、测试命令和结果；确认新增设置键可被旧版本忽略、头像缓存可丢弃、未执行服务重启/镜像推送。若任何运行时验证缺少安全配置，明确标记为未验证，不声称已发布。

~~~powershell
git status --short --branch
git log --oneline --decorate -8
~~~

- [ ] **Step 6: 最终提交前只提交已审核的修正。**

任何修正必须先补对应失败测试，再修改实现，重新运行全量测试；不使用强制推送、不覆盖已有发布标签。
