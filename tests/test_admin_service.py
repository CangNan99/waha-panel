import os
import sqlite3
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from http import HTTPStatus

from panel import admin_service
from panel.app import PanelHandler
from panel.admin_service import (
    AdminAuthUnavailableError,
    AdminService,
    apply_migration,
)


class AdminArgon2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = Path(self.temp.name) / "panel.sqlite3"
        connection = sqlite3.connect(self.database)
        apply_migration(connection)
        connection.commit()
        connection.close()
        self.service = AdminService(self.database)

    def tearDown(self):
        self.temp.cleanup()

    def _password_hash(self):
        connection = sqlite3.connect(self.database)
        try:
            return connection.execute(
                "SELECT password_hash FROM admin_users WHERE username = ?",
                ("admin",),
            ).fetchone()[0]
        finally:
            connection.close()

    def test_new_admin_password_uses_memory_bounded_argon2_parameters(self):
        self.service.create_user("admin", "a" * 12)

        encoded = self._password_hash()

        self.assertIn("m=32768,t=2,p=2", encoded)

    def test_successful_login_rehashes_legacy_high_cost_password(self):
        password = "legacy-password"
        legacy_hash = Argon2id(
            os.urandom(16),
            32,
            3,
            4,
            64 * 1024,
        ).derive_phc_encoded(password.encode("utf-8"))
        now = int(time.time())
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "INSERT INTO admin_users(username, password_hash, is_active, created_at, updated_at) "
                "VALUES (?, ?, 1, ?, ?)",
                ("admin", legacy_hash, now, now),
            )
            connection.commit()
        finally:
            connection.close()

        self.assertTrue(self.service.authenticate("admin", password, "client"))

        self.assertIn("m=32768,t=2,p=2", self._password_hash())

    def test_argon2_derivations_are_limited_to_two_concurrent_operations(self):
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        class TrackingArgon2:
            def __init__(self, *_args):
                pass

            def derive_phc_encoded(self, _password):
                with lock:
                    state["active"] += 1
                    state["maximum"] = max(state["maximum"], state["active"])
                try:
                    time.sleep(0.03)
                    return "$argon2id$v=19$m=32768,t=2,p=2$fixture$fixture"
                finally:
                    with lock:
                        state["active"] -= 1

        with patch.object(admin_service, "Argon2id", TrackingArgon2):
            with ThreadPoolExecutor(max_workers=6) as executor:
                list(executor.map(admin_service._password_hash, ["a"] * 6))

        self.assertLessEqual(state["maximum"], 2)

    def test_argon2_memory_error_is_classified_as_auth_unavailable(self):
        class OutOfMemoryArgon2:
            @staticmethod
            def verify_phc_encoded(_password, _encoded):
                raise MemoryError("simulated Argon2 allocation failure")

        with patch.object(admin_service, "Argon2id", OutOfMemoryArgon2):
            with self.assertRaises(AdminAuthUnavailableError):
                admin_service._verify_password("password", "encoded")

    def test_auth_resource_error_returns_service_unavailable(self):
        handler = object.__new__(PanelHandler)
        responses = []
        handler.send_json = lambda payload, status: responses.append((payload, status))

        handler.send_service_error(AdminAuthUnavailableError("temporary pressure"))

        self.assertEqual(responses[0][1], HTTPStatus.SERVICE_UNAVAILABLE)
        self.assertEqual(responses[0][0]["code"], "ADMIN_AUTH_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
