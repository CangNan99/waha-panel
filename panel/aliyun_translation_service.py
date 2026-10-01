import hashlib
import re
import sqlite3
import threading
import time
import unicodedata
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from types import SimpleNamespace

try:
    from .chat_security import DataCipherError
    from .translation_service import TranslationError
except ImportError:
    from chat_security import DataCipherError
    from translation_service import TranslationError


MAX_TRANSLATION_CHARS = 4800
MAX_BATCH_ITEMS = 30
MAX_BATCH_CHARS = 100_000
CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
ENDPOINT_RE = re.compile(r"^mt\.[a-z0-9-]+\.aliyuncs\.com$")


class AliyunTranslationError(TranslationError):
    def __init__(self, code, public_message):
        self.code = str(code)
        self.public_message = str(public_message)
        super().__init__(self.public_message)


def _is_han(character):
    value = ord(character)
    return (
        0x3400 <= value <= 0x4DBF or 0x4E00 <= value <= 0x9FFF
        or 0xF900 <= value <= 0xFAFF or 0x20000 <= value <= 0x323AF
    )


def is_pure_chinese(text):
    """Han text and nonlinguistic content need no duplicate Chinese line."""
    for character in str(text or ""):
        category = unicodedata.category(character)
        if category.startswith(("L", "M")) and not _is_han(character):
            return False
    return True


class AliyunTranslationService:
    """Alibaba machine translation; never uses the panel's AI transport."""

    def __init__(self, database_path, cipher, logger=None, clock=None, sdk_client_factory=None):
        self.database_path = str(database_path)
        self.cipher = cipher
        self.logger = logger
        self.clock = clock or time.time
        self.sdk_client_factory = sdk_client_factory
        self._slots = threading.BoundedSemaphore(5)
        self._rate_lock = threading.Lock()
        self._request_times = deque()
        self._cache_locks = [threading.Lock() for _ in range(64)]

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _safe_log(self, code):
        if self.logger:
            try:
                self.logger("WARN", "阿里云机器翻译失败：" + str(code))
            except Exception:
                pass

    def _settings(self):
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM aliyun_translation_settings WHERE id=1").fetchone()
        settings = dict(row) if row else {}
        secret = ""
        try:
            encrypted = settings.get("access_key_secret_ciphertext")
            if encrypted:
                secret = str(self.cipher.decrypt_json(encrypted).get("access_key_secret") or "")
        except DataCipherError:
            pass
        settings["access_key_secret"] = secret
        return settings

    def settings_payload(self):
        settings = self._settings()
        return {
            "endpoint": settings.get("endpoint") or "mt.cn-hangzhou.aliyuncs.com",
            "region_id": settings.get("region_id") or "cn-hangzhou",
            "access_key_id_configured": bool(settings.get("access_key_id")),
            "access_key_secret_configured": bool(settings.get("access_key_secret")),
            "last_test_status": settings.get("last_test_status") or "never",
            "last_test_at": settings.get("last_test_at"),
        }

    def save_settings(self, payload):
        if not isinstance(payload, dict):
            raise AliyunTranslationError("INVALID_SETTINGS", "阿里云配置格式不正确")
        current = self._settings()
        endpoint = str(payload.get("endpoint", current.get("endpoint")) or "").strip().lower()
        region = str(payload.get("region_id", current.get("region_id")) or "").strip().lower()
        if not ENDPOINT_RE.fullmatch(endpoint):
            raise AliyunTranslationError("INVALID_ENDPOINT", "请填写阿里云机器翻译官方 Endpoint")
        if not re.fullmatch(r"[a-z][a-z0-9-]{1,40}", region):
            raise AliyunTranslationError("INVALID_REGION", "阿里云地域格式无效")
        access_id = str(payload.get("access_key_id") or "").strip() or current.get("access_key_id", "")
        secret = str(payload.get("access_key_secret") or "").strip() or current.get("access_key_secret", "")
        if payload.get("clear_credentials") is True:
            access_id, secret = "", ""
        if len(access_id) > 256 or len(secret) > 256:
            raise AliyunTranslationError("INVALID_CREDENTIALS", "阿里云凭据长度无效")
        encrypted = self.cipher.encrypt_json({"access_key_secret": secret}) if secret else ""
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE aliyun_translation_settings SET endpoint=?,region_id=?,access_key_id=?,"
                "access_key_secret_ciphertext=?,last_test_status='never',last_test_at=NULL,updated_at=? WHERE id=1",
                (endpoint, region, access_id, encrypted, int(self.clock())),
            )
            connection.commit()
        return self.settings_payload()

    @staticmethod
    def _result(message_ref, status, translation=None, detected_language=None, error_code=None):
        return {"message_ref": message_ref, "status": status, "translation": translation,
                "detected_language": detected_language, "error_code": error_code}

    def _sdk_request(self, text, settings):
        try:
            from alibabacloud_alimt20181012.client import Client
            from alibabacloud_alimt20181012.models import TranslateGeneralRequest
            from alibabacloud_tea_openapi.models import Config
            from alibabacloud_tea_util.models import RuntimeOptions
        except ImportError as error:
            if self.sdk_client_factory is None:
                raise AliyunTranslationError("ALIYUN_SDK_MISSING", "阿里云机器翻译 SDK 未安装") from error
            Client, Config = None, None
            TranslateGeneralRequest = RuntimeOptions = SimpleNamespace
        request = TranslateGeneralRequest(
            source_language="auto", target_language="zh", source_text=text,
            format_type="text", scene="general",
        )
        runtime = RuntimeOptions(connect_timeout=5000, read_timeout=15000, autoretry=False)
        if self.sdk_client_factory is not None:
            client = self.sdk_client_factory(settings)
        else:
            config = Config(
                access_key_id=settings["access_key_id"], access_key_secret=settings["access_key_secret"],
                region_id=settings["region_id"], endpoint=settings["endpoint"], protocol="https",
            )
            client = Client(config)
        response = client.translate_general_with_options(request, runtime)
        if hasattr(response, "to_map"):
            response = response.to_map()
        body = response.get("body", response) if isinstance(response, dict) else {}
        if not isinstance(body, dict) or str(body.get("Code")) != "200":
            raise AliyunTranslationError("ALIYUN_TRANSLATION_FAILED", "阿里云翻译暂不可用")
        data = body.get("Data") or {}
        translated = str(data.get("Translated") or "").strip()
        language = str(data.get("DetectedLanguage") or "").strip()[:40] or None
        if not translated:
            raise AliyunTranslationError("ALIYUN_EMPTY_RESULT", "阿里云未返回译文")
        return translated[:20_000], language

    def _take_rate_slot(self):
        now = self.clock()
        with self._rate_lock:
            while self._request_times and now - self._request_times[0] >= 60:
                self._request_times.popleft()
            if len(self._request_times) >= 300:
                raise AliyunTranslationError("ALIYUN_RATE_LIMIT", "机器翻译请求较多，请稍后重试")
            self._request_times.append(now)

    def _translate_one(self, session_name, chat_key, item):
        reference = str(item.get("message_ref") or "")
        text = str(item.get("text") or "").strip()
        if len(text) > MAX_TRANSLATION_CHARS:
            return self._result(reference, "FAILED", error_code="TEXT_TOO_LONG")
        if is_pure_chinese(text):
            return self._result(reference, "SKIPPED", detected_language="zh")
        fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
        identity = (session_name, chat_key, reference, fingerprint)
        lock = self._cache_locks[int(fingerprint[:8], 16) % len(self._cache_locks)]
        with lock:
            with closing(self._connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM machine_message_translations WHERE session_name=? AND chat_key_hmac=? "
                    "AND message_ref=? AND source_fingerprint=? AND target_language='zh' AND status='READY' "
                    "AND updated_at>=?", (*identity, int(self.clock()) - CACHE_TTL_SECONDS),
                ).fetchone()
            if row and row["translation_ciphertext"]:
                try:
                    cached = self.cipher.decrypt_json(row["translation_ciphertext"])
                    return self._result(reference, "READY", cached["translation"], row["detected_language"])
                except (DataCipherError, KeyError):
                    pass
            settings = self._settings()
            if not (settings.get("access_key_id") and settings.get("access_key_secret")):
                return self._result(reference, "FAILED", error_code="ALIYUN_CONFIG_MISSING")
            if not self._slots.acquire(blocking=False):
                return self._result(reference, "FAILED", error_code="ALIYUN_BUSY")
            try:
                self._take_rate_slot()
                translated, language = self._sdk_request(text, settings)
            except Exception as error:
                code = error.code if isinstance(error, AliyunTranslationError) else "ALIYUN_TRANSLATION_FAILED"
                self._safe_log(code)
                return self._result(reference, "FAILED", error_code=code)
            finally:
                self._slots.release()
            now = int(self.clock())
            encrypted = self.cipher.encrypt_json({"translation": translated})
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT INTO machine_message_translations "
                    "(session_name,chat_key_hmac,message_ref,source_fingerprint,source_language,target_language,"
                    "translation_ciphertext,detected_language,status,last_error_code,created_at,updated_at) "
                    "VALUES (?,?,?,?,'auto','zh',?,?,'READY',NULL,?,?) "
                    "ON CONFLICT(session_name,chat_key_hmac,message_ref,source_fingerprint,target_language) "
                    "DO UPDATE SET translation_ciphertext=excluded.translation_ciphertext,"
                    "detected_language=excluded.detected_language,status='READY',last_error_code=NULL,updated_at=excluded.updated_at",
                    (*identity, encrypted, language, now, now),
                )
                connection.execute("DELETE FROM machine_message_translations WHERE updated_at<?", (now - CACHE_TTL_SECONDS,))
                connection.commit()
            return self._result(reference, "READY", translated, language)

    def translate_batch(self, session_name, chat_ref, items):
        # The HTTP boundary resolves expiring references and passes the stable customer HMAC.
        if not isinstance(items, list) or len(items) > MAX_BATCH_ITEMS:
            raise AliyunTranslationError("INVALID_BATCH", "每批最多翻译 30 条消息")
        if any(not isinstance(item, dict) or not str(item.get("message_ref") or "") for item in items):
            raise AliyunTranslationError("INVALID_BATCH", "翻译消息格式无效")
        if sum(len(str(item.get("text") or "")) for item in items) > MAX_BATCH_CHARS:
            raise AliyunTranslationError("BATCH_TOO_LONG", "本批翻译文本过长")
        with ThreadPoolExecutor(max_workers=5, thread_name_prefix="aliyun-translation") as executor:
            results = list(executor.map(lambda item: self._translate_one(str(session_name), str(chat_ref), item), items))
        return {"items": results}

    def test_connection(self):
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM machine_message_translations WHERE session_name='__connection_test__'")
            connection.commit()
        result = self._translate_one("__connection_test__", "test", {"message_ref": "test", "text": "Hello"})
        with closing(self._connect()) as connection:
            connection.execute("DELETE FROM machine_message_translations WHERE session_name='__connection_test__'")
            status = "success" if result["status"] == "READY" else "failed"
            connection.execute(
                "UPDATE aliyun_translation_settings SET last_test_status=?,last_test_at=?,updated_at=? WHERE id=1",
                (status, int(self.clock()), int(self.clock())),
            )
            connection.commit()
        return {"ok": status == "success", "status": status, "error_code": result["error_code"]}

    def clear_cache(self):
        with closing(self._connect()) as connection:
            deleted = connection.execute("DELETE FROM machine_message_translations").rowcount
            connection.commit()
        return {"cleared": True, "deleted": deleted}
