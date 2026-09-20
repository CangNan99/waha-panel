import base64
import gc
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

from panel.app import PanelHandler, PanelState, WahaApiError, WahaClient, init_db, settings_page
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
        return [
            {"id": "12345@lid", "name": "测试客户"},
            {"id": "67890@c.us", "name": "第二客户"},
        ]

    def get_chat_picture(self, session, chat_id):
        with self.picture_lock:
            self.picture_calls.append((session, chat_id))
            self.picture_call_count += 1
            self.active_picture_calls += 1
            self.max_active_picture_calls = max(
                self.max_active_picture_calls, self.active_picture_calls
            )
        try:
            time.sleep(0.02)
            if self.picture_mode == "404":
                raise WahaApiError(404, "picture unavailable")
            return self.picture_response
        finally:
            with self.picture_lock:
                self.active_picture_calls -= 1


class UpgradeTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.client = getattr(self, "client_class", UpgradeClient)()
        self.state = PanelState(
            self.database,
            self.client,
            "redacted-test-key",
            sleep_fn=lambda _seconds: None,
            uniform_fn=lambda _start, _end: 0,
            data_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        self.state.save_settings({"auto_reply_all_day": True}, "default")
        self.state.last_ai_context = ""

        def fake_ai_reply(_settings, _incoming, **kwargs):
            self.state.last_ai_context = "\n".join(
                item for item in (_incoming, kwargs.get("conversation_context", "")) if item
            )
            return "测试回复", "ai"

        self.state._ai_reply = fake_ai_reply
        self.chat_ref = self.state.chat.overview("default")["items"][0]["chat_ref"]

    def tearDown(self):
        self.state.stop_background_services()
        self.state = None
        gc.collect()
        for _attempt in range(10):
            try:
                self.temp.cleanup()
                break
            except PermissionError:
                time.sleep(0.05)

    def write_setting(self, key, value, session="default"):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "INSERT INTO session_settings(session_name,key,value,updated_at) VALUES (?,?,?,1) "
                "ON CONFLICT(session_name,key) DO UPDATE SET value=excluded.value",
                (session, key, value),
            )

    def event(
        self,
        body="",
        from_me=False,
        message_id="m-1",
        chat_id="chat-1@c.us",
        message_type="chat",
        has_media=False,
        is_group=False,
        **extra,
    ):
        message = {
            "id": message_id,
            "fromMe": from_me,
            "from": chat_id,
            "to": chat_id,
            "body": body,
            "type": message_type,
            "hasMedia": has_media,
            "isGroup": is_group,
        }
        message.update(extra)
        return json.dumps({"event": "message", "session": "default", "payload": message}).encode("utf-8")

    def insert_history(self, inbound, outbound):
        with sqlite3.connect(self.database) as connection:
            sequence = []
            for index in range(max(inbound, outbound)):
                if index < inbound:
                    sequence.append(("inbound", f"in-{index}", f"in-{index}"))
                if index < outbound:
                    sequence.append(("outbound", f"out-{index}", f"out-{index}"))
            connection.executemany(
                "INSERT INTO conversation_messages(session_name,chat_id,direction,message_id,content,created_at) "
                "VALUES ('default','chat-1@c.us',?,?,?,?)",
                [
                    (direction, message_id, content, index + 1)
                    for index, (direction, message_id, content) in enumerate(sequence)
                ],
            )

    def messages_for(self, chat_id):
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT direction,message_id,content,created_at FROM conversation_messages "
                    "WHERE session_name='default' AND chat_id=? ORDER BY id",
                    (chat_id,),
                )
            ]

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
        return self.state.handle_webhook(
            self.event(message_type=message_type, has_media=True, message_id=message_id),
            dispatch=False,
        )

    def last_context_or_prompt(self):
        return self.state.last_ai_context

    def decode_message_ref(self, message_ref):
        return self.state.chat._decode_message("default", message_ref)[1]

    @staticmethod
    def history_fixture(inbound, outbound):
        items = []
        for index in range(max(inbound, outbound)):
            if index < inbound:
                items.append(
                    {
                        "id": f"history-in-{index}",
                        "timestamp": index * 2 + 1,
                        "fromMe": False,
                        "from": "chat-1@c.us",
                        "body": f"客户历史 {index}",
                    }
                )
            if index < outbound:
                items.append(
                    {
                        "id": f"history-out-{index}",
                        "timestamp": index * 2 + 2,
                        "fromMe": True,
                        "to": "chat-1@c.us",
                        "body": f"客服历史 {index}",
                    }
                )
        return items


class SettingsAndContextTests(UpgradeTestCase):
    def test_settings_default_and_context_are_per_side(self):
        payload = self.state.settings_payload("default")
        self.assertEqual(payload.get("auto_reply_context_per_side"), 5)
        self.assertEqual(
            payload.get("auto_reply_media_types"),
            {"image": False, "video": False, "audio": False, "file": False},
        )
        self.insert_history(inbound=6, outbound=6)
        context = self.state._conversation_context("chat-1@c.us", session_name="default", per_side=5)
        self.assertEqual(context.count("客户："), 5)
        self.assertEqual(context.count("客服："), 5)
        self.assertLess(context.index("客户：in-1"), context.index("客服：out-1"))

    def test_corrupt_stored_settings_fall_back_to_safe_defaults(self):
        self.write_setting("auto_reply_context_per_side", "not-a-number")
        self.write_setting("auto_reply_media_types", "{broken")
        payload = self.state.settings_payload("default")
        self.assertEqual(payload.get("auto_reply_context_per_side"), 5)
        self.assertEqual(
            payload.get("auto_reply_media_types"),
            {"image": False, "video": False, "audio": False, "file": False},
        )

    def test_invalid_settings_are_rejected_without_overwriting(self):
        before = self.state.settings_payload("default")
        with self.assertRaisesRegex(ValueError, "5-50"):
            self.state.save_settings({"auto_reply_context_per_side": 51}, "default")
        with self.assertRaisesRegex(ValueError, "媒体"):
            self.state.save_settings({"auto_reply_media_types": {"sticker": True}}, "default")
        after = self.state.settings_payload("default")
        self.assertEqual(after, before)


class SettingsApiTests(UpgradeTestCase):
    def test_context_and_media_settings_round_trip_per_session(self):
        saved = self.state.save_settings(
            {
                "auto_reply_context_per_side": 12,
                "auto_reply_media_types": {
                    "image": True,
                    "video": False,
                    "audio": True,
                    "file": False,
                },
            },
            "sales",
        )
        self.assertEqual(saved["auto_reply_context_per_side"], 12)
        self.assertTrue(saved["auto_reply_media_types"]["image"])
        self.assertTrue(saved["auto_reply_media_types"]["audio"])
        self.assertEqual(self.state.settings_payload("default")["auto_reply_context_per_side"], 5)

    def test_settings_page_exposes_context_and_media_controls_without_secret_echo(self):
        page = settings_page()
        self.assertIn('id="contextPerSide"', page)
        self.assertIn('min="5"', page)
        self.assertIn('max="50"', page)
        for name in ("image", "video", "audio", "file"):
            self.assertIn(f'autoReplyMedia_{name}', page)
        self.assertIn("auto_reply_context_per_side", page)
        self.assertIn("auto_reply_media_types", page)
        self.assertIn("APIKey：已配置（隐藏）", page)


class WebhookArchiveTests(UpgradeTestCase):
    def test_from_me_is_archived_but_never_triggers(self):
        result = self.state.handle_webhook(
            self.event(body="人工内容", from_me=True, message_id="out-1"),
            dispatch=True,
        )
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
        result = self.state.handle_webhook(
            self.event(message_type="image", has_media=True, message_id="img-1")
        )
        self.assertEqual(result["action"], "replied")
        self.assertIn("图片", self.last_context_or_prompt())

    def test_disabled_media_is_archived_without_triggering(self):
        result = self.state.handle_webhook(
            self.event(message_type="image", has_media=True, message_id="img-off"),
            dispatch=True,
        )
        self.assertEqual(result["action"], "ignored")
        self.assertEqual(self.client.send_calls, 0)
        self.assertIn("图片", self.message_content("img-off"))

    def test_ptt_and_document_follow_audio_and_file_switches(self):
        self.state.save_settings(
            {
                "auto_reply_enabled": True,
                "auto_reply_media_types": {"audio": True, "file": True},
            }
        )
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
            "default",
            self.chat_ref,
            "人工回复",
            "00000000-0000-0000-0000-000000000011",
        )
        self.state.handle_webhook(
            self.event(
                body="人工回复",
                from_me=True,
                message_id=self.decode_message_ref(result["message_ref"]),
            )
        )
        self.assertEqual(self.count_outbound_content("人工回复"), 1)

    def test_missing_local_send_id_uses_request_id_but_webhook_without_id_is_ignored(self):
        self.client.send_result = {}
        self.state.chat.send_text(
            "default",
            self.chat_ref,
            "本地出站",
            "00000000-0000-0000-0000-000000000012",
        )
        self.assertEqual(self.count_message_id("local:00000000-0000-0000-0000-000000000012"), 1)
        self.assertEqual(
            self.state.handle_webhook(self.event(from_me=True, message_id=""))["action"],
            "ignored",
        )

    def test_missing_local_history_backfills_both_directions_before_ai(self):
        self.client.messages = self.history_fixture(inbound=5, outbound=5)
        self.state.handle_webhook(
            self.event(body="现在的问题", message_id="current-1"),
            dispatch=False,
        )
        self.assertEqual(self.count_direction("inbound"), 6)
        self.assertEqual(self.count_direction("outbound"), 5)
        self.assertIn("客服：", self.state.last_ai_context)


class AvatarAndMetadataTests(UpgradeTestCase):
    client_class = AvatarClient

    def setUp(self):
        super().setUp()
        items = self.state.chat.overview("default")["items"]
        self.item, self.other = items
        self.other_ref = self.other["chat_ref"]
        self.service = self.state.chat

    def run_parallel_avatar_requests(self, count):
        refs = [
            self.service._encode_chat("default", f"parallel-{index}@c.us")
            for index in range(count)
        ]
        threads = [
            threading.Thread(target=self._ignore_avatar_error, args=(ref,))
            for ref in refs
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)

    def _ignore_avatar_error(self, chat_ref):
        try:
            self.service.avatar("default", chat_ref)
        except ChatServiceError:
            pass

    def test_avatar_uses_full_chat_id_and_panel_proxy(self):
        item = self.service.overview("default")["items"][0]
        self.assertTrue(item["avatar_url"].startswith("/api/chat/sessions/default/avatar?"))
        self.assertNotIn("waha.example", item["avatar_url"])
        self.service.avatar("default", item["chat_ref"])
        self.assertEqual(self.client.picture_calls[-1][1], "12345@lid")

    def test_avatar_failure_returns_placeholder_without_breaking_overview(self):
        self.client.picture_mode = "404"
        with self.assertRaises(ChatServiceError):
            self.service.avatar("default", self.item["chat_ref"])
        self.assertTrue(self.service.overview("default")["items"])

    def test_picture_json_url_must_stay_on_waha_origin(self):
        class Headers:
            @staticmethod
            def get_content_type():
                return "application/json"

        class Response:
            status = 200
            headers = Headers()

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit=-1):
                return b'{"data":{"url":"https://attacker.invalid/a.png"}}'

        client = WahaClient(
            "http://waha:3000",
            "redacted",
            opener=lambda *_args, **_kwargs: Response(),
        )
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
        self.assertEqual(
            [entry["source"] for entry in item["labels"]],
            ["manual", "manual", "manual", "ai"],
        )
        self.assertEqual(item["label_overflow"], 2)

    def test_avatar_concurrency_never_exceeds_four_remote_requests(self):
        self.run_parallel_avatar_requests(8)
        self.assertLessEqual(self.client.max_active_picture_calls, 4)


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
        path = "/api/chat/sessions/default/avatar?chat_ref=" + quote(
            self.item["chat_ref"], safe=""
        )
        with self.assertRaises(HTTPError) as denied:
            urlopen(self.url + path, timeout=3)
        self.assertEqual(denied.exception.code, 401)
        credential = base64.b64encode(b"admin:test-password").decode("ascii")
        request = Request(
            self.url + path,
            headers={"Authorization": "Basic " + credential},
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "image/png")
            self.assertEqual(response.headers["Cache-Control"], "private, no-store")
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
