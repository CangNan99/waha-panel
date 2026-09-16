import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from panel.app import PanelState, WahaClient, init_db, multi_session_html_page
from panel.chat_page import chat_management_page
from panel.chat_service import ChatService


class ReferenceCodec:
    def __init__(self):
        self.values = {}

    def encode(self, kind, session, value, _ttl):
        token = f"{kind}:{session}:{len(self.values)}"
        self.values[token] = value
        return token

    def decode(self, token, kind, session):
        expected = f"{kind}:{session}:"
        if not str(token).startswith(expected):
            raise ValueError("invalid reference")
        return self.values[token]


class ChatClient:
    def get_chats(self, _session, _limit, _offset):
        return [{
            "id": "8613800138000@c.us",
            "name": "客户 A",
            "profilePictureUrl": "https://waha.example/avatar.jpg",
            "lastMessage": {"body": "hello", "timestamp": 20},
        }]


class SessionClient:
    def __init__(self):
        self.deleted = []

    def get_health(self):
        return {"status": "ok"}

    def get_version(self):
        return {"version": "2026.8.2"}

    def get_sessions(self):
        return [
            {"name": "default", "status": "WORKING"},
            {"name": "sales", "status": "WORKING"},
        ]

    def delete_session(self, session_name):
        self.deleted.append(session_name)
        return {}


class ChatFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.codec = ReferenceCodec()
        self.service = ChatService(self.database, ChatClient(), self.codec, b"t" * 32)
        self.item = self.service.overview("default")["items"][0]

    def tearDown(self):
        self.temp.cleanup()

    def test_avatar_and_customer_note_are_session_scoped(self):
        self.assertEqual(self.item["avatar_url"], "https://waha.example/avatar.jpg")
        self.assertEqual(self.item["note"], "")
        saved = self.service.save_note("default", self.item["chat_ref"], "VIP 客户")
        self.assertEqual(saved["note"], "VIP 客户")
        self.assertEqual(self.service.note("default", self.item["chat_ref"])["note"], "VIP 客户")
        self.assertEqual(self.service.overview("default")["items"][0]["note"], "VIP 客户")

    def test_note_is_deleted_when_cleared(self):
        self.service.save_note("default", self.item["chat_ref"], "临时备注")
        self.service.save_note("default", self.item["chat_ref"], "")
        self.assertEqual(self.service.note("default", self.item["chat_ref"])["note"], "")


class WahaDeleteTests(unittest.TestCase):
    def test_client_uses_delete_session_endpoint(self):
        requests = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return b"{}"

        def opener(request, timeout=0):
            requests.append((request.method, urlparse(request.full_url).path, timeout))
            return Response()

        WahaClient("http://waha:3000", "redacted", opener=opener).delete_session("sales")
        self.assertEqual(requests[0][:2], ("DELETE", "/api/sessions/sales"))


class SessionRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.client = SessionClient()
        self.state = PanelState(self.database, self.client, "redacted")

    def tearDown(self):
        self.temp.cleanup()

    def test_status_payload_exposes_sessions_for_multi_session_rendering(self):
        payload = self.state.status_payload("sales")
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["selected_session"], "sales")
        self.assertEqual([item["name"] for item in payload["sessions"]], ["default", "sales"])

        page = multi_session_html_page()
        self.assertIn("function renderSessions()", page)
        self.assertIn("fetch('/api/status?session='", page)
        self.assertIn("$('sessionList').innerHTML = items.length", page)

    def test_delete_session_removes_a_nonfinal_session(self):
        self.state.sessions_payload()
        result = self.state.delete_session("sales")
        self.assertEqual(result, {"deleted": True, "session": "sales"})
        self.assertEqual(self.client.deleted, ["sales"])
        self.assertEqual([item["session_name"] for item in self.state.managed_session_rows()], ["default"])


class ChatPageRegressionTests(unittest.TestCase):
    def test_page_contains_persistent_chat_controls(self):
        page = chat_management_page("default")
        self.assertIn("scrollToBottom", page)
        self.assertIn("translationByMessage", page)
        self.assertIn("handlePaste", page)
        self.assertIn("handleDrop", page)
        self.assertIn("客户备注", page)
        self.assertIn("sessionSelect", page)


if __name__ == "__main__":
    unittest.main()
