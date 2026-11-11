import hashlib
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DAY_SECONDS = 24 * 60 * 60
MARKETING_MAX_MESSAGE_CHARS = 2000
MARKETING_RETRY_SECONDS = 60 * 60
DEFAULT_MARKETING_PROMPT = (
    "请根据客户最近的聊天记录，主动写一条简短、自然、对客户有帮助的营销跟进消息。"
    "使用客户最近使用的语言，不要提及你是 AI；不要编造价格、库存、优惠、账号或服务承诺。"
    "如果没有合适的联系理由，请只返回空字符串。只输出可以直接发送给客户的消息正文。"
)


class MarketingAutomationError(RuntimeError):
    code = "MARKETING_AUTOMATION_ERROR"


def _as_bool(value):
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = int(default)
    return max(int(minimum), min(int(maximum), parsed))


class MarketingAutomationService:
    """Periodically send one safe, AI-generated marketing message per customer.

    This service deliberately has no public HTTP surface and receives all raw chat
    identifiers only from ChatService's internal candidate method. The separate
    ledger protects the monthly campaign from duplicate sends across worker loops
    and process restarts.
    """

    def __init__(self, database_path, chat_service, settings_loader, sessions_loader,
                 message_generator, logger=None, clock=time.time):
        self.database_path = Path(database_path)
        self.chat_service = chat_service
        self.settings_loader = settings_loader
        self.sessions_loader = sessions_loader
        self.message_generator = message_generator
        self.logger = logger
        self.clock = clock
        self._last_scan = {}

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _now(self, value=None):
        return int(self.clock() if value is None else value)

    def _safe_log(self, level, event, message, session):
        if not self.logger:
            return
        try:
            self.logger(level, event, message, session)
        except TypeError:
            try:
                self.logger(level, message)
            except TypeError:
                try:
                    self.logger(message)
                except Exception:
                    return
            except Exception:
                return
        except Exception:
            return

    @staticmethod
    def _settings_value(settings, key, default=""):
        return settings.get(key, default) if isinstance(settings, dict) else default

    @staticmethod
    def _campaign_key(session, prompt, now):
        period = datetime.fromtimestamp(int(now), SHANGHAI_TZ).strftime("%Y-%m")
        raw = f"ai-marketing-v1\x00{session}\x00{period}\x00{prompt}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _request_id(session, chat_key, campaign_key, retry_at=None):
        value = f"ai-marketing:{session}:{chat_key}:{campaign_key}"
        if retry_at is not None:
            value += f":retry:{int(retry_at)}"
        return value[:200]

    @staticmethod
    def _day_start(timestamp):
        current = datetime.fromtimestamp(int(timestamp), SHANGHAI_TZ)
        return int(current.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())

    def _daily_sent_count(self, session, now):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM ai_marketing_send_requests "
                "WHERE session_name=? AND state='SENT' AND updated_at>=?",
                (str(session), self._day_start(now)),
            ).fetchone()
            return int(row[0] if row else 0)
        finally:
            connection.close()

    def _last_sent_at(self, session, chat_key):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT MAX(updated_at) FROM ai_marketing_send_requests "
                "WHERE session_name=? AND chat_key_hmac=? AND state='SENT'",
                (str(session), str(chat_key)),
            ).fetchone()
            return int(row[0]) if row and row[0] is not None else 0
        finally:
            connection.close()

    def _reserve(self, session, chat_key, campaign_key, message, now):
        message_hash = hashlib.sha256(str(message).encode("utf-8")).hexdigest()
        base_request_id = self._request_id(session, chat_key, campaign_key)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM ai_marketing_send_requests "
                "WHERE session_name=? AND chat_key_hmac=? AND campaign_key=?",
                (str(session), str(chat_key), str(campaign_key)),
            ).fetchone()
            if row is not None:
                state = str(row["state"] or "").upper()
                if state == "PENDING":
                    automated = connection.execute(
                        "SELECT state,waha_message_id,error_code FROM automated_send_requests "
                        "WHERE client_request_id=?",
                        (row["client_request_id"],),
                    ).fetchone()
                    automated_state = str(automated["state"] or "").upper() if automated else ""
                    if automated_state == "SENT":
                        connection.execute(
                            "UPDATE ai_marketing_send_requests SET state='SENT',waha_message_id=?,"
                            "error_code=NULL,updated_at=? WHERE id=?",
                            (automated["waha_message_id"], int(now), int(row["id"])),
                        )
                        connection.commit()
                        return {"state": "SENT", "request_id": row["client_request_id"]}
                    if automated_state in {"FAILED", "UNKNOWN"}:
                        connection.execute(
                            "UPDATE ai_marketing_send_requests SET state=?,error_code=?,updated_at=? WHERE id=?",
                            (automated_state, automated["error_code"], int(now), int(row["id"])),
                        )
                        state = automated_state
                    else:
                        connection.commit()
                        return {"state": "PENDING", "request_id": row["client_request_id"]}
                if state == "SENT":
                    connection.commit()
                    return {"state": state, "request_id": row["client_request_id"]}
                if self._now(now) - int(row["updated_at"] or 0) < MARKETING_RETRY_SECONDS:
                    connection.commit()
                    return {"state": state, "request_id": row["client_request_id"]}
                request_id = self._request_id(session, chat_key, campaign_key, now)
                connection.execute(
                    "UPDATE ai_marketing_send_requests SET client_request_id=?,message_hash=?,"
                    "state='PENDING',waha_message_id=NULL,error_code=NULL,updated_at=? WHERE id=?",
                    (request_id, message_hash, int(now), int(row["id"])),
                )
                connection.commit()
                return {"state": "PENDING", "request_id": request_id, "row_id": int(row["id"])}
            cursor = connection.execute(
                "INSERT INTO ai_marketing_send_requests "
                "(session_name,chat_key_hmac,campaign_key,client_request_id,message_hash,state,"
                "waha_message_id,error_code,created_at,updated_at) VALUES (?,?,?,?,?,'PENDING',NULL,NULL,?,?)",
                (str(session), str(chat_key), str(campaign_key), base_request_id, message_hash,
                 int(now), int(now)),
            )
            connection.commit()
            return {"state": "PENDING", "request_id": base_request_id, "row_id": int(cursor.lastrowid)}
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _finish(self, request_id, state, now, error_code=None, message_ref=None):
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE ai_marketing_send_requests SET state=?,waha_message_id=?,error_code=?,updated_at=? "
                "WHERE client_request_id=?",
                (str(state).upper(), message_ref, error_code, int(now), str(request_id)),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _generated_message(result):
        if isinstance(result, dict):
            text = result.get("message") or result.get("content") or ""
            mode = result.get("mode") or "ai"
        elif isinstance(result, (tuple, list)) and len(result) >= 2:
            text, mode = result[0], result[1]
        else:
            text, mode = result, "ai"
        message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or "")).strip()
        return message, str(mode or "").strip().lower()

    @staticmethod
    def _candidate_id(candidate):
        return str(candidate.get("chat_id") or "").strip() if isinstance(candidate, dict) else ""

    def _process_candidate(self, session, candidate, settings, campaign_key, now):
        if not isinstance(candidate, dict):
            return False
        chat_id = self._candidate_id(candidate)
        chat_key = str(candidate.get("chat_key_hmac") or "").strip()
        if not chat_id or not chat_key or chat_id.endswith("@g.us"):
            return False
        history = candidate.get("history")
        if not isinstance(history, list) or not history:
            return False
        try:
            last_inbound = int(candidate.get("last_inbound_at") or 0)
            last_outbound = int(candidate.get("last_outbound_at") or 0)
        except (TypeError, ValueError):
            return False
        idle_days = _bounded_int(self._settings_value(settings, "ai_marketing_idle_days", 7), 7, 1, 365)
        latest_activity = max(last_inbound, last_outbound)
        if last_inbound <= 0 or now - latest_activity < idle_days * DAY_SECONDS:
            return False
        if self.chat_service.is_human_takeover(session, chat_id):
            return False
        if self.chat_service.is_marketing_opted_out(session, chat_id, history=history):
            return False
        if self.chat_service.has_inbound_since(session, chat_id, last_inbound):
            return False
        cooldown_days = _bounded_int(
            self._settings_value(settings, "ai_marketing_cooldown_days", 30), 30, 1, 365
        )
        last_sent_at = self._last_sent_at(session, chat_key)
        if last_sent_at and now - last_sent_at < cooldown_days * DAY_SECONDS:
            return False
        prompt = str(self._settings_value(settings, "ai_marketing_prompt", DEFAULT_MARKETING_PROMPT) or "").strip()
        prompt = prompt[:4000] or DEFAULT_MARKETING_PROMPT
        try:
            generated = self.message_generator(session, chat_id, history, prompt)
            message, mode = self._generated_message(generated)
        except Exception as error:
            self._safe_log("WARN", "marketing.generate_failed", "AI 营销文案生成失败，已跳过", session)
            return False
        if mode not in {"ai", "glm_vision"} or not message or len(message) > MARKETING_MAX_MESSAGE_CHARS:
            return False
        reservation = self._reserve(session, chat_key, campaign_key, message, now)
        if reservation["state"] != "PENDING":
            return False
        request_id = reservation["request_id"]
        try:
            result = self.chat_service.send_automated_text(session, chat_id, message, request_id)
            state = str(result.get("state") if isinstance(result, dict) else "").upper()
            if state != "SENT":
                error_code = "WAHA_SEND_UNKNOWN" if state == "UNKNOWN" else "WAHA_SEND_FAILED"
                self._finish(request_id, "UNKNOWN" if state == "UNKNOWN" else "FAILED", now, error_code)
                return False
            message_ref = result.get("message_ref") if isinstance(result, dict) else None
            self._finish(request_id, "SENT", now, message_ref=message_ref)
            self._safe_log("INFO", "marketing.sent", "已发送一条 AI 主动营销消息", session)
            return True
        except Exception as error:
            error_state = str(getattr(error, "state", "") or "").upper()
            state = "UNKNOWN" if error_state == "UNKNOWN" else "FAILED"
            code = str(getattr(error, "code", "") or "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
                code = "WAHA_SEND_UNKNOWN" if state == "UNKNOWN" else "WAHA_SEND_FAILED"
            self._finish(request_id, state, now, code)
            self._safe_log("WARN", "marketing.send_failed", "AI 主动营销消息发送未完成", session)
            return False

    def _run_session(self, session, now):
        name = str(session or "").strip()
        if not name:
            return 0
        try:
            settings = self.settings_loader(name) or {}
        except Exception:
            return 0
        if not _as_bool(self._settings_value(settings, "ai_marketing_enabled", "0")):
            return 0
        interval = _bounded_int(
            self._settings_value(settings, "ai_marketing_scan_interval_minutes", 15), 15, 5, 1440
        ) * 60
        prior = self._last_scan.get(name)
        if prior is not None and now - prior < interval:
            return 0
        self._last_scan[name] = now
        daily_limit = _bounded_int(
            self._settings_value(settings, "ai_marketing_daily_limit", 20), 20, 0, 1000
        )
        if daily_limit <= 0:
            return 0
        used = self._daily_sent_count(name, now)
        if used >= daily_limit:
            return 0
        idle_days = _bounded_int(self._settings_value(settings, "ai_marketing_idle_days", 7), 7, 1, 365)
        try:
            candidates = self.chat_service.marketing_candidates(name, now, idle_days, limit=200)
        except Exception:
            self._safe_log("WARN", "marketing.scan_failed", "营销客户扫描失败，下一周期重试", name)
            return 0
        prompt = str(self._settings_value(settings, "ai_marketing_prompt", DEFAULT_MARKETING_PROMPT) or "").strip()
        campaign_key = self._campaign_key(name, prompt[:4000] or DEFAULT_MARKETING_PROMPT, now)
        sent = 0
        for candidate in candidates if isinstance(candidates, list) else []:
            if used + sent >= daily_limit:
                break
            if self._process_candidate(name, candidate, settings, campaign_key, now):
                sent += 1
        return sent

    def run_once(self, now=None):
        current = self._now(now)
        try:
            sessions = self.sessions_loader() or []
        except Exception:
            return 0
        total = 0
        for item in sessions:
            name = item.get("session_name") if isinstance(item, dict) else item
            if isinstance(item, dict) and item.get("is_active") is False:
                continue
            try:
                total += self._run_session(name, current)
            except Exception:
                name = str(name or "").strip() or "default"
                self._safe_log("WARN", "marketing.session_failed", "营销周期执行失败，下一周期重试", name)
        return total
