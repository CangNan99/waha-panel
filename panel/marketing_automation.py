import hashlib
import json
import re
import sqlite3
import time
import uuid
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

    def __init__(self, message, code=None):
        self.public_message = str(message)
        if code:
            self.code = str(code)
        super().__init__(self.public_message)


CAMPAIGN_STAGES = {
    "ELIGIBLE",
    "CONTACTED",
    "FOLLOW_UP_DUE",
    "REPLIED",
    "QUALIFIED",
    "HUMAN_HANDOFF",
    "PAUSED",
    "OPTED_OUT",
    "COMPLETED",
}
_TERMINAL_STAGES = {"REPLIED", "QUALIFIED", "HUMAN_HANDOFF", "OPTED_OUT", "COMPLETED"}
_CONTACT_EVENT_ALIASES = {
    "ELIGIBLE": "eligible",
    "CONTACT": "contacted",
    "CONTACTED": "contacted",
    "FIRST_TOUCH": "contacted",
    "FOLLOW_UP": "follow_up_due",
    "FOLLOW_UP_DUE": "follow_up_due",
    "REPLY": "customer_reply",
    "REPLIED": "customer_reply",
    "CUSTOMER_REPLY": "customer_reply",
    "QUALIFY": "qualified",
    "QUALIFIED": "qualified",
    "HUMAN": "human_takeover",
    "HUMAN_TAKEOVER": "human_takeover",
    "HUMAN_HANDOFF": "human_takeover",
    "PAUSE": "pause",
    "PAUSED": "pause",
    "RESUME": "resume",
    "OPT_OUT": "opt_out",
    "OPTOUT": "opt_out",
    "OPTED_OUT": "opt_out",
    "MAX_TOUCHES": "max_touches",
    "COMPLETE": "complete",
    "COMPLETED": "complete",
}
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


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
                 message_generator, logger=None, clock=time.time, delivery_service=None):
        self.database_path = Path(database_path)
        self.chat_service = chat_service
        self.settings_loader = settings_loader
        self.sessions_loader = sessions_loader
        self.message_generator = message_generator
        self.logger = logger
        self.clock = clock
        # Optional unified text + product-image coordinator.  Keeping this
        # injectable preserves the legacy text-only path for old deployments
        # and for callers that have not enabled campaign image delivery yet.
        self.delivery_service = delivery_service
        self._last_scan = {}
        self._ensure_campaign_schema()

    def _ensure_campaign_schema(self):
        """Make the additive campaign schema available for standalone workers."""
        connection = self._connect()
        try:
            migration = Path(__file__).with_name("migrations") / "023_marketing_campaigns.sql"
            connection.executescript(migration.read_text(encoding="utf-8"))
            delivery_migration = Path(__file__).with_name("migrations") / "024_marketing_delivery.sql"
            connection.executescript(delivery_migration.read_text(encoding="utf-8"))
            existing = {str(row[1]) for row in connection.execute("PRAGMA table_info(marketing_events)")}
            for definition in ("message_ref TEXT", "reason_code TEXT"):
                column = definition.split(" ", 1)[0]
                if column not in existing:
                    connection.execute("ALTER TABLE marketing_events ADD COLUMN " + definition)
            connection.commit()
        finally:
            connection.close()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _now(self, value=None):
        return int(self.clock() if value is None else value)

    @staticmethod
    def _strict_bool(value, default=False):
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        text = str(value).strip().casefold()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
        raise MarketingAutomationError("活动开关参数无效", "INVALID_CAMPAIGN_FLAG")

    @staticmethod
    def _json_object(value, field, default):
        if value is None:
            return dict(default)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError) as error:
                raise MarketingAutomationError(f"{field}格式无效", "INVALID_CAMPAIGN_JSON") from error
        if not isinstance(value, dict):
            raise MarketingAutomationError(f"{field}必须是对象", "INVALID_CAMPAIGN_JSON")
        try:
            json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError) as error:
            raise MarketingAutomationError(f"{field}格式无效", "INVALID_CAMPAIGN_JSON") from error
        return dict(value)

    @staticmethod
    def _json_list(value, field, default=None):
        if value is None:
            return list(default or [])
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, (list, tuple, set)):
            raise MarketingAutomationError(f"{field}必须是数组", "INVALID_CAMPAIGN_JSON")
        result = []
        seen = set()
        for item in value:
            text = str(item or "").strip()
            if not text or len(text) > 200:
                raise MarketingAutomationError(f"{field}包含无效值", "INVALID_CAMPAIGN_RULE")
            if text not in seen:
                seen.add(text)
                result.append(text)
        if len(result) > 200:
            raise MarketingAutomationError(f"{field}数量过多", "INVALID_CAMPAIGN_RULE")
        return result

    @staticmethod
    def _validate_time(value, field):
        text = str(value or "").strip()
        if not _TIME_RE.fullmatch(text):
            raise MarketingAutomationError(f"{field}必须是 HH:MM", "INVALID_QUIET_HOURS")
        return text

    @staticmethod
    def _strict_int(value, field, minimum, maximum):
        try:
            parsed = int(str(value).strip())
        except (TypeError, ValueError) as error:
            raise MarketingAutomationError(f"{field}必须是整数", "INVALID_CAMPAIGN_RULE") from error
        if not minimum <= parsed <= maximum:
            raise MarketingAutomationError(
                f"{field}必须在 {minimum}-{maximum} 之间", "INVALID_CAMPAIGN_RULE"
            )
        return parsed

    @classmethod
    def _validate_target_rules(cls, value):
        rules = cls._json_object(value, "目标规则", {})
        if any(str(key).strip().casefold() in {
            "chat_id", "chat_ids", "customer_id", "customer_ids", "phone", "phones",
        } for key in rules):
            raise MarketingAutomationError("目标规则不能包含客户原始标识", "RAW_CUSTOMER_ID_FORBIDDEN")
        if "sessions" in rules:
            rules["sessions"] = cls._json_list(rules.get("sessions"), "目标会话")
        for key in ("include_labels", "exclude_labels", "labels"):
            if key in rules:
                rules[key] = cls._json_list(rules.get(key), key)
        if "idle_days" in rules:
            rules["idle_days"] = cls._strict_int(rules.get("idle_days"), "空闲天数", 1, 365)
        if "min_idle_days" in rules:
            rules["min_idle_days"] = cls._strict_int(rules.get("min_idle_days"), "最小空闲天数", 1, 365)
        if "max_idle_days" in rules:
            rules["max_idle_days"] = cls._strict_int(rules.get("max_idle_days"), "最大空闲天数", 1, 3650)
        if "min_idle_days" in rules and "max_idle_days" in rules and rules["min_idle_days"] > rules["max_idle_days"]:
            raise MarketingAutomationError("目标空闲天数范围无效", "INVALID_CAMPAIGN_RULE")
        if "idle_days" in rules and "min_idle_days" in rules and rules["idle_days"] < rules["min_idle_days"]:
            raise MarketingAutomationError("目标空闲天数与最小范围冲突", "INVALID_CAMPAIGN_RULE")
        if "idle_days" in rules and "max_idle_days" in rules and rules["idle_days"] > rules["max_idle_days"]:
            raise MarketingAutomationError("目标空闲天数与最大范围冲突", "INVALID_CAMPAIGN_RULE")
        return rules

    @classmethod
    def _validate_cadence(cls, value):
        cadence = cls._json_object(value, "活动节奏", {})
        first_delay = cadence.get("first_delay_hours", cadence.get("initial_delay_hours", 0))
        follow_up = cadence.get("follow_up_hours", cadence.get("interval_hours", 24))
        try:
            first_delay = float(first_delay)
            follow_up = float(follow_up)
        except (TypeError, ValueError) as error:
            raise MarketingAutomationError("活动节奏必须是数字", "INVALID_CADENCE") from error
        if not 0 <= first_delay <= 30 * 24 or not 1 <= follow_up <= 30 * 24:
            raise MarketingAutomationError("活动节奏范围无效", "INVALID_CADENCE")
        cadence["first_delay_hours"] = first_delay if first_delay % 1 else int(first_delay)
        cadence["follow_up_hours"] = follow_up if follow_up % 1 else int(follow_up)
        return cadence

    @classmethod
    def _validate_quiet_hours(cls, value):
        quiet = cls._json_object(value, "安静时段", {})
        if not quiet:
            return {}
        if not quiet.get("start") or not quiet.get("end"):
            raise MarketingAutomationError("安静时段必须同时提供开始和结束时间", "INVALID_QUIET_HOURS")
        quiet["start"] = cls._validate_time(quiet.get("start"), "安静时段开始时间")
        quiet["end"] = cls._validate_time(quiet.get("end"), "安静时段结束时间")
        timezone_name = str(quiet.get("timezone") or "Asia/Shanghai").strip()[:64]
        try:
            ZoneInfo(timezone_name)
        except Exception as error:
            raise MarketingAutomationError("安静时区无效", "INVALID_QUIET_HOURS") from error
        quiet["timezone"] = timezone_name
        return quiet

    @classmethod
    def _campaign_payload(cls, payload, prior=None):
        if not isinstance(payload, dict):
            raise MarketingAutomationError("活动数据格式无效", "INVALID_CAMPAIGN")
        merged = dict(prior or {})
        merged.update(payload)
        name = str(merged.get("name") or "").strip()
        objective = str(merged.get("objective") or "").strip()
        if not name:
            raise MarketingAutomationError("活动名称不能为空", "INVALID_CAMPAIGN")
        if len(name) > 200:
            raise MarketingAutomationError("活动名称不能超过 200 个字符", "INVALID_CAMPAIGN")
        if not objective:
            raise MarketingAutomationError("活动目标不能为空", "INVALID_CAMPAIGN")
        if len(objective) > 4000:
            raise MarketingAutomationError("活动目标不能超过 4000 个字符", "INVALID_CAMPAIGN")
        target_rules = cls._validate_target_rules(merged.get("target_rules", merged.get("target_rules_json")))
        product_scope = cls._json_list(merged.get("product_scope", merged.get("product_scope_json")), "产品范围")
        cadence = cls._validate_cadence(merged.get("cadence", merged.get("cadence_json")))
        quiet_hours = cls._validate_quiet_hours(merged.get("quiet_hours", merged.get("quiet_hours_json")))
        try:
            max_touches = int(str(merged.get("max_touches", 1)).strip())
        except (TypeError, ValueError) as error:
            raise MarketingAutomationError("最大触达次数必须是整数", "INVALID_MAX_TOUCHES") from error
        if not 1 <= max_touches <= 100:
            raise MarketingAutomationError("最大触达次数必须在 1-100 之间", "INVALID_MAX_TOUCHES")
        language_policy = str(merged.get("language_policy") or "customer").strip()[:64] or "customer"
        active = cls._strict_bool(merged.get("active"), True)
        paused_reason = str(merged.get("paused_reason") or "").strip()[:500] or None
        return {
            "campaign_id": str(merged.get("campaign_id") or "").strip(),
            "name": name,
            "objective": objective,
            "target_rules": target_rules,
            "product_scope": product_scope,
            "allow_image_send": cls._strict_bool(merged.get("allow_image_send"), False),
            "cadence": cadence,
            "max_touches": max_touches,
            "quiet_hours": quiet_hours,
            "language_policy": language_policy,
            "active": active,
            "paused_reason": paused_reason,
        }

    @staticmethod
    def _campaign_row(row):
        if row is None:
            return None
        item = dict(row)
        for key, fallback in (
            ("target_rules", "target_rules_json"),
            ("product_scope", "product_scope_json"),
            ("cadence", "cadence_json"),
            ("quiet_hours", "quiet_hours_json"),
        ):
            try:
                item[key] = json.loads(item.pop(fallback) or ("{}" if key != "product_scope" else "[]"))
            except (TypeError, ValueError):
                item[key] = {} if key != "product_scope" else []
        item["active"] = bool(item.get("active"))
        item["allow_image_send"] = bool(item.get("allow_image_send"))
        return item

    def _campaign(self, campaign_id, connection=None):
        own = connection is None
        connection = connection or self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM marketing_campaigns WHERE campaign_id=?", (str(campaign_id),)
            ).fetchone()
            return self._campaign_row(row)
        finally:
            if own:
                connection.close()

    def _campaign_is_active(self, campaign_id):
        campaign = self._campaign(campaign_id)
        return bool(campaign and campaign.get("active"))

    def create_campaign(self, payload):
        prepared = self._campaign_payload(payload)
        campaign_id = prepared["campaign_id"] or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", campaign_id):
            raise MarketingAutomationError("活动编号格式无效", "INVALID_CAMPAIGN_ID")
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO marketing_campaigns(campaign_id,name,objective,target_rules_json,product_scope_json,"
                "allow_image_send,cadence_json,max_touches,quiet_hours_json,language_policy,active,paused_reason,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    campaign_id, prepared["name"], prepared["objective"],
                    json.dumps(prepared["target_rules"], ensure_ascii=False, sort_keys=True),
                    json.dumps(prepared["product_scope"], ensure_ascii=False),
                    int(prepared["allow_image_send"]),
                    json.dumps(prepared["cadence"], ensure_ascii=False, sort_keys=True),
                    prepared["max_touches"],
                    json.dumps(prepared["quiet_hours"], ensure_ascii=False, sort_keys=True),
                    prepared["language_policy"], int(prepared["active"]), prepared["paused_reason"], now, now,
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as error:
            connection.rollback()
            raise MarketingAutomationError("活动编号已存在", "CAMPAIGN_EXISTS") from error
        finally:
            connection.close()
        return self._campaign(campaign_id)

    def update_campaign(self, campaign_id, payload):
        campaign = self._campaign(campaign_id)
        if campaign is None:
            raise MarketingAutomationError("活动不存在", "CAMPAIGN_NOT_FOUND")
        prepared = self._campaign_payload(payload, campaign)
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE marketing_campaigns SET name=?,objective=?,target_rules_json=?,product_scope_json=?,"
                "allow_image_send=?,cadence_json=?,max_touches=?,quiet_hours_json=?,language_policy=?,active=?,"
                "paused_reason=?,updated_at=? WHERE campaign_id=?",
                (
                    prepared["name"], prepared["objective"],
                    json.dumps(prepared["target_rules"], ensure_ascii=False, sort_keys=True),
                    json.dumps(prepared["product_scope"], ensure_ascii=False), int(prepared["allow_image_send"]),
                    json.dumps(prepared["cadence"], ensure_ascii=False, sort_keys=True), prepared["max_touches"],
                    json.dumps(prepared["quiet_hours"], ensure_ascii=False, sort_keys=True), prepared["language_policy"],
                    int(prepared["active"]), prepared["paused_reason"], now, str(campaign_id),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return self._campaign(campaign_id)

    def list_campaigns(self, include_inactive=False):
        connection = self._connect()
        try:
            query = "SELECT * FROM marketing_campaigns"
            if not include_inactive:
                query += " WHERE active=1"
            rows = connection.execute(query + " ORDER BY updated_at DESC, campaign_id").fetchall()
        finally:
            connection.close()
        items = [self._campaign_row(row) for row in rows]
        return {"items": items, "total": len(items)}

    def pause_campaign(self, campaign_id, reason):
        campaign = self._campaign(campaign_id)
        if campaign is None:
            raise MarketingAutomationError("活动不存在", "CAMPAIGN_NOT_FOUND")
        text = str(reason or "人工暂停").strip()[:500] or "人工暂停"
        return self.update_campaign(campaign_id, {"active": False, "paused_reason": text})

    def resume_campaign(self, campaign_id):
        campaign = self._campaign(campaign_id)
        if campaign is None:
            raise MarketingAutomationError("活动不存在", "CAMPAIGN_NOT_FOUND")
        return self.update_campaign(campaign_id, {"active": True, "paused_reason": None})

    @staticmethod
    def _state_row(row):
        if row is None:
            return None
        item = dict(row)
        item["opt_out"] = bool(item.get("opt_out"))
        item["touch_count"] = int(item.get("touch_count") or 0)
        return item

    def contact_state(self, session_name, chat_key_hmac, campaign_id):
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM marketing_contact_states WHERE campaign_id=? AND session_name=? AND chat_key_hmac=?",
                (str(campaign_id), str(session_name), str(chat_key_hmac)),
            ).fetchone()
        finally:
            connection.close()
        return self._state_row(row)

    def _ensure_contact(self, state, now, connection):
        if not isinstance(state, dict):
            raise MarketingAutomationError("客户状态格式无效", "INVALID_CONTACT_STATE")
        campaign_id = str(state.get("campaign_id") or "").strip()
        session_name = str(state.get("session_name") or "").strip()
        chat_key = str(state.get("chat_key_hmac") or "").strip()
        if not campaign_id or not session_name or not chat_key or len(chat_key) > 256:
            raise MarketingAutomationError("客户状态标识无效", "INVALID_CONTACT_STATE")
        row = connection.execute(
            "SELECT * FROM marketing_contact_states WHERE campaign_id=? AND session_name=? AND chat_key_hmac=?",
            (campaign_id, session_name, chat_key),
        ).fetchone()
        if row is not None:
            return self._state_row(row)
        connection.execute(
            "INSERT INTO marketing_contact_states(campaign_id,session_name,chat_key_hmac,stage,interest_product_id,"
            "last_contact_at,last_reply_at,next_action_at,touch_count,paused_reason,opt_out,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                campaign_id, session_name, chat_key, str(state.get("stage") or "ELIGIBLE").upper(),
                state.get("interest_product_id"), state.get("last_contact_at"), state.get("last_reply_at"),
                state.get("next_action_at"), int(state.get("touch_count") or 0), state.get("paused_reason"),
                int(bool(state.get("opt_out"))), now, now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM marketing_contact_states WHERE campaign_id=? AND session_name=? AND chat_key_hmac=?",
            (campaign_id, session_name, chat_key),
        ).fetchone()
        return self._state_row(row)

    def _record_event(self, state, event_type, now, connection, product_id=None,
                      metadata=None, message_ref=None, reason_code=None):
        payload = metadata if isinstance(metadata, dict) else {}
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            encoded = "{}"
        connection.execute(
            "INSERT INTO marketing_events(event_id,campaign_id,session_name,chat_key_hmac,event_type,product_id,asset_id,"
            "message_ref,reason_code,metadata_json,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                uuid.uuid4().hex, state["campaign_id"], state["session_name"], state["chat_key_hmac"],
                str(event_type)[:80], str(product_id or state.get("interest_product_id") or "")[:200] or None,
                None, str(message_ref or "")[:256] or None,
                str(reason_code or "")[:80] or None, encoded, int(now),
            ),
        )

    def transition_contact(self, state, event, now):
        current = dict(state or {})
        now = self._now(now)
        canonical = _CONTACT_EVENT_ALIASES.get(str(event or "").strip().upper(), str(event or "").strip().lower())
        if canonical not in {
            "eligible", "contacted", "follow_up_due", "customer_reply", "qualified", "human_takeover",
            "pause", "resume", "opt_out", "max_touches", "complete",
        }:
            raise MarketingAutomationError("客户状态事件无效", "INVALID_CONTACT_EVENT")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            persisted = self._ensure_contact(current, now, connection)
            current = dict(persisted or current)
            campaign = self._campaign(current.get("campaign_id"), connection)
            if campaign is None:
                raise MarketingAutomationError("活动不存在", "CAMPAIGN_NOT_FOUND")
            stage = str(current.get("stage") or "ELIGIBLE").upper()
            if stage not in CAMPAIGN_STAGES:
                stage = "ELIGIBLE"
            touch_count = int(current.get("touch_count") or 0)
            next_action = current.get("next_action_at")
            last_contact = current.get("last_contact_at")
            last_reply = current.get("last_reply_at")
            paused_reason = current.get("paused_reason")
            opt_out = bool(current.get("opt_out"))
            product_id = current.get("interest_product_id")
            # Opt-out, customer replies and human handoff are suppression events;
            # only a matching resume can move a paused contact back to ELIGIBLE.
            if opt_out and canonical != "opt_out":
                connection.commit()
                return current
            guarded_events = {
                "REPLIED": {"customer_reply", "qualified", "human_takeover", "pause", "opt_out", "complete"},
                "QUALIFIED": {"qualified", "human_takeover", "pause", "opt_out", "complete"},
                "HUMAN_HANDOFF": {"resume", "pause", "opt_out", "complete"},
                "PAUSED": {"resume", "pause", "opt_out", "complete"},
            }
            if stage in guarded_events and canonical not in guarded_events[stage]:
                connection.commit()
                return current
            if stage in {"OPTED_OUT", "COMPLETED"} and canonical not in {"opt_out", "complete"}:
                connection.commit()
                return current
            if canonical == "eligible":
                stage = "ELIGIBLE"
                try:
                    next_action = int(current["next_action_at"]) if current.get("next_action_at") is not None else None
                except (TypeError, ValueError):
                    next_action = None
            elif canonical == "contacted":
                if stage not in {"ELIGIBLE", "FOLLOW_UP_DUE", "PAUSED"}:
                    connection.commit()
                    return current
                if touch_count >= campaign["max_touches"]:
                    canonical = "max_touches"
                    stage, next_action = "COMPLETED", None
                else:
                    stage = "CONTACTED"
                    touch_count += 1
                    last_contact = now
                    follow_hours = float(campaign.get("cadence", {}).get("follow_up_hours", 24) or 24)
                    next_action = now + int(follow_hours * 3600)
            if canonical == "follow_up_due":
                if stage == "CONTACTED" and touch_count < campaign["max_touches"]:
                    stage = "FOLLOW_UP_DUE"
                elif touch_count >= campaign["max_touches"]:
                    stage, next_action = "COMPLETED", None
            elif canonical == "customer_reply":
                stage, next_action, last_reply = "REPLIED", None, now
            elif canonical == "qualified":
                if stage in {"REPLIED", "CONTACTED", "FOLLOW_UP_DUE"}:
                    stage, next_action = "QUALIFIED", None
            elif canonical == "human_takeover":
                stage, next_action, paused_reason = "HUMAN_HANDOFF", None, "human_takeover"
            elif canonical == "pause":
                stage, next_action = "PAUSED", None
                paused_reason = str(current.get("paused_reason") or "manual_pause")[:500]
            elif canonical == "resume":
                if stage in {"PAUSED", "HUMAN_HANDOFF"}:
                    stage, next_action, paused_reason = "ELIGIBLE", now, None
            elif canonical == "opt_out":
                stage, next_action, opt_out, paused_reason = "OPTED_OUT", None, True, "opt_out"
            elif canonical in {"max_touches", "complete"}:
                stage, next_action = "COMPLETED", None
            connection.execute(
                "UPDATE marketing_contact_states SET stage=?,interest_product_id=?,last_contact_at=?,last_reply_at=?,"
                "next_action_at=?,touch_count=?,paused_reason=?,opt_out=?,updated_at=? WHERE contact_id=?",
                (
                    stage, product_id, last_contact, last_reply, next_action, touch_count, paused_reason,
                    int(opt_out), now, current["contact_id"],
                ),
            )
            current.update({
                "stage": stage, "interest_product_id": product_id, "last_contact_at": last_contact,
                "last_reply_at": last_reply, "next_action_at": next_action, "touch_count": touch_count,
                "paused_reason": paused_reason, "opt_out": opt_out, "updated_at": now,
            })
            self._record_event(current, canonical, now, connection, product_id=product_id)
            connection.commit()
            return current
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def record_customer_message(self, session_name, chat_id, text="", now=None):
        """Stop existing campaign contacts as soon as a customer replies.

        The raw chat id is accepted only at this internal boundary and is
        immediately converted to the same HMAC used by the ChatService.
        Contacts that have not been created by a campaign are not materialized.
        """
        if self.chat_service is None or not hasattr(self.chat_service, "_chat_key"):
            return 0
        session = str(session_name or "").strip()
        value = str(chat_id or "").strip()
        if not session or not value:
            return 0
        try:
            chat_key = self.chat_service._chat_key(session, value)
        except Exception:
            return 0
        stamp = self._now(now)
        changed = 0
        history = [{"from_me": False, "body": str(text or "")}] if text else None
        campaigns = self.list_campaigns(include_inactive=True).get("items", [])
        for campaign in campaigns:
            state = self.contact_state(session, chat_key, campaign["campaign_id"])
            if not state:
                continue
            try:
                reason = self.chat_service.marketing_stop_reason(
                    session,
                    value,
                    since_timestamp=0,
                    history=history,
                )
            except Exception:
                reason = "CUSTOMER_REPLIED"
            event = "opt_out" if reason == "OPT_OUT" else "customer_reply"
            prior_stage = state.get("stage")
            updated = self.transition_contact(state, event, stamp)
            if updated.get("stage") != prior_stage or event == "opt_out":
                changed += 1
        return changed

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
    def _delivery_id(session, chat_key, campaign_id, touch_number):
        """Derive a per-contact delivery key without persisting raw chat ids."""
        raw = "\x00".join(
            (
                "marketing-delivery-v1",
                str(session or "").strip(),
                str(chat_key or "").strip(),
                str(campaign_id or "").strip(),
                str(int(touch_number or 0)),
            )
        ).encode("utf-8")
        return "delivery-" + hashlib.sha256(raw).hexdigest()[:48]

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

    @classmethod
    def _quota_reached(cls, connection, session, now, daily_limit):
        if daily_limit is None:
            return False
        try:
            limit = int(daily_limit)
        except (TypeError, ValueError):
            return False
        if limit <= 0:
            return True
        row = connection.execute(
            "SELECT COUNT(*) FROM ai_marketing_send_requests "
            "WHERE session_name=? AND updated_at>=? AND state IN ('SENT','PENDING','UNKNOWN')",
            (str(session), cls._day_start(now)),
        ).fetchone()
        return int(row[0] if row else 0) >= limit

    def _reserve(self, session, chat_key, campaign_key, message, now, daily_limit=None):
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
                        return {
                            "state": "SENT",
                            "request_id": row["client_request_id"],
                            "reconciled": True,
                        }
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
                if self._quota_reached(connection, session, now, daily_limit):
                    connection.commit()
                    return {"state": "QUOTA_EXCEEDED", "request_id": row["client_request_id"]}
                request_id = self._request_id(session, chat_key, campaign_key, now)
                connection.execute(
                    "UPDATE ai_marketing_send_requests SET client_request_id=?,message_hash=?,"
                    "state='PENDING',waha_message_id=NULL,error_code=NULL,updated_at=? WHERE id=?",
                    (request_id, message_hash, int(now), int(row["id"])),
                )
                connection.commit()
                return {"state": "PENDING", "request_id": request_id, "row_id": int(row["id"])}
            if self._quota_reached(connection, session, now, daily_limit):
                connection.commit()
                return {"state": "QUOTA_EXCEEDED", "request_id": None}
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
        final_state = str(state).upper()
        connection = self._connect()
        try:
            connection.execute(
                "UPDATE ai_marketing_send_requests SET state=?,waha_message_id=?,error_code=?,updated_at=? "
                "WHERE client_request_id=? AND NOT (state='SENT' AND ? <> 'SENT')",
                (final_state, message_ref, error_code, int(now), str(request_id), final_state),
            )
            connection.commit()
        finally:
            connection.close()

    @staticmethod
    def _generated_message(result):
        if isinstance(result, dict):
            text = result.get("message") or result.get("content") or result.get("text") or ""
            mode = result.get("mode") or "ai"
        elif isinstance(result, (tuple, list)) and len(result) >= 2:
            text, mode = result[0], result[1]
        else:
            text, mode = result, "ai"
        message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", str(text or "")).strip()
        return message, str(mode or "").strip().lower()

    def generate_marketing_action(self, session_name, chat_id, history, campaign):
        """Generate a sanitized campaign action for the delivery coordinator.

        The model may suggest a product key and variant, but it cannot choose
        an asset id, URL or filesystem path.  The delivery service resolves the
        final active asset from the campaign product scope after text succeeds.
        """
        if not isinstance(campaign, dict):
            campaign = {}
        prompt = str(campaign.get("objective") or DEFAULT_MARKETING_PROMPT).strip()
        prompt += "\n营销活动规则：只发送与活动目标相关、可核实且简短的文字；不要编造价格、库存、优惠或承诺。"
        language_policy = str(campaign.get("language_policy") or "customer").strip()[:64]
        if language_policy and language_policy != "customer":
            prompt += f" 语言策略：按活动配置使用 {language_policy}。"
        allow_image = self._strict_bool(campaign.get("allow_image_send"), False)
        scope = campaign.get("product_scope") or campaign.get("product_scope_json") or []
        if isinstance(scope, str):
            try:
                scope = json.loads(scope)
            except (TypeError, ValueError):
                scope = [part.strip() for part in scope.split(",") if part.strip()]
        if not isinstance(scope, (list, tuple, set)):
            scope = []
        scope = [str(item).strip() for item in scope if str(item or "").strip()][:200]
        if not allow_image:
            prompt += " 不要主动发送产品图片。"
        elif scope:
            prompt += " 只有在客户已有明确产品兴趣时，才可建议活动产品范围内的一个产品图片。"
        prompt = prompt[:4000]
        generated = self.message_generator(session_name, chat_id, history, prompt)
        message, mode = self._generated_message(generated)
        if mode not in {"ai", "glm_vision"} or not message or len(message) > MARKETING_MAX_MESSAGE_CHARS:
            return {
                "message": "",
                "mode": mode,
                "allow_image_send": False,
                "product_id": None,
                "variant": "",
                "caption": "",
            }
        product_id = None
        variant = ""
        caption = ""
        if isinstance(generated, dict):
            raw_product = generated.get("product_id") or generated.get("product_key")
            image = generated.get("image") if isinstance(generated.get("image"), dict) else {}
            raw_product = raw_product or image.get("product_id") or image.get("product_key")
            candidate = str(raw_product or "").strip()
            if allow_image and candidate and candidate in set(scope):
                product_id = candidate
                variant = re.sub(r"[\x00-\x1f\x7f]", "", str(generated.get("variant") or image.get("variant") or "")).strip()[:120]
                caption = re.sub(r"[\x00-\x1f\x7f]", "", str(generated.get("caption") or image.get("caption") or "")).strip()[:4096]
        # Production's legacy marketing generator intentionally returns a
        # plain ``(text, mode)`` tuple.  When it does not emit structured
        # product metadata, select a product only if the customer's history
        # contains exactly one product key from the campaign scope.  This is a
        # server-side, scope-limited interest check; no model-provided asset,
        # URL or internal image id is accepted.
        if allow_image and not product_id and scope:
            history_text = " ".join(
                str(item.get("body") or item.get("caption") or "")
                for item in (history or [])
                if isinstance(item, dict)
            ).casefold()
            matches = [item for item in scope if str(item).casefold() in history_text]
            if len(matches) == 1:
                product_id = matches[0]
        return {
            "message": message,
            "mode": mode,
            "allow_image_send": bool(allow_image and product_id),
            "product_id": product_id,
            "variant": variant,
            "caption": caption,
            "product_scope": scope,
        }

    @staticmethod
    def _candidate_id(candidate):
        return str(candidate.get("chat_id") or "").strip() if isinstance(candidate, dict) else ""

    @staticmethod
    def _quiet_hours_active(campaign, now):
        quiet = campaign.get("quiet_hours") if isinstance(campaign, dict) else {}
        if not isinstance(quiet, dict) or not quiet.get("start") or not quiet.get("end"):
            return False
        try:
            start_hour, start_minute = [int(item) for item in str(quiet["start"]).split(":", 1)]
            end_hour, end_minute = [int(item) for item in str(quiet["end"]).split(":", 1)]
            try:
                timezone = ZoneInfo(str(quiet.get("timezone") or "Asia/Shanghai"))
            except Exception:
                timezone = SHANGHAI_TZ
            current = datetime.fromtimestamp(int(now), timezone)
            minute = current.hour * 60 + current.minute
            start = start_hour * 60 + start_minute
            end = end_hour * 60 + end_minute
        except (TypeError, ValueError):
            return False
        if start == end:
            return True
        if start < end:
            return start <= minute < end
        return minute >= start or minute < end

    @staticmethod
    def _candidate_target_matches(candidate, campaign, session, now=None):
        rules = campaign.get("target_rules") if isinstance(campaign, dict) else {}
        if not isinstance(rules, dict):
            return True
        sessions = rules.get("sessions") or []
        if sessions and str(session) not in {str(item) for item in sessions}:
            return False
        if now is not None and (rules.get("min_idle_days") is not None or rules.get("max_idle_days") is not None):
            try:
                latest_activity = max(
                    int(candidate.get("last_inbound_at") or 0),
                    int(candidate.get("last_outbound_at") or 0),
                )
                idle_days = max(0.0, (int(now) - latest_activity) / float(DAY_SECONDS))
            except (TypeError, ValueError):
                return False
            if rules.get("min_idle_days") is not None and idle_days < int(rules["min_idle_days"]):
                return False
            if rules.get("max_idle_days") is not None and idle_days > int(rules["max_idle_days"]):
                return False
        if rules.get("include_labels") or rules.get("labels"):
            expected = {str(item).casefold() for item in (rules.get("include_labels") or rules.get("labels") or [])}
            labels = {str(item).casefold() for item in (candidate.get("labels") or [])}
            history_text = " ".join(
                str(item.get("body") or item.get("caption") or "")
                for item in (candidate.get("history") or []) if isinstance(item, dict)
            ).casefold()
            if not (expected & labels) and not any(item in history_text for item in expected):
                return False
        if rules.get("exclude_labels"):
            excluded = {str(item).casefold() for item in rules.get("exclude_labels")}
            labels = {str(item).casefold() for item in (candidate.get("labels") or [])}
            history_text = " ".join(
                str(item.get("body") or item.get("caption") or "")
                for item in (candidate.get("history") or []) if isinstance(item, dict)
            ).casefold()
            if excluded & labels or any(item in history_text for item in excluded):
                return False
        return True

    def _campaign_stop_reason(self, session, candidate, state):
        chat_id = self._candidate_id(candidate)
        if not chat_id:
            return "INVALID_CHAT"
        # A newly created contact has no last_contact_at yet. Use the
        # candidate snapshot as the baseline so a reply arriving between the
        # idle scan and the send-time check is still detected. The candidate's
        # own inbound message is excluded because has_inbound_since uses a
        # strict greater-than cutoff.
        try:
            since_timestamp = int(state.get("last_contact_at") or 0)
        except (TypeError, ValueError):
            since_timestamp = 0
        if since_timestamp <= 0:
            try:
                since_timestamp = int(candidate.get("last_inbound_at") or 0)
            except (TypeError, ValueError):
                since_timestamp = 0
        helper = getattr(self.chat_service, "marketing_stop_reason", None)
        if callable(helper):
            try:
                return str(helper(
                    session,
                    chat_id,
                    since_timestamp=since_timestamp,
                    history=candidate.get("history"),
                ) or "")
            except TypeError:
                try:
                    return str(helper(session, chat_id, since_timestamp, candidate.get("history")) or "")
                except Exception:
                    return "STOP_CHECK_FAILED"
            except Exception:
                return "STOP_CHECK_FAILED"
        try:
            if self.chat_service.is_human_takeover(session, chat_id):
                return "HUMAN_TAKEOVER"
            if self.chat_service.is_marketing_opted_out(session, chat_id, history=candidate.get("history")):
                return "OPT_OUT"
            if since_timestamp > 0 and self.chat_service.has_inbound_since(
                    session, chat_id, since_timestamp):
                return "CUSTOMER_REPLIED"
        except Exception:
            return "STOP_CHECK_FAILED"
        return ""

    def _prepare_campaign_contact(self, session, campaign, candidate, now):
        chat_key = str(candidate.get("chat_key_hmac") or "").strip()
        if not chat_key:
            return None
        state = self.contact_state(session, chat_key, campaign["campaign_id"])
        if state is None:
            cadence = campaign.get("cadence") if isinstance(campaign.get("cadence"), dict) else {}
            try:
                first_delay = max(0.0, float(cadence.get("first_delay_hours", 0) or 0))
            except (TypeError, ValueError):
                first_delay = 0.0
            next_action = now + int(first_delay * 3600) if first_delay else None
            state = self.transition_contact({
                "campaign_id": campaign["campaign_id"],
                "session_name": session,
                "chat_key_hmac": chat_key,
                "next_action_at": next_action,
            }, "eligible", now)
        stop_reason = self._campaign_stop_reason(session, candidate, state)
        if stop_reason == "HUMAN_TAKEOVER":
            return self.transition_contact(state, "human_takeover", now)
        if stop_reason == "OPT_OUT":
            return self.transition_contact(state, "opt_out", now)
        if stop_reason == "CUSTOMER_REPLIED":
            return self.transition_contact(state, "customer_reply", now)
        if stop_reason:
            return None
        stage = str(state.get("stage") or "ELIGIBLE").upper()
        if stage == "CONTACTED" and state.get("next_action_at") and int(state["next_action_at"]) <= int(now):
            state = self.transition_contact(state, "follow_up_due", now)
            stage = str(state.get("stage") or "").upper()
        if stage in {"REPLIED", "QUALIFIED", "HUMAN_HANDOFF", "PAUSED", "OPTED_OUT", "COMPLETED"}:
            return None
        if int(state.get("touch_count") or 0) >= int(campaign.get("max_touches") or 1):
            return self.transition_contact(state, "max_touches", now)
        if state.get("next_action_at") and int(state["next_action_at"]) > int(now):
            return None
        return state

    def _process_campaign_candidate(self, session, candidate, settings, campaign, now, daily_limit=None):
        if not isinstance(candidate, dict) or not self._candidate_target_matches(candidate, campaign, session, now):
            return False
        if self._quiet_hours_active(campaign, now):
            return False
        chat_id = self._candidate_id(candidate)
        chat_key = str(candidate.get("chat_key_hmac") or "").strip()
        if not chat_id or not chat_key or chat_id.endswith("@g.us"):
            return False
        state = self._prepare_campaign_contact(session, campaign, candidate, now)
        if not state or str(state.get("stage") or "").upper() in {"PAUSED", "OPTED_OUT", "COMPLETED", "REPLIED", "QUALIFIED", "HUMAN_HANDOFF"}:
            return False
        history = candidate.get("history")
        if not isinstance(history, list) or not history:
            return False
        try:
            action = self.generate_marketing_action(session, chat_id, history, campaign)
            message = action.get("message") or ""
            mode = action.get("mode") or ""
        except Exception:
            self._safe_log("WARN", "marketing.generate_failed", "AI 营销文案生成失败，已跳过", session)
            return False
        if mode not in {"ai", "glm_vision"} or not message or len(message) > MARKETING_MAX_MESSAGE_CHARS:
            return False
        request_id = None
        send_confirmed = False
        try:
            # Re-check reply, opt-out and takeover before reserving this touch.
            if not self._campaign_is_active(campaign["campaign_id"]):
                return False
            refreshed = self._prepare_campaign_contact(session, campaign, candidate, now)
            if not refreshed or str(refreshed.get("stage") or "").upper() not in {"ELIGIBLE", "FOLLOW_UP_DUE"}:
                return False
            touch_number = int(refreshed.get("touch_count") or 0) + 1
            # The touch sequence is stable across calendar boundaries.  A
            # month component would create a second ledger row when a pending
            # or failed touch is retried after midnight on the first day.
            campaign_key = f"campaign:{campaign['campaign_id']}:touch:{touch_number}"
            if self.delivery_service is not None:
                delivery = {
                    "delivery_id": self._delivery_id(
                        session,
                        chat_key,
                        campaign["campaign_id"],
                        touch_number,
                    ),
                    "campaign_id": campaign["campaign_id"],
                    "session_name": session,
                    "chat_id": chat_id,
                    "chat_key_hmac": chat_key,
                    "text": message,
                    "product_id": action.get("product_id"),
                    "variant": action.get("variant") or "",
                    "image_caption": action.get("caption") or "",
                    "product_scope": action.get("product_scope") or campaign.get("product_scope") or [],
                    "allow_image_send": bool(action.get("allow_image_send")) and bool(campaign.get("allow_image_send")),
                    "touch_count": int(refreshed.get("touch_count") or 0),
                    "max_touches": int(campaign.get("max_touches") or 1),
                    "customer_last_contact_at": int(refreshed.get("last_contact_at") or candidate.get("last_inbound_at") or 0),
                    "daily_limit": daily_limit,
                    "cooldown_days": self._settings_value(settings, "ai_marketing_cooldown_days", 30),
                    "marketing_product_image_enabled": self._settings_value(settings, "marketing_product_image_enabled", True),
                    "product_image_global_kill_switch": self._settings_value(settings, "product_image_global_kill_switch", False),
                    "history": history,
                    "campaign_active_checker": self._campaign_is_active,
                }
                # A campaign can be paused while the action payload is being
                # assembled.  Check immediately before entering the delivery
                # coordinator so a pause never races into a new transport.
                if not self._campaign_is_active(campaign["campaign_id"]):
                    return False
                latest_before_delivery = self._prepare_campaign_contact(session, campaign, candidate, now)
                if not latest_before_delivery or str(latest_before_delivery.get("stage") or "").upper() not in {"ELIGIBLE", "FOLLOW_UP_DUE"}:
                    return False
                result = self.delivery_service.send_text_and_optional_image(delivery)
                delivery_state = str(result.get("state") or "").upper() if isinstance(result, dict) else ""
                if delivery_state == "SENT":
                    try:
                        self.transition_contact(refreshed, "contacted", now)
                    except Exception:
                        self._safe_log("WARN", "marketing.contact_state_failed", "营销发送已恢复，但客户状态更新未完成", session)
                    self._safe_log("INFO", "marketing.sent", "已发送一条活动营销消息", session)
                    return True
                return False
            reservation = self._reserve(
                session, chat_key, campaign_key, message, now, daily_limit=daily_limit
            )
            if reservation.get("state") == "SENT" and reservation.get("reconciled"):
                # The generic automated ledger confirmed a transport success
                # after a previous worker crashed. Advance the contact state
                # without sending a duplicate or counting this scan twice.
                latest = self._prepare_campaign_contact(session, campaign, candidate, now)
                if latest and str(latest.get("stage") or "").upper() in {"ELIGIBLE", "FOLLOW_UP_DUE"}:
                    try:
                        self.transition_contact(latest, "contacted", now)
                    except Exception:
                        self._safe_log(
                            "WARN",
                            "marketing.contact_state_failed",
                            "营销发送已恢复，但客户状态更新未完成",
                            session,
                        )
                return False
            if reservation["state"] != "PENDING":
                return False
            request_id = reservation["request_id"]
            # A second check closes the reservation-to-send race.  A concurrent
            # worker may have moved this contact to a terminal/suppressed stage.
            if not self._campaign_is_active(campaign["campaign_id"]):
                self._finish(request_id, "FAILED", now, "MARKETING_STOPPED")
                return False
            latest = self._prepare_campaign_contact(session, campaign, candidate, now)
            if not latest or str(latest.get("stage") or "").upper() not in {"ELIGIBLE", "FOLLOW_UP_DUE"}:
                self._finish(request_id, "FAILED", now, "MARKETING_STOPPED")
                return False
            result = self.chat_service.send_automated_text(session, chat_id, message, request_id)
            state_name = str(result.get("state") if isinstance(result, dict) else "").upper()
            if state_name != "SENT":
                code = "WAHA_SEND_UNKNOWN" if state_name == "UNKNOWN" else "WAHA_SEND_FAILED"
                self._finish(request_id, "UNKNOWN" if state_name == "UNKNOWN" else "FAILED", now, code)
                return False
            # Once the transport confirms SENT, never downgrade the delivery
            # to FAILED because a later contact-state write failed.  Leaving
            # the ledger SENT lets the next scan reconcile the contact safely.
            send_confirmed = True
            self._finish(request_id, "SENT", now, message_ref=(result.get("message_ref") if isinstance(result, dict) else None))
            self.transition_contact(latest, "contacted", now)
            self._safe_log("INFO", "marketing.sent", "已发送一条活动营销消息", session)
            return True
        except Exception as error:
            error_state = str(getattr(error, "state", "") or "").upper()
            state_name = "UNKNOWN" if error_state == "UNKNOWN" else "FAILED"
            code = str(getattr(error, "code", "") or "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
                code = "WAHA_SEND_UNKNOWN" if state_name == "UNKNOWN" else "WAHA_SEND_FAILED"
            if request_id and not send_confirmed:
                self._finish(request_id, state_name, now, code)
            if send_confirmed:
                self._safe_log("WARN", "marketing.contact_state_failed", "营销消息已发送，但客户状态更新未完成，保留已发送账本", session)
                return True
            self._safe_log("WARN", "marketing.send_failed", "活动营销消息发送未完成", session)
            return False

    def _run_campaign(self, session, campaign, settings, now, remaining):
        rules = campaign.get("target_rules") if isinstance(campaign.get("target_rules"), dict) else {}
        if rules.get("idle_days") is not None:
            scan_idle_days = rules.get("idle_days")
        elif rules.get("min_idle_days") is not None:
            scan_idle_days = rules.get("min_idle_days")
        elif rules.get("max_idle_days") is not None:
            # The candidate scan is lower-bound based. A max-only window must
            # start at the minimum supported idle age; the upper bound is
            # applied later by _candidate_target_matches.
            scan_idle_days = 1
        else:
            scan_idle_days = settings.get("ai_marketing_idle_days", 7)
        idle_days = _bounded_int(scan_idle_days, 7, 1, 365)
        try:
            candidates = self.chat_service.marketing_candidates(session, now, idle_days, limit=200)
        except Exception:
            self._safe_log("WARN", "marketing.scan_failed", "营销客户扫描失败，下一周期重试", session)
            return 0
        sent = 0
        for candidate in candidates if isinstance(candidates, list) else []:
            if sent >= remaining:
                break
            if self._process_campaign_candidate(
                    session, candidate, settings, campaign, now, daily_limit=remaining):
                sent += 1
        return sent

    def _process_candidate(self, session, candidate, settings, campaign_key, now, daily_limit=None):
        if not isinstance(candidate, dict):
            return False
        marketing_images_enabled = str(self._settings_value(settings, "marketing_product_image_enabled", True)).strip().casefold() in {"1", "true", "yes", "on", "enabled"}
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
        if not marketing_images_enabled:
            prompt += "\n营销策略：不要主动发送产品图片，只发送文字。"
        prompt = prompt[:4000] or DEFAULT_MARKETING_PROMPT
        try:
            generated = self.message_generator(session, chat_id, history, prompt)
            message, mode = self._generated_message(generated)
        except Exception as error:
            self._safe_log("WARN", "marketing.generate_failed", "AI 营销文案生成失败，已跳过", session)
            return False
        if mode not in {"ai", "glm_vision"} or not message or len(message) > MARKETING_MAX_MESSAGE_CHARS:
            return False
        reservation = self._reserve(
            session, chat_key, campaign_key, message, now, daily_limit=daily_limit
        )
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
        campaigns = self.list_campaigns(include_inactive=False).get("items", [])
        if campaigns:
            total = 0
            for campaign in campaigns:
                if used + total >= daily_limit:
                    break
                total += self._run_campaign(
                    name,
                    campaign,
                    settings,
                    now,
                    daily_limit - used - total,
                )
            return total
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
            if self._process_candidate(
                    name, candidate, settings, campaign_key, now, daily_limit=daily_limit):
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
