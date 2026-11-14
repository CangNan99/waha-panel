import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

from panel.app import PanelHandler, PanelState, init_db


class AiProfileRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        database = Path(self.temp.name) / "panel.sqlite3"
        init_db(database, seed_business=False)
        self.state = PanelState(database, object(), "waha-key")
        self.state._admin_db_auth = False
        self.state.test_ai_profile = lambda session, capability: {
            "ok": True,
            "session": session,
            "capability": capability,
        }
        PanelHandler.state = self.state
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), PanelHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def test_test_ai_profile_route_returns_connection_result(self):
        request = Request(
            self.url + "/api/settings/test-ai?session=sales&capability=vision_understanding",
            method="POST",
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            payload = json.load(response)
        self.assertEqual(
            payload,
            {"ok": True, "session": "sales", "capability": "vision_understanding"},
        )


if __name__ == "__main__":
    unittest.main()
