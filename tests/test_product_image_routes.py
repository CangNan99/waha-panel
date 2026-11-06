import base64
import json
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from cryptography.fernet import Fernet

from panel.app import PanelHandler, PanelState, init_db


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _Client:
    def get_sessions(self):
        return [{"name": "default", "status": "WORKING"}]

    def get_chats(self, *_args):
        return [{"id": "customer@c.us", "name": "客户"}]


def multipart_body(fields, file_field, filename, content_type, data):
    boundary = "----product-image-test"
    chunks = []
    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    chunks.extend(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return f"multipart/form-data; boundary={boundary}", b"".join(chunks)


class ProductImageRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        init_db(self.database, seed_business=False)
        self.state = PanelState(
            self.database,
            _Client(),
            "fixture-key",
            admin_username="admin",
            admin_password="fixture-password",
            data_encryption_key=Fernet.generate_key().decode("ascii"),
        )
        handler = type("ProductImageHandler", (PanelHandler,), {"state": self.state})
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

    def request(self, path, authenticated=True, method="GET", payload=None, csrf=False, headers=None):
        request_headers = dict(headers or {})
        if authenticated:
            request_headers.setdefault("Authorization", self.authorization)
        if csrf:
            request_headers["X-CSRF-Token"] = self.state.csrf_token
        body = None
        if payload is not None and not isinstance(payload, bytes):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        else:
            body = payload
        try:
            response = urlopen(
                Request(self.base + path, headers=request_headers, data=body, method=method),
                timeout=5,
            )
        except HTTPError as error:
            response = error
        with closing(response):
            raw = response.read()
            content_type = response.headers.get("Content-Type", "")
            if content_type.startswith("application/json"):
                data = json.loads(raw.decode("utf-8"))
            else:
                data = raw
            return response.status, dict(response.headers), data

    def upload(self, csrf=True):
        content_type, body = multipart_body(
            {"title": "产品图", "tags": json.dumps([" IPTV ", "Premium"]), "caption_template": "欢迎了解"},
            "image",
            "product.png",
            "image/png",
            PNG_1X1,
        )
        return self.request(
            "/api/product-images",
            method="POST",
            payload=body,
            csrf=csrf,
            headers={"Content-Type": content_type},
        )

    def test_product_image_routes_require_admin_and_csrf(self):
        self.assertEqual(self.request("/api/product-images", authenticated=False)[0], 401)
        self.assertEqual(self.request("/api/product-images")[0], 200)
        self.assertEqual(self.upload(csrf=False)[0], 403)
        status, _, payload = self.upload(csrf=True)
        self.assertEqual(status, 201)
        self.assertNotIn("relative_path", payload)
        self.assertNotIn("sha256", payload)
        self.assertNotIn(str(self.temp.name), json.dumps(payload, ensure_ascii=False))

    def test_preview_is_private_and_metadata_never_exposes_filesystem_paths(self):
        _, _, uploaded = self.upload()
        asset_id = uploaded["asset_id"]
        status, headers, body = self.request(f"/api/product-images/{asset_id}/preview")
        self.assertEqual(status, 200)
        self.assertEqual(headers["Cache-Control"], "private, no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(body, PNG_1X1)
        status, _, body = self.request("/product-images")
        self.assertEqual(status, 200)
        self.assertNotIn(str(self.temp.name), body.decode("utf-8"))
        self.assertIn("设为主图", body.decode("utf-8"))
        self.assertIn("保存优先级", body.decode("utf-8"))

    def test_asset_update_disable_tag_crud_and_settings_round_trip(self):
        _, _, uploaded = self.upload()
        asset_id = uploaded["asset_id"]
        status, _, updated = self.request(
            f"/api/product-images/{asset_id}",
            method="PUT",
            payload={"title": "更新产品", "tags": ["新标签"], "active": True},
            csrf=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(updated["title"], "更新产品")
        status, _, deleted = self.request(f"/api/product-images/{asset_id}", method="DELETE", csrf=True)
        self.assertEqual(status, 200)
        self.assertFalse(deleted["active"])

        status, _, tag = self.request(
            "/api/product-tags", method="POST", payload={"display_name": "VIP 客户"}, csrf=True
        )
        self.assertEqual(status, 201)
        status, _, tags = self.request("/api/product-tags")
        self.assertEqual(status, 200)
        self.assertTrue(any(item["tag_key"] == "vip 客户" for item in tags["items"]))
        status, _, settings = self.request(
            "/api/product-image-settings", method="PUT", payload={"auto_send_enabled": False, "daily_limit": 9}, csrf=True
        )
        self.assertEqual(status, 200)
        self.assertEqual(settings, {"auto_send_enabled": False, "daily_limit": 9})
        self.assertEqual(self.request("/api/product-image-settings")[2], settings)


if __name__ == "__main__":
    unittest.main()
