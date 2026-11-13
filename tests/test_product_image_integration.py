import base64
import io
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from cryptography.fernet import Fernet
from PIL import Image

from panel.app import PanelHandler, PanelState, init_db


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def multipart_body(fields, file_field, filename, content_type, data):
    boundary = "----product-image-integration"
    chunks = []
    for name, value in fields.items():
        chunks.extend([
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
            str(value).encode("utf-8"),
            b"\r\n",
        ])
    chunks.extend([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {content_type}\r\n\r\n".encode(),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ])
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class _Client:
    def __init__(self):
        self.status = "WORKING"
        self.mode = "ok"
        self.image_calls = 0

    def get_sessions(self):
        return [{"name": "default", "status": self.status}]

    def get_chats(self, *_args):
        return [{"id": "customer@c.us", "name": "客户"}]

    def get_messages(self, *_args, **_kwargs):
        return []

    def send_image(self, *_args):
        self.image_calls += 1
        if self.mode == "unknown":
            raise TimeoutError("synthetic timeout")
        return {"id": f"wamid.product.{self.image_calls}"}


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload, ensure_ascii=False).encode("utf-8")


class ProductImageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.client = _Client()
        self.state = PanelState(
            self.database,
            self.client,
            "fixture-key",
            admin_username="admin",
            admin_password="fixture-password",
            data_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        self.state.save_settings({"auto_reply_all_day": True}, "default")
        self.asset = self.state.product_images.upload(
            "product.png", "image/png", PNG_1X1, "产品图", ["IPTV"], "了解 {product_tag}",
            product_name="G5 Plus", sku="G5-PLUS",
        )
        self.chat_id = "customer@c.us"
        self.timestamp = int(time.time()) + 2
        self.ticket = self.state._reply_ticket("default", self.timestamp, self.chat_id)
        handler = type("ProductImageIntegrationHandler", (PanelHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.authorization = "Basic " + base64.b64encode(b"admin:fixture-password").decode("ascii")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.state.stop_background_services()
        self.temp.cleanup()

    def request(self, path, method="GET", payload=None, authenticated=True, csrf=False, headers=None):
        request_headers = dict(headers or {})
        if authenticated:
            request_headers.setdefault("Authorization", self.authorization)
        if csrf:
            request_headers["X-CSRF-Token"] = self.state.csrf_token
        body = payload
        if payload is not None and not isinstance(payload, bytes):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        try:
            response = urlopen(Request(self.base + path, headers=request_headers, data=body, method=method), timeout=5)
        except HTTPError as error:
            response = error
        with closing(response):
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            parsed = json.loads(raw.decode("utf-8")) if content_type.startswith("application/json") else raw
            return response.status, dict(response.headers), parsed

    def execute(self, arguments, message_id="message-1", incoming="我想了解 IPTV"):
        return self.state._execute_product_image(
            "default", self.chat_id, message_id, self.timestamp, self.ticket,
            json.dumps(arguments, ensure_ascii=False), incoming, "客户：" + incoming, True,
        )

    def test_exact_tag_match_sends_once_and_repeated_tool_call_is_idempotent(self):
        first = self.execute({"product_tag": "iptv", "caption": "产品说明"})
        second = self.execute({"product_tag": "iptv", "caption": "产品说明"})
        self.assertEqual(first["action"], "replied", first)
        self.assertEqual(second["action"], "ignored")
        self.assertEqual(self.client.image_calls, 1)
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT state FROM product_image_send_requests").fetchone()[0], "SENT")

    def test_authenticated_match_diagnostic_and_product_search_are_real_http_requests(self):
        status, _, denied = self.request("/api/product-image-match-test", method="POST", payload=b"", authenticated=False)
        self.assertEqual(status, 401)
        self.assertIn("message", denied)

        content_type, body = multipart_body(
            {"candidate_product_id": ""}, "image", "probe.png", "image/png", PNG_1X1
        )
        status, _, payload = self.request(
            "/api/product-image-match-test",
            method="POST",
            payload=body,
            csrf=True,
            headers={"Content-Type": content_type},
        )
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["status"], "MATCHED")
        self.assertTrue(payload["candidates"])
        self.assertEqual(self.client.image_calls, 0, "diagnostic requests must never send WhatsApp images")

        status, _, filtered = self.request("/api/product-images?product=does-not-exist")
        self.assertEqual(status, 200)
        self.assertEqual(filtered["items"], [])
        status, _, filtered = self.request("/api/product-images?product=G5%20Plus")
        self.assertEqual(status, 200)
        self.assertEqual(filtered["total"], 1)
        self.assertEqual(filtered["items"][0]["asset_id"], self.asset["asset_id"])

        content_type, body = multipart_body(
            {"candidate_product_id": "missing-product"}, "image", "probe.png", "image/png", PNG_1X1
        )
        status, _, payload = self.request(
            "/api/product-image-match-test",
            method="POST",
            payload=body,
            csrf=True,
            headers={"Content-Type": content_type},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "NO_MATCH")
        self.assertEqual(payload["candidates"], [])
        self.assertEqual(self.client.image_calls, 0)

    def test_catalog_migration_preserves_legacy_asset_metadata_and_send_ledger(self):
        # Re-running init_db must be additive and leave the legacy image row
        # and its idempotency ledger readable.
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(
                "UPDATE product_image_assets SET product_name=?,sku=?,aliases_json=?,caption_template=? WHERE id=?",
                ("Legacy IPTV", "L-IPTV", json.dumps(["legacy alias"]), "Legacy {product_tag}", self.asset["asset_id"]),
            )
            connection.execute(
                "INSERT INTO product_image_tags(tag_key,display_name,created_at,updated_at) VALUES (?,?,?,?)",
                ("legacy-tag", "Legacy Tag", 1, 1),
            )
            tag_id = connection.execute("SELECT id FROM product_image_tags WHERE tag_key='legacy-tag'").fetchone()[0]
            connection.execute(
                "INSERT INTO product_image_asset_tags(asset_id,tag_id,priority,is_primary,created_at) VALUES (?,?,?,?,?)",
                (self.asset["asset_id"], tag_id, 1, 1, 1),
            )
            connection.execute(
                "INSERT INTO product_image_send_requests(request_id,session_name,chat_key_hmac,message_key,asset_id,tag_key,caption_hash,payload_hash,state,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy-ledger", "default", "hmac", "legacy-message", self.asset["asset_id"], "legacy-tag", "caption", "payload", "SENT", 1, 1),
            )
            connection.commit()
        init_db(self.database, seed_business=False)
        with closing(sqlite3.connect(self.database)) as connection:
            asset = connection.execute(
                "SELECT product_name,sku,aliases_json,caption_template FROM product_image_assets WHERE id=?",
                (self.asset["asset_id"],),
            ).fetchone()
            tags = connection.execute(
                "SELECT t.tag_key,t.display_name FROM product_image_asset_tags at JOIN product_image_tags t ON t.id=at.tag_id WHERE at.asset_id=?",
                (self.asset["asset_id"],),
            ).fetchall()
            ledger = connection.execute(
                "SELECT state FROM product_image_send_requests WHERE asset_id=?",
                (self.asset["asset_id"],),
            ).fetchone()
        self.assertEqual(asset, ("Legacy IPTV", "L-IPTV", '["legacy alias"]', "Legacy {product_tag}"))
        self.assertEqual(tags, [("IPTV", "IPTV"), ("legacy-tag", "Legacy Tag")])
        self.assertEqual(ledger, ("SENT",))

    def test_no_match_ambiguous_and_disabled_assets_send_nothing(self):
        self.assertEqual(self.execute({"product_tag": "missing"})["action"], "ignored")
        second = self.state.product_images.upload(
            "second.png", "image/png", self._alt_png(), "第二张", ["IPTV"], ""
        )
        self.assertEqual(self.execute({"product_tag": "iptv"}, "ambiguous")["action"], "ignored")
        self.state.product_images.disable_asset(second["asset_id"])
        self.assertEqual(self.execute({"product_tag": "iptv"}, "disabled")["action"], "replied")
        self.assertEqual(self.client.image_calls, 1)

    def test_product_tag_must_be_a_complete_ascii_token_in_current_message(self):
        self.state.product_images.upload(
            "pro.png", "image/png", self._alt_png(), "专业版", ["pro"], ""
        )
        self.assertEqual(
            self.execute({"product_tag": "pro"}, "substring", incoming="这是 problem 说明")["action"],
            "ignored",
        )
        self.assertEqual(self.client.image_calls, 0)

    def test_product_name_and_alias_match_exactly_without_substring_fallback(self):
        self.state.product_images.upload(
            "alias.png",
            "image/png",
            self._alt_png(),
            "G5 图片",
            ["catalog"],
            "",
            product_name="G5 Plus",
            sku="G5-PLUS",
            aliases=["IPTV Pro"],
        )
        sent = self.execute(
            {"product_tag": "IPTV Pro"},
            "alias-exact",
            incoming="我想了解 IPTV Pro",
        )
        self.assertEqual(sent["action"], "replied", sent)
        self.assertEqual(self.client.image_calls, 1)
        ignored = self.execute(
            {"product_tag": "pro"},
            "alias-substring",
            incoming="我想了解 problem",
        )
        self.assertEqual(ignored["action"], "ignored")
        self.assertEqual(self.client.image_calls, 1)

    @staticmethod
    def _alt_png():
        output = io.BytesIO()
        Image.new("RGBA", (2, 2), (20, 30, 40, 255)).save(output, format="PNG")
        return output.getvalue()

    def test_human_takeover_disconnected_and_daily_limit_block_sends(self):
        chat_ref = self.state.chat.overview("default")["items"][0]["chat_ref"]
        self.state.chat.takeover("default", chat_ref)
        self.assertEqual(self.execute({"product_tag": "iptv"}, "takeover")["action"], "ignored")
        self.state.chat.resume_ai("default", chat_ref)
        self.client.status = "FAILED"
        self.assertEqual(self.execute({"product_tag": "iptv"}, "disconnected")["action"], "ignored")
        self.client.status = "WORKING"
        self.state._sync_reply_connections()
        self.ticket = self.state._reply_ticket("default", self.timestamp, self.chat_id)
        self.state.product_images.save_settings({"daily_limit": 1})
        self.assertEqual(self.execute({"product_tag": "iptv"}, "daily-1")["action"], "replied")
        self.assertEqual(self.execute({"product_tag": "iptv"}, "daily-2")["action"], "ignored")
        self.assertEqual(self.client.image_calls, 1)

    def test_invalid_arguments_and_unknown_are_persisted_without_retry(self):
        self.assertEqual(self.execute({"product_tag": "iptv", "unexpected": True})["action"], "ignored")
        self.client.mode = "unknown"
        result = self.execute({"product_tag": "iptv"}, "unknown")
        self.assertEqual(result["action"], "ignored")
        repeated = self.execute({"product_tag": "iptv"}, "unknown")
        self.assertEqual(repeated["action"], "ignored")
        self.assertEqual(self.client.image_calls, 1)
        with closing(sqlite3.connect(self.database)) as connection:
            state = connection.execute(
                "SELECT state FROM product_image_send_requests WHERE message_key='unknown'"
            ).fetchone()[0]
        self.assertEqual(state, "UNKNOWN")

    def test_ai_tool_schema_is_strict_and_respects_global_switch(self):
        self.state.save_settings(
            {"ai_base_url": "https://ai.example/v1", "ai_model": "model", "ai_api_key": "secret"},
            "default",
        )
        self.state._ai_reply = PanelState._ai_reply.__get__(self.state, PanelState)
        response = _Response({"choices": [{"message": {"content": "好的"}}]})
        with patch("panel.app.urlopen", return_value=response) as opener:
            self.state._ai_reply(
                self.state._settings("default"), "请介绍 IPTV", customer_id=self.chat_id,
                include_tools=True, session_name="default"
            )
        payload = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        product_tools = [item for item in payload["tools"] if item["function"]["name"] == "send_product_image"]
        self.assertEqual(len(product_tools), 1)
        parameters = product_tools[0]["function"]["parameters"]
        self.assertEqual(parameters["required"], ["product_tag"])
        self.assertFalse(parameters["additionalProperties"])
        self.state.product_images.save_settings({"auto_send_enabled": False})
        with patch("panel.app.urlopen", return_value=response) as opener:
            self.state._ai_reply(
                self.state._settings("default"), "请介绍 IPTV", customer_id=self.chat_id,
                include_tools=True, session_name="default"
            )
        payload = json.loads(opener.call_args.args[0].data.decode("utf-8"))
        self.assertFalse(any(item["function"]["name"] == "send_product_image" for item in payload.get("tools", [])))

    def test_webhook_tool_call_sends_product_image_and_duplicate_webhook_is_archived_once(self):
        self.state.save_settings(
            {"ai_base_url": "https://ai.example/v1", "ai_model": "model", "ai_api_key": "secret"},
            "default",
        )
        tool_response = _Response(
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "tool_calls": [
                                {
                                    "function": {
                                        "name": "send_product_image",
                                        "arguments": json.dumps({"product_tag": "iptv"}),
                                    }
                                }
                            ]
                        },
                    }
                ]
            }
        )
        event = {
            "event": "message",
            "session": "default",
            "payload": {
                "id": "webhook-product-1",
                "from": self.chat_id,
                "to": self.chat_id,
                "fromMe": False,
                "body": "我想了解 IPTV",
                "type": "chat",
                "timestamp": self.timestamp,
            },
        }
        with patch("panel.app.urlopen", return_value=tool_response):
            first = self.state.handle_webhook(json.dumps(event).encode("utf-8"), dispatch=True)
            second = self.state.handle_webhook(json.dumps(event).encode("utf-8"), dispatch=True)
        self.assertEqual(first["action"], "replied", first)
        self.assertEqual(second["action"], "ignored", second)
        self.assertEqual(self.client.image_calls, 1)


if __name__ == "__main__":
    unittest.main()
