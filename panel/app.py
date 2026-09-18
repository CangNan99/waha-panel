import base64
import html
import hashlib
import hmac
import json
import os
import random
import re
import sqlite3
import threading
import time
from dataclasses import replace
from datetime import datetime
from email.parser import BytesParser
from email.policy import default as email_policy
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    from .admin_service import (
        AdminConflictError,
        AdminNotFoundError,
        AdminService,
        AdminServiceError,
        AdminValidationError,
        LastActiveAdministratorError,
        apply_migration as apply_admin_migration,
    )
    from .business_context import BusinessContextService, seed_business_database
    from .chat_security import DataCipher, ReferenceCodec, derive_csrf_token
    from .chat_service import (
        ChatAccessError,
        ChatSendError,
        ChatService,
        ChatServiceError,
        ImageValidationError,
        SendConflictError,
    )
    from .chat_automation import ChatAutomationService
    from .chat_page import chat_management_page
    from .commerce import (
        CommerceConfig,
        CommerceError,
        ManualOrderCaseService,
        OrderQueryCoordinator,
        VerificationService,
        apply_migration as apply_commerce_migration,
        redact_sensitive,
    )
    from .commerce_page import commerce_page
    from .translation_service import TranslationError, TranslationService
    from .update_service import UpdateService
except ImportError:  # Supports the existing `python app.py` container entrypoint.
    from admin_service import (
        AdminConflictError,
        AdminNotFoundError,
        AdminService,
        AdminServiceError,
        AdminValidationError,
        LastActiveAdministratorError,
        apply_migration as apply_admin_migration,
    )
    from business_context import BusinessContextService, seed_business_database
    from chat_security import DataCipher, ReferenceCodec, derive_csrf_token
    from chat_service import (
        ChatAccessError,
        ChatSendError,
        ChatService,
        ChatServiceError,
        ImageValidationError,
        SendConflictError,
    )
    from chat_automation import ChatAutomationService
    from chat_page import chat_management_page
    from commerce import (
        CommerceConfig,
        CommerceError,
        ManualOrderCaseService,
        OrderQueryCoordinator,
        VerificationService,
        apply_migration as apply_commerce_migration,
        redact_sensitive,
    )
    from commerce_page import commerce_page
    from translation_service import TranslationError, TranslationService
    from update_service import UpdateService


CONNECTED_STATES = {"WORKING", "CONNECTED"}
IN_PROGRESS_STATES = {"STARTING", "SCAN_QR_CODE", "AUTHENTICATING"}
SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_REPLY_TEXT = "您好，我现在暂时无法及时回复，稍后回复您。"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
THEMES = ("daylight", "night", "paper")
SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
DEFAULT_SESSION_NAME = "default"
DEFAULT_SESSION_DISPLAY_NAME = "默认会话"
DEFAULT_SETTINGS = {
    "auto_reply_enabled": "0",
    "auto_reply_all_day": "0",
    "weekly_reply_windows": json.dumps({}, ensure_ascii=False),
    "default_reply_text": DEFAULT_REPLY_TEXT,
    "persona": "",
    "system_prompt": "",
    "ai_base_url": "",
    "ai_model": "",
    "ai_api_key": "",
    "theme": "daylight",
}
HOT_CONVERSATION_SECONDS = 10 * 60
MIN_TYPING_DELAY_SECONDS = 6.0
MAX_TYPING_DELAY_SECONDS = 15.0
CONTEXT_MESSAGE_LIMIT = 20
CONTEXT_CHARACTER_LIMIT = 12000
SENSITIVE_NAME = re.compile(
    r"(?i)(api[_-]?key|password|token|secret|authorization)\s*([:=])\s*([^\s,;}&]+)"
)


def read_credentials(path):
    values = {}
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        values[key.strip()] = value
    return values


def redact_error(message, secret=None):
    safe = str(message or "")
    if secret:
        safe = safe.replace(secret, "<redacted>")
    safe = SENSITIVE_NAME.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", safe)
    return safe[:500]


def classify_session_state(state):
    return str(state or "").upper() in CONNECTED_STATES


def as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalize_theme(value):
    theme = str(value or "daylight").strip().lower()
    return theme if theme in THEMES else "daylight"


def normalize_session_name(value):
    """Validate the immutable WAHA technical session identifier."""
    name = str(value or "").strip()
    if not SESSION_NAME_RE.fullmatch(name):
        raise ValueError("会话技术名称只能使用字母、数字、点、下划线或短横线，且长度为 1-64 个字符")
    return name


def normalize_display_name(value, fallback=None):
    name = str(value or "").strip()
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)[:80]
    return name or str(fallback or "")[:80]


def extract_sessions(value):
    """Return a list for both the current WAHA response and wrapped variants."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("data", "sessions", "items"):
            candidate = value.get(key)
            if isinstance(candidate, list):
                return [item for item in candidate if isinstance(item, dict)]
    return []


def normalize_phone_number(value):
    raw = str(value or "").strip()
    if raw.startswith("+"):
        raw = raw[1:]
    digits = re.sub(r"[\s().-]", "", raw)
    if not re.fullmatch(r"[1-9]\d{6,14}", digits):
        raise ValueError("请输入包含国家/地区码的手机号，例如 8613812345678")
    return digits


def normalize_windows(value):
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("每周回复时间段格式不正确")
    normalized = {}
    for day, ranges in value.items():
        if day not in WEEKDAYS:
            raise ValueError("每周回复时间段包含无效星期")
        if not isinstance(ranges, list):
            raise ValueError("每周回复时间段必须是列表")
        normalized[day] = []
        for item in ranges:
            start = str(item.get("start", "")) if isinstance(item, dict) else ""
            end = str(item.get("end", "")) if isinstance(item, dict) else ""
            if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", end):
                raise ValueError("时间段必须使用 HH:MM 格式")
            if start == end:
                raise ValueError("时间段开始和结束时间不能相同")
            normalized[day].append({"start": start, "end": end})
    return normalized


def parse_networks(value):
    networks = []
    for raw in str(value or "").split(","):
        raw = raw.strip()
        if raw:
            networks.append(ip_network(raw, strict=False))
    return networks


def _table_columns(connection, table_name):
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()]


def _add_column_if_missing(connection, table_name, column_definition):
    column_name = column_definition.split()[0]
    if column_name not in _table_columns(connection, table_name):
        connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_definition}")


def _migrate_composite_table(connection, table_name, create_sql, copy_sql, primary_columns):
    """Upgrade legacy single-key tables without losing their existing rows."""
    info = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    if not info:
        connection.execute(create_sql)
        return
    primary = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
    if primary == list(primary_columns):
        return
    legacy_name = f"{table_name}__legacy"
    # A leftover legacy table can only be an artifact of an interrupted migration.
    # It is safe to remove it after the live table has been confirmed to be legacy.
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (legacy_name,)
    ).fetchone():
        connection.execute(f"DROP TABLE {legacy_name}")
    connection.execute(f"ALTER TABLE {table_name} RENAME TO {legacy_name}")
    connection.execute(create_sql)
    connection.execute(copy_sql.format(legacy=legacy_name))
    connection.execute(f"DROP TABLE {legacy_name}")


def ensure_multi_session_schema(connection):
    """Apply the multi-session schema in an idempotent way to old installations."""
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS managed_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1))
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS session_settings (
            session_name TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (session_name, key),
            FOREIGN KEY (session_name) REFERENCES managed_sessions(session_name) ON DELETE CASCADE
        )
        """
    )
    _add_column_if_missing(connection, "knowledge_base", "session_name TEXT NOT NULL DEFAULT 'default'")
    _add_column_if_missing(connection, "conversation_messages", "session_name TEXT NOT NULL DEFAULT 'default'")
    _add_column_if_missing(connection, "system_logs", "session_name TEXT NOT NULL DEFAULT 'default'")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_session ON knowledge_base(session_name, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversation_messages_session_chat "
        "ON conversation_messages(session_name, chat_id, id)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_system_logs_session ON system_logs(session_name, id)"
    )
    _migrate_composite_table(
        connection,
        "message_dedupe",
        """
        CREATE TABLE message_dedupe (
            session_name TEXT NOT NULL DEFAULT 'default',
            message_id TEXT NOT NULL,
            received_at INTEGER NOT NULL,
            PRIMARY KEY (session_name, message_id)
        )
        """,
        "INSERT INTO message_dedupe(session_name, message_id, received_at) "
        "SELECT 'default', message_id, received_at FROM {legacy}",
        ("session_name", "message_id"),
    )
    _migrate_composite_table(
        connection,
        "conversation_activity",
        """
        CREATE TABLE conversation_activity (
            session_name TEXT NOT NULL DEFAULT 'default',
            chat_id TEXT NOT NULL,
            last_incoming_at INTEGER NOT NULL,
            PRIMARY KEY (session_name, chat_id)
        )
        """,
        "INSERT INTO conversation_activity(session_name, chat_id, last_incoming_at) "
        "SELECT 'default', chat_id, last_incoming_at FROM {legacy}",
        ("session_name", "chat_id"),
    )
    now = int(time.time())
    connection.execute(
        "INSERT OR IGNORE INTO managed_sessions(session_name, display_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?)",
        (DEFAULT_SESSION_NAME, DEFAULT_SESSION_DISPLAY_NAME, now, now),
    )
    connection.execute(
        "UPDATE managed_sessions SET display_name = ?, updated_at = ? "
        "WHERE session_name = ? AND trim(display_name) = ''",
        (DEFAULT_SESSION_DISPLAY_NAME, now, DEFAULT_SESSION_NAME),
    )
    # Copy the pre-migration global settings exactly once.  INSERT OR IGNORE keeps
    # values already customized in the new table intact on subsequent startups.
    connection.execute(
        "INSERT OR IGNORE INTO session_settings(session_name, key, value, updated_at) "
        "SELECT ?, key, value, updated_at FROM settings",
        (DEFAULT_SESSION_NAME,),
    )
    connection.executemany(
        "INSERT OR IGNORE INTO session_settings(session_name, key, value, updated_at) VALUES (?, ?, ?, ?)",
        [(DEFAULT_SESSION_NAME, key, value, now) for key, value in DEFAULT_SETTINGS.items()],
    )
    connection.execute("PRAGMA foreign_keys = ON")


def init_db(path, seed_business=True):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS knowledge_base (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS message_dedupe (
                message_id TEXT PRIMARY KEY,
                received_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_activity (
                chat_id TEXT PRIMARY KEY,
                last_incoming_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS conversation_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound')),
                message_id TEXT,
                content TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_conversation_messages_chat_id
                ON conversation_messages(chat_id, id);
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                event TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            """
        )
        migration_path = Path(__file__).with_name("migrations") / "001_ai_business_schema.sql"
        connection.executescript(migration_path.read_text(encoding="utf-8"))
        apply_commerce_migration(connection)
        now = int(time.time())
        defaults = DEFAULT_SETTINGS
        connection.executemany(
            "INSERT OR IGNORE INTO settings(key, value, updated_at) VALUES (?, ?, ?)",
            [(key, value, now) for key, value in defaults.items()],
        )
        connection.execute(
            "UPDATE settings SET value = ?, updated_at = ? "
            "WHERE key = 'default_reply_text' AND trim(value) = ''",
            (DEFAULT_REPLY_TEXT, now),
        )
        ensure_multi_session_schema(connection)
        multi_session_migration = Path(__file__).with_name("migrations") / "003_multi_session.sql"
        connection.executescript(multi_session_migration.read_text(encoding="utf-8"))
        chat_ai_migration = Path(__file__).with_name("migrations") / "004_chat_ai_assistant.sql"
        connection.executescript(chat_ai_migration.read_text(encoding="utf-8"))
        chat_metadata_migration = Path(__file__).with_name("migrations") / "006_chat_metadata.sql"
        connection.executescript(chat_metadata_migration.read_text(encoding="utf-8"))
        engagement_migration = Path(__file__).with_name("migrations") / "007_chat_engagement.sql"
        connection.executescript(engagement_migration.read_text(encoding="utf-8"))
        _add_column_if_missing(connection, "chat_takeovers", "last_manual_sent_at INTEGER")
        _add_column_if_missing(connection, "chat_takeovers", "auto_resume_at INTEGER")
        admin_migration_path = Path(__file__).with_name("migrations") / "005_admin_users.sql"
        connection.executescript(admin_migration_path.read_text(encoding="utf-8"))
        apply_admin_migration(connection)
        if seed_business:
            seed_business_database(connection)
        connection.commit()
    finally:
        connection.close()


class WahaApiError(RuntimeError):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class AdminAuthError(PermissionError):
    pass


class RequestTooLarge(ValueError):
    code = "REQUEST_TOO_LARGE"


class UnsupportedRequestMedia(ValueError):
    code = "UNSUPPORTED_MEDIA_TYPE"


class WahaClient:
    def __init__(self, base_url, api_key, opener=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.opener = opener or urlopen

    def request_json(self, path, method="GET", payload=None):
        body = None
        headers = {"Accept": "application/json", "X-Api-Key": self.api_key}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=body, headers=headers, method=method)
        try:
            with self.opener(request, timeout=10) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else {}
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            raise WahaApiError(error.code, redact_error(raw, self.api_key)) from error
        except (URLError, TimeoutError, OSError) as error:
            raise WahaApiError(0, redact_error(error, self.api_key)) from error

    def request_bytes(self, path):
        request = Request(
            self.base_url + path,
            headers={"Accept": "image/png, image/jpeg, application/json", "X-Api-Key": self.api_key},
        )
        try:
            with self.opener(request, timeout=15) as response:
                return response.status, response.headers.get_content_type(), response.read()
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            raise WahaApiError(error.code, redact_error(raw, self.api_key)) from error
        except (URLError, TimeoutError, OSError) as error:
            raise WahaApiError(0, redact_error(error, self.api_key)) from error

    def get_health(self):
        return self.request_json("/health")

    def get_version(self):
        return self.request_json("/api/version")

    def get_sessions(self):
        return self.request_json("/api/sessions")

    @staticmethod
    def _list_result(value):
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            for key in ("data", "items", "chats", "messages"):
                items = value.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    @staticmethod
    def _session_path(session_name):
        return quote(normalize_session_name(session_name), safe="")

    def create_session(self, session_name=DEFAULT_SESSION_NAME, config=None, start=False):
        name = normalize_session_name(session_name)
        payload = {
            "name": name,
            "config": config if isinstance(config, dict) else {},
            "start": bool(start),
        }
        return self.request_json("/api/sessions", method="POST", payload=payload)

    def start_session(self, session_name=DEFAULT_SESSION_NAME):
        name = self._session_path(session_name)
        return self.request_json(f"/api/sessions/{name}/start", method="POST")

    def stop_session(self, session_name=DEFAULT_SESSION_NAME):
        name = self._session_path(session_name)
        return self.request_json(f"/api/sessions/{name}/stop", method="POST")

    def restart_session(self, session_name=DEFAULT_SESSION_NAME):
        name = self._session_path(session_name)
        return self.request_json(f"/api/sessions/{name}/restart", method="POST")

    def delete_session(self, session_name=DEFAULT_SESSION_NAME):
        name = self._session_path(session_name)
        return self.request_json(f"/api/sessions/{name}", method="DELETE")

    def get_qr(self, session_name=DEFAULT_SESSION_NAME):
        name = self._session_path(session_name)
        return self.request_bytes(f"/api/{name}/auth/qr?format=image")

    def request_pairing_code(self, session_name=DEFAULT_SESSION_NAME, phone_number=None):
        # Keep the old two-argument shape (phone_number) usable for integrations
        # that still call the default session directly.
        if phone_number is None:
            phone_number = session_name
            session_name = DEFAULT_SESSION_NAME
        name = self._session_path(session_name)
        return self.request_json(
            f"/api/{name}/auth/request-code", method="POST",
            payload={"phoneNumber": phone_number},
        )

    def send_text(self, session_name=DEFAULT_SESSION_NAME, chat_id=None, text=None):
        # Backward compatible with send_text(chat_id, text).
        if text is None:
            text = chat_id
            chat_id = session_name
            session_name = DEFAULT_SESSION_NAME
        return self.request_json(
            "/api/sendText", method="POST", payload={
                "session": normalize_session_name(session_name), "chatId": chat_id, "text": text
            }
        )

    def get_chats(self, session_name=DEFAULT_SESSION_NAME, limit=30, offset=0):
        name = self._session_path(session_name)
        query = urlencode({
            "limit": max(1, min(int(limit), 100)),
            "offset": max(0, int(offset)),
            # WAHA's chats endpoint accepts conversationTimestamp, id, or name.
            # conversationTimestamp keeps the list ordered by the latest chat activity.
            "sortBy": "conversationTimestamp",
            "sortOrder": "desc",
        })
        return self._list_result(self.request_json(f"/api/{name}/chats?{query}"))

    def get_messages(self, session_name, chat_id, limit=50, offset=0, before=None,
                     download_media=False):
        name = self._session_path(session_name)
        chat = quote(str(chat_id), safe="")
        query_values = {
            "limit": max(1, min(int(limit), 100)),
            "offset": max(0, int(offset)),
            "downloadMedia": "true" if download_media else "false",
        }
        if before is not None:
            query_values["filter.timestamp.lte"] = max(0, int(before))
        query = urlencode(query_values)
        return self._list_result(
            self.request_json(f"/api/{name}/chats/{chat}/messages?{query}")
        )

    def get_message(self, session_name, chat_id, message_id, download_media=True):
        name = self._session_path(session_name)
        chat = quote(str(chat_id), safe="")
        message = quote(str(message_id), safe="")
        query = urlencode({"downloadMedia": "true" if download_media else "false"})
        return self.request_json(f"/api/{name}/chats/{chat}/messages/{message}?{query}")

    @staticmethod
    def _url_origin(value):
        parsed = urlparse(value)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if scheme == "https" else 80 if scheme == "http" else None)
        return scheme, host, port

    def get_media_bytes(self, media_url):
        target = urljoin(self.base_url + "/", str(media_url or ""))
        parsed = urlparse(target)
        if parsed.username or parsed.password or self._url_origin(target) != self._url_origin(self.base_url):
            raise ValueError("媒体地址不属于 WAHA 服务")
        request = Request(
            target,
            headers={
                "Accept": "image/jpeg, image/png, image/webp, application/octet-stream",
                "X-Api-Key": self.api_key,
            },
        )
        try:
            with self.opener(request, timeout=20) as response:
                return response.status, response.headers.get_content_type(), response.read()
        except HTTPError as error:
            raw = error.read().decode("utf-8", errors="replace")
            raise WahaApiError(error.code, redact_error(raw, self.api_key)) from error
        except (URLError, TimeoutError, OSError) as error:
            raise WahaApiError(0, redact_error(error, self.api_key)) from error

    def send_image(self, session_name, chat_id, filename, mimetype, data_b64, caption=""):
        return self.request_json(
            "/api/sendImage",
            method="POST",
            payload={
                "session": normalize_session_name(session_name),
                "chatId": str(chat_id),
                "file": {
                    "mimetype": str(mimetype),
                    "filename": str(filename),
                    "data": str(data_b64),
                },
                "caption": str(caption or ""),
            },
        )


def session_record(sessions, session_name=DEFAULT_SESSION_NAME):
    name = str(session_name or DEFAULT_SESSION_NAME)
    for session in extract_sessions(sessions):
        if str(session.get("name", "")) == name:
            return session
    return None


def session_state_for(sessions, session_name=DEFAULT_SESSION_NAME):
    session = session_record(sessions, session_name)
    if session:
        status = session.get("status") or session.get("state") or session.get("sessionStatus")
        if isinstance(status, dict):
            status = status.get("status") or status.get("state")
        return str(status or "UNKNOWN").upper()
    return "NOT_CREATED"


def session_state(sessions):
    return session_state_for(sessions, DEFAULT_SESSION_NAME)


class PanelState:
    def __init__(self, database_path, client, api_key, webhook_secret=None, allowed_cidrs=None,
                 sleep_fn=None, uniform_fn=None, commerce_config=None, commerce=None,
                 admin_username="", admin_password="", data_encryption_key=None):
        self.database_path = Path(database_path)
        self.client = client
        self.api_key = api_key
        self.admin_username = str(admin_username or "")
        self.admin_password = str(admin_password or "")
        self.admin_service = AdminService(self.database_path)
        self._admin_db_auth = as_bool(os.environ.get("PANEL_ADMIN_DB_AUTH", "0"))
        self._update_service = UpdateService(
            current_panel=os.environ.get("PANEL_VERSION", "1.0.2"),
            current_waha=os.environ.get("WAHA_IMAGE_TAG", "latest-2026.8.2"),
        )
        self.sleep_fn = sleep_fn or time.sleep
        self.uniform_fn = uniform_fn or random.uniform
        self.clock = time.time
        self.chat = None
        self.translation = None
        self.automation = None
        self.data_cipher = None
        self.csrf_token = None
        self.chat_capability_error = "面板数据加密密钥未配置"
        self._translation_limit_lock = threading.Lock()
        self._translation_request_times = []
        self._translation_slots = threading.BoundedSemaphore(2)
        self._commerce_sender_lock = threading.Lock()
        self.business = BusinessContextService(self.database_path)
        bootstrap_path = os.environ.get("PANEL_ADMIN_BOOTSTRAP_FILE", "").strip()
        if bootstrap_path:
            self.admin_service.bootstrap_from_file(bootstrap_path)
        self.ensure_managed_session(DEFAULT_SESSION_NAME)
        self.commerce_config = self._load_commerce_config(commerce_config or CommerceConfig.from_environment())
        self.commerce = commerce or OrderQueryCoordinator(
            self.database_path,
            self.commerce_config,
            cases=ManualOrderCaseService(
                self.database_path,
                self.commerce_config.verification_secret,
                send_message=lambda target, text: self._send_text(DEFAULT_SESSION_NAME, target, text),
                admin_phone=self.commerce_config.admin_whatsapp,
            ),
        )
        self.webhook_secret = webhook_secret or api_key
        self.allowed_networks = parse_networks(
            allowed_cidrs or "127.0.0.1/32,::1/128,172.16.0.0/12"
        )
        if str(data_encryption_key or "").strip():
            self.enable_chat_services(data_encryption_key)

    def admin_users_payload(self):
        return {"items": self.admin_service.list_users()}

    def authenticate_admin(self, username, password, client_key=""):
        if self._admin_db_auth:
            return self.admin_service.authenticate(username, password, client_key)
        return hmac.compare_digest(str(username or ""), self.admin_username) and hmac.compare_digest(
            str(password or ""), self.admin_password
        )

    def create_admin_user(self, payload):
        if not isinstance(payload, dict):
            raise AdminValidationError("管理员数据格式不正确")
        return self.admin_service.create_user(
            payload.get("username"), payload.get("password"), payload.get("is_active", True)
        )

    def update_admin_user(self, user_id, payload):
        if not isinstance(payload, dict):
            raise AdminValidationError("管理员数据格式不正确")
        return self.admin_service.update_user(
            user_id, payload.get("username"), payload.get("is_active")
        )

    def change_admin_password(self, user_id, payload):
        if not isinstance(payload, dict):
            raise AdminValidationError("管理员数据格式不正确")
        return self.admin_service.change_password(user_id, payload.get("password"))

    def check_updates(self):
        return self._update_service.check()

    def enable_chat_services(self, data_encryption_key):
        """Enable chat references and the isolated translation AI from one stable key."""
        cipher = DataCipher(data_encryption_key)
        secret = hashlib.sha256(
            ("waha-panel-chat-hmac-v1\x00" + str(data_encryption_key)).encode("utf-8")
        ).digest()
        codec = ReferenceCodec(cipher)
        self.data_cipher = cipher
        self.csrf_token = derive_csrf_token(secret)
        self.chat = ChatService(
            self.database_path,
            self.client,
            codec,
            secret,
            logger=lambda level, message: self.log(
                str(level or "INFO").upper(), "chat.service", message, DEFAULT_SESSION_NAME
            ),
            clock=self.clock,
        )
        self.translation = TranslationService(
            self.database_path,
            cipher,
            logger=lambda level, message: self.log(
                str(level or "INFO").upper(), "translation.service", message, DEFAULT_SESSION_NAME
            ),
            clock=self.clock,
        )
        self.automation = ChatAutomationService(
            self.database_path,
            self.chat,
            self.translation,
            logger=lambda level, message: self.log(
                str(level or "INFO").upper(), "chat.automation", message, DEFAULT_SESSION_NAME
            ),
            clock=self.clock,
        )
        self.chat_capability_error = ""
        return self.chat

    def start_background_services(self):
        automation = self.automation
        if automation is not None:
            automation.start()

    def stop_background_services(self):
        automation = self.automation
        if automation is not None:
            automation.stop()

    def begin_translation_request(self):
        now = self.clock()
        with self._translation_limit_lock:
            self._translation_request_times = [
                item for item in self._translation_request_times if now - item < 60
            ]
            if len(self._translation_request_times) >= 30:
                raise TranslationError("AI_RATE_LIMIT", "翻译与建议请求过于频繁，请稍后重试")
        if not self._translation_slots.acquire(blocking=False):
            raise TranslationError("AI_BUSY", "已有两项 AI 请求正在处理，请稍后重试")
        with self._translation_limit_lock:
            self._translation_request_times.append(now)

    def end_translation_request(self):
        self._translation_slots.release()

    def _send_text(self, session_name, chat_id, text):
        """Call both the new and legacy client method shapes safely."""
        name = normalize_session_name(session_name or DEFAULT_SESSION_NAME)
        method = getattr(self.client, "send_text")
        try:
            return method(name, chat_id, text)
        except TypeError:
            if name == DEFAULT_SESSION_NAME:
                return method(chat_id, text)
            raise

    def _client_session_call(self, method_name, session_name, *args):
        name = normalize_session_name(session_name or DEFAULT_SESSION_NAME)
        method = getattr(self.client, method_name)
        try:
            return method(name, *args)
        except TypeError:
            if name == DEFAULT_SESSION_NAME:
                return method(*args)
            raise

    def _handle_commerce(self, request, session_name):
        """Run order tools with an outbound sender bound to the triggering session."""
        cases = getattr(self.commerce, "cases", None)
        sender = getattr(cases, "send_message", None) if cases is not None else None
        if cases is None or not hasattr(cases, "send_message"):
            return self.commerce.handle(request)
        with self._commerce_sender_lock:
            cases.send_message = lambda target, text: self._send_text(session_name, target, text)
            try:
                return self.commerce.handle(request)
            finally:
                cases.send_message = sender

    def ensure_managed_session(self, session_name, display_name=None):
        name = normalize_session_name(session_name)
        fallback = DEFAULT_SESSION_DISPLAY_NAME if name == DEFAULT_SESSION_NAME else name
        display = normalize_display_name(display_name, fallback)
        now = int(time.time())
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "INSERT OR IGNORE INTO managed_sessions(session_name, display_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (name, display, now, now),
            )
            if display_name is not None:
                connection.execute(
                    "UPDATE managed_sessions SET display_name = ?, updated_at = ? WHERE session_name = ?",
                    (display, now, name),
                )
            connection.executemany(
                "INSERT OR IGNORE INTO session_settings(session_name, key, value, updated_at) VALUES (?, ?, ?, ?)",
                [(name, key, value, now) for key, value in DEFAULT_SETTINGS.items()],
            )
            connection.commit()
        finally:
            connection.close()
        return self.session_metadata(name)

    def session_metadata(self, session_name):
        name = normalize_session_name(session_name)
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT session_name, display_name, created_at, updated_at, is_active "
                "FROM managed_sessions WHERE session_name = ?",
                (name,),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return self.ensure_managed_session(name)
        return {
            "session_name": row[0], "display_name": row[1] or row[0],
            "created_at": row[2], "updated_at": row[3], "is_active": bool(row[4]),
        }

    def managed_session_rows(self):
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT session_name, display_name, created_at, updated_at, is_active "
                "FROM managed_sessions ORDER BY CASE WHEN session_name = 'default' THEN 0 ELSE 1 END, id"
            ).fetchall()
        finally:
            connection.close()
        return [
            {"session_name": row[0], "display_name": row[1] or row[0],
             "created_at": row[2], "updated_at": row[3], "is_active": bool(row[4])}
            for row in rows
        ]

    def _load_commerce_config(self, config):
        connection = sqlite3.connect(self.database_path)
        try:
            values = {row[0]: row[1] for row in connection.execute("SELECT key, value FROM commerce_settings")}
        finally:
            connection.close()
        updates = {}
        if values.get("woocommerce_url") is not None:
            updates["woocommerce_url"] = values["woocommerce_url"]
        if values.get("admin_whatsapp") is not None:
            updates["admin_whatsapp"] = values["admin_whatsapp"]
        for key in ("wordpress_host", "wordpress_database", "wordpress_user", "wordpress_prefix"):
            if values.get(key) is not None:
                updates[key] = values[key]
        if values.get("wordpress_port"):
            try:
                updates["wordpress_port"] = max(1, min(65535, int(values["wordpress_port"])))
            except ValueError:
                pass
        if values.get("paypal_base_url") in {"https://api-m.sandbox.paypal.com", "https://api-m.paypal.com"}:
            updates["paypal_base_url"] = values["paypal_base_url"]
        if values.get("timeout"):
            try:
                updates["timeout"] = max(0.1, min(60.0, float(values["timeout"])))
            except ValueError:
                pass
        if values.get("enabled") is not None:
            updates["enabled"] = as_bool(values["enabled"])
        return replace(config, **updates) if updates else config

    def _settings(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        connection = sqlite3.connect(self.database_path)
        try:
            values = {
                row[0]: row[1]
                for row in connection.execute(
                    "SELECT key, value FROM session_settings WHERE session_name = ?", (name,)
                )
            }
        finally:
            connection.close()
        if not values:
            self.ensure_managed_session(name)
            connection = sqlite3.connect(self.database_path)
            try:
                values = {
                    row[0]: row[1]
                    for row in connection.execute(
                        "SELECT key, value FROM session_settings WHERE session_name = ?", (name,)
                    )
                }
            finally:
                connection.close()
        return values

    def settings_payload(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        values = self._settings(name)
        return {
            "session_name": name,
            "auto_reply_enabled": as_bool(values.get("auto_reply_enabled", "0")),
            "auto_reply_all_day": as_bool(values.get("auto_reply_all_day", "0")),
            "weekly_reply_windows": normalize_windows(values.get("weekly_reply_windows", "{}")),
            "default_reply_text": values.get("default_reply_text", "").strip() or DEFAULT_REPLY_TEXT,
            "persona": values.get("persona", ""),
            "system_prompt": values.get("system_prompt", ""),
            "ai_base_url": values.get("ai_base_url", ""),
            "ai_model": values.get("ai_model", ""),
            "ai_api_key_configured": bool(values.get("ai_api_key", "")),
            "theme": normalize_theme(values.get("theme", "daylight")),
            "timezone": "Asia/Shanghai",
        }

    def save_settings(self, payload, session_name=DEFAULT_SESSION_NAME):
        if not isinstance(payload, dict):
            raise ValueError("设置格式不正确")
        name = normalize_session_name(session_name)
        self.ensure_managed_session(name)
        updates = {}
        if "auto_reply_enabled" in payload:
            updates["auto_reply_enabled"] = "1" if as_bool(payload["auto_reply_enabled"]) else "0"
        if "auto_reply_all_day" in payload:
            updates["auto_reply_all_day"] = "1" if as_bool(payload["auto_reply_all_day"]) else "0"
        if "weekly_reply_windows" in payload:
            updates["weekly_reply_windows"] = json.dumps(
                normalize_windows(payload["weekly_reply_windows"]), ensure_ascii=False
            )
        for key in ("default_reply_text", "persona", "system_prompt", "ai_base_url", "ai_model"):
            if key in payload:
                updates[key] = str(payload[key] or "").strip()
        if "theme" in payload:
            updates["theme"] = normalize_theme(payload["theme"])
        if payload.get("clear_ai_api_key"):
            updates["ai_api_key"] = ""
        elif str(payload.get("ai_api_key", "")):
            updates["ai_api_key"] = str(payload["ai_api_key"])
        if not updates:
            return self.settings_payload(name)
        now = int(time.time())
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executemany(
                "INSERT INTO session_settings(session_name, key, value, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(session_name, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                [(name, key, value, now) for key, value in updates.items()],
            )
            # Keep the legacy table synchronized for default-session integrations
            # that still inspect it directly; it is not used as the runtime source.
            if name == DEFAULT_SESSION_NAME:
                connection.executemany(
                    "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                    [(key, value, now) for key, value in updates.items()],
                )
            connection.commit()
        finally:
            connection.close()
        self.log("INFO", "settings.save", "自动回复设置已保存", name)
        return self.settings_payload(name)

    def _commerce_secret_path(self):
        configured = os.environ.get("COMMERCE_SECRET_FILE", "").strip()
        return Path(configured) if configured else self.database_path.with_name("commerce-secrets.json")

    def commerce_config_payload(self):
        config = self.commerce_config
        public = config.public_payload()
        public["admin_whatsapp"] = config.admin_whatsapp
        public.update({
            "configured": public["woocommerce_configured"] or public["paypal_configured"],
            "paypal_environment": "sandbox" if "sandbox" in config.paypal_base_url else "live",
            "request_timeout_seconds": config.timeout,
            "wordpress_host": config.wordpress_host,
            "wordpress_port": config.wordpress_port,
            "wordpress_database": config.wordpress_database,
            "wordpress_user": config.wordpress_user,
            "wordpress_prefix": config.wordpress_prefix,
            "credentials": {
                "woocommerce_consumer_key": bool(config.woocommerce_consumer_key),
                "woocommerce_consumer_secret": bool(config.woocommerce_consumer_secret),
                "paypal_client_id": bool(config.paypal_client_id),
                "paypal_client_secret": bool(config.paypal_client_secret),
                "wordpress_password": bool(config.wordpress_password),
            },
        })
        return public

    def save_commerce_config(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("订单配置格式不正确")
        current = self.commerce_config
        updates = {}
        for key in ("woocommerce_consumer_key", "woocommerce_consumer_secret", "paypal_client_id", "paypal_client_secret", "wordpress_password"):
            if str(payload.get(key, "")):
                updates[key] = str(payload[key]).strip()
        if payload.get("woocommerce_url") is not None:
            updates["woocommerce_url"] = str(payload.get("woocommerce_url") or "").strip().rstrip("/")
        if payload.get("admin_whatsapp") is not None:
            updates["admin_whatsapp"] = str(payload.get("admin_whatsapp") or "").strip()
        for key in ("wordpress_host", "wordpress_database", "wordpress_user"):
            if payload.get(key) is not None:
                updates[key] = str(payload.get(key) or "").strip()
        if payload.get("wordpress_port") is not None:
            try:
                updates["wordpress_port"] = max(1, min(65535, int(payload["wordpress_port"])))
            except (TypeError, ValueError) as error:
                raise ValueError("WordPress 数据库端口必须是整数") from error
        if payload.get("wordpress_prefix") is not None:
            prefix = str(payload.get("wordpress_prefix") or "wp_").strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", prefix):
                raise ValueError("WordPress 表前缀格式不正确")
            updates["wordpress_prefix"] = prefix
        if payload.get("request_timeout_seconds") is not None:
            try:
                updates["timeout"] = max(0.1, min(60.0, float(payload["request_timeout_seconds"])))
            except (TypeError, ValueError) as error:
                raise ValueError("请求超时必须是数字") from error
        if payload.get("paypal_environment") in {"sandbox", "live"}:
            updates["paypal_base_url"] = (
                "https://api-m.sandbox.paypal.com"
                if payload["paypal_environment"] == "sandbox"
                else "https://api-m.paypal.com"
            )
        if payload.get("enabled") is not None:
            updates["enabled"] = as_bool(payload["enabled"])
        if not updates:
            return self.commerce_config_payload()
        new_config = replace(current, **updates)
        now = int(time.time())
        connection = sqlite3.connect(self.database_path)
        try:
            connection.executemany(
                "INSERT INTO commerce_settings(key, value, is_active, updated_at) VALUES (?, ?, 1, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, is_active=1, updated_at=excluded.updated_at",
                [(key, str(getattr(new_config, key)), now) for key in (
                    "woocommerce_url", "admin_whatsapp", "paypal_base_url", "timeout", "enabled",
                    "wordpress_host", "wordpress_port", "wordpress_database", "wordpress_user", "wordpress_prefix",
                )],
            )
            connection.commit()
        finally:
            connection.close()
        secret_path = self._commerce_secret_path()
        secret_path.parent.mkdir(parents=True, exist_ok=True)
        secret_data = {}
        if secret_path.exists():
            try:
                loaded = json.loads(secret_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    secret_data.update({str(k): str(v) for k, v in loaded.items()})
            except (OSError, ValueError):
                pass
        for field, env_name in {
            "woocommerce_consumer_key": "WOOCOMMERCE_CONSUMER_KEY",
            "woocommerce_consumer_secret": "WOOCOMMERCE_CONSUMER_SECRET",
            "paypal_client_id": "PAYPAL_CLIENT_ID",
            "paypal_client_secret": "PAYPAL_CLIENT_SECRET",
            "wordpress_password": "WORDPRESS_DB_PASSWORD",
        }.items():
            if field in updates:
                secret_data[env_name] = updates[field]
        secret_path.write_text(json.dumps(secret_data, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(secret_path, 0o600)
        except OSError:
            pass
        self.commerce_config = new_config
        self.commerce = OrderQueryCoordinator(
            self.database_path,
            new_config,
            cases=ManualOrderCaseService(
                self.database_path,
                new_config.verification_secret,
                send_message=lambda target, text: self._send_text(DEFAULT_SESSION_NAME, target, text),
                admin_phone=new_config.admin_whatsapp,
            ),
        )
        self.log("INFO", "commerce.config", "订单查询配置已更新（凭据已隐藏）")
        return self.commerce_config_payload()

    def commerce_status(self):
        connection = sqlite3.connect(self.database_path)
        try:
            open_cases = connection.execute("SELECT COUNT(*) FROM manual_order_cases WHERE status = 'pending'").fetchone()[0]
            locked = connection.execute("SELECT COUNT(*) FROM order_verification_states WHERE email_locked = 1").fetchone()[0]
            today = int(time.time()) - (int(time.time()) % 86400)
            queries = connection.execute("SELECT COUNT(*) FROM order_query_audit WHERE created_at >= ?", (today,)).fetchone()[0]
            last = connection.execute("SELECT created_at FROM order_query_audit ORDER BY id DESC LIMIT 1").fetchone()
        finally:
            connection.close()
        return {"connected": bool(self.commerce_config.woocommerce_url), "ok": True,
                "stats": {"open_tickets": open_cases, "locked_emails": locked,
                           "today_queries": queries, "last_query_at": last[0] if last else None}}

    def commerce_cases(self, status="open"):
        connection = sqlite3.connect(self.database_path)
        try:
            query = "SELECT id, chat_id, safe_clue, failure_code, status, created_at, answered_at, closed_at FROM manual_order_cases"
            params = ()
            if status == "open":
                query += " WHERE status = 'pending'"
            elif status == "resolved":
                query += " WHERE status IN ('answered', 'closed')"
            rows = connection.execute(query + " ORDER BY id DESC LIMIT 100", params).fetchall()
        finally:
            connection.close()
        return {"items": [{"id": row[0], "customer_phone": "已隐藏", "safe_clue": row[2], "reason": row[3], "status": row[4], "created_at": row[5], "updated_at": row[6] or row[7] or row[5]} for row in rows]}

    def commerce_case(self, case_id):
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute("SELECT id, chat_id, query_type, safe_clue, failure_code, status, admin_answer, created_at, answered_at, closed_at FROM manual_order_cases WHERE id = ?", (int(case_id),)).fetchone()
        finally:
            connection.close()
        if not row:
            raise KeyError("工单不存在")
        return {"ticket": {"id": row[0], "customer_phone": "已隐藏", "query_type": row[2], "safe_clue": row[3], "reason": row[4], "status": row[5], "admin_answer": row[6], "created_at": row[7], "answered_at": row[8], "closed_at": row[9]}}

    def commerce_recheck(self, case_id):
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute("SELECT id, chat_id, query_type, safe_clue, status FROM manual_order_cases WHERE id = ?", (int(case_id),)).fetchone()
        finally:
            connection.close()
        if not row:
            raise KeyError("工单不存在")
        clue = row[3]
        request = {"chat_id": row[1], "sender_phone": row[1].split("@", 1)[0], "query_type": row[2]}
        if str(clue).isdigit():
            request["order_id"] = str(clue)
        result = self.commerce.handle(request)
        if result.get("action") == "reply":
            return {"ticket": self.commerce.cases.save_admin_result(row[0], result["text"], close=False), "order": result.get("order"), "payment": result.get("payment")}
        return {"ticket": self.commerce_case(row[0])["ticket"], "result": result}

    def commerce_locks(self):
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute("SELECT id, email_failure_count, last_attempt_at FROM order_verification_states WHERE email_locked = 1 ORDER BY last_attempt_at DESC").fetchall()
        finally:
            connection.close()
        return {"items": [{"id": row[0], "customer_phone": "已隐藏", "email": "已隐藏", "failed_attempts": row[1], "locked_at": row[2]} for row in rows]}

    def commerce_audit(self, limit=50):
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute("SELECT created_at, result_code, source, safe_summary FROM order_query_audit ORDER BY id DESC LIMIT ?", (max(1, min(200, int(limit))),)).fetchall()
        finally:
            connection.close()
        return {"items": [{"created_at": row[0], "result": row[1], "actor": row[2], "action": row[3]} for row in rows]}

    def commerce_test_connection(self):
        results = {}
        for name, client, action in (
            ("woocommerce", self.commerce.woocommerce, lambda: client.test_connection()),
            ("paypal", self.commerce.paypal, lambda: client.test_connection()),
            ("wordpress", self.commerce.wordpress, lambda: client.detect_schema()),
        ):
            try:
                action()
                results[name] = {"ok": True}
            except Exception as error:
                results[name] = {"ok": False, "message": redact_sensitive(error)}
        ok = any(item["ok"] for item in results.values())
        return {"ok": ok, "connected": ok, "message": "连接测试完成", "results": results}

    def knowledge_list(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT id, file_name, length(content), created_at FROM knowledge_base "
                "WHERE session_name = ? ORDER BY id DESC", (name,)
            ).fetchall()
        finally:
            connection.close()
        return [
            {"id": row[0], "file_name": row[1], "characters": row[2], "created_at": row[3]}
            for row in rows
        ]

    def knowledge_item(self, item_id, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT id, file_name, content, created_at FROM knowledge_base "
                "WHERE id = ? AND session_name = ?", (item_id, name),
            ).fetchone()
        finally:
            connection.close()
        if not row:
            raise KeyError("资料不存在")
        return {"id": row[0], "file_name": row[1], "content": row[2], "created_at": row[3]}

    def add_knowledge(self, file_name, content, session_name=DEFAULT_SESSION_NAME):
        name_session = normalize_session_name(session_name)
        self.ensure_managed_session(name_session)
        name = str(file_name or "粘贴文字").strip()[:255]
        body = str(content or "")
        if not name or not body.strip():
            raise ValueError("资料名称和内容不能为空")
        if Path(name).suffix.lower() not in {"", ".txt", ".md"} and name != "粘贴文字":
            raise ValueError("知识库仅支持 .txt 或 .md 文件")
        if len(body.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("单份资料不能超过 2 MB")
        connection = sqlite3.connect(self.database_path)
        try:
            cursor = connection.execute(
                "INSERT INTO knowledge_base(session_name, file_name, content, created_at) VALUES (?, ?, ?, ?)",
                (name_session, name, body, int(time.time())),
            )
            connection.commit()
            item_id = cursor.lastrowid
        finally:
            connection.close()
        self.log("INFO", "knowledge.add", f"已保存知识库资料：{name}", name_session)
        return self.knowledge_item(item_id, name_session)

    def delete_knowledge(self, item_id, session_name=DEFAULT_SESSION_NAME):
        name_session = normalize_session_name(session_name)
        connection = sqlite3.connect(self.database_path)
        try:
            cursor = connection.execute(
                "DELETE FROM knowledge_base WHERE id = ? AND session_name = ?", (item_id, name_session)
            )
            connection.commit()
        finally:
            connection.close()
        if not cursor.rowcount:
            raise KeyError("资料不存在")
        self.log("INFO", "knowledge.delete", f"已删除知识库资料 #{item_id}", name_session)

    def _within_schedule(self, windows, now=None):
        current = now.astimezone(SHANGHAI_TZ) if now else datetime.now(SHANGHAI_TZ)
        day_index = current.weekday()
        day = WEEKDAYS[day_index]
        previous_day = WEEKDAYS[(day_index - 1) % len(WEEKDAYS)]
        current_time = current.strftime("%H:%M")
        for item in windows.get(day, []):
            if item["start"] < item["end"] and item["start"] <= current_time < item["end"]:
                return True
            if item["start"] > item["end"] and current_time >= item["start"]:
                return True
        return any(
            item["start"] > item["end"] and current_time < item["end"]
            for item in windows.get(previous_day, [])
        )

    def _knowledge_context(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT file_name, content FROM knowledge_base WHERE session_name = ? ORDER BY id", (name,)
            ).fetchall()
        finally:
            connection.close()
        return "\n\n".join(f"[{name}]\n{content}" for name, content in rows)

    def _conversation_context(self, chat_id, exclude_message_id=None, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT direction, message_id, content FROM conversation_messages "
                "WHERE session_name = ? AND chat_id = ? "
                "AND (? IS NULL OR message_id IS NULL OR message_id != ?) "
                "ORDER BY id DESC LIMIT ?",
                (name, chat_id, exclude_message_id, exclude_message_id, CONTEXT_MESSAGE_LIMIT),
            ).fetchall()
        finally:
            connection.close()
        lines = []
        total = 0
        for direction, _message_id, content in reversed(rows):
            label = "客户" if direction == "inbound" else "客服"
            line = f"{label}：{content.strip()}"
            if total + len(line) > CONTEXT_CHARACTER_LIMIT:
                remaining = CONTEXT_CHARACTER_LIMIT - total
                if remaining > 20:
                    lines.append(line[:remaining] + "…")
                break
            lines.append(line)
            total += len(line)
        return "\n".join(lines)

    def _record_conversation_message(self, chat_id, direction, message_id, content, created_at=None,
                                     session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        text = str(content or "").strip()
        if not text:
            return
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO conversation_messages(session_name, chat_id, direction, message_id, content, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (name, chat_id, direction, message_id, text, int(created_at or time.time())),
            )
            connection.commit()
        finally:
            connection.close()

    def _ai_reply(self, settings, incoming_text, customer_id=None, conversation_context="", include_tools=False,
                  session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        base_url = settings.get("ai_base_url", "").strip()
        model = settings.get("ai_model", "").strip()
        api_key = settings.get("ai_api_key", "")
        if not (base_url and model and api_key):
            return settings.get("default_reply_text", "").strip() or DEFAULT_REPLY_TEXT, "fixed"
        endpoint = base_url if base_url.rstrip("/").endswith("/chat/completions") else base_url.rstrip("/") + "/chat/completions"
        knowledge_context = self._knowledge_context(name)
        try:
            business_context = self.business.get_business_context(customer_id=customer_id)
        except KeyError:
            business_context = None
        business_rules = (
            "IMPORTANT BUSINESS DATA RULES\n"
            "The BUSINESS CONTEXT supplied by the database is the current source of truth.\n"
            "Never use prices, coupons, package contents, payment methods, shipping information "
            "or website information remembered from previous conversations when they conflict with BUSINESS CONTEXT.\n"
            "Never invent a missing business value. If a required value is unavailable, tell the customer that you need to check the information.\n"
            "A historical customer quote applies only to that specific customer and must never be presented as the normal public product price."
        )
        system_parts = [part for part in (
            settings.get("system_prompt", "").strip(),
            f"人设：{settings.get('persona', '').strip()}" if settings.get("persona", "").strip() else "",
            f"知识库：\n{knowledge_context}" if knowledge_context else "",
            (
                "当前对话上下文（按时间从早到晚，仅作参考；请优先回答当前客户消息）：\n"
                + conversation_context
            ) if conversation_context else "",
            (
                f"BUSINESS CONTEXT (database source of truth):\n"
                f"{json.dumps(business_context, ensure_ascii=False)}"
            ) if business_context else "",
            business_rules if business_context else "",
        ) if part]
        request_payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "\n\n".join(system_parts)},
                {"role": "user", "content": incoming_text},
            ],
        }
        if include_tools and self.commerce_config.enabled:
            request_payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": "query_order",
                    "description": "查询当前客户自己的 WooCommerce 订单或 PayPal 支付状态",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "query_type": {"type": "string", "enum": ["order_status", "payment_status"]},
                            "order_id": {"type": "string"},
                            "email": {"type": "string"},
                            "selected_order_id": {"type": "string"},
                        },
                        "required": ["query_type"],
                    },
                },
            }]
        payload = json.dumps(request_payload).encode("utf-8")
        request = Request(endpoint, data=payload, headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }, method="POST")
        try:
            with urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
            message_result = result["choices"][0]["message"]
            tool_calls = message_result.get("tool_calls") or []
            if include_tools and tool_calls:
                for tool_call in tool_calls:
                    function = tool_call.get("function") if isinstance(tool_call, dict) else None
                    if not isinstance(function, dict) or function.get("name") != "query_order":
                        continue
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(arguments, dict) or set(arguments) - {"query_type", "order_id", "email", "selected_order_id"}:
                        continue
                    if arguments.get("query_type") not in {"order_status", "payment_status"}:
                        continue
                    return json.dumps(arguments, ensure_ascii=False), "order_tool"
            reply = str(message_result.get("content") or "").strip()
            return reply or DEFAULT_REPLY_TEXT, "ai"
        except Exception as error:
            self.log("ERROR", "ai.reply", redact_error(error, api_key), name)
            return settings.get("default_reply_text", "").strip() or DEFAULT_REPLY_TEXT, "fixed_fallback"

    def _record_incoming_activity(self, chat_id, received_at, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute(
                "SELECT last_incoming_at FROM conversation_activity WHERE session_name = ? AND chat_id = ?",
                (name, chat_id),
            ).fetchone()
            is_hot = bool(row and 0 <= received_at - row[0] <= HOT_CONVERSATION_SECONDS)
            connection.execute(
                "INSERT INTO conversation_activity(session_name, chat_id, last_incoming_at) VALUES (?, ?, ?) "
                "ON CONFLICT(session_name, chat_id) DO UPDATE SET last_incoming_at=excluded.last_incoming_at",
                (name, chat_id, received_at),
            )
            connection.commit()
            return is_hot
        finally:
            connection.close()

    def _reply_delays(self, _reply, is_hot):
        pre_delay = self.uniform_fn(1.0, 5.0) if is_hot else self.uniform_fn(20.0, 100.0)
        typing_delay = self.uniform_fn(MIN_TYPING_DELAY_SECONDS, MAX_TYPING_DELAY_SECONDS)
        return pre_delay, typing_delay

    def _wait_before_reply(self, reply, is_hot):
        pre_delay, typing_delay = self._reply_delays(reply, is_hot)
        self.sleep_fn(pre_delay)
        self.sleep_fn(typing_delay)
        return pre_delay, typing_delay

    def _webhook_source_valid(self, body, headers, remote_addr):
        official_hmac = headers.get("X-Webhook-Hmac")
        if official_hmac:
            algorithm = str(headers.get("X-Webhook-Hmac-Algorithm") or "sha512").strip().lower()
            if algorithm != "sha512":
                return False
            supplied = official_hmac.strip().removeprefix("sha512=")
            expected = hmac.new(self.webhook_secret.encode(), body, hashlib.sha512).hexdigest()
            return hmac.compare_digest(supplied, expected)
        signature = headers.get("X-Webhook-Signature") or headers.get("X-WAHA-Signature")
        if signature:
            supplied = signature.strip().removeprefix("sha256=")
            expected = hmac.new(self.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(supplied, expected)
        forwarded = headers.get("X-Real-IP") or remote_addr
        try:
            address = ip_address(forwarded.strip())
        except ValueError:
            return False
        return any(address in network for network in self.allowed_networks)

    @staticmethod
    def _event_session_name(event, message, metadata):
        candidates = []
        if isinstance(event, dict):
            candidates.extend([
                event.get("session"),
                event.get("sessionName"),
                event.get("metadata", {}).get("session") if isinstance(event.get("metadata"), dict) else None,
            ])
        if isinstance(message, dict):
            candidates.extend([message.get("session"), message.get("sessionName")])
        if isinstance(metadata, dict):
            candidates.extend([metadata.get("session"), metadata.get("sessionName")])
        for candidate in candidates:
            if candidate:
                try:
                    return normalize_session_name(candidate)
                except ValueError:
                    break
        return DEFAULT_SESSION_NAME

    def _publish_chat_event(self, session_name, event_name, message, chat_id, message_id,
                            from_me, is_media, message_type):
        if self.chat is None:
            return None
        safe = {
            "type": "message" if str(event_name).startswith("message") else "session_status",
            "event": str(event_name)[:80],
            "session": session_name,
            "body": str(message.get("body") or message.get("text") or "")[:65535],
            "caption": str(message.get("caption") or "")[:4096],
            "message_type": str(message_type or "")[:40],
            "from_me": bool(from_me),
            "has_media": bool(is_media),
            "timestamp": 0,
        }
        try:
            safe["timestamp"] = int(message.get("timestamp") or message.get("t") or time.time())
        except (TypeError, ValueError):
            safe["timestamp"] = int(time.time())
        ack = message.get("ack")
        if isinstance(ack, (str, int, float)) and not isinstance(ack, bool):
            safe["ack"] = ack
        if chat_id:
            safe["chat_ref"] = self.chat._encode_chat(session_name, chat_id)
        if chat_id and message_id:
            safe["message_ref"] = self.chat._encode_message(session_name, chat_id, message_id)
        return self.chat.broker.publish(session_name, safe)

    def handle_webhook(self, body, headers=None, remote_addr="127.0.0.1", now=None, dispatch=True):
        headers = headers or {}
        if not self._webhook_source_valid(body, headers, remote_addr):
            raise PermissionError("webhook 来源校验失败")
        event = json.loads(body.decode("utf-8"))
        raw = event.get("payload") if isinstance(event, dict) else None
        message = raw if isinstance(raw, dict) else event
        metadata = message.get("_data") if isinstance(message, dict) and isinstance(message.get("_data"), dict) else {}
        session_name = self._event_session_name(event, message, metadata)
        self.ensure_managed_session(session_name)
        event_name = str(event.get("event", event.get("type", "message"))).lower()
        message_type = str(message.get("type", "")).lower()
        from_me = as_bool(message.get("fromMe") or metadata.get("fromMe"))
        chat_id = str(
            (message.get("to") if from_me else message.get("from"))
            or message.get("chatId") or message.get("from") or message.get("to") or ""
        )
        raw_message_id = message.get("id") or message.get("messageId") or metadata.get("id") or ""
        if isinstance(raw_message_id, dict):
            raw_message_id = raw_message_id.get("_serialized") or raw_message_id.get("id") or ""
        message_id = str(raw_message_id)
        body_text = message.get("body") or message.get("text") or ""
        is_group = as_bool(message.get("isGroup")) or "@g.us" in chat_id
        is_status = "status" in event_name or "@broadcast" in chat_id or message_type == "status"
        is_media = as_bool(message.get("hasMedia")) or bool(message.get("media")) or message_type in {"image", "video", "audio", "document", "sticker", "ptt", "location", "contact"}
        self._publish_chat_event(
            session_name, event_name, message, chat_id, message_id, from_me, is_media, message_type
        )
        if not event_name.startswith("message") or from_me or is_group or is_status or is_media or not isinstance(body_text, str) or not body_text.strip() or not chat_id or not message_id:
            return {"action": "ignored", "reason": "消息不符合自动回复范围", "session": session_name}
        connection = sqlite3.connect(self.database_path)
        try:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO message_dedupe(session_name, message_id, received_at) VALUES (?, ?, ?)",
                (session_name, message_id, int(time.time())),
            )
            connection.commit()
        finally:
            connection.close()
        if cursor.rowcount == 0:
            return {"action": "ignored", "reason": "重复消息", "session": session_name}
        current = now.astimezone(SHANGHAI_TZ) if now else datetime.now(SHANGHAI_TZ)
        received_at = int(current.timestamp())
        customer_text = body_text.strip()
        is_hot = self._record_incoming_activity(chat_id, received_at, session_name)
        self._record_conversation_message(chat_id, "inbound", message_id, customer_text, received_at, session_name)
        if self.chat is not None and self.chat.is_human_takeover(session_name, chat_id):
            self.log("INFO", "auto_reply.ignored", "人工接管中", session_name)
            return {"action": "ignored", "reason": "人工接管中", "session": session_name}
        settings = self._settings(session_name)
        all_day = as_bool(settings.get("auto_reply_all_day", "0"))
        if not all_day and not as_bool(settings.get("auto_reply_enabled", "0")):
            self.log("INFO", "auto_reply.ignored", "自动回复已关闭", session_name)
            return {"action": "ignored", "reason": "自动回复已关闭", "session": session_name}
        if not all_day and not self._within_schedule(normalize_windows(settings.get("weekly_reply_windows", "{}")), now):
            self.log("INFO", "auto_reply.ignored", "当前不在回复时间段", session_name)
            return {"action": "ignored", "reason": "当前不在回复时间段", "session": session_name}
        context = self._conversation_context(chat_id, exclude_message_id=message_id, session_name=session_name)
        reply, mode = self._ai_reply(
            settings,
            customer_text,
            customer_id=chat_id,
            conversation_context=context,
            include_tools=True,
            session_name=session_name,
        )
        if mode == "order_tool" and self.commerce_config.enabled:
            try:
                tool_request = json.loads(reply)
                tool_request.update({"chat_id": chat_id, "sender_phone": chat_id.split("@", 1)[0]})
                commerce_result = self._handle_commerce(tool_request, session_name)
                if commerce_result.get("action") == "reply":
                    reply = commerce_result["text"]
                    mode = "order"
                elif commerce_result.get("action") == "verification_required":
                    reply = commerce_result["message"]
                    mode = "order_verification"
                elif commerce_result.get("action") == "select_order":
                    summaries = commerce_result.get("orders", [])
                    reply = "请回复您要查询的订单号：\n" + "\n".join(
                        f"订单 {item.get('order_id')}，状态 {item.get('status')}，金额 {item.get('total')} {item.get('currency')}"
                        for item in summaries if isinstance(item, dict)
                    )
                    mode = "order_selection"
                else:
                    reply = "您的订单正在人工核对，请稍候。"
                    mode = "order_manual"
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self.log("ERROR", "commerce.tool", redact_sensitive(error), session_name)
                reply = settings.get("default_reply_text", "").strip() or DEFAULT_REPLY_TEXT
                mode = "fixed_fallback"
        if dispatch:
            self._wait_before_reply(reply, is_hot)
            self._send_text(session_name, chat_id, reply)
            self._record_conversation_message(chat_id, "outbound", None, reply, session_name=session_name)
        conversation_type = "热对话" if is_hot else "冷对话"
        self.log("INFO", "auto_reply.sent", f"已处理私聊文字消息（{mode}，{conversation_type}）", session_name)
        return {"action": "replied", "mode": mode, "chat_id": chat_id, "text": reply, "session": session_name}

    def log(self, level, event, message, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name or DEFAULT_SESSION_NAME)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute(
                "INSERT INTO system_logs(session_name, level, event, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (name, level, event, redact_error(message, self.api_key), int(time.time())),
            )
            connection.commit()
        finally:
            connection.close()

    def recent_logs(self, limit=20, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name or DEFAULT_SESSION_NAME)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT level, event, message, created_at FROM system_logs "
                "WHERE session_name = ? ORDER BY id DESC LIMIT ?",
                (name, limit),
            ).fetchall()
        finally:
            connection.close()
        return [dict(row) for row in rows]

    def _session_detail(self, session_name, remote_sessions):
        name = normalize_session_name(session_name)
        metadata = self.session_metadata(name)
        remote = session_record(remote_sessions, name)
        state = session_state_for(remote_sessions, name)
        settings = self.settings_payload(name)
        return {
            "name": name,
            "session_name": name,
            "display_name": metadata["display_name"],
            "state": state,
            "connected": classify_session_state(state),
            "exists": remote is not None,
            "is_active": metadata["is_active"],
            "created_at": metadata["created_at"],
            "updated_at": metadata["updated_at"],
            "auto_reply": {
                "enabled": settings["auto_reply_enabled"],
                "all_day": settings["auto_reply_all_day"],
                "ai_configured": settings["ai_api_key_configured"] and bool(
                    settings["ai_base_url"] and settings["ai_model"]
                ),
            },
        }

    def sessions_payload(self, remote_sessions=None):
        remote = extract_sessions(remote_sessions) if remote_sessions is not None else extract_sessions(self.client.get_sessions())
        managed = self.managed_session_rows()
        names = [item["session_name"] for item in managed]
        for item in remote:
            raw_name = item.get("name")
            try:
                name = normalize_session_name(raw_name)
            except ValueError:
                continue
            if name not in names:
                self.ensure_managed_session(name)
                names.append(name)
        if not names:
            self.ensure_managed_session(DEFAULT_SESSION_NAME)
            names = [DEFAULT_SESSION_NAME]
        return [self._session_detail(name, remote) for name in names]

    def session_detail(self, session_name, remote_sessions=None):
        name = normalize_session_name(session_name)
        remote = extract_sessions(remote_sessions) if remote_sessions is not None else extract_sessions(self.client.get_sessions())
        # A managed record may exist before WAHA has finished creating it.
        return self._session_detail(name, remote)

    def status_payload(self, selected_session=DEFAULT_SESSION_NAME):
        running = False
        version = None
        errors = []
        try:
            self.client.get_health()
            running = True
        except WahaApiError as error:
            errors.append(f"WAHA 健康检查失败（HTTP {error.status or '网络'}）：{redact_error(error, self.api_key)}")
        try:
            version_result = self.client.get_version()
            version = version_result.get("version") if isinstance(version_result, dict) else None
            running = True
        except WahaApiError as error:
            errors.append(f"WAHA 版本接口失败（HTTP {error.status or '网络'}）：{redact_error(error, self.api_key)}")

        remote_sessions = []
        try:
            remote_sessions = extract_sessions(self.client.get_sessions())
        except WahaApiError as error:
            errors.append(f"会话状态读取失败（HTTP {error.status or '网络'}）：{redact_error(error, self.api_key)}")
        sessions = self.sessions_payload(remote_sessions)
        try:
            selected = normalize_session_name(selected_session or DEFAULT_SESSION_NAME)
        except ValueError:
            selected = DEFAULT_SESSION_NAME
        if selected not in {item["name"] for item in sessions}:
            selected = sessions[0]["name"] if sessions else DEFAULT_SESSION_NAME
        selected_item = next((item for item in sessions if item["name"] == selected), None)
        if selected_item is None:
            selected_item = self._session_detail(selected, remote_sessions)
        settings = self.settings_payload(selected)
        database_ok = False
        try:
            connection = sqlite3.connect(self.database_path)
            connection.execute("SELECT 1").fetchone()
            connection.close()
            database_ok = True
        except sqlite3.Error as error:
            errors.append(f"数据库检查失败：{redact_error(error)}")
        return {
            "waha": {"running": running, "version": version},
            "sessions": sessions,
            "selected_session": selected,
            "whatsapp": {
                "session": selected,
                "display_name": selected_item["display_name"],
                "state": selected_item["state"],
                "connected": selected_item["connected"],
                "exists": selected_item["exists"],
            },
            "database": {"ok": database_ok},
            "auto_reply": {
                "enabled": settings["auto_reply_enabled"],
                "all_day": settings["auto_reply_all_day"],
                "ai_configured": settings["ai_api_key_configured"] and bool(
                    settings["ai_base_url"] and settings["ai_model"]
                ),
                "theme": settings["theme"],
                "timezone": settings["timezone"],
            },
            "errors": errors,
            "logs": self.recent_logs(session_name=selected),
        }

    def create_session(self, session_name, display_name=None, start=False):
        name = normalize_session_name(session_name)
        remote_sessions = extract_sessions(self.client.get_sessions())
        if session_record(remote_sessions, name) is not None:
            raise ValueError("该技术会话已经存在")
        config = self._session_config()
        self._create_client_session(name, config, bool(start))
        self.ensure_managed_session(name, display_name)
        self.log("INFO", "session.create", "已创建会话", name)
        return self.session_detail(name)

    def _session_config(self):
        """Build the safe default webhook configuration for new WAHA sessions."""
        webhook_url = os.environ.get(
            "WAHA_WEBHOOK_URL", "http://waha-panel:3001/api/webhook"
        ).strip()
        webhook = {
            "url": webhook_url,
            "events": ["session.status", "message", "message.any"],
        }
        webhook_secret = str(self.webhook_secret or "").strip()
        if webhook_secret:
            webhook["hmac"] = {"key": webhook_secret}
        return {"webhooks": [webhook]}

    def _create_client_session(self, session_name, config, start=False):
        method = getattr(self.client, "create_session")
        name = normalize_session_name(session_name)
        try:
            return method(name, config, start)
        except TypeError:
            try:
                return method(name, config)
            except TypeError:
                if name == DEFAULT_SESSION_NAME:
                    return method()
                raise

    def update_session_display_name(self, session_name, display_name):
        name = normalize_session_name(session_name)
        display = normalize_display_name(display_name, name)
        self.ensure_managed_session(name, display)
        self.log("INFO", "session.rename", "已更新会话显示名称", name)
        return self.session_detail(name)

    def start_session(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        self.ensure_managed_session(name)
        self._client_session_call("start_session", name)
        self.log("INFO", "session.start", "已请求启动会话", name)
        return self.session_detail(name)

    def stop_session(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        self.ensure_managed_session(name)
        self._client_session_call("stop_session", name)
        self.log("INFO", "session.stop", "已请求停止会话", name)
        return self.session_detail(name)

    def restart_session(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        self.ensure_managed_session(name)
        self._client_session_call("restart_session", name)
        self.log("INFO", "session.restart", "已请求重启会话", name)
        return self.session_detail(name)

    def delete_session(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        managed = self.managed_session_rows()
        if len(managed) <= 1:
            raise ValueError("至少保留一个会话，无法删除最后一个会话")
        self._client_session_call("delete_session", name)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            for table in (
                "session_settings", "knowledge_base", "conversation_messages", "system_logs",
                "chat_takeovers", "manual_send_requests", "chat_notes",
            ):
                connection.execute(f"DELETE FROM {table} WHERE session_name = ?", (name,))
            connection.execute("DELETE FROM managed_sessions WHERE session_name = ?", (name,))
            connection.commit()
        finally:
            connection.close()
        self.log("INFO", "session.delete", "已删除会话", name)
        return {"deleted": True, "session": name}

    def get_qr(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        return self._client_session_call("get_qr", name)

    def ensure_session(self, session_name=DEFAULT_SESSION_NAME):
        name = normalize_session_name(session_name)
        self.ensure_managed_session(name)
        sessions = extract_sessions(self.client.get_sessions())
        state = session_state_for(sessions, name)
        created = False
        started = False
        if state == "NOT_CREATED":
            self._create_client_session(name, self._session_config(), False)
            created = True
            state = "CREATED"
        if state not in CONNECTED_STATES and state not in IN_PROGRESS_STATES:
            self._client_session_call("start_session", name)
            started = True
        self.log("INFO", "session.ensure", "已请求创建或启动会话", name)
        return {"session": name, "created": created, "started": started, "state": state}

    def ensure_default(self):
        result = self.ensure_session(DEFAULT_SESSION_NAME)
        # Preserve the original response shape for existing callers.
        return {key: result[key] for key in ("created", "started", "state")}

    def request_pairing_code(self, phone_number, session_name=DEFAULT_SESSION_NAME):
        normalized = normalize_phone_number(phone_number)
        name = normalize_session_name(session_name)
        self.ensure_managed_session(name)
        sessions = extract_sessions(self.client.get_sessions())
        state = session_state_for(sessions, name)
        if state == "NOT_CREATED":
            self._create_client_session(name, self._session_config(), False)
            state = "CREATED"
        if state not in CONNECTED_STATES and state not in IN_PROGRESS_STATES:
            self._client_session_call("start_session", name)
        result = self._client_session_call("request_pairing_code", name, normalized)
        if not isinstance(result, dict):
            raise WahaApiError(502, "WAHA 配对码响应格式不正确")
        code = result.get("code") or result.get("pairingCode") or result.get("pairing_code")
        if not code:
            raise WahaApiError(502, "WAHA 配对码响应中没有 code")
        self.log("INFO", "session.pairing_code", "已请求手机号配对码", name)
        response = {"phone_number": normalized, "code": str(code)}
        if name != DEFAULT_SESSION_NAME:
            response["session"] = name
        return response


def html_page():
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WAHA 本地控制台</title>
  <style>
    :root { color-scheme:light; --ink:#17212b; --muted:#62707c; --paper:#f4f7fb; --surface:#fff; --surface-2:#edf3f8; --line:#d5dfe8; --good:#147d55; --good-bg:#e5f5ed; --warn:#a15c00; --warn-bg:#fff2dd; --bad:#b42318; --bad-bg:#ffebe9; --blue:#1d4ed8; --blue-bg:#e9f0ff; }
    html[data-theme="night"] { color-scheme:dark; --ink:#edf5f7; --muted:#a8bac1; --paper:#10171c; --surface:#18232a; --surface-2:#223139; --line:#33464f; --good:#62d7a0; --good-bg:#17382f; --warn:#f1c56d; --warn-bg:#40341c; --bad:#ff9a91; --bad-bg:#472522; --blue:#62c8d4; --blue-bg:#19383e; }
    html[data-theme="paper"] { --ink:#302c27; --muted:#756b60; --paper:#f3eee6; --surface:#fffaf3; --surface-2:#f2e8da; --line:#d8c8b5; --good:#2f7658; --good-bg:#e3f0e7; --warn:#986323; --warn-bg:#f7e8ca; --bad:#a64236; --bad-bg:#f5dfd9; --blue:#a34a2d; --blue-bg:#f4e0d6; }
    * { box-sizing:border-box; }
    body { margin:0; min-width:320px; background:var(--paper); color:var(--ink); font:15px/1.5 "Segoe UI", system-ui, sans-serif; }
    button { min-height:44px; border:1px solid var(--line); border-radius:7px; padding:0 16px; background:var(--surface); color:var(--ink); font:inherit; font-weight:600; cursor:pointer; transition:background .18s, border-color .18s, transform .18s; touch-action:manipulation; }
    button:hover { border-color:var(--blue); background:var(--surface-2); } button:active { transform:translateY(1px); } button:focus-visible { outline:3px solid var(--blue); outline-offset:2px; }
    button.primary { border-color:var(--blue); background:var(--blue); color:#fff; } button.primary:hover { filter:brightness(.9); } button:disabled { cursor:wait; opacity:.65; transform:none; }
    .shell { max-width:1180px; margin:auto; padding:28px 24px 48px; }
    header { display:flex; justify-content:space-between; gap:24px; align-items:flex-start; border-bottom:1px solid var(--line); padding-bottom:24px; }
    .header-actions { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    a.nav-link { display:inline-flex; align-items:center; min-height:44px; border:1px solid var(--line); border-radius:7px; padding:0 14px; color:var(--ink); background:var(--surface); font-weight:700; text-decoration:none; }
    a.nav-link:hover { border-color:var(--blue); background:var(--surface-2); }
    .eyebrow { color:var(--blue); font:600 12px/1.2 ui-monospace, SFMono-Regular, Consolas, monospace; letter-spacing:0; } h1 { margin:7px 0 4px; font-size:clamp(28px,4vw,44px); line-height:1.1; letter-spacing:0; } header p { margin:0; color:var(--muted); }
    .db-pill { display:flex; align-items:center; gap:8px; border:1px solid #b8dfca; border-radius:999px; padding:8px 12px; background:var(--good-bg); color:var(--good); white-space:nowrap; font-weight:700; } .dot { width:8px; height:8px; border-radius:50%; background:currentColor; }
    main { padding-top:24px; } .metrics { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; } .metric { background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:18px; min-height:128px; } .metric-label { color:var(--muted); font-size:13px; } .metric-value { margin-top:15px; font-size:24px; font-weight:700; } .metric-note { color:var(--muted); font-size:13px; }
    .workspace { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr); gap:12px; margin-top:12px; } .panel { background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:20px; } .panel h2 { margin:0; font-size:18px; } .panel-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; margin-bottom:16px; } .panel-kicker { color:var(--muted); font:12px ui-monospace, SFMono-Regular, Consolas, monospace; }
    .actions { display:flex; flex-wrap:wrap; gap:8px; margin:18px 0; } .qr-frame { display:grid; place-items:center; min-height:300px; aspect-ratio:1/1; max-width:340px; margin:0 auto; border:1px dashed var(--line); background:var(--surface-2); border-radius:6px; } .qr-frame img { width:100%; height:100%; object-fit:contain; padding:20px; } .qr-placeholder { max-width:220px; color:var(--muted); text-align:center; } .hint { margin:14px 0 0; color:var(--muted); font-size:13px; }
    .theme-control { display:flex; align-items:center; gap:8px; min-height:44px; color:var(--muted); font-weight:700; } .theme-control select { min-height:44px; border:1px solid var(--line); border-radius:7px; padding:0 30px 0 10px; background:var(--surface); color:var(--ink); font:inherit; }
    .pairing { margin-top:24px; padding-top:20px; border-top:1px solid var(--line); } .pairing h3 { margin:0 0 4px; font-size:16px; } .pairing p { margin:0; color:var(--muted); font-size:13px; } .pairing-form { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; align-items:end; margin-top:12px; } .pairing-form label { display:block; margin:0; color:var(--muted); font-size:13px; font-weight:700; } .pairing-form input { display:block; width:100%; min-height:44px; margin-top:5px; border:1px solid var(--line); border-radius:6px; padding:0 11px; background:var(--surface); color:var(--ink); font:inherit; } .pairing-form input:focus, .theme-control select:focus { outline:3px solid var(--blue-bg); border-color:var(--blue); } .pairing-code { margin-top:14px; padding:14px; border:1px solid var(--line); border-radius:7px; background:var(--surface-2); text-align:center; } .pairing-code strong { display:block; color:var(--blue); font:700 26px/1.2 ui-monospace,Consolas,monospace; letter-spacing:2px; }
    .state-line { display:flex; align-items:center; gap:8px; margin:7px 0 0; } .state-line.good { color:var(--good); } .state-line.warn { color:var(--warn); } .state-line.bad { color:var(--bad); } .state-badge { display:inline-flex; align-items:center; border-radius:999px; padding:4px 9px; background:#eef1f3; font-size:13px; font-weight:700; } .state-badge.good { color:var(--good); background:var(--good-bg); } .state-badge.warn { color:var(--warn); background:var(--warn-bg); } .state-badge.bad { color:var(--bad); background:var(--bad-bg); }
    .logs { max-height:420px; overflow:auto; } .log { border-top:1px solid var(--line); padding:12px 0; } .log:first-child { border-top:0; padding-top:0; } .log-meta { display:flex; justify-content:space-between; gap:12px; color:var(--muted); font:12px ui-monospace, SFMono-Regular, Consolas, monospace; } .log-message { margin-top:4px; overflow-wrap:anywhere; } .log.error .log-message { color:var(--bad); } .empty { color:var(--muted); }
    .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; } .notice { min-height:24px; margin-top:8px; color:var(--bad); }
    @media (max-width:760px) { .shell { padding:20px 14px 36px; } header { display:block; } .header-actions { margin-top:16px; } .db-pill { display:inline-flex; } .metrics, .workspace { grid-template-columns:1fr; } .qr-frame { min-height:260px; } .pairing-form { grid-template-columns:1fr; } }
    @media (prefers-reduced-motion:reduce) { *, *::before, *::after { scroll-behavior:auto!important; transition-duration:.01ms!important; animation-duration:.01ms!important; } }
  </style>
</head>
<body>
  <div class="shell">
    <header>
      <div><div class="eyebrow">LOCAL OPERATIONS CONSOLE</div><h1>WAHA 本地控制台</h1><p>仅供本机使用的服务状态与会话入口。</p></div>
      <div class="header-actions"><label class="theme-control" for="themeSelect"><span>主题</span><select id="themeSelect" aria-label="选择主题"><option value="daylight">白天</option><option value="night">夜间</option><option value="paper">纸张</option></select></label><a class="nav-link" href="/settings">自动回复设置</a><div class="db-pill" id="dbState"><span class="dot" aria-hidden="true"></span><span>数据库检查中</span></div></div>
    </header>
    <main id="main">
      <section class="metrics" aria-label="系统状态">
        <article class="metric"><div class="metric-label">WAHA 服务</div><div class="metric-value" id="wahaState">检查中</div><div class="metric-note" id="wahaVersion">等待状态读取</div></article>
        <article class="metric"><div class="metric-label">WhatsApp 连接</div><div class="metric-value" id="whatsappState">检查中</div><div class="metric-note" id="sessionState">default / 等待状态读取</div></article>
        <article class="metric"><div class="metric-label">最近检查</div><div class="metric-value" id="checkedAt">--:--:--</div><div class="metric-note">状态每 10 秒刷新</div></article>
      </section>
      <section class="workspace">
        <article class="panel">
          <div class="panel-head"><div><h2>default 会话</h2><div class="panel-kicker">SESSION / DEFAULT</div></div><span class="state-badge" id="sessionBadge">未创建</span></div>
          <div class="actions"><button class="primary" id="ensureButton">创建 / 启动 default</button><button id="qrButton">刷新二维码</button></div>
          <div class="qr-frame" id="qrFrame"><div class="qr-placeholder">会话准备完成后，二维码会显示在这里。</div></div>
          <p class="hint">请使用手机 WhatsApp 扫描当前二维码。二维码只由本地面板代理，不向浏览器下发 API 密钥。</p>
          <div class="pairing">
            <h3>手机号配对码</h3>
            <p>输入包含国家/地区码的手机号，例如 8613812345678。</p>
            <div class="pairing-form"><label for="phoneNumber">手机号<input id="phoneNumber" type="tel" inputmode="numeric" autocomplete="tel" placeholder="8613812345678"></label><button class="primary" id="pairingButton" type="button">获取配对码</button></div>
            <div class="pairing-code" id="pairingCode" hidden role="status" aria-live="polite"></div>
          </div>
          <div class="notice" id="notice" role="status" aria-live="polite"></div>
        </article>
        <aside class="panel"><div class="panel-head"><div><h2>系统记录</h2><div class="panel-kicker">RECENT STATUS / ERRORS</div></div><button id="refreshButton">刷新数据</button></div><div class="logs" id="logs"><div class="empty">正在读取记录...</div></div></aside>
      </section>
    </main>
  </div>
  <script>
    const $ = (id) => document.getElementById(id);
    const sessionName = new URLSearchParams(location.search).get('session') || 'default';
    const sessionQuery = '?session=' + encodeURIComponent(sessionName);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const stateText = (state) => ({'NOT_CREATED':'未创建','CREATED':'已创建','STARTING':'启动中','SCAN_QR_CODE':'等待扫码','AUTHENTICATING':'认证中','WORKING':'已连接','CONNECTED':'已连接','STOPPED':'已停止','FAILED':'失败'}[state] || state || '未知');
    const stateTone = (state) => ['WORKING','CONNECTED'].includes(state) ? 'good' : ['FAILED','STOPPED'].includes(state) ? 'bad' : 'warn';
    const themeNames = {daylight:'白天', night:'夜间', paper:'纸张'};
    function applyTheme(theme) { const selected = Object.prototype.hasOwnProperty.call(themeNames, theme) ? theme : 'daylight'; document.documentElement.dataset.theme = selected; $('themeSelect').value = selected; localStorage.setItem('waha-panel-theme', selected); }
    async function saveTheme(theme) { applyTheme(theme); try { const response = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({theme})}); if (!response.ok) { const data = await response.json(); throw new Error(data.message || '主题保存失败'); } } catch (error) { $('notice').textContent = error.message; } }
    function renderLogs(logs, errors) {
      const all = [...(errors || []).map((message) => ({level:'ERROR', event:'current', message, created_at:Date.now()/1000})), ...(logs || [])];
      $('logs').innerHTML = all.length ? all.map((log) => `<div class="log ${log.level === 'ERROR' ? 'error' : ''}"><div class="log-meta"><span>${esc(log.level)} / ${esc(log.event)}</span><span>${new Date(Number(log.created_at) * 1000).toLocaleString()}</span></div><div class="log-message">${esc(log.message)}</div></div>`).join('') : '<div class="empty">暂无系统记录</div>';
    }
    function render(data) {
      const ws = data.waha || {}, wa = data.whatsapp || {}, db = data.database || {};
      $('wahaState').textContent = ws.running ? '运行中' : '不可用'; $('wahaState').className = `metric-value ${ws.running ? 'good' : 'bad'}`; $('wahaVersion').textContent = ws.version ? `版本 ${esc(ws.version)}` : '未读取到版本';
      $('whatsappState').textContent = wa.connected ? '已连接' : stateText(wa.state); $('whatsappState').className = `metric-value ${wa.connected ? 'good' : stateTone(wa.state)}`; $('sessionState').textContent = `default / ${stateText(wa.state)}`;
      $('sessionBadge').textContent = stateText(wa.state); $('sessionBadge').className = `state-badge ${stateTone(wa.state)}`; $('checkedAt').textContent = new Date().toLocaleTimeString();
      $('dbState').innerHTML = `<span class="dot" aria-hidden="true"></span><span>${db.ok ? '数据库正常' : '数据库异常'}</span>`; $('dbState').style.color = db.ok ? 'var(--good)' : 'var(--bad)'; $('dbState').style.background = db.ok ? 'var(--good-bg)' : 'var(--bad-bg)';
      renderLogs(data.logs, data.errors); if (data.auto_reply && data.auto_reply.theme) applyTheme(data.auto_reply.theme);
    }
    async function refresh() { try { const response = await fetch('/api/status', {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '读取状态失败'); render(data); } catch (error) { $('notice').textContent = error.message; } }
    async function ensure() { const button = $('ensureButton'); button.disabled = true; button.textContent = '处理中...'; $('notice').textContent = ''; try { const response = await fetch('/api/session/ensure', {method:'POST'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '会话操作失败'); $('notice').textContent = '已提交 default 会话操作，请等待状态更新。'; await refresh(); } catch (error) { $('notice').textContent = error.message; } finally { button.disabled = false; button.textContent = '创建 / 启动 default'; } }
    async function loadQr() { $('qrFrame').innerHTML = '<div class="qr-placeholder">正在获取二维码...</div>'; $('notice').textContent = ''; try { const response = await fetch(`/api/qr?ts=${Date.now()}`, {cache:'no-store'}); const type = response.headers.get('content-type') || ''; if (!response.ok || !type.startsWith('image/')) { const data = await response.json(); throw new Error(data.message || '二维码暂不可用'); } const blob = await response.blob(); const image = document.createElement('img'); image.alt = 'WhatsApp 扫码二维码'; image.src = URL.createObjectURL(blob); $('qrFrame').replaceChildren(image); } catch (error) { $('qrFrame').innerHTML = '<div class="qr-placeholder">二维码暂不可用，请先创建 / 启动 default 会话。</div>'; $('notice').textContent = error.message; } }
    async function requestPairingCode() { const button = $('pairingButton'); button.disabled = true; button.textContent = '请求中...'; $('notice').textContent = ''; $('pairingCode').hidden = true; try { const response = await fetch('/api/session/pairing-code', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({phone_number:$('phoneNumber').value})}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '配对码获取失败'); $('pairingCode').innerHTML = `<span>请在手机 WhatsApp 中输入</span><strong>${esc(data.code)}</strong>`; $('pairingCode').hidden = false; $('notice').textContent = '配对码已生成，请尽快在手机端输入。'; await refresh(); } catch (error) { $('notice').textContent = error.message; } finally { button.disabled = false; button.textContent = '获取配对码'; } }
    const storedTheme = localStorage.getItem('waha-panel-theme'); if (storedTheme) applyTheme(storedTheme); $('themeSelect').addEventListener('change', (event) => saveTheme(event.target.value)); $('ensureButton').addEventListener('click', ensure); $('qrButton').addEventListener('click', loadQr); $('pairingButton').addEventListener('click', requestPairingCode); $('refreshButton').addEventListener('click', refresh); refresh(); setInterval(refresh, 10000);
  </script>
</body>
</html>"""


def _sponsor_markup():
    """Return opt-in sponsor HTML and a small event binding for the console."""
    if not as_bool(os.environ.get("PANEL_SPONSOR_ENABLED", "1")):
        return "", "", ""
    sponsor_url = os.environ.get(
        "PANEL_SPONSOR_IMAGE_URL",
        "https://www.6spring.com/wp-content/uploads/2026/09/cangnan.jpg",
    ).strip()
    parsed_sponsor = urlparse(sponsor_url)
    if (
        parsed_sponsor.scheme != "https"
        or not parsed_sponsor.hostname
        or parsed_sponsor.username
        or parsed_sponsor.password
    ):
        return "", "", ""
    safe_url = html.escape(sponsor_url, quote=True)
    slot = '<div class="sponsor-slot"><button class="sponsor-button" id="sponsorButton" type="button">赞助一下</button></div>'
    dialog = (
        '<dialog id="sponsorDialog"><form method="dialog" class="dialog-body">'
        '<div class="dialog-head"><div><div class="dialog-title">赞助一下</div>'
        '<div class="dialog-note">感谢你的支持。</div></div>'
        '<button class="close-button" value="cancel" aria-label="关闭">×</button></div>'
        f'<img class="sponsor-dialog-image" src="{safe_url}" alt="赞助图片" loading="lazy" '
        'onerror="this.hidden=true; this.nextElementSibling.hidden=false">'
        '<div class="sponsor-dialog-error" hidden>图片暂时无法加载。</div>'
        '<div class="dialog-actions"><button value="cancel" type="button" id="sponsorClose">关闭</button></div>'
        '</form></dialog>'
    )
    script = "    $('sponsorButton').addEventListener('click', () => $('sponsorDialog').showModal()); $('sponsorClose').addEventListener('click', () => $('sponsorDialog').close());\n"
    return slot, dialog, script


def multi_session_html_page():
    """Apple-like multi-session operations workspace."""
    page = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>WAHA · 多会话控制台</title>
  <style>
    :root {
      color-scheme: light;
      --bg:#f5f5f7; --surface:#ffffff; --surface-soft:#f8f8fa; --line:#e4e4e8;
      --ink:#1d1d1f; --muted:#6e6e73; --accent:#0071e3; --accent-soft:#e8f2ff;
      --good:#18864b; --good-soft:#e7f6ed; --warn:#a15c00; --warn-soft:#fff3df;
      --bad:#c9342d; --bad-soft:#ffebe9; --shadow:0 12px 38px rgba(31,35,41,.07);
    }
    html[data-theme="night"] { color-scheme:dark; --bg:#111214; --surface:#1c1d20; --surface-soft:#242529; --line:#37383d; --ink:#f5f5f7; --muted:#a1a1a6; --accent:#2997ff; --accent-soft:#183450; --good:#5dd58d; --good-soft:#173827; --warn:#f0bf6c; --warn-soft:#44351e; --bad:#ff8179; --bad-soft:#45211f; --shadow:0 16px 40px rgba(0,0,0,.22); }
    html[data-theme="paper"] { --bg:#f4efe8; --surface:#fffaf4; --surface-soft:#f8f0e7; --line:#e4d8ca; --ink:#2b2825; --muted:#766e66; --accent:#b55332; --accent-soft:#f7e6de; --good:#397653; --good-soft:#e6f1e8; --warn:#99631f; --warn-soft:#faebd2; --bad:#a94439; --bad-soft:#f5e3de; --shadow:0 12px 34px rgba(83,60,43,.09); }
    * { box-sizing:border-box; }
    body { margin:0; min-width:320px; background:var(--bg); color:var(--ink); font:15px/1.45 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",system-ui,sans-serif; }
    button, input, select { font:inherit; }
    button { min-height:42px; border:1px solid var(--line); border-radius:11px; padding:0 14px; background:var(--surface); color:var(--ink); font-weight:650; cursor:pointer; transition:background .16s,border-color .16s,transform .16s,opacity .16s; }
    button:hover { border-color:var(--accent); background:var(--surface-soft); } button:active { transform:translateY(1px); } button:disabled { opacity:.55; cursor:wait; transform:none; }
    button:focus-visible, input:focus-visible, select:focus-visible { outline:3px solid var(--accent-soft); outline-offset:2px; border-color:var(--accent); }
    button.primary { border-color:var(--accent); background:var(--accent); color:#fff; } button.primary:hover { filter:brightness(.94); }
    button.subtle { background:var(--surface-soft); } button.danger { color:var(--bad); }
    .app { min-height:100dvh; display:grid; grid-template-rows:auto 1fr; }
    .topbar { display:flex; align-items:center; justify-content:space-between; gap:20px; padding:22px clamp(18px,4vw,52px); border-bottom:1px solid var(--line); background:color-mix(in srgb,var(--bg) 88%,transparent); backdrop-filter:blur(16px); position:sticky; top:0; z-index:5; }
    .brand { display:flex; align-items:center; gap:13px; min-width:0; } .brand-mark { width:34px; height:34px; display:grid; place-items:center; border-radius:10px; background:var(--ink); color:var(--bg); font-weight:800; letter-spacing:-.04em; } .brand-copy { min-width:0; } .brand-title { font-size:16px; font-weight:760; letter-spacing:-.01em; } .brand-subtitle { color:var(--muted); font-size:12px; margin-top:1px; }
    .top-actions { display:flex; align-items:center; gap:9px; flex-wrap:wrap; justify-content:flex-end; } .theme-select { min-height:42px; border:1px solid var(--line); border-radius:11px; padding:0 11px; background:var(--surface); color:var(--ink); }
    .service-pill { display:inline-flex; align-items:center; gap:8px; min-height:42px; padding:0 13px; border:1px solid var(--line); border-radius:999px; background:var(--surface); color:var(--muted); font-size:13px; font-weight:700; white-space:nowrap; } .service-pill.good { color:var(--good); border-color:color-mix(in srgb,var(--good) 30%,var(--line)); background:var(--good-soft); } .service-dot { width:7px; height:7px; border-radius:50%; background:currentColor; }
    .layout { width:min(1480px,100%); margin:0 auto; padding:24px clamp(14px,4vw,52px) 50px; display:grid; grid-template-columns:300px minmax(0,1fr); gap:22px; }
    .sidebar, .card { background:var(--surface); border:1px solid var(--line); border-radius:18px; box-shadow:var(--shadow); }
    .sidebar { align-self:start; overflow:hidden; position:sticky; top:96px; } .side-head { display:flex; justify-content:space-between; align-items:center; gap:10px; padding:18px; border-bottom:1px solid var(--line); } .side-title { font-size:16px; font-weight:760; } .side-count { color:var(--muted); font-size:12px; margin-top:2px; }
    .session-list { padding:8px; max-height:calc(100dvh - 190px); overflow:auto; } .session-item { width:100%; display:block; text-align:left; border:1px solid transparent; border-radius:13px; padding:13px 12px; margin:2px 0; background:transparent; min-height:76px; } .session-item:hover { background:var(--surface-soft); border-color:var(--line); } .session-item.selected { background:var(--accent-soft); border-color:color-mix(in srgb,var(--accent) 28%,transparent); } .session-name-row { display:flex; align-items:center; justify-content:space-between; gap:8px; } .session-display { min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:720; } .session-tech { color:var(--muted); font:11px ui-monospace,SFMono-Regular,Consolas,monospace; margin-top:3px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; } .session-meta { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-top:9px; color:var(--muted); font-size:12px; } .mini-status { display:inline-flex; align-items:center; gap:5px; font-weight:700; } .mini-status.good { color:var(--good); } .mini-status.warn { color:var(--warn); } .mini-status.bad { color:var(--bad); } .mini-status .service-dot { width:6px; height:6px; }
    .main { min-width:0; } .hero { display:flex; justify-content:space-between; align-items:flex-start; gap:18px; margin:2px 0 20px; } .eyebrow { color:var(--accent); font:700 11px ui-monospace,SFMono-Regular,Consolas,monospace; letter-spacing:.08em; } h1 { margin:7px 0 5px; font-size:clamp(27px,4vw,42px); line-height:1.08; letter-spacing:-.04em; } .hero p { margin:0; color:var(--muted); } .hero-actions { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
    .status-banner { display:flex; align-items:center; justify-content:space-between; gap:16px; padding:14px 16px; border:1px solid var(--line); border-radius:14px; background:var(--surface); margin-bottom:14px; } .status-copy { min-width:0; } .status-label { color:var(--muted); font-size:12px; } .status-value { margin-top:2px; font-size:18px; font-weight:760; } .status-note { color:var(--muted); font-size:12px; margin-top:2px; overflow-wrap:anywhere; } .status-badge { display:inline-flex; align-items:center; gap:7px; padding:7px 11px; border-radius:999px; font-size:13px; font-weight:750; white-space:nowrap; } .status-badge.good { color:var(--good); background:var(--good-soft); } .status-badge.warn { color:var(--warn); background:var(--warn-soft); } .status-badge.bad { color:var(--bad); background:var(--bad-soft); }
    .detail-grid { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(300px,.85fr); gap:14px; } .card { padding:20px; } .card + .card { margin-top:14px; } .card-head { display:flex; align-items:flex-start; justify-content:space-between; gap:14px; margin-bottom:17px; } .card-title { font-size:17px; font-weight:760; letter-spacing:-.015em; } .card-note { color:var(--muted); font-size:12px; margin-top:3px; }
    .identity { display:flex; align-items:center; gap:13px; padding:14px; border-radius:14px; background:var(--surface-soft); border:1px solid var(--line); } .identity-icon { width:42px; height:42px; display:grid; place-items:center; border-radius:13px; background:var(--accent-soft); color:var(--accent); font-weight:800; } .identity-main { min-width:0; flex:1; } .identity-name { font-size:18px; font-weight:760; overflow-wrap:anywhere; } .identity-tech { color:var(--muted); font:12px ui-monospace,SFMono-Regular,Consolas,monospace; margin-top:2px; } .identity-actions { display:flex; gap:7px; flex-wrap:wrap; justify-content:flex-end; }
    .action-row { display:flex; flex-wrap:wrap; gap:8px; margin-top:14px; } .action-row button { flex:0 0 auto; } .summary-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:9px; margin-top:14px; } .summary { padding:12px; border:1px solid var(--line); border-radius:12px; background:var(--surface-soft); } .summary-label { color:var(--muted); font-size:12px; } .summary-value { margin-top:4px; font-weight:730; overflow-wrap:anywhere; }
    .qr-wrap { display:grid; place-items:center; min-height:320px; border:1px dashed var(--line); border-radius:15px; background:var(--surface-soft); overflow:hidden; } .qr-wrap img { display:block; width:min(100%,330px); aspect-ratio:1; object-fit:contain; padding:18px; } .qr-empty { max-width:230px; padding:25px; color:var(--muted); text-align:center; } .qr-caption { color:var(--muted); font-size:12px; margin-top:10px; }
    .pairing { margin-top:18px; padding-top:17px; border-top:1px solid var(--line); } .pairing-title { font-weight:730; } .pairing-note { color:var(--muted); font-size:12px; margin-top:3px; } .pairing-form { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; margin-top:10px; } input[type=text],input[type=tel],input[type=url],input[type=password],input[type=time],select,textarea { width:100%; min-height:42px; border:1px solid var(--line); border-radius:10px; padding:9px 11px; background:var(--surface); color:var(--ink); } .pairing-code { margin-top:10px; padding:12px; border-radius:11px; text-align:center; background:var(--accent-soft); color:var(--accent); } .pairing-code strong { display:block; font:750 23px ui-monospace,SFMono-Regular,Consolas,monospace; letter-spacing:.08em; margin-top:3px; }
    .logs { max-height:410px; overflow:auto; } .log { padding:12px 0; border-top:1px solid var(--line); } .log:first-child { border-top:0; padding-top:0; } .log-meta { display:flex; justify-content:space-between; gap:10px; color:var(--muted); font:11px ui-monospace,SFMono-Regular,Consolas,monospace; } .log-message { margin-top:4px; overflow-wrap:anywhere; } .log.error .log-message { color:var(--bad); } .empty { color:var(--muted); padding:9px 0; }
    .notice { min-height:23px; margin-top:10px; color:var(--bad); font-size:13px; } .notice.success { color:var(--good); } .sr-only { position:absolute; width:1px; height:1px; padding:0; margin:-1px; overflow:hidden; clip:rect(0,0,0,0); white-space:nowrap; border:0; }
    dialog { width:min(480px,calc(100% - 28px)); border:1px solid var(--line); border-radius:18px; padding:0; color:var(--ink); background:var(--surface); box-shadow:0 24px 80px rgba(0,0,0,.25); } dialog::backdrop { background:rgba(0,0,0,.32); backdrop-filter:blur(3px); } .dialog-body { padding:22px; } .dialog-head { display:flex; justify-content:space-between; gap:14px; align-items:flex-start; } .dialog-title { font-size:19px; font-weight:780; } .dialog-note { color:var(--muted); font-size:13px; margin-top:4px; } .close-button { min-width:42px; padding:0; border-radius:50%; font-size:20px; line-height:1; } .field { margin-top:15px; } .field label { display:block; font-size:13px; font-weight:720; margin-bottom:6px; } .field-help { color:var(--muted); font-size:12px; margin-top:5px; } .dialog-actions { display:flex; justify-content:flex-end; gap:8px; margin-top:20px; }
    @media (max-width:1050px) { .layout { grid-template-columns:250px minmax(0,1fr); } .detail-grid { grid-template-columns:1fr; } }
    @media (max-width:760px) { .topbar { display:block; padding:16px 14px; min-width:0; } .top-actions { width:100%; min-width:0; gap:6px; margin-top:12px; justify-content:flex-start; } .top-actions .theme-select { flex:1 1 130px; min-width:0; } .top-actions .service-pill { order:3; width:100%; } .layout { display:block; width:100%; max-width:100%; min-width:0; padding:14px 12px 35px; overflow:hidden; } .sidebar { position:static; width:100%; min-width:0; margin-bottom:16px; } .session-list { display:flex; gap:7px; min-width:0; max-width:100%; overflow-x:auto; max-height:none; padding:8px; } .session-item { min-width:210px; margin:0; } .hero { display:block; } .hero-actions { margin-top:14px; justify-content:flex-start; } .status-banner { align-items:flex-start; flex-direction:column; } .identity { align-items:flex-start; flex-wrap:wrap; } .identity-actions { width:100%; justify-content:flex-start; } .pairing-form { grid-template-columns:1fr; } .summary-grid { grid-template-columns:1fr; } }
    @media (prefers-reduced-motion:reduce) { *,*::before,*::after { transition-duration:.01ms!important; animation-duration:.01ms!important; scroll-behavior:auto!important; } }
    .sponsor-slot { padding:12px 14px 14px; border-top:1px solid var(--line); background:var(--surface); } .sponsor-button { width:100%; color:var(--accent); background:var(--accent-soft); border-color:color-mix(in srgb,var(--accent) 28%,var(--line)); } .sponsor-button:hover { background:var(--surface-soft); } .update-grid { display:grid; gap:10px; margin-top:16px; } .update-row { display:grid; grid-template-columns:1fr auto; gap:12px; align-items:center; padding:13px; border:1px solid var(--line); border-radius:12px; background:var(--surface-soft); } .update-name { font-weight:750; } .update-meta { color:var(--muted); font-size:12px; margin-top:3px; overflow-wrap:anywhere; } .update-state { color:var(--muted); font-size:13px; font-weight:700; text-align:right; } .update-state.good { color:var(--good); } .update-state.warn { color:var(--warn); } .update-state.bad { color:var(--bad); } .admin-list { display:grid; gap:8px; margin-top:14px; } .admin-row { display:grid; grid-template-columns:minmax(0,1fr) auto auto; gap:9px; align-items:center; padding:10px 12px; border:1px solid var(--line); border-radius:12px; background:var(--surface-soft); } .admin-name { min-width:0; overflow-wrap:anywhere; font-weight:700; } .admin-meta { color:var(--muted); font-size:12px; margin-top:2px; } .admin-row label { display:flex; align-items:center; gap:6px; color:var(--muted); font-size:12px; white-space:nowrap; } .admin-row input[type=checkbox] { width:18px; height:18px; accent-color:var(--accent); } .admin-password { width:180px!important; } .sponsor-dialog-image { display:block; width:100%; max-height:58dvh; object-fit:contain; border-radius:12px; background:var(--surface-soft); } .sponsor-dialog-error { min-height:24px; color:var(--bad); font-size:13px; }
  </style>
</head>
<body>
  <div class="app">
    <header class="topbar">
      <div class="brand"><div class="brand-mark" aria-hidden="true">W</div><div class="brand-copy"><div class="brand-title">WAHA 控制台</div><div class="brand-subtitle">本地多会话工作区</div></div></div>
      <div class="top-actions"><select id="themeSelect" class="theme-select" aria-label="选择界面主题"><option value="daylight">白天</option><option value="night">夜间</option><option value="paper">纸张</option></select><a class="subtle nav-link" href="/settings" style="display:inline-flex;align-items:center;min-height:42px;border:1px solid var(--line);border-radius:11px;padding:0 13px;color:var(--ink);text-decoration:none;font-weight:650;">自动回复设置</a><button class="subtle" id="updateButton" type="button">检查更新</button><button class="subtle" id="adminButton" type="button">管理员</button><div class="service-pill" id="servicePill"><span class="service-dot" aria-hidden="true"></span><span>WAHA 检查中</span></div></div>
    </header>
    <div class="layout">
      <aside class="sidebar" aria-label="会话列表"><div class="side-head"><div><div class="side-title">会话</div><div class="side-count" id="sessionCount">读取中</div></div><button class="primary" id="newSessionButton" type="button">新建</button></div><div class="session-list" id="sessionList"><div class="empty">正在读取会话...</div></div><!-- SPONSOR_SLOT --></aside>
      <main class="main">
        <div class="hero"><div><div class="eyebrow">SESSION WORKSPACE</div><h1 id="pageTitle">会话详情</h1><p id="pageSubtitle">选择一个会话开始管理。</p></div><div class="hero-actions"><button id="refreshButton" type="button">刷新状态</button><button id="deleteButton" class="danger" type="button">删除会话</button><a id="chatLink" class="primary nav-link" href="#" style="display:inline-flex;align-items:center;min-height:42px;border:1px solid var(--accent);border-radius:11px;padding:0 14px;background:var(--accent);color:#fff;text-decoration:none;font-weight:700;">聊天管理</a><a id="settingsLink" class="subtle nav-link" href="/settings" style="display:inline-flex;align-items:center;min-height:42px;border:1px solid var(--line);border-radius:11px;padding:0 14px;color:var(--ink);text-decoration:none;font-weight:650;">设置此会话</a></div></div>
        <div class="status-banner"><div class="status-copy"><div class="status-label">当前会话</div><div class="status-value" id="currentSessionLabel">—</div><div class="status-note" id="currentSessionTech">—</div></div><div class="status-badge warn" id="currentBadge"><span class="service-dot" aria-hidden="true"></span><span>读取中</span></div></div>
        <div class="detail-grid">
          <section>
            <article class="card"><div class="card-head"><div><div class="card-title">会话控制</div><div class="card-note">每个技术会话独立启动、停止与重启。</div></div><button id="renameButton" type="button">编辑名称</button></div><div class="identity"><div class="identity-icon" aria-hidden="true">W</div><div class="identity-main"><div class="identity-name" id="identityName">—</div><div class="identity-tech" id="identityTech">—</div></div><div class="identity-actions"><span class="status-badge warn" id="identityBadge">未知</span></div></div><div class="action-row"><button class="primary" id="startButton" type="button">启动</button><button id="stopButton" type="button">停止</button><button class="subtle" id="restartButton" type="button">重启</button><button class="subtle" id="ensureButton" type="button">创建 / 启动</button></div><div class="summary-grid"><div class="summary"><div class="summary-label">自动回复</div><div class="summary-value" id="autoReplySummary">—</div></div><div class="summary"><div class="summary-label">AI 模型</div><div class="summary-value" id="aiSummary">—</div></div><div class="summary"><div class="summary-label">WAHA 状态</div><div class="summary-value" id="remoteStateSummary">—</div></div><div class="summary"><div class="summary-label">最后更新</div><div class="summary-value" id="updatedSummary">—</div></div></div><div class="notice" id="controlNotice" role="status" aria-live="polite"></div></article>
            <article class="card"><div class="card-head"><div><div class="card-title">系统记录</div><div class="card-note">仅显示当前会话的处理记录与错误。</div></div><button id="refreshLogsButton" type="button">刷新记录</button></div><div class="logs" id="logs"><div class="empty">正在读取记录...</div></div></article>
          </section>
          <section>
            <article class="card"><div class="card-head"><div><div class="card-title">扫码连接</div><div class="card-note">二维码属于当前会话，不会把 WAHA 密钥交给浏览器。</div></div><button id="qrButton" type="button">刷新二维码</button></div><div class="qr-wrap" id="qrWrap"><div class="qr-empty">点击“刷新二维码”获取当前会话的登录二维码。</div></div><div class="qr-caption">如果会话已连接，二维码可能暂不可用。</div><div class="pairing"><div class="pairing-title">手机号配对码</div><div class="pairing-note">填写包含国家/地区码的手机号，例如 8613812345678。</div><div class="pairing-form"><label class="sr-only" for="phoneNumber">手机号</label><input id="phoneNumber" type="tel" inputmode="numeric" autocomplete="tel" placeholder="8613812345678"><button class="primary" id="pairingButton" type="button">获取配对码</button></div><div id="pairingCode" class="pairing-code" hidden role="status" aria-live="polite"></div></div></article>
            <article class="card"><div class="card-head"><div><div class="card-title">服务概况</div><div class="card-note">面板每 10 秒自动刷新。</div></div></div><div class="summary-grid"><div class="summary"><div class="summary-label">WAHA 服务</div><div class="summary-value" id="wahaSummary">检查中</div></div><div class="summary"><div class="summary-label">数据库</div><div class="summary-value" id="dbSummary">检查中</div></div></div><div class="notice" id="globalNotice" role="status" aria-live="polite"></div></article>
          </section>
        </div>
      </main>
    </div>
  </div>
  <dialog id="newDialog"><form method="dialog" class="dialog-body" id="newForm"><div class="dialog-head"><div><div class="dialog-title">新建会话</div><div class="dialog-note">技术名称创建后保持不变；显示名称可以随时编辑。</div></div><button class="close-button" value="cancel" aria-label="关闭">×</button></div><div class="field"><label for="newName">技术名称</label><input id="newName" type="text" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}" placeholder="例如 sales"><div class="field-help">仅支持字母、数字、点、下划线和短横线。</div></div><div class="field"><label for="newDisplayName">显示名称</label><input id="newDisplayName" type="text" maxlength="80" placeholder="例如 销售账号"></div><div class="field"><label><input id="newStart" type="checkbox" style="width:18px;height:18px;vertical-align:-4px;margin-right:7px;accent-color:var(--accent);">创建后立即启动</label></div><div class="notice" id="newNotice" role="status" aria-live="polite"></div><div class="dialog-actions"><button value="cancel" type="button" id="newCancel">取消</button><button class="primary" type="submit" id="newSubmit">创建会话</button></div></form></dialog>
  <dialog id="renameDialog"><form method="dialog" class="dialog-body" id="renameForm"><div class="dialog-head"><div><div class="dialog-title">编辑显示名称</div><div class="dialog-note">不会修改 WAHA 技术名称，也不会使当前会话掉线。</div></div><button class="close-button" value="cancel" aria-label="关闭">×</button></div><div class="field"><label for="renameInput">显示名称</label><input id="renameInput" type="text" required maxlength="80"></div><div class="notice" id="renameNotice" role="status" aria-live="polite"></div><div class="dialog-actions"><button value="cancel" type="button" id="renameCancel">取消</button><button class="primary" type="submit" id="renameSubmit">保存名称</button></div></form></dialog>
  <dialog id="updateDialog"><form method="dialog" class="dialog-body" id="updateForm"><div class="dialog-head"><div><div class="dialog-title">组件更新检查</div><div class="dialog-note">这里只读取公开版本信息，不会自动拉取镜像或重启服务。</div></div><button class="close-button" value="cancel" aria-label="关闭">×</button></div><div class="update-grid"><div class="update-row"><div><div class="update-name">面板</div><div class="update-meta" id="panelUpdateMeta">等待检查</div></div><div class="update-state" id="panelUpdateState">未检查</div></div><div class="update-row"><div><div class="update-name">WAHA</div><div class="update-meta" id="wahaUpdateMeta">等待检查</div></div><div class="update-state" id="wahaUpdateState">未检查</div></div></div><div class="notice" id="updateNotice" role="status" aria-live="polite"></div><div class="dialog-actions"><button value="cancel" type="button" id="updateClose">关闭</button><button class="primary" type="button" id="updateRun">重新检查</button></div></form></dialog>
  <dialog id="adminDialog"><form method="dialog" class="dialog-body" id="adminForm"><div class="dialog-head"><div><div class="dialog-title">管理员账号</div><div class="dialog-note">密码只在提交时使用，不会显示或回传。</div></div><button class="close-button" value="cancel" aria-label="关闭">×</button></div><div class="admin-list" id="adminList"><div class="empty">正在读取管理员...</div></div><div class="field"><label for="newAdminName">新增账号</label><input id="newAdminName" type="text" maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9._-]{0,63}" autocomplete="off"></div><div class="field"><label for="newAdminPassword">初始密码</label><input id="newAdminPassword" class="admin-password" type="password" minlength="12" maxlength="256" autocomplete="new-password"></div><div class="notice" id="adminNotice" role="status" aria-live="polite"></div><div class="dialog-actions"><button value="cancel" type="button" id="adminClose">关闭</button><button class="primary" type="button" id="createAdmin">新增管理员</button></div></form></dialog>
  <!-- SPONSOR_DIALOG_SLOT -->
  <script>
    const $ = (id) => document.getElementById(id);
    const stateText = (state) => ({NOT_CREATED:'未创建',CREATED:'已创建',STARTING:'启动中',SCAN_QR_CODE:'等待扫码',AUTHENTICATING:'认证中',WORKING:'已连接',CONNECTED:'已连接',STOPPED:'已停止',FAILED:'失败'}[state] || state || '未知');
    const stateTone = (state) => ['WORKING','CONNECTED'].includes(state) ? 'good' : ['FAILED','STOPPED'].includes(state) ? 'bad' : 'warn';
    const themeNames = {daylight:'白天',night:'夜间',paper:'纸张'};
    let selected = new URLSearchParams(location.search).get('session') || 'default';
    let statusData = null;
    let csrfToken = '';
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const apiSession = (path) => '/api/sessions/' + encodeURIComponent(selected) + path;
    async function ensureCsrf() { if (csrfToken) return csrfToken; const response = await fetch('/api/security/csrf', {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '安全校验不可用'); csrfToken = data.csrf_token || ''; return csrfToken; }
    async function mutateFetch(url, options={}) { const headers = new Headers(options.headers || {}); try { await ensureCsrf(); } catch (_) {} if (csrfToken) headers.set('X-CSRF-Token', csrfToken); return fetch(url, {...options, headers, credentials:'same-origin', cache:'no-store'}); }
    function applyTheme(theme) { const value = themeNames[theme] ? theme : 'daylight'; document.documentElement.dataset.theme = value; $('themeSelect').value = value; localStorage.setItem('waha-panel-theme', value); }
    async function saveTheme(theme) { applyTheme(theme); try { const response = await mutateFetch('/api/settings?session=' + encodeURIComponent(selected), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({theme})}); if (!response.ok) throw new Error((await response.json()).message || '主题保存失败'); } catch (error) { $('globalNotice').textContent = error.message; } }
    function badge(element, state) { element.className = 'status-badge ' + stateTone(state); element.innerHTML = `<span class="service-dot" aria-hidden="true"></span><span>${esc(stateText(state))}</span>`; }
    function formatTime(value) { if (!value) return '—'; return new Date(Number(value) * 1000).toLocaleString(); }
    function currentItem() { return (statusData?.sessions || []).find(item => item.name === selected) || null; }
    function renderSessions() { const items = statusData?.sessions || []; $('sessionCount').textContent = `${items.length} 个会话`; $('sessionList').innerHTML = items.length ? items.map(item => `<button class="session-item ${item.name === selected ? 'selected' : ''}" data-session="${esc(item.name)}" type="button"><div class="session-name-row"><span class="session-display">${esc(item.display_name || item.name)}</span><span class="mini-status ${stateTone(item.state)}"><span class="service-dot"></span>${esc(stateText(item.state))}</span></div><div class="session-tech">${esc(item.name)}</div><div class="session-meta"><span>${item.auto_reply?.all_day ? '全时段自动回复' : item.auto_reply?.enabled ? '按时段自动回复' : '自动回复关闭'}</span><span>${item.auto_reply?.ai_configured ? 'AI 已配置' : '固定文案'}</span></div></button>`).join('') : '<div class="empty">WAHA 尚未返回会话。</div>'; document.querySelectorAll('[data-session]').forEach(button => button.addEventListener('click', () => selectSession(button.dataset.session))); }
     function renderDetail() { const item = currentItem(); if (!item) { $('pageTitle').textContent = '没有可用会话'; $('pageSubtitle').textContent = '请先新建或让 WAHA 返回一个会话。'; $('deleteButton').disabled = true; return; } $('pageTitle').textContent = item.display_name || item.name; $('pageSubtitle').textContent = '独立管理连接状态、二维码、聊天、日志和自动回复。'; $('currentSessionLabel').textContent = item.display_name || item.name; $('currentSessionTech').textContent = item.name; $('identityName').textContent = item.display_name || item.name; $('identityTech').textContent = 'WAHA / ' + item.name; badge($('currentBadge'), item.state); badge($('identityBadge'), item.state); $('autoReplySummary').textContent = item.auto_reply?.all_day ? '全时段开启' : item.auto_reply?.enabled ? '按时段开启' : '已关闭'; $('aiSummary').textContent = item.auto_reply?.ai_configured ? '已配置（密钥隐藏）' : '未配置，使用固定文案'; $('remoteStateSummary').textContent = stateText(item.state); $('updatedSummary').textContent = formatTime(item.updated_at); $('chatLink').href = '/sessions/' + encodeURIComponent(item.name) + '/chats'; $('settingsLink').href = '/settings?session=' + encodeURIComponent(item.name); $('startButton').disabled = ['WORKING','CONNECTED','STARTING','AUTHENTICATING','SCAN_QR_CODE'].includes(item.state); $('stopButton').disabled = item.state === 'STOPPED' || item.state === 'NOT_CREATED'; $('restartButton').disabled = item.state === 'NOT_CREATED'; $('deleteButton').disabled = (statusData?.sessions || []).length <= 1; }
    function renderGlobal() { const waha = statusData?.waha || {}; $('wahaSummary').textContent = waha.running ? (waha.version ? '运行中 · ' + waha.version : '运行中') : '不可用'; $('dbSummary').textContent = statusData?.database?.ok ? '数据库正常' : '数据库异常'; $('servicePill').className = 'service-pill ' + (waha.running ? 'good' : ''); $('servicePill').innerHTML = `<span class="service-dot" aria-hidden="true"></span><span>${waha.running ? 'WAHA 运行中' : 'WAHA 不可用'}</span>`; $('globalNotice').textContent = statusData?.errors?.length ? statusData.errors.join('；') : ''; }
    function renderLogs(logs, errors) { const all = [...(errors || []).map(message => ({level:'ERROR',event:'当前检查',message,created_at:Date.now()/1000})), ...(logs || [])]; $('logs').innerHTML = all.length ? all.map(log => `<div class="log ${log.level === 'ERROR' ? 'error' : ''}"><div class="log-meta"><span>${esc(log.level)} / ${esc(log.event)}</span><span>${esc(formatTime(log.created_at))}</span></div><div class="log-message">${esc(log.message)}</div></div>`).join('') : '<div class="empty">暂无当前会话记录</div>'; }
    async function refreshLogs() { if (!selected) return; try { const response = await fetch(apiSession('/logs'), {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '读取记录失败'); renderLogs(data.logs, []); } catch (error) { $('logs').innerHTML = `<div class="empty">${esc(error.message)}</div>`; } }
    function render(data) { statusData = data; const names = (data.sessions || []).map(item => item.name); if (!names.includes(selected)) selected = data.selected_session || names[0] || 'default'; renderSessions(); renderDetail(); renderGlobal(); const item = currentItem(); renderLogs(data.logs, data.errors); if (item && data.auto_reply?.theme) applyTheme(data.auto_reply.theme); const url = new URL(location.href); url.searchParams.set('session', selected); history.replaceState(null,'',url); }
    async function refresh() { try { const response = await fetch('/api/status?session=' + encodeURIComponent(selected), {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '读取状态失败'); render(data); } catch (error) { $('globalNotice').textContent = error.message; } }
    async function selectSession(name) { selected = name; $('globalNotice').textContent = ''; $('logs').innerHTML = '<div class="empty">正在读取记录...</div>'; await refresh(); }
     async function operation(action) { const button = $(action + 'Button'); if (button) { button.disabled = true; } $('controlNotice').textContent = ''; try { const response = await mutateFetch(apiSession('/' + action), {method:'POST'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '会话操作失败'); $('controlNotice').className = 'notice success'; $('controlNotice').textContent = '操作已提交，状态正在更新。'; await refresh(); } catch (error) { $('controlNotice').className = 'notice'; $('controlNotice').textContent = error.message; } finally { renderDetail(); } }
     async function deleteSelected() { const item = currentItem(); if (!item || (statusData?.sessions || []).length <= 1) return; if (!window.confirm('确认删除会话“' + (item.display_name || item.name) + '”？这会同时删除 WAHA 会话和面板中的关联数据。')) return; const button = $('deleteButton'); button.disabled = true; $('controlNotice').textContent = ''; try { const response = await mutateFetch('/api/sessions/' + encodeURIComponent(item.name), {method:'DELETE'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '删除会话失败'); const remaining = (statusData?.sessions || []).filter(entry => entry.name !== item.name); selected = remaining[0]?.name || 'default'; $('controlNotice').className = 'notice success'; $('controlNotice').textContent = '会话已删除。'; await refresh(); } catch (error) { $('controlNotice').className = 'notice'; $('controlNotice').textContent = error.message; } finally { renderDetail(); } }
    async function loadQr() { $('qrWrap').innerHTML = '<div class="qr-empty">正在获取二维码...</div>'; $('qrButton').disabled = true; try { const response = await fetch(apiSession('/qr?ts=' + Date.now()), {cache:'no-store'}); const type = response.headers.get('content-type') || ''; if (!response.ok || !type.startsWith('image/')) { const data = await response.json(); throw new Error(data.message || '二维码暂不可用'); } const image = document.createElement('img'); image.alt = '当前 WhatsApp 会话二维码'; image.src = URL.createObjectURL(await response.blob()); $('qrWrap').replaceChildren(image); } catch (error) { $('qrWrap').innerHTML = `<div class="qr-empty">${esc(error.message)}<br>请先启动会话后重试。</div>`; } finally { $('qrButton').disabled = false; } }
    async function pairing() { const button = $('pairingButton'); button.disabled = true; $('pairingCode').hidden = true; try { const response = await mutateFetch(apiSession('/pairing-code'), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phone_number:$('phoneNumber').value})}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '配对码获取失败'); $('pairingCode').innerHTML = `<span>请在手机 WhatsApp 中输入</span><strong>${esc(data.code)}</strong>`; $('pairingCode').hidden = false; } catch (error) { $('controlNotice').className = 'notice'; $('controlNotice').textContent = error.message; } finally { button.disabled = false; } }
    function openNew() { $('newNotice').textContent = ''; $('newForm').reset(); $('newDialog').showModal(); $('newName').focus(); }
    async function createNew(event) { event.preventDefault(); const button = $('newSubmit'); button.disabled = true; $('newNotice').textContent = ''; try { const response = await mutateFetch('/api/sessions', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('newName').value.trim(),display_name:$('newDisplayName').value.trim(),start:$('newStart').checked})}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '创建会话失败'); $('newDialog').close(); selected = data.name || data.session_name || $('newName').value.trim(); await refresh(); } catch (error) { $('newNotice').textContent = error.message; } finally { button.disabled = false; } }
    function openRename() { const item = currentItem(); if (!item) return; $('renameInput').value = item.display_name || item.name; $('renameNotice').textContent = ''; $('renameDialog').showModal(); $('renameInput').focus(); $('renameInput').select(); }
    async function rename(event) { event.preventDefault(); const button = $('renameSubmit'); button.disabled = true; $('renameNotice').textContent = ''; try { const response = await mutateFetch(apiSession(''), {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({display_name:$('renameInput').value.trim()})}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '名称保存失败'); $('renameDialog').close(); await refresh(); } catch (error) { $('renameNotice').textContent = error.message; } finally { button.disabled = false; } }
    function updateStatusText(value) { return ({current:'当前',update_available:'有新版本',error:'检查失败',unknown:'未检查'}[value] || value || '未检查'); }
    function renderUpdateItem(prefix, item) { const state = $(prefix + 'UpdateState'); const meta = $(prefix + 'UpdateMeta'); state.textContent = updateStatusText(item.status); state.className = 'update-state ' + (item.status === 'current' ? 'good' : item.status === 'update_available' ? 'warn' : item.status === 'error' ? 'bad' : ''); meta.textContent = item.status === 'error' ? (item.error || '无法读取公开版本信息') : `当前 ${item.current_tag || item.current_version || '未知'} · 最新 ${item.latest_tag || item.latest_version || '未知'}`; }
    async function checkUpdates() { $('updateDialog').showModal(); $('updateNotice').textContent = '正在读取公开版本信息...'; $('updateRun').disabled = true; try { const response = await fetch('/api/updates/check', {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '更新检查失败'); renderUpdateItem('panel', data.panel || {}); renderUpdateItem('waha', data.waha || {}); $('updateNotice').textContent = '检查完成；更新需在宿主机分别执行。'; } catch (error) { $('updateNotice').textContent = error.message; } finally { $('updateRun').disabled = false; } }
    function renderAdmins(items) { const list = $('adminList'); if (!items.length) { list.innerHTML = '<div class="empty">尚未初始化管理员账号</div>'; return; } list.innerHTML = items.map(item => `<div class="admin-row" data-admin-id="${esc(item.id)}"><div><div class="admin-name">${esc(item.username)}</div><div class="admin-meta">${item.last_login_at ? '最近登录 ' + esc(new Date(item.last_login_at * 1000).toLocaleString()) : '尚未登录'}</div></div><label><input class="admin-active" type="checkbox" ${item.is_active ? 'checked' : ''} aria-label="启用 ${esc(item.username)}">启用</label><input class="admin-password" type="password" minlength="12" maxlength="256" autocomplete="new-password" placeholder="新密码"><button class="subtle admin-save" type="button">保存</button></div>`).join(''); list.querySelectorAll('.admin-save').forEach(button => button.addEventListener('click', () => saveAdmin(button.closest('.admin-row')))); }
    async function loadAdmins() { $('adminList').innerHTML = '<div class="empty">正在读取管理员...</div>'; try { const response = await fetch('/api/admin/users', {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '管理员读取失败'); renderAdmins(data.items || []); } catch (error) { $('adminList').innerHTML = `<div class="notice">${esc(error.message)}</div>`; } }
    async function saveAdmin(row) { const id = row.dataset.adminId; const button = row.querySelector('.admin-save'); const password = row.querySelector('.admin-password').value; button.disabled = true; $('adminNotice').textContent = ''; try { const response = await mutateFetch('/api/admin/users/' + encodeURIComponent(id), {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({is_active:row.querySelector('.admin-active').checked})}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '管理员保存失败'); if (password) { const passwordResponse = await mutateFetch('/api/admin/users/' + encodeURIComponent(id) + '/password', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password})}); const passwordData = await passwordResponse.json(); if (!passwordResponse.ok) throw new Error(passwordData.message || '密码保存失败'); row.querySelector('.admin-password').value = ''; } $('adminNotice').className = 'notice success'; $('adminNotice').textContent = '管理员设置已保存'; await loadAdmins(); } catch (error) { $('adminNotice').className = 'notice'; $('adminNotice').textContent = error.message; } finally { button.disabled = false; } }
    async function createAdmin() { const button = $('createAdmin'); button.disabled = true; $('adminNotice').textContent = ''; try { const response = await mutateFetch('/api/admin/users', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:$('newAdminName').value.trim(),password:$('newAdminPassword').value})}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '管理员创建失败'); $('newAdminName').value = ''; $('newAdminPassword').value = ''; $('adminNotice').className = 'notice success'; $('adminNotice').textContent = '管理员已创建'; await loadAdmins(); } catch (error) { $('adminNotice').className = 'notice'; $('adminNotice').textContent = error.message; } finally { button.disabled = false; } }
    function openAdmin() { $('adminNotice').textContent = ''; $('adminDialog').showModal(); loadAdmins(); }
     $('themeSelect').addEventListener('change', event => saveTheme(event.target.value)); $('refreshButton').addEventListener('click', refresh); $('deleteButton').addEventListener('click', deleteSelected); $('refreshLogsButton').addEventListener('click', refreshLogs); $('newSessionButton').addEventListener('click', openNew); $('newCancel').addEventListener('click', () => $('newDialog').close()); $('newForm').addEventListener('submit', createNew); $('renameButton').addEventListener('click', openRename); $('renameCancel').addEventListener('click', () => $('renameDialog').close()); $('renameForm').addEventListener('submit', rename); $('startButton').addEventListener('click', () => operation('start')); $('stopButton').addEventListener('click', () => operation('stop')); $('restartButton').addEventListener('click', () => operation('restart')); $('ensureButton').addEventListener('click', () => operation('ensure')); $('qrButton').addEventListener('click', loadQr); $('pairingButton').addEventListener('click', pairing); $('updateButton').addEventListener('click', checkUpdates); $('updateRun').addEventListener('click', checkUpdates); $('updateClose').addEventListener('click', () => $('updateDialog').close()); $('adminButton').addEventListener('click', openAdmin); $('adminClose').addEventListener('click', () => $('adminDialog').close()); $('createAdmin').addEventListener('click', createAdmin);
    <!-- SPONSOR_SCRIPT -->
    const storedTheme = localStorage.getItem('waha-panel-theme'); if (storedTheme) applyTheme(storedTheme); refresh(); setInterval(refresh, 10000);
  </script>
</body>
</html>"""
    sponsor_slot, sponsor_dialog, sponsor_script = _sponsor_markup()
    return (
        page.replace("<!-- SPONSOR_SLOT -->", sponsor_slot)
        .replace("<!-- SPONSOR_DIALOG_SLOT -->", sponsor_dialog)
        .replace("    <!-- SPONSOR_SCRIPT -->", sponsor_script)
    )


def settings_page():
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>自动回复设置 · WAHA</title>
  <style>
    :root { color-scheme:light; --ink:#17212b; --muted:#62707c; --paper:#f5f7f8; --surface:#fff; --surface-2:#f2f5f7; --line:#dce2e6; --blue:#175cd3; --blue-bg:#e9f0ff; --good:#147d55; --good-bg:#e5f5ed; --bad:#b42318; --bad-bg:#ffebe9; }
    html[data-theme="night"] { color-scheme:dark; --ink:#edf5f7; --muted:#a8bac1; --paper:#10171c; --surface:#18232a; --surface-2:#223139; --line:#33464f; --blue:#62c8d4; --blue-bg:#19383e; --good:#62d7a0; --good-bg:#17382f; --bad:#ff9a91; --bad-bg:#472522; }
    html[data-theme="paper"] { --ink:#302c27; --muted:#756b60; --paper:#f3eee6; --surface:#fffaf3; --surface-2:#f2e8da; --line:#d8c8b5; --blue:#a34a2d; --blue-bg:#f4e0d6; --good:#2f7658; --good-bg:#e3f0e7; --bad:#a64236; --bad-bg:#f5dfd9; }
    * { box-sizing:border-box; } body { margin:0; min-width:320px; background:var(--paper); color:var(--ink); font:15px/1.5 "Segoe UI", system-ui, sans-serif; }
    .shell { max-width:1180px; margin:auto; padding:28px 24px 48px; } header { display:flex; justify-content:space-between; align-items:flex-start; gap:20px; border-bottom:1px solid var(--line); padding-bottom:22px; } h1 { margin:0 0 5px; font-size:32px; line-height:1.15; } header p,.muted { color:var(--muted); margin:0; } .eyebrow { color:var(--blue); font:600 12px/1.2 ui-monospace, Consolas, monospace; } .header-actions { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
    .theme-control { display:flex; align-items:center; gap:8px; min-height:44px; color:var(--muted); font-weight:700; } .theme-control select { min-height:44px; border:1px solid var(--line); border-radius:7px; padding:0 28px 0 10px; color:var(--ink); background:var(--surface); font:inherit; }
    .nav-link { display:inline-flex; align-items:center; min-height:44px; border:1px solid var(--line); border-radius:7px; padding:0 14px; color:var(--ink); background:var(--surface); font-weight:700; text-decoration:none; } .nav-link:hover { background:var(--surface-2); border-color:var(--blue); }
    main { padding-top:22px; } .grid { display:grid; grid-template-columns:minmax(0,1.15fr) minmax(320px,.85fr); gap:12px; align-items:start; } .panel { background:var(--surface); border:1px solid var(--line); border-radius:8px; padding:20px; } .panel + .panel { margin-top:12px; } h2 { font-size:18px; margin:0 0 4px; } h3 { font-size:15px; margin:22px 0 10px; } .panel-head { margin-bottom:16px; } .section-note { color:var(--muted); font-size:13px; }
    label { display:block; font-weight:700; margin:14px 0 6px; } input[type=text], input[type=password], input[type=url], input[type=tel], input[type=time], textarea { width:100%; border:1px solid var(--line); border-radius:6px; padding:10px 11px; color:var(--ink); background:var(--surface); font:inherit; } textarea { min-height:110px; resize:vertical; } input:focus, textarea:focus, select:focus { outline:3px solid var(--blue-bg); border-color:var(--blue); } .check-label { display:flex; align-items:center; gap:10px; min-height:44px; margin:0; } input[type=checkbox] { width:18px; height:18px; accent-color:var(--blue); }
    .schedule { display:grid; gap:8px; align-items:center; } .day-row { display:grid; grid-template-columns:68px 34px minmax(0,1fr) minmax(0,1fr); gap:8px; align-items:center; } .day-row label { margin:0; font-weight:600; } .day-row input[type=time]:disabled { opacity:.55; background:var(--surface-2); } .switch { display:flex; justify-content:space-between; align-items:center; border:1px solid var(--line); border-radius:7px; padding:10px 12px; background:var(--surface-2); } .switch + .switch { margin-top:8px; } .rule-note { margin:8px 0 0; color:var(--muted); font-size:13px; }
    .actions { display:flex; gap:8px; flex-wrap:wrap; margin-top:18px; } button { min-height:44px; border:1px solid var(--line); border-radius:7px; padding:0 15px; background:var(--surface); color:var(--ink); font:inherit; font-weight:700; cursor:pointer; } button:hover { background:var(--surface-2); border-color:var(--blue); } button:focus-visible { outline:3px solid var(--blue); outline-offset:2px; } button.primary { background:var(--blue); border-color:var(--blue); color:#fff; } button.primary:hover { filter:brightness(.9); } button.danger { color:var(--bad); } button:disabled { opacity:.6; cursor:wait; }
    .notice { min-height:24px; margin-top:10px; color:var(--bad); } .success { color:var(--good); } .status-chip { display:inline-flex; border-radius:999px; padding:4px 9px; background:var(--good-bg); color:var(--good); font-weight:700; font-size:13px; } .status-chip.off { background:var(--surface-2); color:var(--muted); }
    .knowledge-list { margin-top:12px; border-top:1px solid var(--line); } .knowledge-item { display:flex; align-items:center; justify-content:space-between; gap:12px; border-bottom:1px solid var(--line); padding:12px 0; } .knowledge-name { overflow-wrap:anywhere; font-weight:700; } .knowledge-meta { color:var(--muted); font-size:13px; } .knowledge-actions { display:flex; gap:6px; flex-shrink:0; } .preview { white-space:pre-wrap; overflow:auto; max-height:260px; padding:12px; background:var(--surface-2); border:1px solid var(--line); border-radius:6px; }
    @media (max-width:780px) { .shell { padding:20px 14px 36px; } header { display:block; } .header-actions { margin-top:16px; } .grid { grid-template-columns:1fr; } .day-row { grid-template-columns:58px 28px minmax(0,1fr) minmax(0,1fr); gap:6px; } }
    @media (prefers-reduced-motion:reduce) { *,*::before,*::after { transition-duration:.01ms!important; animation-duration:.01ms!important; } }
  </style>
</head>
<body>
  <div class="shell">
    <header><div><div class="eyebrow">AUTOMATION SETTINGS</div><h1>自动回复设置</h1><p>回复时间按 Asia/Shanghai 计算。</p></div><div class="header-actions"><label class="theme-control" for="themeSelect"><span>主题</span><select id="themeSelect" aria-label="选择主题"><option value="daylight">白天</option><option value="night">夜间</option><option value="paper">纸张</option></select></label><a class="nav-link" id="backLink" href="/">返回状态面板</a></div></header>
    <main>
      <form id="settingsForm" class="grid">
        <section>
          <div class="panel">
            <div class="panel-head"><h2>回复规则</h2><div class="section-note">只处理别人发来的私聊文字消息。</div></div>
            <div class="switch"><label class="check-label" for="allDay"><input id="allDay" type="checkbox">全局总开关：全时段自动回复</label><span id="allDayState" class="status-chip off">已关闭</span></div>
            <div class="switch"><label class="check-label" for="enabled"><input id="enabled" type="checkbox">按时间段自动回复</label><span id="enabledState" class="status-chip off">已关闭</span></div>
            <h3>每周回复时间段</h3><div class="section-note">开启上面的全局总开关时全天回复；否则仅在启用的时间段内回复。</div><div class="schedule" id="schedule"></div><p class="rule-note">结束时间早于开始时间表示跨天，例如 20:00 → 11:00，覆盖当日晚上及次日凌晨。</p>
            <label for="defaultText">默认固定回复文案</label><textarea id="defaultText" maxlength="4000"></textarea>
            <label for="persona">人设</label><textarea id="persona" maxlength="4000"></textarea>
            <label for="systemPrompt">系统提示词</label><textarea id="systemPrompt" maxlength="8000"></textarea>
          </div>
          <div class="panel">
            <div class="panel-head"><h2>知识库</h2><div class="section-note">支持粘贴文字或上传 .txt、.md。</div></div>
            <label for="knowledgeName">资料名称</label><input id="knowledgeName" type="text" maxlength="255" value="粘贴文字">
            <label for="knowledgeText">文字内容</label><textarea id="knowledgeText" maxlength="2000000"></textarea>
            <div class="actions"><button class="primary" type="button" id="addKnowledge">保存文字资料</button><label class="nav-link" for="knowledgeFile">上传 .txt / .md</label><input id="knowledgeFile" type="file" accept=".txt,.md,text/plain,text/markdown" hidden></div>
            <div id="knowledgeNotice" class="notice" role="status" aria-live="polite"></div><div class="knowledge-list" id="knowledgeList"><div class="muted">正在读取资料...</div></div><pre id="knowledgePreview" class="preview" hidden></pre>
          </div>
        </section>
        <section>
          <div class="panel">
            <div class="panel-head"><h2>AI 模型设置</h2><div class="section-note">APIKey 仅保存在服务端，不会回显。</div></div>
            <label for="aiBaseUrl">服务地址</label><input id="aiBaseUrl" type="url" placeholder="https://example.com/v1">
            <label for="aiModel">模型名</label><input id="aiModel" type="text" placeholder="model-name">
            <label for="aiKey">APIKey</label><input id="aiKey" type="password" autocomplete="new-password" placeholder="留空表示保持当前 Key">
            <label class="check-label" for="clearKey"><input id="clearKey" type="checkbox">清除已保存的 APIKey</label>
            <div id="keyState" class="section-note">正在读取 Key 状态...</div>
          </div>
          <div class="panel"><h2>保存设置</h2><div class="section-note">关闭总开关或不在时间段内时不会回复；未配置完整 AI 模型时使用固定文案。</div><div class="actions"><button class="primary" type="submit" id="saveButton">保存全部设置</button></div><div id="settingsNotice" class="notice" role="status" aria-live="polite"></div></div>
        </section>
      </form>
    </main>
  </div>
  <script>
    const $ = (id) => document.getElementById(id);
    const sessionName = new URLSearchParams(location.search).get('session') || 'default';
    const sessionQuery = '?session=' + encodeURIComponent(sessionName);
    let csrfToken = '';
    const days = [['mon','周一'],['tue','周二'],['wed','周三'],['thu','周四'],['fri','周五'],['sat','周六'],['sun','周日']];
    document.getElementById('backLink').href = '/?session=' + encodeURIComponent(sessionName);
    const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    function renderSchedule(windows) { $('schedule').innerHTML = days.map(([key,label]) => { const item = (windows[key] || [])[0] || {start:'09:00',end:'18:00'}; const active = Boolean((windows[key] || []).length); return `<div class="day-row"><label for="${key}Enabled">${label}</label><input id="${key}Enabled" type="checkbox" ${active ? 'checked' : ''} aria-label="启用${label}"><input id="${key}Start" type="time" value="${esc(item.start)}" ${active ? '' : 'disabled'} aria-label="${label}开始时间"><input id="${key}End" type="time" value="${esc(item.end)}" ${active ? '' : 'disabled'} aria-label="${label}结束时间"></div>`; }).join(''); days.forEach(([key]) => $(key+'Enabled').addEventListener('change', () => { const on = $(key+'Enabled').checked; $(key+'Start').disabled = !on; $(key+'End').disabled = !on; })); }
    function collectWindows() { const result = {}; days.forEach(([key]) => { if ($(key+'Enabled').checked) result[key] = [{start:$(key+'Start').value,end:$(key+'End').value}]; }); return result; }
    function setChip(id, checked) { $(id).textContent = checked ? '已开启' : '已关闭'; $(id).className = 'status-chip' + (checked ? '' : ' off'); }
    function setEnabledState() { setChip('enabledState', $('enabled').checked); setChip('allDayState', $('allDay').checked); }
    function applyTheme(theme) { const selected = ['daylight','night','paper'].includes(theme) ? theme : 'daylight'; document.documentElement.dataset.theme = selected; $('themeSelect').value = selected; localStorage.setItem('waha-panel-theme', selected); }
    async function ensureCsrf() { if (csrfToken) return csrfToken; const response = await fetch('/api/security/csrf', {cache:'no-store', credentials:'same-origin'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '安全校验不可用'); csrfToken = data.csrf_token || ''; return csrfToken; }
    async function mutateFetch(url, options={}) { const headers = new Headers(options.headers || {}); try { await ensureCsrf(); } catch (_) {} if (csrfToken) headers.set('X-CSRF-Token', csrfToken); return fetch(url, {...options, headers, credentials:'same-origin', cache:'no-store'}); }
    async function saveTheme(theme) { applyTheme(theme); try { const response = await mutateFetch('/api/settings' + sessionQuery, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({theme})}); if (!response.ok) { const data = await response.json(); throw new Error(data.message || '主题保存失败'); } } catch (error) { $('settingsNotice').textContent = error.message; } }
    async function loadSettings() { const response = await fetch('/api/settings' + sessionQuery, {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '读取设置失败'); $('enabled').checked = data.auto_reply_enabled; $('allDay').checked = data.auto_reply_all_day; setEnabledState(); renderSchedule(data.weekly_reply_windows); $('defaultText').value = data.default_reply_text || ''; $('persona').value = data.persona || ''; $('systemPrompt').value = data.system_prompt || ''; $('aiBaseUrl').value = data.ai_base_url || ''; $('aiModel').value = data.ai_model || ''; $('keyState').textContent = data.ai_api_key_configured ? 'APIKey：已配置（隐藏）' : 'APIKey：未配置'; applyTheme(data.theme); }
    async function saveSettings(event) { event.preventDefault(); const button = $('saveButton'); button.disabled = true; $('settingsNotice').textContent = ''; try { const body = {auto_reply_enabled:$('enabled').checked,auto_reply_all_day:$('allDay').checked,weekly_reply_windows:collectWindows(),default_reply_text:$('defaultText').value,persona:$('persona').value,system_prompt:$('systemPrompt').value,ai_base_url:$('aiBaseUrl').value,ai_model:$('aiModel').value,ai_api_key:$('aiKey').value,clear_ai_api_key:$('clearKey').checked,theme:$('themeSelect').value}; const response = await mutateFetch('/api/settings' + sessionQuery, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '保存失败'); $('aiKey').value = ''; $('clearKey').checked = false; $('keyState').textContent = data.ai_api_key_configured ? 'APIKey：已配置（隐藏）' : 'APIKey：未配置'; $('settingsNotice').className = 'notice success'; $('settingsNotice').textContent = '设置已保存'; } catch (error) { $('settingsNotice').className = 'notice'; $('settingsNotice').textContent = error.message; } finally { button.disabled = false; } }
    async function addKnowledge(name, content) { $('knowledgeNotice').textContent = ''; const response = await mutateFetch('/api/knowledge' + sessionQuery, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({file_name:name,content})}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '资料保存失败'); $('knowledgeText').value = ''; await loadKnowledge(); }
    async function loadKnowledge() { const response = await fetch('/api/knowledge' + sessionQuery, {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '资料读取失败'); $('knowledgeList').innerHTML = data.items.length ? data.items.map(item => `<div class="knowledge-item"><div><div class="knowledge-name">${esc(item.file_name)}</div><div class="knowledge-meta">${item.characters} 字符 · ${new Date(item.created_at * 1000).toLocaleString()}</div></div><div class="knowledge-actions"><button type="button" data-view="${item.id}">查看</button><button type="button" class="danger" data-delete="${item.id}">删除</button></div></div>`).join('') : '<div class="muted">暂无资料</div>'; document.querySelectorAll('[data-view]').forEach(button => button.addEventListener('click', () => viewKnowledge(button.dataset.view))); document.querySelectorAll('[data-delete]').forEach(button => button.addEventListener('click', () => deleteKnowledge(button.dataset.delete))); }
    async function viewKnowledge(id) { const response = await fetch('/api/knowledge/' + id + sessionQuery, {cache:'no-store'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '资料读取失败'); $('knowledgePreview').hidden = false; $('knowledgePreview').textContent = data.content; }
    async function deleteKnowledge(id) { if (!window.confirm('确认删除这份资料？')) return; const response = await mutateFetch('/api/knowledge/' + id + sessionQuery, {method:'DELETE'}); const data = await response.json(); if (!response.ok) throw new Error(data.message || '删除失败'); $('knowledgePreview').hidden = true; await loadKnowledge(); }
    const storedTheme = localStorage.getItem('waha-panel-theme'); if (storedTheme) applyTheme(storedTheme); $('themeSelect').addEventListener('change', (event) => saveTheme(event.target.value)); $('enabled').addEventListener('change', setEnabledState); $('allDay').addEventListener('change', setEnabledState); $('settingsForm').addEventListener('submit', saveSettings); $('addKnowledge').addEventListener('click', async () => { try { await addKnowledge($('knowledgeName').value || '粘贴文字', $('knowledgeText').value); } catch (error) { $('knowledgeNotice').textContent = error.message; } }); $('knowledgeFile').addEventListener('change', () => { const file = $('knowledgeFile').files[0]; if (!file) return; if (!/[.]txt$|[.]md$/i.test(file.name)) { $('knowledgeNotice').textContent = '仅支持 .txt 或 .md 文件'; return; } const reader = new FileReader(); reader.onload = async () => { try { await addKnowledge(file.name, reader.result); $('knowledgeFile').value = ''; } catch (error) { $('knowledgeNotice').textContent = error.message; } }; reader.readAsText(file); });
    loadSettings().catch(error => { $('settingsNotice').textContent = error.message; }); loadKnowledge().catch(error => { $('knowledgeNotice').textContent = error.message; });
  </script>
</body>
</html>"""


class PanelHandler(BaseHTTPRequestHandler):
    state = None

    def log_message(self, format_string, *args):
        return

    def send_json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_body(self, limit=2 * 1024 * 1024):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("请求长度不正确") from error
        if length > limit:
            raise ValueError("请求内容超过 2 MB 限制")
        return self.rfile.read(length)

    def read_json(self):
        try:
            return json.loads(self.read_body().decode("utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("请求 JSON 格式不正确") from error

    def read_api_json(self):
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            raise UnsupportedRequestMedia("请求必须使用 application/json")
        value = self.read_json()
        if not isinstance(value, dict):
            raise ValueError("请求 JSON 必须是对象")
        return value

    def read_image_form(self):
        maximum = 11 * 1024 * 1024
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("请求长度不正确") from error
        if length > maximum:
            raise RequestTooLarge("图片上传请求不能超过 11MB")
        content_type = self.headers.get("Content-Type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise UnsupportedRequestMedia("图片发送必须使用 multipart/form-data")
        body = self.rfile.read(length)
        try:
            envelope = BytesParser(policy=email_policy).parsebytes(
                ("Content-Type: " + content_type + "\r\nMIME-Version: 1.0\r\n\r\n").encode("utf-8")
                + body
            )
        except Exception as error:
            raise ValueError("图片上传表单格式不正确") from error
        if not envelope.is_multipart():
            raise ValueError("图片上传表单格式不正确")
        allowed = {"chat_ref", "client_request_id", "caption", "image"}
        values = {}
        for part in envelope.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if not name or name not in allowed:
                raise ValueError("图片上传表单包含不支持的字段")
            if name in values:
                raise ValueError("图片上传表单字段不能重复")
            if part.is_multipart():
                raise ValueError("图片上传表单不支持嵌套内容")
            raw = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if name == "image":
                if not filename:
                    raise ValueError("请选择图片文件")
                values[name] = {
                    "filename": filename,
                    "content_type": part.get_content_type(),
                    "data": raw,
                }
            else:
                if filename:
                    raise ValueError("文字字段不能作为文件上传")
                try:
                    values[name] = raw.decode(part.get_content_charset() or "utf-8")
                except (LookupError, UnicodeError) as error:
                    raise ValueError("图片上传文字字段编码无效") from error
        if not all(values.get(name) for name in ("chat_ref", "client_request_id", "image")):
            raise ValueError("图片上传缺少必要字段")
        values.setdefault("caption", "")
        return values

    def require_admin_auth(self):
        """Require panel credentials for all chat and translation surfaces."""
        if getattr(self.state, "_admin_db_auth", False):
            authorization = self.headers.get("Authorization", "")
            supplied_user = supplied_password = ""
            if authorization.startswith("Basic "):
                try:
                    decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                    supplied_user, supplied_password = decoded.split(":", 1)
                except (ValueError, UnicodeDecodeError):
                    pass
            client_key = self.client_address[0] if self.client_address else ""
            if self.state.authenticate_admin(supplied_user, supplied_password, client_key):
                return
            raise AdminAuthError("需要管理员身份验证")
        username = self.state.admin_username
        password = self.state.admin_password
        if not (username and password):
            self.require_local_admin()
            return
        authorization = self.headers.get("Authorization", "")
        supplied_user = supplied_password = ""
        if authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                supplied_user, supplied_password = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                pass
        if not (
            hmac.compare_digest(supplied_user, username)
            and hmac.compare_digest(supplied_password, password)
        ):
            raise AdminAuthError("需要管理员身份验证")

    def require_csrf(self):
        self.require_admin_auth()
        expected = str(self.state.csrf_token or "")
        supplied = self.headers.get("X-CSRF-Token", "")
        if not expected or not supplied or not hmac.compare_digest(supplied, expected):
            raise PermissionError("安全校验失败，请刷新页面后重试")
        origin = self.headers.get("Origin")
        if not origin:
            return
        scheme = (self.headers.get("X-Forwarded-Proto") or "http").split(",", 1)[0].strip().lower()
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").split(",", 1)[0].strip().lower()
        try:
            parsed = urlparse(origin)
            supplied_origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        except ValueError as error:
            raise PermissionError("请求来源无效") from error
        expected_origin = f"{scheme}://{host}"
        if (
            parsed.path not in {"", "/"}
            or parsed.params
            or parsed.query
            or parsed.fragment
            or not hmac.compare_digest(supplied_origin, expected_origin)
        ):
            raise PermissionError("请求来源与当前面板不一致")

    def require_mutation_auth(self):
        """Require an administrator and CSRF token before state changes."""
        if getattr(self.state, "_admin_db_auth", False):
            self.require_csrf()
        else:
            self.require_admin_auth()

    def require_chat_service(self):
        if self.state.chat is None:
            raise ChatServiceError(
                self.state.chat_capability_error or "聊天管理功能不可用",
                "CHAT_CAPABILITY_UNAVAILABLE",
            )
        return self.state.chat

    def require_translation_service(self):
        if self.state.translation is None:
            raise TranslationError("TRANSLATION_UNAVAILABLE", "翻译功能暂不可用，请检查面板加密密钥")
        return self.state.translation

    def require_waha_session(self, session_name, connected=False):
        name = normalize_session_name(session_name)
        sessions = self.state.client.get_sessions()
        record = session_record(sessions, name)
        if record is None:
            raise ChatAccessError("WAHA 中不存在该会话", "SESSION_NOT_FOUND")
        state = session_state_for(sessions, name)
        if connected and not classify_session_state(state):
            raise ChatServiceError("当前 WhatsApp 会话未连接", "SESSION_NOT_CONNECTED")
        return name

    @staticmethod
    def chat_route(route):
        prefix = "/api/chat/sessions/"
        if not route.startswith(prefix):
            return None, None
        parts = route[len(prefix):].split("/")
        if len(parts) < 2 or not parts[0] or not parts[1]:
            raise ValueError("聊天接口路径不正确")
        decoded = unquote(parts[0])
        if "%" in decoded:
            raise ValueError("会话名称编码无效")
        name = normalize_session_name(decoded)
        action = "/".join(parts[1:])
        allowed = {
            "overview", "messages", "media", "events",
            "send-text", "send-image", "takeover", "resume-ai", "note",
            "follow-ups", "labels", "summary",
        }
        if action not in allowed and not (
            len(parts) == 4 and parts[1] == "follow-ups" and parts[2] and parts[3] == "cancel"
        ) and not (
            len(parts) == 3 and parts[1] == "labels" and parts[2].isascii() and parts[2].isdigit()
        ):
            raise ValueError("不支持的聊天操作")
        return name, action

    @staticmethod
    def chat_page_session(route):
        prefix = "/sessions/"
        suffix = "/chats"
        if not route.startswith(prefix) or not route.endswith(suffix):
            return None
        encoded = route[len(prefix):-len(suffix)]
        if not encoded or "/" in encoded:
            raise ValueError("聊天页面路径不正确")
        decoded = unquote(encoded)
        if "%" in decoded:
            raise ValueError("会话名称编码无效")
        return normalize_session_name(decoded)

    def send_media(self, status, content_type, body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_event_stream_body(self, body):
        try:
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return False

    def send_chat_get(self, route):
        self.require_admin_auth()
        chat = self.require_chat_service()
        name, action = self.chat_route(route)
        query = parse_qs(urlparse(self.path).query)
        self.require_waha_session(name)
        chat_ref = query.get("chat_ref", [""])[0]
        if action == "follow-ups":
            if not chat_ref:
                raise ValueError("缺少聊天引用")
            if self.state.automation is None:
                raise ChatServiceError("跟进功能暂不可用", "AUTOMATION_UNAVAILABLE")
            self.send_json(self.state.automation.list_tasks(name, chat_ref))
            return
        if action == "labels":
            if not chat_ref:
                raise ValueError("缺少聊天引用")
            self.send_json(chat.customer_label_records(name, chat_ref))
            return
        if action == "summary":
            if not chat_ref:
                raise ValueError("缺少聊天引用")
            self.send_json(chat.current_summary(name, chat_ref))
            return
        if action == "overview":
            self.send_json(chat.overview(
                name,
                query.get("limit", [30])[0],
                query.get("offset", [0])[0],
                query.get("search", [""])[0],
                as_bool(query.get("unread_only", [False])[0]),
            ))
            return
        if action == "messages":
            chat_ref = query.get("chat_ref", [""])[0]
            if not chat_ref:
                raise ValueError("缺少聊天引用")
            self.send_json(chat.messages(
                name,
                chat_ref,
                query.get("limit", [50])[0],
                query.get("offset", [0])[0],
                query.get("before", [None])[0],
            ))
            return
        if action == "media":
            chat_ref = query.get("chat_ref", [""])[0]
            message_ref = query.get("message_ref", [""])[0]
            if not chat_ref or not message_ref:
                raise ValueError("缺少聊天或消息引用")
            self.send_media(*chat.media(name, chat_ref, message_ref))
            return
        if action == "events":
            after = self.headers.get("Last-Event-ID") or query.get("after", [0])[0]
            timeout = query.get("timeout", [25])[0]
            latest, events = chat.broker.wait(name, int(after or 0), min(25, max(0, float(timeout))))
            chunks = []
            for item in events:
                chunks.append(
                    f"id: {item['event_id']}\nevent: message\ndata: "
                    + json.dumps(item, ensure_ascii=False, separators=(",", ":"))
                    + "\n\n"
                )
            if not chunks:
                chunks.append(
                    f"id: {latest}\nevent: heartbeat\ndata: "
                    + json.dumps({"event_id": latest}, separators=(",", ":"))
                    + "\n\n"
                )
            body = "".join(chunks).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.write_event_stream_body(body)
            return
        if action == "note":
            chat_ref = query.get("chat_ref", [""])[0]
            if not chat_ref:
                raise ValueError("缺少聊天引用")
            self.send_json(chat.note(name, chat_ref))
            return
        raise ValueError("不支持的聊天查询操作")

    def send_chat_post(self, route):
        self.require_csrf()
        chat = self.require_chat_service()
        name, action = self.chat_route(route)
        self.require_waha_session(name, connected=action in {"send-text", "send-image"})
        if action == "labels":
            self.send_chat_labels_write(route, "POST")
            return
        if action == "follow-ups":
            if self.state.automation is None:
                raise ChatServiceError("跟进功能暂不可用", "AUTOMATION_UNAVAILABLE")
            payload = self.read_api_json()
            result = self.state.automation.create_task(
                name,
                payload.get("chat_ref"),
                payload.get("delay_code"),
                payload.get("mode"),
                payload.get("fixed_copy"),
            )
            self.send_json(result, HTTPStatus.CREATED)
            return
        if action.startswith("follow-ups/") and action.endswith("/cancel"):
            if self.state.automation is None:
                raise ChatServiceError("跟进功能暂不可用", "AUTOMATION_UNAVAILABLE")
            task_id = action[len("follow-ups/"):-len("/cancel")]
            if not task_id:
                raise ValueError("跟进任务编号无效")
            self.send_json(self.state.automation.cancel_task(name, task_id))
            return
        if action == "summary":
            payload = self.read_api_json()
            chat_ref = payload.get("chat_ref")
            if not chat_ref:
                raise ValueError("缺少聊天引用")
            translation = self.require_translation_service()
            history = chat.messages(name, chat_ref, limit=20).get("items", [])
            self.state.begin_translation_request()
            try:
                generated = translation.summarize_conversation(history)
            finally:
                self.state.end_translation_request()
            if not isinstance(generated, dict):
                raise TranslationError("SUMMARY_RESULT_INVALID", "对话总结格式无效")
            ai_labels = generated.get("ai_labels", [])
            summary = dict(generated)
            summary.pop("ai_labels", None)
            self.send_json(chat.save_summary_and_ai_labels(name, chat_ref, summary, ai_labels))
            return
        if action == "send-image":
            form = self.read_image_form()
            image = form["image"]
            result = chat.send_image(
                name, form["chat_ref"], image["filename"], image["content_type"],
                image["data"], form["caption"], form["client_request_id"],
            )
        else:
            payload = self.read_api_json()
            chat_ref = payload.get("chat_ref")
            if action == "send-text":
                result = chat.send_text(
                    name, chat_ref, payload.get("text"), payload.get("client_request_id")
                )
            elif action == "takeover":
                result = chat.takeover(name, chat_ref)
            elif action == "resume-ai":
                result = chat.resume_ai(name, chat_ref)
            elif action == "note":
                result = chat.save_note(name, chat_ref, payload.get("note"))
            else:
                raise ValueError("不支持的聊天写入操作")
        chat.broker.publish(name, {"type": "refresh", "reason": action})
        self.state.log("INFO", "chat." + action, "聊天管理操作已完成", name)
        self.send_json(result)

    def send_chat_labels_write(self, route, method):
        self.require_csrf()
        chat = self.require_chat_service()
        name, action = self.chat_route(route)
        self.require_waha_session(name)
        if not action.startswith("labels"):
            raise ValueError("不支持的标签操作")
        query = parse_qs(urlparse(self.path).query)
        chat_ref = query.get("chat_ref", [""])[0]
        payload = {}
        if method != "DELETE" or int(self.headers.get("Content-Length", "0") or 0) > 0:
            payload = self.read_api_json()
        if not chat_ref:
            chat_ref = payload.get("chat_ref")
        if not chat_ref:
            raise ValueError("缺少聊天引用")
        if method == "POST" and action == "labels":
            chat.add_manual_label(name, chat_ref, payload.get("label"), payload.get("source", "MANUAL"))
            self.send_json(chat.customer_label_records(name, chat_ref), HTTPStatus.CREATED)
            return
        if method == "PUT" and action.startswith("labels/"):
            label_id = unquote(action[len("labels/"):])
            self.send_json(chat.update_manual_label_by_id(name, chat_ref, label_id, payload.get("label", payload.get("new_label")), payload.get("source", "MANUAL")))
            return
        if method == "DELETE" and action.startswith("labels/"):
            label_id = unquote(action[len("labels/"):])
            self.send_json(chat.delete_manual_label_by_id(name, chat_ref, label_id, payload.get("source", "MANUAL")))
            return
        raise ValueError("不支持的标签操作")

    def send_translation_get(self, route):
        self.require_admin_auth()
        translation = self.require_translation_service()
        if route != "/api/translation/settings":
            raise ValueError("不支持的翻译查询操作")
        self.send_json(translation.settings_payload())

    def send_translation_write(self, route, method):
        self.require_csrf()
        translation = self.require_translation_service()
        if route == "/api/translation/settings" and method == "PUT":
            self.send_json(translation.save_settings(self.read_api_json()))
            return
        if route == "/api/translation/settings/test" and method == "POST":
            self.state.begin_translation_request()
            try:
                self.send_json(translation.test_connection())
            finally:
                self.state.end_translation_request()
            return
        if route == "/api/translation/translate" and method == "POST":
            payload = self.read_api_json()
            self.state.begin_translation_request()
            try:
                self.send_json(translation.translate(
                    payload.get("text"), force=as_bool(payload.get("force", False))
                ))
            finally:
                self.state.end_translation_request()
            return
        if route == "/api/translation/compose" and method == "POST":
            payload = self.read_api_json()
            name = self.require_waha_session(payload.get("session"))
            chat = self.require_chat_service()
            history = chat.messages(name, payload.get("chat_ref"), limit=20).get("items", [])
            settings = self.state._settings(name)
            knowledge = [{
                "file_name": "会话知识库",
                "content": self.state._knowledge_context(name),
            }]
            self.state.begin_translation_request()
            try:
                self.send_json(translation.optimize_and_translate(
                    payload.get("draft"),
                    history,
                    settings.get("persona", ""),
                    settings.get("system_prompt", ""),
                    knowledge,
                    payload.get("target_language"),
                ))
            finally:
                self.state.end_translation_request()
            return
        if route == "/api/translation/suggest-reply" and method == "POST":
            payload = self.read_api_json()
            name = self.require_waha_session(payload.get("session"))
            chat = self.require_chat_service()
            history = chat.messages(name, payload.get("chat_ref"), limit=20).get("items", [])
            settings = self.state._settings(name)
            knowledge = [{"file_name": "会话知识库", "content": self.state._knowledge_context(name)}]
            self.state.begin_translation_request()
            try:
                self.send_json(translation.suggest_reply(
                    history,
                    settings.get("persona", ""),
                    settings.get("system_prompt", ""),
                    knowledge,
                    payload.get("target_language"),
                ))
            finally:
                self.state.end_translation_request()
            return
        if route == "/api/translation/cache" and method == "DELETE":
            self.send_json(translation.clear_cache())
            return
        raise ValueError("不支持的翻译操作")

    def send_service_error(self, error):
        code = getattr(error, "code", "SERVICE_ERROR")
        status = HTTPStatus.BAD_REQUEST
        if isinstance(error, SendConflictError):
            status = HTTPStatus.CONFLICT
        elif isinstance(error, RequestTooLarge):
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
        elif isinstance(error, UnsupportedRequestMedia):
            status = HTTPStatus.UNSUPPORTED_MEDIA_TYPE
        elif isinstance(error, ImageValidationError):
            status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE if code == "IMAGE_TOO_LARGE" else HTTPStatus.UNPROCESSABLE_ENTITY
        elif isinstance(error, ChatAccessError):
            status = HTTPStatus.FORBIDDEN
        elif isinstance(error, AdminNotFoundError):
            status = HTTPStatus.NOT_FOUND
        elif isinstance(error, (AdminConflictError, LastActiveAdministratorError)):
            status = HTTPStatus.CONFLICT
        elif code in {"CHAT_CAPABILITY_UNAVAILABLE", "TRANSLATION_UNAVAILABLE"}:
            status = HTTPStatus.SERVICE_UNAVAILABLE
        elif code in {"SESSION_NOT_CONNECTED", "TRANSLATION_NOT_CONFIGURED"}:
            status = HTTPStatus.CONFLICT
        elif code in {"AI_RATE_LIMIT", "AI_BUSY"}:
            status = HTTPStatus.TOO_MANY_REQUESTS
        elif code in {"AI_CONNECTION_ERROR", "AI_UPSTREAM_ERROR", "AI_RESPONSE_INVALID"}:
            status = HTTPStatus.BAD_GATEWAY
        payload = {"code": code, "message": getattr(error, "public_message", str(error))}
        if isinstance(error, ChatSendError):
            payload["state"] = error.state
        self.send_json(payload, status)

    def require_local_admin(self):
        """Allow loopback callers or an authenticated reverse-proxy request."""
        if getattr(self.state, "_admin_db_auth", False):
            self.require_admin_auth()
            return
        try:
            address = ip_address(self.client_address[0])
        except ValueError as error:
            raise PermissionError("业务管理接口仅允许本机访问") from error
        forwarded = self.headers.get("X-Real-IP") or self.headers.get("X-Forwarded-For")
        if address.is_loopback and not forwarded:
            return
        username = self.state.admin_username
        password = self.state.admin_password
        authorization = self.headers.get("Authorization", "")
        if username and password and authorization.startswith("Basic "):
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                supplied_user, supplied_password = decoded.split(":", 1)
            except (ValueError, UnicodeDecodeError):
                supplied_user = supplied_password = ""
            if hmac.compare_digest(supplied_user, username) and hmac.compare_digest(supplied_password, password):
                return
        if username and password:
            raise AdminAuthError("需要管理员身份验证")
        raise PermissionError("业务管理接口仅允许本机访问")

    def send_admin_auth_required(self, error):
        body = json.dumps({"message": str(error)}, ensure_ascii=False).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="WAHA Commerce", charset="UTF-8"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    @staticmethod
    def admin_route(route):
        prefix = "/api/admin/users"
        if route == prefix:
            return None
        if not route.startswith(prefix + "/"):
            raise ValueError("管理员接口路径不正确")
        suffix = route[len(prefix) + 1:]
        parts = suffix.split("/")
        if len(parts) not in {1, 2} or not parts[0].isdigit():
            raise ValueError("管理员接口路径不正确")
        if len(parts) == 2 and parts[1] != "password":
            raise ValueError("管理员接口路径不正确")
        return int(parts[0]), parts[1] if len(parts) == 2 else None

    def send_admin_users_get(self, route):
        self.require_admin_auth()
        if self.admin_route(route) is not None:
            raise ValueError("管理员查询路径不正确")
        self.send_json(self.state.admin_users_payload())

    def send_admin_users_write(self, route, method):
        self.require_csrf()
        target, action = self.admin_route(route)
        if target is None:
            if method != "POST":
                raise ValueError("管理员写入方法不正确")
            self.send_json(self.state.create_admin_user(self.read_api_json()), HTTPStatus.CREATED)
            return
        if action == "password" and method == "POST":
            self.send_json(self.state.change_admin_password(target, self.read_api_json()))
            return
        if action is None and method == "PATCH":
            self.send_json(self.state.update_admin_user(target, self.read_api_json()))
            return
        raise ValueError("管理员写入路径或方法不正确")

    def send_update_check(self):
        self.require_admin_auth()
        self.send_json(self.state.check_updates())

    @staticmethod
    def business_route(route):
        prefix = "/api/admin/ai-business"
        if route == prefix:
            return None, None
        if not route.startswith(prefix + "/"):
            return None, None
        parts = route[len(prefix) + 1:].split("/")
        if len(parts) > 2 or not parts[0]:
            raise ValueError("业务管理接口路径不正确")
        record_id = int(parts[1]) if len(parts) == 2 else None
        if parts[0] not in {"settings", "products", "coupons", "payments", "shipping", "context"}:
            raise ValueError("不支持的业务资源")
        return parts[0], record_id

    def send_business_get(self, route):
        self.require_local_admin()
        resource, record_id = self.business_route(route)
        if resource == "context":
            if record_id is not None:
                raise ValueError("context 不支持记录编号")
            self.send_json(self.state.business.get_business_context())
            return
        if resource is None or record_id is not None:
            raise ValueError("业务管理查询路径不正确")
        self.send_json({"items": self.state.business.list_records(resource)})

    def send_business_write(self, route, method):
        self.require_local_admin()
        resource, record_id = self.business_route(route)
        if resource in {None, "context"}:
            raise ValueError("业务管理写入路径不正确")
        result = self.state.business.save_record(resource, self.read_json(), record_id)
        self.state.log("INFO", "business.save", f"已保存业务资源 {resource}")
        status = HTTPStatus.CREATED if record_id is None and method == "POST" else HTTPStatus.OK
        self.send_json(result, status)

    def send_business_delete(self, route):
        self.require_local_admin()
        resource, record_id = self.business_route(route)
        if resource in {None, "context"} or record_id is None:
            raise ValueError("业务管理删除路径不正确")
        self.state.business.delete_record(resource, record_id)
        self.state.log("INFO", "business.delete", f"已删除业务资源 {resource} #{record_id}")
        self.send_json({"deleted": True})

    def send_commerce_get(self, route):
        self.require_local_admin()
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if route == "/api/commerce/status":
            self.send_json(self.state.commerce_status())
        elif route == "/api/commerce/config":
            self.send_json(self.state.commerce_config_payload())
        elif route == "/api/commerce/tickets":
            self.send_json(self.state.commerce_cases(query.get("status", ["open"])[0]))
        elif route.startswith("/api/commerce/tickets/"):
            parts = route.split("/")
            if len(parts) != 5:
                raise ValueError("工单路径不正确")
            self.send_json(self.state.commerce_case(int(parts[4])))
        elif route == "/api/commerce/email-locks":
            self.send_json(self.state.commerce_locks())
        elif route == "/api/commerce/audit":
            self.send_json(self.state.commerce_audit(query.get("limit", [50])[0]))
        else:
            raise ValueError("订单接口路径不正确")

    def send_commerce_post(self, route):
        self.require_local_admin()
        if route == "/api/commerce/config":
            self.send_json(self.state.save_commerce_config(self.read_json()))
            return
        if route == "/api/commerce/test-connection":
            self.send_json(self.state.commerce_test_connection())
            return
        parts = route.split("/")
        if len(parts) != 6 or parts[1:3] != ["api", "commerce"] or parts[3] != "tickets":
            if len(parts) == 6 and parts[1:3] == ["api", "commerce"] and parts[3] == "email-locks" and parts[5] == "unlock":
                ok = self.state.commerce.verification.unlock_email_verification(int(parts[4]), "local-admin")
                self.send_json({"unlocked": ok})
                return
            raise ValueError("订单接口路径不正确")
        case_id = int(parts[4])
        action = parts[5]
        if action == "recheck":
            self.send_json(self.state.commerce_recheck(case_id))
            return
        if action == "reply":
            result = self.state.commerce.cases.save_admin_result(case_id, self.read_json().get("message"), close=False)
            self.send_json({"ticket": result})
            return
        if action == "confirm":
            result = self.state.commerce.cases.confirm_and_reply(case_id)
            self.send_json(result)
            return
        raise ValueError("不支持的工单操作")

    @staticmethod
    def session_route(route):
        """Parse /api/sessions[/name[/action...]] without accepting path tricks."""
        prefix = "/api/sessions"
        if route == prefix:
            return None, None
        if not route.startswith(prefix + "/"):
            return None, None
        parts = route[len(prefix) + 1:].split("/")
        if not parts or len(parts) > 3 or not parts[0]:
            raise ValueError("会话接口路径不正确")
        name = normalize_session_name(unquote(parts[0]))
        action = parts[1] if len(parts) > 1 and parts[1] else None
        if len(parts) == 3 and action != "settings":
            raise ValueError("会话接口路径不正确")
        return name, action

    def send_session_get(self, route):
        self.require_admin_auth()
        name, action = self.session_route(route)
        if name is None:
            items = self.state.sessions_payload()
            self.send_json({"sessions": items, "items": items})
            return
        if action == "settings":
            self.send_json(self.state.settings_payload(name))
            return
        if action == "logs":
            self.send_json({"logs": self.state.recent_logs(session_name=name)})
            return
        if action == "qr":
            status, content_type, body = self.state.get_qr(name)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if action is not None:
            raise ValueError("不支持的会话查询操作")
        self.send_json(self.state.session_detail(name))

    def send_session_post(self, route):
        self.require_mutation_auth()
        name, action = self.session_route(route)
        if name is None:
            payload = self.read_json()
            name = payload.get("name") or payload.get("session_name")
            result = self.state.create_session(name, payload.get("display_name"), as_bool(payload.get("start")))
            self.send_json(result, HTTPStatus.CREATED)
            return
        if action == "start":
            self.send_json(self.state.start_session(name))
            return
        if action == "stop":
            self.send_json(self.state.stop_session(name))
            return
        if action == "restart":
            self.send_json(self.state.restart_session(name))
            return
        if action == "pairing-code":
            payload = self.read_json()
            self.send_json(self.state.request_pairing_code(payload.get("phone_number"), name))
            return
        if action == "ensure":
            self.send_json(self.state.ensure_session(name))
            return
        if action == "settings":
            self.send_json(self.state.save_settings(self.read_json(), name))
            return
        raise ValueError("不支持的会话操作")

    def send_session_patch(self, route):
        self.require_mutation_auth()
        name, action = self.session_route(route)
        if name is None or action is not None:
            raise ValueError("会话名称更新路径不正确")
        payload = self.read_json()
        self.send_json(self.state.update_session_display_name(name, payload.get("display_name")))

    def send_session_delete(self, route):
        name, action = self.session_route(route)
        if name is None or action is not None:
            raise ValueError("会话删除路径不正确")
        self.send_json(self.state.delete_session(name))

    def do_GET(self):
        route = urlparse(self.path).path
        try:
            if route.startswith("/sessions/") and route.endswith("/chats"):
                self.require_admin_auth()
                name = self.chat_page_session(route)
                self.require_waha_session(name)
                body = chat_management_page(name).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "same-origin")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/security/csrf":
                self.require_admin_auth()
                if not self.state.csrf_token:
                    raise ChatServiceError("聊天管理功能不可用", "CHAT_CAPABILITY_UNAVAILABLE")
                self.send_json({"csrf_token": self.state.csrf_token})
            elif route.startswith("/api/chat/sessions/"):
                self.send_chat_get(route)
            elif route == "/api/admin/users" or route.startswith("/api/admin/users/"):
                self.send_admin_users_get(route)
            elif route == "/api/updates/check":
                self.send_update_check()
            elif route.startswith("/api/translation/"):
                self.send_translation_get(route)
            elif route == "/api/sessions" or route.startswith("/api/sessions/"):
                self.send_session_get(route)
            elif route.startswith("/api/commerce"):
                self.send_commerce_get(route)
            elif route.startswith("/api/admin/ai-business"):
                self.send_business_get(route)
            elif route == "/":
                self.require_admin_auth()
                body = multi_session_html_page().encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/status":
                self.require_admin_auth()
                query = parse_qs(urlparse(self.path).query)
                self.send_json(self.state.status_payload(query.get("session", [DEFAULT_SESSION_NAME])[0]))
            elif route == "/api/logs":
                self.require_admin_auth()
                query = parse_qs(urlparse(self.path).query)
                self.send_json({"logs": self.state.recent_logs(session_name=query.get("session", [DEFAULT_SESSION_NAME])[0])})
            elif route == "/settings":
                self.require_admin_auth()
                body = settings_page().encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/commerce":
                self.require_local_admin()
                body = commerce_page().encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif route == "/api/settings":
                self.require_admin_auth()
                query = parse_qs(urlparse(self.path).query)
                self.send_json(self.state.settings_payload(query.get("session", [DEFAULT_SESSION_NAME])[0]))
            elif route == "/api/knowledge":
                self.require_admin_auth()
                query = parse_qs(urlparse(self.path).query)
                self.send_json({"items": self.state.knowledge_list(query.get("session", [DEFAULT_SESSION_NAME])[0])})
            elif route.startswith("/api/knowledge/"):
                self.require_admin_auth()
                item_id = int(route.rsplit("/", 1)[1])
                query = parse_qs(urlparse(self.path).query)
                self.send_json(self.state.knowledge_item(item_id, query.get("session", [DEFAULT_SESSION_NAME])[0]))
            elif route == "/api/qr":
                self.require_admin_auth()
                query = parse_qs(urlparse(self.path).query)
                status, content_type, body = self.state.get_qr(query.get("session", [DEFAULT_SESSION_NAME])[0])
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_json({"message": "Not found"}, HTTPStatus.NOT_FOUND)
        except (ChatServiceError, TranslationError, RequestTooLarge, UnsupportedRequestMedia) as error:
            self.send_service_error(error)
        except AdminServiceError as error:
            self.send_service_error(error)
        except WahaApiError as error:
            self.state.log("ERROR", "waha.request", str(error))
            self.send_json({"message": redact_error(error, self.state.api_key)}, HTTPStatus.BAD_GATEWAY)
        except AdminAuthError as error:
            self.send_admin_auth_required(error)
        except PermissionError as error:
            self.state.log("WARN", "request.reject", str(error))
            self.send_json({"message": str(error)}, HTTPStatus.FORBIDDEN)
        except (ValueError, KeyError) as error:
            self.send_json({"message": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.state.log("ERROR", "panel.request", str(error))
            self.send_json({"message": "面板处理失败，请查看系统记录"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        route = urlparse(self.path).path
        try:
            if route != "/api/webhook":
                self.require_mutation_auth()
            if route.startswith("/api/chat/sessions/"):
                self.send_chat_post(route)
            elif route.startswith("/api/translation/"):
                self.send_translation_write(route, "POST")
            elif route == "/api/admin/users" or route.startswith("/api/admin/users/"):
                self.send_admin_users_write(route, "POST")
            elif route == "/api/updates/check":
                raise ValueError("更新检查只支持 GET")
            elif route == "/api/sessions" or route.startswith("/api/sessions/"):
                self.send_session_post(route)
            elif route.startswith("/api/commerce"):
                self.send_commerce_post(route)
            elif route.startswith("/api/admin/ai-business"):
                self.send_business_write(route, "POST")
            elif route == "/api/session/ensure":
                self.send_json(self.state.ensure_default(), HTTPStatus.OK)
            elif route == "/api/session/pairing-code":
                payload = self.read_json()
                self.send_json(self.state.request_pairing_code(payload.get("phone_number")), HTTPStatus.OK)
            elif route == "/api/settings":
                query = parse_qs(urlparse(self.path).query)
                self.send_json(self.state.save_settings(self.read_json(), query.get("session", [DEFAULT_SESSION_NAME])[0]), HTTPStatus.OK)
            elif route == "/api/knowledge":
                payload = self.read_json()
                query = parse_qs(urlparse(self.path).query)
                self.send_json(self.state.add_knowledge(payload.get("file_name"), payload.get("content"), query.get("session", [DEFAULT_SESSION_NAME])[0]), HTTPStatus.CREATED)
            elif route == "/api/webhook":
                result = self.state.handle_webhook(self.read_body(), self.headers, self.client_address[0])
                self.send_json(result, HTTPStatus.OK)
            else:
                self.send_json({"message": "Not found"}, HTTPStatus.NOT_FOUND)
        except (ChatServiceError, TranslationError, RequestTooLarge, UnsupportedRequestMedia) as error:
            self.send_service_error(error)
        except AdminServiceError as error:
            self.send_service_error(error)
        except WahaApiError as error:
            self.state.log("ERROR", "session.ensure", str(error))
            self.send_json({"message": redact_error(error, self.state.api_key)}, HTTPStatus.BAD_GATEWAY)
        except AdminAuthError as error:
            self.send_admin_auth_required(error)
        except PermissionError as error:
            self.state.log("WARN", "request.reject", str(error))
            self.send_json({"message": str(error)}, HTTPStatus.FORBIDDEN)
        except (ValueError, KeyError) as error:
            self.send_json({"message": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.state.log("ERROR", "session.ensure", str(error))
            self.send_json({"message": "会话操作失败，请查看系统记录"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self):
        route = urlparse(self.path).path
        method = self.command.upper()
        try:
            self.require_mutation_auth()
            if route.startswith("/api/chat/sessions/"):
                self.send_chat_labels_write(route, "PUT")
            elif route.startswith("/api/translation/"):
                self.send_translation_write(route, "PUT")
            elif route == "/api/sessions" or route.startswith("/api/sessions/"):
                self.send_session_patch(route)
            elif route == "/api/admin/users" or route.startswith("/api/admin/users/"):
                self.send_admin_users_write(route, method)
            elif route.startswith("/api/admin/ai-business"):
                self.send_business_write(route, "PUT")
            else:
                self.send_json({"message": "Not found"}, HTTPStatus.NOT_FOUND)
        except (ChatServiceError, TranslationError, RequestTooLarge, UnsupportedRequestMedia) as error:
            self.send_service_error(error)
        except AdminServiceError as error:
            self.send_service_error(error)
        except AdminAuthError as error:
            self.send_admin_auth_required(error)
        except PermissionError as error:
            self.state.log("WARN", "request.reject", str(error))
            self.send_json({"message": str(error)}, HTTPStatus.FORBIDDEN)
        except (ValueError, KeyError) as error:
            self.send_json({"message": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.state.log("ERROR", "business.save", str(error))
            self.send_json({"message": "业务数据保存失败，请查看系统记录"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    do_PATCH = do_PUT

    def do_DELETE(self):
        route = urlparse(self.path).path
        try:
            self.require_mutation_auth()
        except AdminAuthError as error:
            self.send_admin_auth_required(error)
            return
        except PermissionError as error:
            self.send_json({"message": str(error)}, HTTPStatus.FORBIDDEN)
            return
        if route.startswith("/api/chat/sessions/"):
            try:
                self.send_chat_labels_write(route, "DELETE")
            except (ChatServiceError, TranslationError, RequestTooLarge, UnsupportedRequestMedia) as error:
                self.send_service_error(error)
            except AdminAuthError as error:
                self.send_admin_auth_required(error)
            except PermissionError as error:
                self.state.log("WARN", "request.reject", str(error))
                self.send_json({"message": str(error)}, HTTPStatus.FORBIDDEN)
            except (ValueError, KeyError) as error:
                self.send_json({"message": str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:
                self.state.log("ERROR", "chat.labels", str(error))
                self.send_json({"message": "标签操作失败，请查看系统记录"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route == "/api/translation/cache":
            try:
                self.send_translation_write(route, "DELETE")
            except (ChatServiceError, TranslationError, RequestTooLarge, UnsupportedRequestMedia) as error:
                self.send_service_error(error)
            except AdminAuthError as error:
                self.send_admin_auth_required(error)
            except PermissionError as error:
                self.state.log("WARN", "request.reject", str(error))
                self.send_json({"message": str(error)}, HTTPStatus.FORBIDDEN)
            except (ValueError, KeyError) as error:
                self.send_json({"message": str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception:
                self.state.log("ERROR", "translation.cache", "翻译缓存清理失败")
                self.send_json({"message": "清理翻译缓存失败"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route == "/api/sessions" or route.startswith("/api/sessions/"):
            try:
                self.send_session_delete(route)
            except (ChatServiceError, TranslationError, RequestTooLarge, UnsupportedRequestMedia) as error:
                self.send_service_error(error)
            except WahaApiError as error:
                self.state.log("ERROR", "session.delete", str(error))
                self.send_json({"message": redact_error(error, self.state.api_key)}, HTTPStatus.BAD_GATEWAY)
            except (ValueError, KeyError) as error:
                self.send_json({"message": str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:
                self.state.log("ERROR", "session.delete", str(error))
                self.send_json({"message": "删除会话失败，请查看系统记录"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route.startswith("/api/admin/ai-business"):
            try:
                self.send_business_delete(route)
            except AdminAuthError as error:
                self.send_admin_auth_required(error)
            except PermissionError as error:
                self.state.log("WARN", "request.reject", str(error))
                self.send_json({"message": str(error)}, HTTPStatus.FORBIDDEN)
            except (ValueError, KeyError) as error:
                self.send_json({"message": str(error)}, HTTPStatus.BAD_REQUEST)
            except Exception as error:
                self.state.log("ERROR", "business.delete", str(error))
                self.send_json({"message": "业务数据删除失败，请查看系统记录"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        if route == "/api/admin/users" or route.startswith("/api/admin/users/"):
            try:
                raise ValueError("管理员删除接口未启用")
            except ValueError as error:
                self.send_json({"message": str(error)}, HTTPStatus.METHOD_NOT_ALLOWED)
            return
        if not route.startswith("/api/knowledge/"):
            self.send_json({"message": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            query = parse_qs(urlparse(self.path).query)
            self.state.delete_knowledge(int(route.rsplit("/", 1)[1]), query.get("session", [DEFAULT_SESSION_NAME])[0])
            self.send_json({"deleted": True})
        except (ValueError, KeyError) as error:
            self.send_json({"message": str(error)}, HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.state.log("ERROR", "knowledge.delete", str(error))
            self.send_json({"message": "删除资料失败，请查看系统记录"}, HTTPStatus.INTERNAL_SERVER_ERROR)


def build_state():
    database_path = Path(os.environ.get("DB_PATH", "/app/data/panel.sqlite3"))
    credentials_path = Path(os.environ.get("CREDENTIALS_PATH", "/run/secrets/waha_credentials"))
    credentials = read_credentials(credentials_path)
    api_key = credentials.get("WAHA_API_KEY", "")
    if not api_key:
        raise RuntimeError("WAHA_API_KEY 未配置")
    init_db(
        database_path,
        seed_business=as_bool(os.environ.get("PANEL_SEED_BUSINESS_DATA", "1")),
    )
    os.environ.setdefault("COMMERCE_SECRET_FILE", str(database_path.with_name("commerce-secrets.json")))
    client = WahaClient(os.environ.get("WAHA_URL", "http://waha:3000"), api_key)
    return PanelState(
        database_path,
        client,
        api_key,
        credentials.get("WAHA_WEBHOOK_SECRET") or os.environ.get("WAHA_WEBHOOK_SECRET"),
        os.environ.get("WEBHOOK_ALLOWED_CIDRS"),
        admin_username=credentials.get("WAHA_DASHBOARD_USERNAME", ""),
        admin_password=credentials.get("WAHA_DASHBOARD_PASSWORD", ""),
        data_encryption_key=os.environ.get("PANEL_DATA_ENCRYPTION_KEY"),
    )


def main():
    state = build_state()
    PanelHandler.state = state
    port = int(os.environ.get("PORT", "3001"))
    server = ThreadingHTTPServer(("0.0.0.0", port), PanelHandler)
    print(f"WAHA local panel listening on {port}", flush=True)
    try:
        state.start_background_services()
        server.serve_forever()
    finally:
        server.shutdown()
        server.server_close()
        state.stop_background_services()


if __name__ == "__main__":
    main()
