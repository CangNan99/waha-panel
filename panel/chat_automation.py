import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

try:
    from .chat_service import ChatServiceError
except ImportError:
    from chat_service import ChatServiceError


DELAY_SECONDS = {
    "24h": 86_400,
    "3d": 259_200,
    "7d": 604_800,
    "15d": 1_296_000,
}
TASK_MODES = {"AI", "FIXED"}
PUBLIC_TASK_COLUMNS = (
    "id", "created_at", "due_at", "updated_at", "delay_code", "mode", "state",
    "skip_reason", "error_code", "error_message", "claimed_at", "completed_at",
)


class AutomationError(ChatServiceError):
    code = "AUTOMATION_ERROR"


class ChatAutomationService:
    def __init__(self, database_path, chat_service, translation_service, logger=None,
                 clock=time.time, scan_interval=15):
        self.database_path = Path(database_path)
        self.chat_service = chat_service
        self.translation_service = translation_service
        self.logger = logger
        self.clock = clock
        self.scan_interval = max(0.1, float(scan_interval))
        codec = getattr(chat_service, "codec", None)
        self.cipher = getattr(codec, "cipher", None)
        if not self.cipher or not callable(getattr(self.cipher, "encrypt_json", None)) \
                or not callable(getattr(self.cipher, "decrypt_json", None)):
            raise AutomationError("任务加密服务不可用", "AUTOMATION_ENCRYPTION_UNAVAILABLE")
        self._lifecycle_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._recover_running_tasks()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _now(self, value=None):
        return int(self.clock() if value is None else value)

    def _safe_log(self, level, task_id, session, state, error_code=None):
        if not self.logger:
            return
        fields = [f"task_id={int(task_id)}", f"session={session}", f"state={state}"]
        if error_code:
            fields.append(f"error_code={error_code}")
        message = "follow-up task " + " ".join(fields)
        try:
            self.logger(level, message)
        except TypeError:
            try:
                self.logger(message)
            except Exception:
                return
        except Exception:
            return

    @staticmethod
    def _task_payload(row):
        return {name: row[name] for name in PUBLIC_TASK_COLUMNS}

    @staticmethod
    def _task_id(value):
        try:
            task_id = int(value)
        except (TypeError, ValueError) as error:
            raise AutomationError("跟进任务编号无效", "INVALID_TASK_ID") from error
        if isinstance(value, bool) or task_id < 1:
            raise AutomationError("跟进任务编号无效", "INVALID_TASK_ID")
        return task_id

    def create_task(self, session, chat_ref, delay_code, mode, fixed_copy=None):
        delay = str(delay_code or "").strip()
        if delay not in DELAY_SECONDS:
            raise AutomationError("跟进时间选项无效", "INVALID_FOLLOW_UP_DELAY")
        task_mode = str(mode or "").strip().upper()
        if task_mode not in TASK_MODES:
            raise AutomationError("跟进模式无效", "INVALID_FOLLOW_UP_MODE")
        fixed_text = str(fixed_copy or "").strip()
        if task_mode == "FIXED" and not fixed_text:
            raise AutomationError("固定文案不能为空", "EMPTY_FIXED_COPY")
        if task_mode == "FIXED" and len(fixed_text) > 12_000:
            raise AutomationError("固定文案不能超过 12000 个字符", "FIXED_COPY_TOO_LONG")

        identity = self.chat_service.chat_identity(session, chat_ref)
        chat_id = str(identity.get("chat_id") or "").strip()
        chat_key = str(identity.get("chat_key_hmac") or "").strip()
        if not chat_id or not chat_key:
            raise AutomationError("聊天引用无效", "INVALID_CHAT_REFERENCE")
        try:
            chat_ciphertext = self.cipher.encrypt_json({"chat_id": chat_id})
            fixed_ciphertext = (
                self.cipher.encrypt_json({"text": fixed_text}) if task_mode == "FIXED" else None
            )
        except Exception as error:
            raise AutomationError("任务内容无法安全保存", "AUTOMATION_ENCRYPTION_FAILED") from error

        now = self._now()
        request_id = str(uuid.uuid4())
        connection = self._connect()
        try:
            cursor = connection.execute(
                "INSERT INTO follow_up_tasks("
                "session_name,chat_key_hmac,chat_id_ciphertext,created_at,due_at,updated_at,"
                "delay_code,mode,fixed_copy_ciphertext,state,client_request_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,'PENDING',?)",
                (str(session), chat_key, chat_ciphertext, now, now + DELAY_SECONDS[delay], now,
                 delay, task_mode, fixed_ciphertext, request_id),
            )
            row = connection.execute(
                "SELECT * FROM follow_up_tasks WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._task_payload(row)

    def list_tasks(self, session, chat_ref):
        identity = self.chat_service.chat_identity(session, chat_ref)
        chat_key = str(identity.get("chat_key_hmac") or "").strip()
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM follow_up_tasks WHERE session_name=? AND chat_key_hmac=? "
                "ORDER BY created_at DESC,id DESC",
                (str(session), chat_key),
            ).fetchall()
        finally:
            connection.close()
        return {"items": [self._task_payload(row) for row in rows]}

    def cancel_task(self, session, task_id):
        identifier = self._task_id(task_id)
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM follow_up_tasks WHERE id=? AND session_name=?",
                (identifier, str(session)),
            ).fetchone()
            if row is None:
                raise AutomationError("跟进任务不存在或不可取消", "FOLLOW_UP_NOT_CANCELLABLE")
            if row["state"] == "PENDING":
                connection.execute(
                    "UPDATE follow_up_tasks SET state='CANCELLED',updated_at=?,completed_at=? "
                    "WHERE id=? AND state='PENDING'",
                    (now, now, identifier),
                )
            elif row["state"] != "CANCELLED":
                raise AutomationError("跟进任务不存在或不可取消", "FOLLOW_UP_NOT_CANCELLABLE")
            result = connection.execute(
                "SELECT * FROM follow_up_tasks WHERE id=?", (identifier,)
            ).fetchone()
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._task_payload(result)

    def _claim_due_task(self, now):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM follow_up_tasks WHERE state='PENDING' AND due_at<=? "
                "ORDER BY due_at,id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            changed = connection.execute(
                "UPDATE follow_up_tasks SET state='RUNNING',claimed_at=?,updated_at=? "
                "WHERE id=? AND state='PENDING'",
                (now, now, row["id"]),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            claimed = connection.execute(
                "SELECT * FROM follow_up_tasks WHERE id=?", (row["id"],)
            ).fetchone()
            connection.commit()
            return claimed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _finish_task(self, task_id, state, now, *, skip_reason=None, error_code=None,
                     error_message=None, waha_message_id=None):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE follow_up_tasks SET state=?,skip_reason=?,error_code=?,error_message=?,"
                "waha_message_id=?,updated_at=?,completed_at=? WHERE id=? AND state='RUNNING'",
                (state, skip_reason, error_code, error_message, waha_message_id,
                 now, now, task_id),
            ).rowcount
            connection.commit()
            return changed == 1
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _send_ledger(self, request_id):
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT state,waha_message_id,error_code FROM automated_send_requests "
                "WHERE client_request_id=?",
                (request_id,),
            ).fetchone()
        finally:
            connection.close()

    @staticmethod
    def _safe_error(error):
        raw_code = str(getattr(error, "code", "") or "")
        code = raw_code if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", raw_code) else "TASK_EXECUTION_FAILED"
        message = getattr(error, "public_message", None)
        if not isinstance(message, str) or not message.strip():
            message = "跟进任务执行失败"
        return code, " ".join(message.split())[:500]

    def _decode_task(self, task):
        chat_payload = self.cipher.decrypt_json(task["chat_id_ciphertext"])
        chat_id = str(chat_payload.get("chat_id") or "").strip()
        if not chat_id:
            raise AutomationError("任务聊天信息无法读取", "TASK_CHAT_UNREADABLE")
        fixed_text = None
        if task["mode"] == "FIXED":
            fixed_payload = self.cipher.decrypt_json(task["fixed_copy_ciphertext"])
            fixed_text = str(fixed_payload.get("text") or "").strip()
            if not fixed_text:
                raise AutomationError("任务固定文案无法读取", "TASK_COPY_UNREADABLE")
        return chat_id, fixed_text

    def _process_claimed(self, task, now):
        task_id = task["id"]
        session = task["session_name"]
        try:
            chat_id, fixed_text = self._decode_task(task)
            if self.chat_service.has_inbound_since(session, chat_id, task["created_at"]):
                self._finish_task(task_id, "SKIPPED", now, skip_reason="创建后客户已发新消息")
                self._safe_log("INFO", task_id, session, "SKIPPED")
                return
            if self.chat_service.is_human_takeover(session, chat_id):
                self._finish_task(task_id, "SKIPPED", now, skip_reason="人工接管中")
                self._safe_log("INFO", task_id, session, "SKIPPED")
                return

            history = self.chat_service.history_for_id(session, chat_id, 20)
            if task["mode"] == "AI":
                generated = self.translation_service.generate_follow_up(history)
            else:
                generated = self.translation_service.translate_follow_up(fixed_text, history)
            message = str(generated.get("message") if isinstance(generated, dict) else "").strip()
            if not message:
                raise AutomationError("AI 未生成可发送的跟进消息", "EMPTY_FOLLOW_UP_RESULT")

            result = self.chat_service.send_automated_text(
                session, chat_id, message, task["client_request_id"]
            )
            state = str(result.get("state") if isinstance(result, dict) else "").upper()
            ledger = self._send_ledger(task["client_request_id"])
            if state != "SENT":
                terminal = "UNKNOWN" if state == "UNKNOWN" else "FAILED"
                code = "WAHA_SEND_UNKNOWN" if terminal == "UNKNOWN" else "WAHA_SEND_FAILED"
                self._finish_task(task_id, terminal, now, error_code=code,
                                  error_message="自动跟进消息发送状态异常")
                self._safe_log("WARNING", task_id, session, terminal, code)
                return
            message_id = ledger["waha_message_id"] if ledger and ledger["state"] == "SENT" else None
            self._finish_task(task_id, "SENT", now, waha_message_id=message_id)
            self._safe_log("INFO", task_id, session, "SENT")
        except Exception as error:
            ledger = self._send_ledger(task["client_request_id"])
            if ledger and ledger["state"] == "SENT" and ledger["waha_message_id"]:
                self._finish_task(task_id, "SENT", now, waha_message_id=ledger["waha_message_id"])
                self._safe_log("INFO", task_id, session, "SENT")
                return
            error_state = str(getattr(error, "state", "") or "").upper()
            terminal = "UNKNOWN" if error_state == "UNKNOWN" else "FAILED"
            code, message = self._safe_error(error)
            if ledger and ledger["error_code"]:
                code = str(ledger["error_code"])
            self._finish_task(task_id, terminal, now, error_code=code, error_message=message)
            self._safe_log("WARNING", task_id, session, terminal, code)

    def run_once(self, now=None):
        current = self._now(now)
        task = self._claim_due_task(current)
        if task is None:
            return 0
        self._process_claimed(task, current)
        return 1

    def _recover_running_tasks(self):
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT t.id,t.waha_message_id,r.waha_message_id AS ledger_message_id "
                "FROM follow_up_tasks t LEFT JOIN automated_send_requests r "
                "ON r.client_request_id=t.client_request_id WHERE t.state='RUNNING'"
            ).fetchall()
            for row in rows:
                message_id = row["waha_message_id"] or row["ledger_message_id"]
                if message_id:
                    connection.execute(
                        "UPDATE follow_up_tasks SET state='SENT',waha_message_id=?,error_code=NULL,"
                        "error_message=NULL,updated_at=?,completed_at=? WHERE id=? AND state='RUNNING'",
                        (message_id, now, now, row["id"]),
                    )
                else:
                    connection.execute(
                        "UPDATE follow_up_tasks SET state='UNKNOWN',error_code='RESTART_RECOVERY_UNKNOWN',"
                        "error_message='服务重启前的发送结果无法确认',updated_at=?,completed_at=? "
                        "WHERE id=? AND state='RUNNING'",
                        (now, now, row["id"]),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def resume_expired_takeovers(self, now=None):
        current = self._now(now)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE chat_takeovers SET state='AI_ELIGIBLE',resumed_at=?,updated_at=?,"
                "auto_resume_at=NULL WHERE state='HUMAN_TAKEOVER' "
                "AND auto_resume_at IS NOT NULL AND auto_resume_at<=?",
                (current, current, current),
            ).rowcount
            connection.commit()
            return int(changed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _run_loop(self):
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:
                pass
            try:
                self.resume_expired_takeovers()
            except Exception:
                pass
            self._stop_event.wait(self.scan_interval)

    def start(self):
        with self._lifecycle_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="chat-automation", daemon=True,
            )
            self._thread.start()

    def stop(self):
        with self._lifecycle_lock:
            thread = self._thread
            if thread is None:
                return
            self._stop_event.set()
            if thread is threading.current_thread():
                return
            # Keep start() serialized until the active external call and worker fully exit.
            thread.join()
            if self._thread is thread:
                self._thread = None
