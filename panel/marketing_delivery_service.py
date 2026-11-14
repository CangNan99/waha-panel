"""Coordinate idempotent marketing text and optional product-image delivery.

The campaign state machine owns eligibility; this module owns the transport
boundary.  A delivery is reserved once, text is sent first, and a product
image is considered only after the text transport has returned ``SENT``.  The
marketing table keeps an HMAC chat identity and derived request ids only.  Raw
chat identifiers remain in the existing ChatService automated-send ledger.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path


class MarketingDeliveryError(RuntimeError):
    code = "MARKETING_DELIVERY_ERROR"

    def __init__(self, message, code=None):
        self.public_message = str(message)
        if code:
            self.code = str(code)
        super().__init__(self.public_message)


_STATES = {"PENDING", "SENT", "FAILED", "UNKNOWN", "SKIPPED", "STOPPED"}
_IMAGE_STATES = {"PENDING", "SENT", "FAILED", "UNKNOWN", "SKIPPED", "NOT_REQUESTED"}
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")


def _as_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def _clean_text(value, limit=2000):
    text = _CONTROL_RE.sub("", str(value or "")).strip()
    if len(text) > limit:
        raise MarketingDeliveryError("营销文案过长", "MARKETING_TEXT_TOO_LONG")
    return text


class MarketingDeliveryService:
    """Reserve, send and reconcile one campaign delivery bundle.

    ``chat_service`` and ``product_image_service`` are deliberately injected so
    tests can use deterministic fakes and production wiring can continue to use
    ChatService/ProductImageService.  ``product_image_service`` may be omitted
    for text-only compatibility mode.
    """

    def __init__(
        self,
        database_path,
        chat_service,
        product_image_service=None,
        marketing_service=None,
        settings_loader=None,
        stop_checker=None,
        clock=time.time,
        logger=None,
        **kwargs,
    ):
        self.database_path = Path(database_path)
        self.chat_service = chat_service
        self.product_image_service = (
            product_image_service
            or kwargs.get("image_service")
            or kwargs.get("product_images")
        )
        self.marketing_service = marketing_service
        self.settings_loader = settings_loader
        self.stop_checker = stop_checker
        self.clock = clock
        self.logger = logger
        self._runtime = {}
        self._ensure_schema()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _ensure_schema(self):
        migration = Path(__file__).with_name("migrations") / "024_marketing_delivery.sql"
        connection = self._connect()
        try:
            connection.executescript(migration.read_text(encoding="utf-8"))
            existing = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(marketing_deliveries)")
            }
            # The table is additive so a partially upgraded local database can
            # still be opened by a newer worker.
            for definition in (
                "text_message_ref TEXT",
                "image_message_ref TEXT",
                "variant TEXT NOT NULL DEFAULT ''",
                "asset_id TEXT",
                "product_scope_json TEXT NOT NULL DEFAULT '[]'",
                "allow_image_send INTEGER NOT NULL DEFAULT 0",
                "touch_number INTEGER NOT NULL DEFAULT 0",
                "image_retry_count INTEGER NOT NULL DEFAULT 0",
            ):
                name = definition.split(" ", 1)[0]
                if name not in existing:
                    connection.execute(
                        "ALTER TABLE marketing_deliveries ADD COLUMN " + definition
                    )
            connection.commit()
        finally:
            connection.close()

    def _now(self, value=None):
        return int(self.clock() if value is None else value)

    @staticmethod
    def _message_hash(text):
        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()

    @staticmethod
    def _scope(value):
        if value is None:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = [part.strip() for part in value.split(",") if part.strip()]
        if not isinstance(value, (list, tuple, set)):
            raise MarketingDeliveryError("活动产品范围无效", "INVALID_PRODUCT_SCOPE")
        result = []
        seen = set()
        for item in value:
            key = str(item or "").strip()
            if not key or len(key) > 200 or key in seen:
                continue
            seen.add(key)
            result.append(key)
        return result[:200]

    @staticmethod
    def _safe_id(value, field):
        text = str(value or "").strip()
        if not text or not _ID_RE.fullmatch(text):
            raise MarketingDeliveryError(f"{field}无效", "INVALID_DELIVERY_ID")
        return text

    @staticmethod
    def _derived_id(delivery):
        raw = "\x00".join(
            str(delivery.get(key) or "").strip()
            for key in ("campaign_id", "session_name", "chat_key_hmac", "touch_number", "text")
        )
        return "delivery-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]

    @staticmethod
    def _request_id(delivery_id, kind, retry=0):
        suffix = f":retry:{int(retry)}" if retry else ""
        return f"marketing-delivery:{delivery_id}:{kind}{suffix}"[:200]

    def _safe_log(self, level, event, message, session=""):
        if not self.logger:
            return
        try:
            self.logger(level, event, message, session)
        except TypeError:
            try:
                self.logger(level, message)
            except Exception:
                return
        except Exception:
            return

    @staticmethod
    def _row_public(row, reserved=None):
        if row is None:
            return None
        result = {
            "delivery_id": str(row["delivery_id"]),
            "campaign_id": str(row["campaign_id"]),
            "session_name": str(row["session_name"]),
            "chat_key_hmac": str(row["chat_key_hmac"]),
            "state": str(row["state"]),
            "text_state": str(row["text_state"]),
            "text_request_id": str(row["text_request_id"]),
            "text_message_ref": row["text_message_ref"],
            "image_state": str(row["image_state"]),
            "image_request_id": row["image_request_id"],
            "image_message_ref": row["image_message_ref"],
            "product_id": row["product_id"],
            "variant": str(row["variant"] or ""),
            "asset_id": row["asset_id"],
            "allow_image_send": bool(row["allow_image_send"]),
            "touch_number": int(row["touch_number"] or 0),
            "error_code": row["error_code"],
            "created_at": int(row["created_at"]),
            "updated_at": int(row["updated_at"]),
        }
        if reserved is not None:
            result["reserved"] = bool(reserved)
        return result

    def _get(self, delivery_id, connection=None):
        own = connection is None
        connection = connection or self._connect()
        try:
            return connection.execute(
                "SELECT * FROM marketing_deliveries WHERE delivery_id=?",
                (str(delivery_id),),
            ).fetchone()
        finally:
            if own:
                connection.close()

    def _runtime_delivery(self, delivery_id, fallback=None):
        value = dict(self._runtime.get(str(delivery_id)) or {})
        if fallback:
            value.update(dict(fallback))
        return value

    def _set(self, delivery_id, **fields):
        allowed = {
            "state",
            "text_state",
            "text_message_ref",
            "image_state",
            "image_request_id",
            "image_message_ref",
            "asset_id",
            "error_code",
            "image_retry_count",
            "updated_at",
        }
        updates = {key: value for key, value in fields.items() if key in allowed}
        if not updates:
            return self._get(delivery_id)
        updates.setdefault("updated_at", self._now())
        assignments = ",".join(f"{key}=?" for key in updates)
        connection = self._connect()
        try:
            connection.execute(
                f"UPDATE marketing_deliveries SET {assignments} WHERE delivery_id=?",
                tuple(updates.values()) + (str(delivery_id),),
            )
            connection.commit()
            return self._get(delivery_id, connection)
        finally:
            connection.close()

    def _stop_reason(self, delivery):
        if callable(self.stop_checker):
            try:
                reason = self.stop_checker(delivery) or ""
                if reason:
                    return str(reason)[:80]
            except Exception:
                return "STOP_CHECK_FAILED"
        explicit = str(delivery.get("stop_reason") or "").strip()
        if explicit:
            return explicit[:80]
        session = str(delivery.get("session_name") or "").strip()
        chat_id = str(delivery.get("chat_id") or "").strip()
        if not session or not chat_id or self.chat_service is None:
            return ""
        try:
            if delivery.get("customer_last_contact_at") is not None:
                helper = getattr(self.chat_service, "has_inbound_since", None)
                if callable(helper) and helper(
                    session,
                    chat_id,
                    int(delivery.get("customer_last_contact_at") or 0),
                ):
                    return "CUSTOMER_REPLIED"
            helper = getattr(self.chat_service, "is_marketing_opted_out", None)
            if callable(helper) and helper(session, chat_id, history=delivery.get("history")):
                return "OPT_OUT"
            helper = getattr(self.chat_service, "is_human_takeover", None)
            if callable(helper) and helper(session, chat_id):
                return "HUMAN_TAKEOVER"
        except Exception:
            return "STOP_CHECK_FAILED"
        return ""

    def _skip_payload(self, delivery, state, reason):
        delivery_id = self._safe_id(delivery["delivery_id"], "delivery_id")
        now = self._now()
        text = _clean_text(delivery.get("text"), 2000)
        message_hash = self._message_hash(text)
        campaign_id = str(delivery.get("campaign_id") or "legacy").strip()[:200] or "legacy"
        session = str(delivery.get("session_name") or "").strip()[:64]
        chat_key = str(delivery.get("chat_key_hmac") or "").strip()[:200]
        if not session or not chat_key:
            raise MarketingDeliveryError("营销身份参数无效", "INVALID_MARKETING_IDENTITY")
        scope = self._scope(delivery.get("product_scope"))
        product_id = str(delivery.get("product_id") or "").strip() or None
        connection = self._connect()
        try:
            connection.execute(
                "INSERT OR IGNORE INTO marketing_deliveries "
                "(delivery_id,campaign_id,session_name,chat_key_hmac,message_hash,text_request_id,text_state," 
                "image_state,product_id,variant,product_scope_json,allow_image_send,touch_number,state,error_code,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,'SKIPPED',?,?,?,0,?, ?,?,?,?)",
                (
                    delivery_id,
                    campaign_id,
                    session,
                    chat_key,
                    message_hash,
                    self._request_id(delivery_id, "text"),
                    "SKIPPED",
                    product_id,
                    str(delivery.get("variant") or "").strip()[:120],
                    json.dumps(scope, ensure_ascii=False),
                    int(delivery.get("touch_number") or delivery.get("touch_count") or 0),
                    state,
                    str(reason or "POLICY_BLOCKED")[:80],
                    now,
                    now,
                ),
            )
            connection.commit()
            row = self._get(delivery_id, connection)
        finally:
            connection.close()
        self._runtime[delivery_id] = dict(delivery)
        return self._row_public(row, reserved=False)

    def reserve(self, delivery):
        """Atomically reserve one campaign touch and return its public state."""
        if not isinstance(delivery, dict):
            raise MarketingDeliveryError("营销发送数据格式无效", "INVALID_DELIVERY")
        normalized = dict(delivery)
        text = _clean_text(normalized.get("text") or normalized.get("message"), 2000)
        normalized["text"] = text
        normalized["campaign_id"] = str(normalized.get("campaign_id") or "legacy").strip()[:200] or "legacy"
        normalized["session_name"] = str(normalized.get("session_name") or "").strip()[:64]
        normalized["chat_key_hmac"] = str(normalized.get("chat_key_hmac") or "").strip()[:200]
        if not normalized["session_name"] or not normalized["chat_key_hmac"]:
            raise MarketingDeliveryError("营销身份参数无效", "INVALID_MARKETING_IDENTITY")
        touch_count = int(normalized.get("touch_count") or 0)
        touch_number = int(normalized.get("touch_number") or (touch_count + 1))
        if touch_number < 1:
            touch_number = 1
        normalized["touch_number"] = touch_number
        normalized["delivery_id"] = self._safe_id(
            normalized.get("delivery_id") or self._derived_id(normalized), "delivery_id"
        )
        scope = self._scope(normalized.get("product_scope"))
        normalized["product_scope"] = scope
        product_id = str(normalized.get("product_id") or "").strip() or None
        normalized["product_id"] = product_id
        allow_image = _as_bool(normalized.get("allow_image_send"), False)
        allow_image = allow_image and _as_bool(normalized.get("marketing_product_image_enabled"), True)
        allow_image = allow_image and not _as_bool(normalized.get("product_image_global_kill_switch"), False)
        if product_id and scope and product_id not in scope:
            allow_image = False
        if not product_id or not scope:
            allow_image = False
        normalized["allow_image_send"] = allow_image
        normalized["variant"] = str(normalized.get("variant") or "").strip()[:120]
        normalized["daily_limit"] = normalized.get("daily_limit")
        normalized["cooldown_seconds"] = normalized.get("cooldown_seconds")
        normalized["max_touches"] = normalized.get("max_touches")
        delivery_id = normalized["delivery_id"]
        self._runtime[delivery_id] = dict(normalized)
        message_hash = self._message_hash(text)
        text_request_id = self._request_id(delivery_id, "text")
        image_state = "REQUESTED" if allow_image else "NOT_REQUESTED"
        now = self._now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM marketing_deliveries WHERE delivery_id=?", (delivery_id,)
            ).fetchone()
            if existing:
                if (
                    str(existing["session_name"]) != normalized["session_name"]
                    or str(existing["chat_key_hmac"]) != normalized["chat_key_hmac"]
                    or str(existing["message_hash"]) != message_hash
                ):
                    raise MarketingDeliveryError("发送编号已用于其他活动", "DELIVERY_CONFLICT")
                connection.commit()
                return self._row_public(existing, reserved=False)
            duplicate = connection.execute(
                "SELECT * FROM marketing_deliveries WHERE session_name=? AND chat_key_hmac=? "
                "AND campaign_id=? AND touch_number=? ORDER BY created_at LIMIT 1",
                (
                    normalized["session_name"],
                    normalized["chat_key_hmac"],
                    normalized["campaign_id"],
                    touch_number,
                ),
            ).fetchone()
            if duplicate:
                connection.commit()
                return self._row_public(duplicate, reserved=False)
            reason = self._stop_reason(normalized)
            if reason:
                connection.commit()
                return self._skip_payload(normalized, "STOPPED", reason)
            try:
                max_touches = int(normalized.get("max_touches")) if normalized.get("max_touches") is not None else 0
            except (TypeError, ValueError):
                max_touches = 0
            if max_touches and touch_count >= max_touches:
                connection.commit()
                return self._skip_payload(normalized, "SKIPPED", "MAX_TOUCHES")
            daily_limit = normalized.get("daily_limit")
            try:
                daily_limit = int(daily_limit) if daily_limit is not None else None
            except (TypeError, ValueError):
                daily_limit = None
            if daily_limit is not None:
                if daily_limit <= 0:
                    connection.commit()
                    return self._skip_payload(normalized, "SKIPPED", "DAILY_LIMIT")
                day_start = now - now % 86400
                used = connection.execute(
                    "SELECT COUNT(*) FROM marketing_deliveries WHERE session_name=? AND updated_at>=? "
                    "AND state IN ('PENDING','IMAGE_PENDING','SENT','UNKNOWN')",
                    (normalized["session_name"], day_start),
                ).fetchone()[0]
                if int(used) >= daily_limit:
                    connection.commit()
                    return self._skip_payload(normalized, "SKIPPED", "DAILY_LIMIT")
            cooldown = normalized.get("cooldown_seconds")
            if cooldown is None and normalized.get("cooldown_days") is not None:
                try:
                    cooldown = int(normalized.get("cooldown_days")) * 86400
                except (TypeError, ValueError):
                    cooldown = None
            try:
                cooldown = int(cooldown) if cooldown is not None else 0
            except (TypeError, ValueError):
                cooldown = 0
            if cooldown > 0:
                prior = connection.execute(
                    "SELECT updated_at FROM marketing_deliveries WHERE session_name=? AND chat_key_hmac=? "
                    "AND state IN ('PENDING','SENT','UNKNOWN') ORDER BY updated_at DESC LIMIT 1",
                    (normalized["session_name"], normalized["chat_key_hmac"]),
                ).fetchone()
                if prior and now - int(prior[0] or 0) < cooldown:
                    connection.commit()
                    return self._skip_payload(normalized, "SKIPPED", "CUSTOMER_COOLDOWN")
            connection.execute(
                "INSERT INTO marketing_deliveries "
                "(delivery_id,campaign_id,session_name,chat_key_hmac,message_hash,text_request_id,text_state," 
                "image_state,product_id,variant,product_scope_json,allow_image_send,touch_number,state,error_code,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?, ?,?,?,?,?,?,'PENDING',NULL,?,?)",
                (
                    delivery_id,
                    normalized["campaign_id"],
                    normalized["session_name"],
                    normalized["chat_key_hmac"],
                    message_hash,
                    text_request_id,
                    "PENDING",
                    image_state,
                    product_id,
                    normalized["variant"],
                    json.dumps(scope, ensure_ascii=False),
                    int(allow_image),
                    touch_number,
                    now,
                    now,
                ),
            )
            connection.commit()
            row = self._get(delivery_id, connection)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._row_public(row, reserved=True)

    @staticmethod
    def _result_state(result, default="FAILED"):
        if isinstance(result, dict):
            state = str(result.get("state") or "").upper()
        else:
            state = ""
        return state if state in {"SENT", "FAILED", "UNKNOWN", "PENDING"} else default

    def _finish_image_ledger(self, request_id, state, message_ref=None, error_code=None, session_name=None):
        if not request_id or self.product_image_service is None:
            return
        method = getattr(self.product_image_service, "finish_auto_send", None)
        if callable(method):
            try:
                message_id = message_ref
                if isinstance(message_ref, dict):
                    message_id = message_ref.get("message_ref") or message_ref.get("id")
                # ChatService returns an opaque encoded message_ref to callers;
                # its product-image ledger stores only the underlying WAHA id.
                decoder = getattr(self.chat_service, "_decode_message", None)
                if message_id and callable(decoder) and session_name:
                    try:
                        _chat, decoded = decoder(session_name, message_id)
                        message_id = decoded
                    except Exception:
                        pass
                method(request_id, state, message_id=message_id, error_code=error_code)
            except TypeError:
                method(request_id, state, message_ref, error_code)

    def _resolve_asset(self, product_id, variant):
        service = self.product_image_service
        if service is None or not product_id:
            return None
        resolver = getattr(service, "resolve_product_asset", None)
        if not callable(resolver):
            return None
        try:
            asset = resolver(product_id, variant or "", "primary")
        except TypeError:
            asset = resolver(product_id, variant or "")
        if not isinstance(asset, dict) or str(asset.get("status") or "FOUND").upper() != "FOUND":
            return None
        asset_id = str(asset.get("id") or asset.get("asset_id") or "").strip()
        if not asset_id:
            return None
        # ProductImageService calls this field ``id`` in public asset payloads
        # while lightweight adapters and older callers use ``asset_id``.  Keep
        # one server-side canonical value before touching any ledger.
        asset = dict(asset)
        asset["id"] = asset_id
        return asset

    def _image_policy_allowed(self, delivery):
        """Re-read image switches immediately before reserving an image."""
        if _as_bool(delivery.get("product_image_global_kill_switch"), False):
            return False, "GLOBAL_KILL_SWITCH"
        if not _as_bool(delivery.get("marketing_product_image_enabled"), True):
            return False, "MARKETING_IMAGE_DISABLED"
        if callable(self.settings_loader):
            try:
                settings = self.settings_loader(str(delivery.get("session_name") or "")) or {}
            except Exception:
                return False, "MARKETING_SETTINGS_UNAVAILABLE"
            if not _as_bool(settings.get("marketing_product_image_enabled"), True):
                return False, "MARKETING_IMAGE_DISABLED"
            if _as_bool(settings.get("product_image_global_kill_switch"), False):
                return False, "GLOBAL_KILL_SWITCH"
        service = self.product_image_service
        getter = getattr(service, "settings_payload", None) if service is not None else None
        if callable(getter):
            try:
                image_settings = getter() or {}
            except Exception:
                return False, "IMAGE_SETTINGS_UNAVAILABLE"
            if not _as_bool(image_settings.get("auto_send_enabled"), True):
                return False, "PRODUCT_IMAGE_DISABLED"
        return True, ""

    def _send_image(self, row, delivery, explicit_reconcile=False):
        delivery_id = str(row["delivery_id"])
        if str(row["text_state"]).upper() != "SENT":
            return self._row_public(row)
        if str(row["image_state"]).upper() in {"SENT", "SKIPPED", "NOT_REQUESTED"}:
            return self._row_public(row)
        if str(row["image_state"]).upper() in {"UNKNOWN", "PENDING"} and not explicit_reconcile:
            return self._row_public(row)
        if explicit_reconcile and str(row["image_state"]).upper() not in {"FAILED", "REQUESTED"}:
            return self._row_public(row)
        reason = self._stop_reason(delivery)
        if reason:
            updated = self._set(delivery_id, image_state="SKIPPED", state="SENT", error_code=reason)
            return self._row_public(updated)
        allowed, policy_reason = self._image_policy_allowed(delivery)
        if not allowed:
            updated = self._set(
                delivery_id,
                image_state="SKIPPED",
                state="SENT",
                error_code=policy_reason,
            )
            return self._row_public(updated)
        product_id = str(row["product_id"] or "").strip()
        scope = self._scope(row["product_scope_json"])
        if not product_id or (scope and product_id not in scope):
            updated = self._set(delivery_id, image_state="SKIPPED", state="SENT", error_code="PRODUCT_SCOPE")
            return self._row_public(updated)
        asset = self._resolve_asset(product_id, str(row["variant"] or ""))
        if not asset:
            updated = self._set(delivery_id, image_state="SKIPPED", state="SENT", error_code="PRODUCT_ASSET_NOT_FOUND")
            return self._row_public(updated)
        caption = _clean_text(
            delivery.get("image_caption")
            or delivery.get("caption")
            or asset.get("caption_template")
            or asset.get("title")
            or product_id,
            4096,
        )
        retry = int(row["image_retry_count"] or 0) + (1 if explicit_reconcile else 0)
        message_key = f"{delivery_id}:image" + (f":retry:{retry}" if explicit_reconcile else "")
        reserver = getattr(self.product_image_service, "reserve_auto_send", None)
        if not callable(reserver):
            updated = self._set(delivery_id, image_state="SKIPPED", state="SENT", error_code="IMAGE_SERVICE_UNAVAILABLE")
            return self._row_public(updated)
        try:
            reservation = reserver(
                delivery.get("session_name"),
                delivery.get("chat_id"),
                message_key,
                product_id,
                asset.get("id"),
                "marketing",
                caption,
            )
        except Exception as error:
            code = str(getattr(error, "code", "IMAGE_RESERVATION_FAILED"))[:80]
            updated = self._set(delivery_id, image_state="FAILED", state="FAILED", error_code=code)
            return self._row_public(updated)
        request_id = str((reservation or {}).get("request_id") or "")
        reservation_state = str((reservation or {}).get("state") or "").upper()
        if reservation_state in {"LIMIT", "DAILY_LIMIT"}:
            updated = self._set(delivery_id, image_state="SKIPPED", state="SENT", error_code="DAILY_LIMIT")
            return self._row_public(updated)
        if not request_id:
            updated = self._set(delivery_id, image_state="SKIPPED", state="SENT", error_code="IMAGE_RESERVATION_FAILED")
            return self._row_public(updated)
        if reservation_state == "SENT":
            updated = self._set(
                delivery_id,
                image_request_id=request_id,
                image_state="SENT",
                asset_id=asset.get("id"),
                state="SENT",
                error_code=None,
            )
            return self._row_public(updated)
        if reservation_state in {"PENDING", "UNKNOWN"} and not bool((reservation or {}).get("reserved")):
            updated = self._set(
                delivery_id,
                image_request_id=request_id,
                image_state="UNKNOWN" if reservation_state == "UNKNOWN" else "PENDING",
                asset_id=asset.get("id"),
                state="UNKNOWN" if reservation_state == "UNKNOWN" else "PENDING",
                error_code="IMAGE_RECOVERY_REQUIRED",
            )
            return self._row_public(updated)
        self._set(
            delivery_id,
            image_request_id=request_id,
            image_state="PENDING",
            asset_id=asset.get("id"),
            image_retry_count=retry,
            state="IMAGE_PENDING",
            error_code=None,
        )
        reason = self._stop_reason(delivery)
        if reason:
            self._finish_image_ledger(request_id, "FAILED", error_code="MARKETING_STOPPED")
            updated = self._set(delivery_id, image_state="SKIPPED", state="SENT", error_code=reason)
            return self._row_public(updated)
        preview = getattr(self.product_image_service, "preview", None)
        if not callable(preview):
            self._finish_image_ledger(request_id, "FAILED", error_code="IMAGE_READ_FAILED")
            updated = self._set(delivery_id, image_state="FAILED", state="FAILED", error_code="IMAGE_READ_FAILED")
            return self._row_public(updated)
        try:
            mime, data = preview(asset.get("id"))
            sender = getattr(self.chat_service, "send_automated_image")
            try:
                result = sender(
                    delivery.get("session_name"),
                    delivery.get("chat_id"),
                    asset.get("original_filename") or "product-image",
                    mime or asset.get("mime_type") or "image/png",
                    data,
                    caption,
                    request_id,
                    origin="marketing",
                )
            except TypeError:
                result = sender(
                    delivery.get("session_name"),
                    delivery.get("chat_id"),
                    asset.get("original_filename") or "product-image",
                    mime or asset.get("mime_type") or "image/png",
                    data,
                    caption,
                    request_id,
                )
            state = self._result_state(result)
            message_ref = result.get("message_ref") if isinstance(result, dict) else None
            if state == "SENT":
                self._finish_image_ledger(
                    request_id,
                    "SENT",
                    message_ref=message_ref,
                    session_name=delivery.get("session_name"),
                )
                updated = self._set(
                    delivery_id,
                    image_state="SENT",
                    image_message_ref=message_ref,
                    state="SENT",
                    error_code=None,
                    asset_id=asset.get("id"),
                )
                return self._row_public(updated)
            final = "UNKNOWN" if state == "UNKNOWN" else "FAILED"
            self._finish_image_ledger(request_id, final, error_code="WAHA_SEND_UNKNOWN" if final == "UNKNOWN" else "WAHA_SEND_FAILED")
            updated = self._set(
                delivery_id,
                image_state=final,
                state=final,
                error_code="WAHA_SEND_UNKNOWN" if final == "UNKNOWN" else "WAHA_SEND_FAILED",
                asset_id=asset.get("id"),
            )
            return self._row_public(updated)
        except Exception as error:
            state = str(getattr(error, "state", "") or "").upper()
            final = "UNKNOWN" if state == "UNKNOWN" else "FAILED"
            code = str(getattr(error, "code", "") or "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
                code = "WAHA_SEND_UNKNOWN" if final == "UNKNOWN" else "WAHA_SEND_FAILED"
            self._finish_image_ledger(request_id, final, error_code=code)
            updated = self._set(
                delivery_id,
                image_state=final,
                state=final,
                error_code=code,
                asset_id=asset.get("id"),
            )
            return self._row_public(updated)

    def send_text_and_optional_image(self, delivery):
        reservation = self.reserve(delivery)
        delivery_id = reservation["delivery_id"]
        if not reservation.get("reserved"):
            if reservation.get("state") == "PENDING":
                # A worker may have reserved the bundle and crashed before
                # calling the transport.  Reconcile the generic text ledger
                # first; if it already confirms SENT, reconciliation can also
                # continue a REQUESTED image after a process restart.
                return self.reconcile(delivery_id)
            if reservation.get("state") in {"SENT", "UNKNOWN", "FAILED", "STOPPED", "SKIPPED"}:
                return reservation
        row = self._get(delivery_id)
        if row is None:
            raise MarketingDeliveryError("营销发送记录不存在", "DELIVERY_NOT_FOUND")
        runtime = self._runtime_delivery(delivery_id, delivery)
        if str(row["text_state"]).upper() != "PENDING":
            return self._row_public(row)
        reason = self._stop_reason(runtime)
        if reason:
            updated = self._set(delivery_id, text_state="SKIPPED", image_state="SKIPPED", state="STOPPED", error_code=reason)
            return self._row_public(updated)
        sender = getattr(self.chat_service, "send_automated_text", None)
        if not callable(sender):
            updated = self._set(delivery_id, text_state="FAILED", image_state="SKIPPED", state="FAILED", error_code="TEXT_SERVICE_UNAVAILABLE")
            return self._row_public(updated)
        request_id = str(row["text_request_id"])
        try:
            result = sender(runtime.get("session_name"), runtime.get("chat_id"), runtime.get("text"), request_id)
            state = self._result_state(result)
            message_ref = result.get("message_ref") if isinstance(result, dict) else None
        except Exception as error:
            raw_state = str(getattr(error, "state", "") or "").upper()
            state = "UNKNOWN" if raw_state == "UNKNOWN" else "FAILED"
            message_ref = None
        if state != "SENT":
            updated = self._set(
                delivery_id,
                text_state=state,
                image_state="SKIPPED",
                state=state,
                text_message_ref=message_ref,
                error_code="WAHA_SEND_UNKNOWN" if state == "UNKNOWN" else "WAHA_SEND_FAILED",
            )
            return self._row_public(updated)
        self._set(delivery_id, text_state="SENT", text_message_ref=message_ref, state="SENT", error_code=None)
        row = self._get(delivery_id)
        if str(row["image_state"]).upper() == "NOT_REQUESTED" or not bool(row["allow_image_send"]):
            updated = self._set(delivery_id, image_state="SKIPPED", state="SENT")
            return self._row_public(updated)
        return self._send_image(row, runtime, explicit_reconcile=False)

    def _automated_row(self, request_id):
        if not request_id:
            return None
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT state,waha_message_id,error_code,session_name,chat_id FROM automated_send_requests WHERE client_request_id=?",
                (str(request_id),),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            connection.close()

    def _image_ledger_row(self, request_id):
        if not request_id:
            return None
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT state,waha_message_id,error_code,session_name,chat_key_hmac,message_key FROM product_image_send_requests WHERE request_id=?",
                (str(request_id),),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        finally:
            connection.close()

    def reconcile(self, delivery_id):
        row = self._get(delivery_id)
        if row is None:
            raise MarketingDeliveryError("营销发送记录不存在", "DELIVERY_NOT_FOUND")
        delivery = self._runtime_delivery(delivery_id)
        text_ledger = self._automated_row(row["text_request_id"])
        if str(row["text_state"]).upper() == "PENDING" and text_ledger:
            text_state = str(text_ledger["state"] or "").upper()
            if text_state in {"SENT", "FAILED", "UNKNOWN"}:
                self._set(
                    delivery_id,
                    text_state=text_state,
                    text_message_ref=text_ledger["waha_message_id"],
                    image_state="SKIPPED" if text_state != "SENT" else row["image_state"],
                    state=text_state if text_state != "SENT" else "SENT",
                    error_code=text_ledger["error_code"],
                )
                row = self._get(delivery_id)
        if str(row["text_state"]).upper() != "SENT":
            return self._row_public(row)
        if text_ledger and not delivery.get("chat_id"):
            # Chat identifiers are kept only in ChatService's automated ledger;
            # use that transiently to recover a post-text image after a worker
            # restart without copying the raw value into marketing_deliveries.
            delivery["session_name"] = text_ledger["session_name"]
            delivery["chat_id"] = text_ledger["chat_id"]
        image_state = str(row["image_state"]).upper()
        if image_state in {"NOT_REQUESTED", "SKIPPED", "SENT"}:
            return self._row_public(row)
        # ProductImageService and ChatService intentionally keep separate
        # ledgers.  If the process stopped after WAHA confirmed the image but
        # before the product-image row was finalized, the generic automated
        # ledger is authoritative for the transport result.
        image_transport = self._automated_row(row["image_request_id"])
        if image_transport:
            transport_state = str(image_transport["state"] or "").upper()
            if transport_state == "SENT":
                row = self._set(
                    delivery_id,
                    image_state="SENT",
                    image_message_ref=image_transport["waha_message_id"],
                    state="SENT",
                    error_code=None,
                )
                self._finish_image_ledger(
                    row["image_request_id"],
                    "SENT",
                    message_ref=image_transport["waha_message_id"],
                    session_name=delivery.get("session_name"),
                )
                return self._row_public(row)
            if transport_state in {"PENDING", "UNKNOWN"} and image_state != "FAILED":
                row = self._set(
                    delivery_id,
                    image_state=transport_state,
                    state="UNKNOWN" if transport_state == "UNKNOWN" else "PENDING",
                    error_code="IMAGE_RECOVERY_REQUIRED",
                )
                return self._row_public(row)
        image_ledger = self._image_ledger_row(row["image_request_id"])
        if image_ledger:
            ledger_state = str(image_ledger["state"] or "").upper()
            if ledger_state == "SENT":
                row = self._set(
                    delivery_id,
                    image_state="SENT",
                    image_message_ref=image_ledger["waha_message_id"],
                    state="SENT",
                    error_code=None,
                )
                return self._row_public(row)
            if ledger_state in {"PENDING", "UNKNOWN"}:
                row = self._set(delivery_id, image_state=ledger_state, state="UNKNOWN" if ledger_state == "UNKNOWN" else "PENDING", error_code="IMAGE_RECOVERY_REQUIRED")
                return self._row_public(row)
        if image_state == "FAILED":
            if not delivery.get("chat_id") and text_ledger:
                delivery["session_name"] = text_ledger["session_name"]
                delivery["chat_id"] = text_ledger["chat_id"]
            return self._send_image(row, delivery, explicit_reconcile=True)
        # A worker may have stopped after text confirmation but before image
        # reservation.  Reconcile only if the caller can supply the transient
        # customer id; otherwise leave the row pending for an operator.
        if image_state == "REQUESTED" and delivery.get("chat_id"):
            return self._send_image(row, delivery, explicit_reconcile=True)
        return self._row_public(row)

    def finish(self, delivery_id, state, error_code=None):
        final = str(state or "").upper()
        if final not in _STATES:
            raise MarketingDeliveryError("营销发送状态无效", "INVALID_DELIVERY_STATE")
        row = self._get(delivery_id)
        if row is None:
            raise MarketingDeliveryError("营销发送记录不存在", "DELIVERY_NOT_FOUND")
        if str(row["state"]).upper() == "SENT" and final != "SENT":
            return self._row_public(row)
        updated = self._set(delivery_id, state=final, error_code=(str(error_code)[:80] if error_code else None))
        return self._row_public(updated)


__all__ = ["MarketingDeliveryError", "MarketingDeliveryService"]
