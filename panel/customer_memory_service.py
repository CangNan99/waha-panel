import json
import sqlite3
import threading
import time
from contextlib import closing

try:
    from .chat_security import DataCipherError, chat_key_hmac
except ImportError:  # Supports the existing `python app.py` container entrypoint.
    from chat_security import DataCipherError, chat_key_hmac


MEMORY_PROMPT_VERSION = "customer-memory-v1"
MAX_MEMORY_MESSAGES = 80
MAX_MEMORY_CHARS = 12_000
MAX_MEMORY_FIELD_CHARS = 2_000
MAX_MEMORY_LIST_ITEMS = 20
MAX_MEMORY_RETRY_SECONDS = 15 * 60

MEMORY_FIELDS = (
    "profile",
    "preferences",
    "customer_intent",
    "confirmed_items",
    "unresolved_items",
    "manual_reply_style",
)


class CustomerMemoryError(RuntimeError):
    def __init__(self, code, public_message):
        super().__init__(public_message)
        self.code = str(code)
        self.public_message = str(public_message)


def _normalized_text(value, limit=MAX_MEMORY_FIELD_CHARS):
    text = " ".join(str(value or "").replace("\r", " ").replace("\n", " ").split())
    return text[:limit].strip()


def _normalized_list(value):
    values = value if isinstance(value, list) else ([value] if value else [])
    result = []
    for item in values[:MAX_MEMORY_LIST_ITEMS]:
        text = _normalized_text(item)
        if text:
            result.append(text)
    return result


def normalize_memory(value):
    if not isinstance(value, dict):
        raise CustomerMemoryError("MEMORY_RESULT_INVALID", "客户记忆格式无效")
    result = {
        "profile": _normalized_text(value.get("profile")),
        "preferences": _normalized_list(value.get("preferences")),
        "customer_intent": _normalized_text(value.get("customer_intent")),
        "confirmed_items": _normalized_list(value.get("confirmed_items")),
        "unresolved_items": _normalized_list(value.get("unresolved_items")),
        "manual_reply_style": _normalized_text(value.get("manual_reply_style")),
    }
    if not any(result.values()):
        raise CustomerMemoryError("MEMORY_RESULT_EMPTY", "客户记忆为空")
    return result


class CustomerMemoryService:
    """Encrypted per-customer memory with a coalesced background worker."""

    def __init__(
        self,
        database_path,
        cipher,
        hmac_secret,
        settings_loader,
        completion_fn,
        logger=None,
        clock=None,
    ):
        self.database_path = str(database_path)
        self.cipher = cipher
        self.hmac_secret = bytes(hmac_secret)
        self.settings_loader = settings_loader
        self.completion_fn = completion_fn
        self.logger = logger
        self.clock = clock or time.time
        self._condition = threading.Condition()
        self._stopping = False
        self._worker = None

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _safe_log(self, level, message):
        if not self.logger:
            return
        try:
            self.logger(level, message)
        except TypeError:
            try:
                self.logger(message)
            except Exception:
                return
        except Exception:
            return

    def _settings(self, session_name):
        try:
            settings = self.settings_loader(session_name)
        except Exception:
            return {}
        return settings if isinstance(settings, dict) else {}

    def _enabled(self, session_name):
        value = self._settings(session_name).get("customer_memory_enabled", False)
        return value is True or str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _key(self, session_name, chat_id):
        return chat_key_hmac(self.hmac_secret, str(session_name), str(chat_id))

    def start(self):
        with self._condition:
            if self._worker and self._worker.is_alive():
                return
            self._stopping = False
            self._worker = threading.Thread(
                target=self._run,
                name="customer-memory-worker",
                daemon=True,
            )
            self._worker.start()

    def stop(self, timeout=2.0):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=max(0.0, float(timeout)))
        with self._condition:
            if self._worker and not self._worker.is_alive():
                self._worker = None

    def _run(self):
        while True:
            with self._condition:
                if self._stopping:
                    return
                self._condition.wait(timeout=0.5)
                if self._stopping:
                    return
            try:
                self._run_one_due_job()
            except Exception as error:
                self._safe_log("ERROR", "客户记忆后台任务失败：" + type(error).__name__)

    def enqueue(self, session_name, chat_id):
        if not self._enabled(session_name):
            return
        session = str(session_name)
        customer = str(chat_id or "").strip()
        if not customer:
            return
        now = int(self.clock())
        key = self._key(session, customer)
        chat_ciphertext = self.cipher.encrypt_json({"chat_id": customer})
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO conversation_memory_jobs "
                "(session_name,chat_key_hmac,chat_id_ciphertext,state,attempts,next_run_at,"
                "claimed_at,last_error_code,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(session_name,chat_key_hmac) DO UPDATE SET "
                "chat_id_ciphertext=excluded.chat_id_ciphertext, "
                "state=CASE WHEN conversation_memory_jobs.state='RUNNING' THEN 'RUNNING' ELSE 'PENDING' END, "
                "next_run_at=excluded.next_run_at, updated_at=excluded.updated_at",
                (session, key, chat_ciphertext, "PENDING", 0, now, None, None, now, now),
            )
            connection.commit()
        with self._condition:
            self._condition.notify_all()

    def _load_memory_row(self, session_name, key):
        with closing(self._connect()) as connection:
            return connection.execute(
                "SELECT * FROM customer_memories WHERE session_name=? AND chat_key_hmac=?",
                (session_name, key),
            ).fetchone()

    def _decrypt_memory(self, row):
        if not row or not row["memory_ciphertext"]:
            return {}
        try:
            return normalize_memory(self.cipher.decrypt_json(row["memory_ciphertext"]))
        except (DataCipherError, CustomerMemoryError, TypeError, ValueError):
            return {}

    def get(self, session_name, chat_id):
        session = str(session_name)
        customer = str(chat_id or "").strip()
        key = self._key(session, customer)
        row = self._load_memory_row(session, key)
        if not row:
            return {
                "enabled": self._enabled(session),
                "status": "EMPTY",
                "version": 0,
                "memory": {},
                "updated_at": None,
                "last_error_code": None,
            }
        return {
            "enabled": self._enabled(session),
            "status": row["status"],
            "version": int(row["version"] or 0),
            "memory": self._decrypt_memory(row),
            "updated_at": row["updated_at"],
            "last_error_code": row["last_error_code"],
        }

    def context_for_reply(self, session_name, chat_id):
        if not self._enabled(session_name):
            return ""
        memory = self.get(session_name, chat_id).get("memory") or {}
        labels = {
            "profile": "客户画像",
            "preferences": "客户偏好",
            "customer_intent": "客户意图",
            "confirmed_items": "已确认事项",
            "unresolved_items": "未解决事项",
            "manual_reply_style": "有效人工回复风格",
        }
        lines = []
        for key in MEMORY_FIELDS:
            value = memory.get(key)
            if isinstance(value, list):
                value = "；".join(value)
            if value:
                lines.append(f"{labels[key]}：{value}")
        return "\n".join(lines)[:MAX_MEMORY_CHARS]

    def _load_messages(self, session_name, chat_id, cursor=0):
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id,direction,origin,content,created_at FROM conversation_messages "
                "WHERE session_name=? AND chat_id=? AND id>? ORDER BY id LIMIT ?",
                (session_name, chat_id, int(cursor or 0), MAX_MEMORY_MESSAGES),
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _prompt_messages(rows):
        lines = []
        for row in rows:
            direction = "客户" if row.get("direction") == "inbound" else "客服"
            origin = str(row.get("origin") or "unknown")
            lines.append(f"{direction}（{origin}）：{_normalized_text(row.get('content'), 600)}")
        result = "\n".join(lines)
        return result[:MAX_MEMORY_CHARS]

    def _generate(self, session_name, chat_id, previous, cursor):
        rows = self._load_messages(session_name, chat_id, cursor)
        if not rows:
            return previous, None, cursor
        previous_json = json.dumps(previous, ensure_ascii=False) if previous else "无"
        system_prompt = (
            "你负责维护单个客户的长期对话记忆。只根据提供的原始对话更新记忆，"
            "不要猜测，不要保存密钥、支付凭据或无关隐私。输出 JSON 对象，字段只能是 "
            + ", ".join(MEMORY_FIELDS)
            + "；列表字段使用字符串数组；没有内容使用空字符串或空数组。"
        )
        user_prompt = (
            f"旧记忆：{previous_json}\n\n"
            "新增原始对话（包含人工和 AI 回复，人工回复可用于提取有效回复风格）：\n"
            + self._prompt_messages(rows)
        )
        raw = self.completion_fn(self._settings(session_name), system_prompt, user_prompt)
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError) as error:
                raise CustomerMemoryError("MEMORY_RESULT_INVALID", "客户记忆格式无效") from error
        memory = normalize_memory(raw)
        return memory, rows[-1]["id"], cursor

    def rebuild(self, session_name, chat_id):
        session = str(session_name)
        customer = str(chat_id or "").strip()
        if not customer:
            raise CustomerMemoryError("INVALID_CHAT", "客户编号无效")
        if not self._enabled(session):
            return self.get(session, customer)
        key = self._key(session, customer)
        previous_row = self._load_memory_row(session, key)
        previous = self._decrypt_memory(previous_row)
        cursor = 0
        if previous_row and previous_row["status"] != "FAILED":
            cursor = int(previous_row["message_cursor"] or 0)
        now = int(self.clock())
        try:
            memory, new_cursor, _old_cursor = self._generate(session, customer, previous, cursor)
            chat_ciphertext = self.cipher.encrypt_json({"chat_id": customer})
            fingerprint = str(new_cursor or cursor)
            memory_ciphertext = self.cipher.encrypt_json(memory)
            previous_version = int(previous_row["version"] or 0) if previous_row else 0
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT INTO customer_memories "
                    "(session_name,chat_key_hmac,chat_id_ciphertext,memory_ciphertext,message_cursor,"
                    "message_fingerprint,model_fingerprint,prompt_version,version,status,last_error_code,"
                    "generated_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(session_name,chat_key_hmac) DO UPDATE SET "
                    "chat_id_ciphertext=excluded.chat_id_ciphertext,memory_ciphertext=excluded.memory_ciphertext,"
                    "message_cursor=excluded.message_cursor,message_fingerprint=excluded.message_fingerprint,"
                    "model_fingerprint=excluded.model_fingerprint,prompt_version=excluded.prompt_version,"
                    "version=excluded.version,status=excluded.status,last_error_code=NULL,"
                    "generated_at=excluded.generated_at,updated_at=excluded.updated_at",
                    (
                        session,
                        key,
                        chat_ciphertext,
                        memory_ciphertext,
                        int(new_cursor or cursor),
                        fingerprint,
                        str(self._settings(session).get("ai_model") or ""),
                        MEMORY_PROMPT_VERSION,
                        previous_version + 1,
                        "READY",
                        None,
                        now,
                        now,
                    ),
                )
                connection.commit()
            return self.get(session, customer)
        except Exception as error:
            error_code = getattr(error, "code", "MEMORY_GENERATION_FAILED")
            chat_ciphertext = self.cipher.encrypt_json({"chat_id": customer})
            with closing(self._connect()) as connection:
                connection.execute(
                    "INSERT INTO customer_memories "
                    "(session_name,chat_key_hmac,chat_id_ciphertext,memory_ciphertext,message_cursor,"
                    "message_fingerprint,model_fingerprint,prompt_version,version,status,last_error_code,"
                    "generated_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(session_name,chat_key_hmac) DO UPDATE SET "
                    "chat_id_ciphertext=excluded.chat_id_ciphertext,status='FAILED',"
                    "last_error_code=excluded.last_error_code,updated_at=excluded.updated_at",
                    (
                        session,
                        key,
                        chat_ciphertext,
                        previous_row["memory_ciphertext"] if previous_row else "",
                        int(previous_row["message_cursor"] or 0) if previous_row else 0,
                        previous_row["message_fingerprint"] if previous_row else "",
                        previous_row["model_fingerprint"] if previous_row else "",
                        previous_row["prompt_version"] if previous_row else MEMORY_PROMPT_VERSION,
                        int(previous_row["version"] or 0) if previous_row else 0,
                        "FAILED",
                        str(error_code),
                        previous_row["generated_at"] if previous_row else None,
                        now,
                    ),
                )
                connection.commit()
            self._safe_log("WARN", "客户记忆生成失败：" + str(error_code))
            return self.get(session, customer)

    def clear(self, session_name, chat_id):
        session = str(session_name)
        customer = str(chat_id or "").strip()
        key = self._key(session, customer)
        with closing(self._connect()) as connection:
            connection.execute(
                "DELETE FROM customer_memories WHERE session_name=? AND chat_key_hmac=?",
                (session, key),
            )
            connection.execute(
                "DELETE FROM conversation_memory_jobs WHERE session_name=? AND chat_key_hmac=?",
                (session, key),
            )
            connection.commit()
        return {"cleared": True, "session_name": session}

    def _run_one_due_job(self):
        now = int(self.clock())
        job = None
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM conversation_memory_jobs "
                "WHERE state='PENDING' AND next_run_at<=? ORDER BY updated_at LIMIT 1",
                (now,),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE conversation_memory_jobs SET state='RUNNING',claimed_at=?,updated_at=? "
                    "WHERE session_name=? AND chat_key_hmac=?",
                    (now, now, row["session_name"], row["chat_key_hmac"]),
                )
                connection.commit()
                job = dict(row)
            else:
                connection.rollback()
        if not job:
            return False
        try:
            chat_id = self.cipher.decrypt_json(job["chat_id_ciphertext"])["chat_id"]
            result = self.rebuild(job["session_name"], chat_id)
            if result.get("status") == "FAILED":
                raise CustomerMemoryError(
                    result.get("last_error_code") or "MEMORY_GENERATION_FAILED",
                    "客户记忆生成失败",
                )
            with closing(self._connect()) as connection:
                connection.execute(
                    "UPDATE conversation_memory_jobs SET state='DONE',claimed_at=NULL,updated_at=? "
                    "WHERE session_name=? AND chat_key_hmac=?",
                    (int(self.clock()), job["session_name"], job["chat_key_hmac"]),
                )
                connection.commit()
        except Exception as error:
            attempts = int(job.get("attempts") or 0) + 1
            delay = min(MAX_MEMORY_RETRY_SECONDS, 2 ** min(attempts, 8))
            with closing(self._connect()) as connection:
                connection.execute(
                    "UPDATE conversation_memory_jobs SET state='PENDING',attempts=?,next_run_at=?,"
                    "claimed_at=NULL,last_error_code=?,updated_at=? WHERE session_name=? AND chat_key_hmac=?",
                    (
                        attempts,
                        int(self.clock()) + delay,
                        "MEMORY_WORKER_FAILED",
                        int(self.clock()),
                        job["session_name"],
                        job["chat_key_hmac"],
                    ),
                )
                connection.commit()
            self._safe_log("WARN", "客户记忆任务重试：MEMORY_WORKER_FAILED")
        return True
