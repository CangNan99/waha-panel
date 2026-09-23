import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from panel.app import PanelState, WahaClient, html_page, init_db, multi_session_html_page
from panel.chat_page import chat_management_page
from panel.chat_service import ChatService
from panel.commerce_page import commerce_page
from panel.update_service import UpdateService


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
        return {"version": "2026.9.1"}

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
        self.assertTrue(
            self.item["avatar_url"].startswith(
                "/api/chat/sessions/default/avatar?chat_ref="
            )
        )
        self.assertNotIn("waha.example", self.item["avatar_url"])
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


class QrAndReleaseTests(unittest.TestCase):
    def test_qr_placeholder_is_frosted_non_scannable_and_stateful(self):
        page = multi_session_html_page()
        self.assertIn(".qr-frosted", page)
        self.assertRegex(page, r"(?:backdrop-)?filter:\s*blur\(")
        self.assertIn("@supports not", page)
        self.assertIn("prefers-reduced-transparency", page)
        self.assertIn("刷新二维码", page)
        self.assertIn("正在获取二维码", page)
        self.assertIn("二维码获取失败", page)
        for state in ("idle", "loading", "error"):
            self.assertIn(f"setQrState('{state}'", page)
        self.assertIn("qr-decoration", page)
        self.assertIn("qrImageLayer", page)
        self.assertIn('aria-busy', page)
        self.assertNotIn("data:image/", page)

    def test_qr_image_replaces_placeholder_only_after_image_response(self):
        page = multi_session_html_page()
        content_type_guard = "if (!response.ok || !type.startsWith('image/'))"
        self.assertIn(content_type_guard, page)
        self.assertIn("$('qrImageLayer').replaceChildren(image)", page)
        load_qr = page[page.index("async function loadQr()"):]
        self.assertLess(load_qr.index(content_type_guard), load_qr.index("await revealQrImage"))

    def test_qr_placeholder_uses_embossed_white_glass_contract(self):
        page = multi_session_html_page()
        self.assertIn('id="qrGhost"', page)
        self.assertIn("feDisplacementMap", page)
        self.assertRegex(page, r'baseFrequency="\.012 \.018"')
        self.assertRegex(page, r"backdrop-filter:blur\(18px\) saturate\(90%\)")
        self.assertIn("-webkit-backdrop-filter:blur(18px) saturate(90%)", page)
        self.assertIn("background:rgba(255,255,255,.46)", page)
        self.assertIn("border:1px solid rgba(255,255,255,.72)", page)
        self.assertIn("qr-frosted::after", page)
        self.assertIn("@keyframes qr-glass-breathe", page)
        self.assertRegex(page, r'qr-wrap\[data-state="ready"\] \.qr-ghost')
        self.assertIn("$('qrButton').addEventListener('click', loadQr)", page)

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

    def test_release_metadata_uses_panel_1_0_8_and_waha_2026_9_1(self):
        root = Path(__file__).resolve().parents[1]
        release = json.loads((root / "panel-release.json").read_text(encoding="utf-8"))
        compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
        env_example = (root / ".env.example").read_text(encoding="utf-8")
        install_sh = (root / "install.sh").read_text(encoding="utf-8")
        install_ps1 = (root / "install.ps1").read_text(encoding="utf-8")
        install_docs = (root / "INSTALL.zh-CN.md").read_text(encoding="utf-8")
        release_plan = (root / "docs" / "superpowers" / "plans" / "2026-09-18-chat-engagement.md").read_text(encoding="utf-8")
        release_design = (root / "docs" / "superpowers" / "specs" / "2026-09-18-chat-engagement-design.md").read_text(encoding="utf-8")
        readme = (root / "README.md").read_text(encoding="utf-8")
        readme_zh = (root / "README.zh-CN.md").read_text(encoding="utf-8")

        self.assertEqual(release["tag"], "1.0.8")
        self.assertEqual(release["version"], "1.0.8")
        self.assertIn("${PANEL_IMAGE:-docker.io/cangnan88/waha-panel:1.0.8}", compose)
        self.assertIn("${PANEL_VERSION:-1.0.8}", compose)
        self.assertIn('os.environ.get("PANEL_VERSION", "1.0.8")', (root / "panel" / "app.py").read_text(encoding="utf-8"))
        self.assertIn("mem_limit: 512m", compose)
        for installer in (env_example, install_sh, install_ps1):
            self.assertIn("PANEL_IMAGE=docker.io/cangnan88/waha-panel:1.0.8", installer)
            self.assertIn("PANEL_VERSION=1.0.8", installer)
            self.assertNotIn("PANEL_IMAGE=docker.io/cangnan88/waha-panel:1.0.2", installer)
            self.assertNotIn("PANEL_VERSION=1.0.2", installer)
        self.assertIn("docker.io/cangnan88/waha-panel:1.0.8", install_docs)
        self.assertIn("1.0.2", release_plan)
        self.assertIn("1.0.2", release_design)
        self.assertIn("docker.io/cangnan88/waha-panel:1.0.8", readme)
        self.assertIn("docker.io/cangnan88/waha-panel:1.0.8", readme_zh)
        self.assertIn("${WAHA_IMAGE:-devlikeapro/waha:latest-2026.9.1}", compose)
        self.assertIn("${WAHA_IMAGE_TAG:-latest-2026.9.1}", compose)
        self.assertIn("${WAHA_BIND_ADDRESS:-127.0.0.1}:${WAHA_PORT:-3002}:3000", compose)
        self.assertIn("${PANEL_BIND_ADDRESS:-127.0.0.1}:${PANEL_PORT:-3003}:3001", compose)
        self.assertIn("- internal", compose)
        self.assertIn("(INSTALL.zh-CN.md)", readme)
        self.assertIn("(INSTALL.zh-CN.md)", readme_zh)

    def test_update_service_defaults_to_panel_1_0_8(self):
        self.assertEqual(UpdateService().current["panel"], "1.0.8")
        self.assertEqual(UpdateService().current["waha"], "latest-2026.9.1")


class AvatarProxyTests(unittest.TestCase):
    def test_waha_avatar_downloads_whatsapp_cdn_without_api_key(self):
        requests = []
        png = b"\x89PNG\r\n\x1a\n"

        class Headers:
            def __init__(self, content_type):
                self.content_type = content_type

            def get_content_type(self):
                return self.content_type

        class Response:
            status = 200

            def __init__(self, body):
                self.body = body
                content_type = "application/json" if body.lstrip().startswith(b"{") else "image/png"
                self.headers = Headers(content_type)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, *_args):
                return self.body

        def opener(request, timeout=0):
            requests.append(request)
            if request.full_url.endswith("/picture"):
                return Response(b'{"url":"https://pps.whatsapp.net/avatar.jpg"}')
            return Response(png)

        client = WahaClient("http://waha:3000", "secret-api-key", opener=opener)
        status, content_type, body = client.get_chat_picture("default", "12345@c.us")
        self.assertEqual((status, content_type, body), (200, "image/png", png))
        self.assertEqual(requests[0].full_url, "http://waha:3000/api/default/chats/12345%40c.us/picture")
        self.assertNotIn("X-Api-Key", requests[1].headers)
        self.assertEqual(requests[1].host, "pps.whatsapp.net")

    def test_qr_placeholder_uses_frosted_texture_layers(self):
        page = multi_session_html_page()
        self.assertIn("feTurbulence", page)
        self.assertIn("grid-area:1 / 1", page)
        self.assertNotIn("repeating-conic-gradient", page)


class ChatPageRegressionTests(unittest.TestCase):
    def test_page_contains_persistent_chat_controls(self):
        page = chat_management_page("default")
        self.assertIn("scrollToBottom", page)
        self.assertIn("translationByMessage", page)
        self.assertIn("handlePaste", page)
        self.assertIn("handleDrop", page)
        self.assertIn("客户备注", page)
        self.assertIn("sessionSelect", page)

    def test_page_contains_chat_engagement_controls(self):
        page = chat_management_page("default")
        for control_id in ("followUpButton", "labelButton", "summaryButton", "followUpDialog", "labelDialog", "summaryDialog"):
            self.assertIn(f'id="{control_id}"', page)
        self.assertIn("手动", page)
        self.assertIn("AI", page)
        for delay in ("24h", "3d", "7d", "15d"):
            self.assertIn(f'value="{delay}"', page)
        for mode in ("AI", "FIXED"):
            self.assertIn(f'value="{mode}"', page)
        self.assertIn("当前总结", page)

    def test_chat_engagement_script_has_state_aware_handlers(self):
        page = chat_management_page("default")
        for function_name in ("loadLabels", "saveManualLabel", "loadSummary", "generateSummary", "loadFollowUps", "createFollowUp", "cancelFollowUp"):
            self.assertIn(f"function {function_name}", page)
        self.assertIn("state.selected.chat_ref", page)
        self.assertIn("$('messageStack').replaceChildren", page)
        self.assertNotIn("$('messageStack').replaceChildren($('labelDialog'))", page)

    def test_chat_engagement_state_and_accessibility_guards(self):
        page = chat_management_page("default")
        for dialog_id in ("labelDialog", "summaryDialog", "followUpDialog"):
            self.assertIn(f'aria-labelledby="{dialog_id}Title"', page)
            self.assertIn(f'aria-describedby="{dialog_id}Description"', page)
        self.assertIn('role="radiogroup"', page)
        self.assertIn('data-task-id', page)
        self.assertIn("requestedChatRef", page)
        self.assertIn("requestGeneration", page)
        self.assertIn("updated_at", page)
        self.assertNotIn("Math.floor(Date.now()/1000)", page)


if __name__ == "__main__":
    unittest.main()
