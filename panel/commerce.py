"""WooCommerce/PayPal order-query domain services.

The module has no application or WAHA dependencies.  Network and database
adapters are injectable so the domain can be tested without touching a real
WordPress installation or sending a message.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


SENSITIVE_RE = re.compile(
    r"(?i)(api[_-]?key|password|token|secret|authorization|consumer[_-]?(?:key|secret)|client[_-]?(?:id|secret))"
    r"\s*([:=])\s*([^\s,;}&]+)"
)
PHONE_RE = re.compile(r"^[1-9]\d{6,14}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PAYPAL_METHODS = {"paypal", "paypal_express", "ppcp-gateway", "ppec_paypal"}
PAYPAL_STATUSES = {"created", "saved", "approved", "completed", "captured", "voided", "refunded", "partially_refunded"}


class CommerceError(RuntimeError):
    def __init__(self, code: str, message: str, source: str = "commerce"):
        self.code = code
        self.source = source
        super().__init__(redact_sensitive(message))


def redact_sensitive(value: Any, secrets: Iterable[str] = ()) -> str:
    text = str(value or "")
    for secret in secrets:
        if secret:
            text = text.replace(str(secret), "<redacted>")
    return SENSITIVE_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)[:500]


def _as_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _now() -> int:
    return int(time.time())


def _hash(value: str, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), str(value).encode("utf-8"), hashlib.sha256).hexdigest()


def normalize_phone(value: Any) -> str:
    raw = str(value or "").strip()
    if raw.startswith("+"):
        raw = raw[1:]
    digits = re.sub(r"[\s().-]", "", raw)
    if not PHONE_RE.fullmatch(digits):
        raise ValueError("手机号格式不可靠")
    return digits


def normalize_email(value: Any) -> str:
    email = str(value or "").strip().casefold()
    if not EMAIL_RE.fullmatch(email):
        raise ValueError("邮箱格式不可靠")
    return email


def _json(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (bytes, bytearray)):
        value = value.decode("utf-8")
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise CommerceError("invalid_response", "外部响应不是对象")
    return parsed


@dataclass(frozen=True)
class CommerceConfig:
    woocommerce_url: str = ""
    woocommerce_consumer_key: str = ""
    woocommerce_consumer_secret: str = ""
    paypal_base_url: str = "https://api-m.paypal.com"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    wordpress_host: str = ""
    wordpress_port: int = 3306
    wordpress_database: str = ""
    wordpress_user: str = ""
    wordpress_password: str = ""
    wordpress_prefix: str = "wp_"
    admin_whatsapp: str = ""
    enabled: bool = False
    timeout: float = 10.0
    retry_once: bool = True
    verification_secret: str = "commerce-verification"

    @classmethod
    def from_environment(cls, environ: Optional[Dict[str, str]] = None) -> "CommerceConfig":
        env = environ if environ is not None else os.environ

        secret_values: Dict[str, str] = {}
        secret_file = str(env.get("COMMERCE_SECRET_FILE", "")).strip()
        if secret_file:
            try:
                loaded = json.loads(Path(secret_file).read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    secret_values = {str(key): str(value) for key, value in loaded.items() if value is not None}
            except (OSError, ValueError):
                secret_values = {}

        def first(*names: str) -> str:
            for name in names:
                value = str(env.get(name, secret_values.get(name, ""))).strip()
                if value:
                    return value
            return ""

        def number(name: str, default: float) -> float:
            try:
                return max(0.1, float(env.get(name, default)))
            except (TypeError, ValueError):
                return default

        prefix = first("WORDPRESS_TABLE_PREFIX", "WP_TABLE_PREFIX") or "wp_"
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,31}", prefix):
            prefix = "wp_"
        return cls(
            woocommerce_url=first("WOOCOMMERCE_URL", "WC_URL"),
            woocommerce_consumer_key=first("WOOCOMMERCE_CONSUMER_KEY", "WC_CONSUMER_KEY"),
            woocommerce_consumer_secret=first("WOOCOMMERCE_CONSUMER_SECRET", "WC_CONSUMER_SECRET"),
            paypal_base_url=first("PAYPAL_BASE_URL") or "https://api-m.paypal.com",
            paypal_client_id=first("PAYPAL_CLIENT_ID"),
            paypal_client_secret=first("PAYPAL_CLIENT_SECRET"),
            wordpress_host=first("WORDPRESS_DB_HOST", "WP_DB_HOST"),
            wordpress_port=int(first("WORDPRESS_DB_PORT", "WP_DB_PORT") or 3306),
            wordpress_database=first("WORDPRESS_DB_NAME", "WP_DB_NAME"),
            wordpress_user=first("WORDPRESS_DB_USER", "WP_DB_USER"),
            wordpress_password=first("WORDPRESS_DB_PASSWORD", "WP_DB_PASSWORD"),
            wordpress_prefix=prefix,
            admin_whatsapp=first("COMMERCE_ADMIN_WHATSAPP", "ADMIN_WHATSAPP"),
            enabled=str(env.get("COMMERCE_ENABLED", "0")).strip().lower() in {"1", "true", "yes", "on"},
            timeout=number("COMMERCE_HTTP_TIMEOUT", 10.0),
            verification_secret=first("COMMERCE_VERIFICATION_SECRET") or "commerce-verification",
        )

    def public_payload(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "woocommerce_url": self.woocommerce_url,
            "admin_whatsapp": _mask_phone(self.admin_whatsapp),
            "woocommerce_configured": bool(self.woocommerce_url and self.woocommerce_consumer_key and self.woocommerce_consumer_secret),
            "paypal_configured": bool(self.paypal_client_id and self.paypal_client_secret),
            "wordpress_configured": bool(self.wordpress_host and self.wordpress_database and self.wordpress_user and self.wordpress_password),
            "timeout": self.timeout,
        }


def _mask_phone(value: Any) -> str:
    raw = str(value or "")
    return ("*" * max(0, len(raw) - 4) + raw[-4:]) if raw else ""


class _HttpClient:
    def __init__(self, opener: Optional[Callable] = None, timeout: float = 10.0, secrets: Iterable[str] = ()):
        self.opener = opener or urlopen
        self.timeout = timeout
        self.secrets = tuple(secrets)

    def request(self, url: str, method: str = "GET", headers: Optional[Dict[str, str]] = None,
                data: Optional[bytes] = None, expected: Iterable[int] = (200,)) -> Any:
        last: Optional[Exception] = None
        attempts = 2
        for attempt in range(attempts):
            request = Request(url, data=data, headers=headers or {}, method=method)
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    if response.status not in set(expected):
                        raise CommerceError("http_error", f"HTTP {response.status}")
                    return response.read()
            except HTTPError as error:
                last = error
                if error.code < 500 or attempt == attempts - 1:
                    body = error.read().decode("utf-8", errors="replace")
                    raise CommerceError("http_error", f"HTTP {error.code}: {redact_sensitive(body, self.secrets)}") from error
            except (URLError, TimeoutError, OSError, CommerceError) as error:
                last = error
                if isinstance(error, CommerceError) and error.code == "http_error":
                    if attempt == attempts - 1:
                        raise error
                elif attempt == attempts - 1:
                    raise CommerceError("network_error", redact_sensitive(error, self.secrets)) from error
        raise CommerceError("network_error", redact_sensitive(last, self.secrets))


class WooCommerceClient:
    def __init__(self, config: CommerceConfig, opener: Optional[Callable] = None):
        self.config = config
        self.base_url = config.woocommerce_url.rstrip("/") + "/wp-json/wc/v3/" if config.woocommerce_url else ""
        auth = base64.b64encode(f"{config.woocommerce_consumer_key}:{config.woocommerce_consumer_secret}".encode()).decode()
        self.http = _HttpClient(opener, config.timeout, (config.woocommerce_consumer_key, config.woocommerce_consumer_secret, auth))

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self.base_url:
            raise CommerceError("not_configured", "WooCommerce 未配置", "woocommerce")
        url = urljoin(self.base_url, path.lstrip("/"))
        if params:
            url += "?" + urlencode(params)
        auth = base64.b64encode(f"{self.config.woocommerce_consumer_key}:{self.config.woocommerce_consumer_secret}".encode()).decode()
        raw = self.http.request(url, headers={"Accept": "application/json", "Authorization": f"Basic {auth}"})
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as error:
            raise CommerceError("invalid_response", "WooCommerce 响应格式不正确", "woocommerce") from error

    def list_orders_by_billing_phone(self, phone: str) -> List[Dict[str, Any]]:
        normalized = normalize_phone(phone)
        result = self._get("orders", {"billing_phone": normalized, "per_page": 20})
        if not isinstance(result, list):
            raise CommerceError("invalid_response", "订单列表格式不正确", "woocommerce")
        return [item for item in result if isinstance(item, dict)]

    def get_order(self, order_id: Any) -> Dict[str, Any]:
        if not re.fullmatch(r"\d+", str(order_id or "")):
            raise CommerceError("invalid_order_id", "订单号格式不正确", "woocommerce")
        result = self._get(f"orders/{int(order_id)}")
        if not isinstance(result, dict):
            raise CommerceError("invalid_response", "订单格式不正确", "woocommerce")
        return result

    def test_connection(self) -> bool:
        result = self._get("orders", {"per_page": 1})
        if not isinstance(result, list):
            raise CommerceError("invalid_response", "WooCommerce 连接响应格式不正确", "woocommerce")
        return True


class WordPressReadOnlyRepository:
    """Optional read-only fallback.  A connection factory is preferred in tests."""

    def __init__(self, config: CommerceConfig, connection_factory: Optional[Callable] = None):
        self.config = config
        self.connection_factory = connection_factory
        self.schema: Optional[str] = None

    def _connect(self):
        if self.connection_factory:
            return self.connection_factory()
        try:
            import pymysql  # type: ignore
        except ImportError as error:
            raise CommerceError("driver_unavailable", "WordPress 只读数据库驱动不可用", "wordpress") from error
        if not (self.config.wordpress_host and self.config.wordpress_database):
            raise CommerceError("not_configured", "WordPress 只读数据库未配置", "wordpress")
        return pymysql.connect(host=self.config.wordpress_host, port=self.config.wordpress_port,
                               user=self.config.wordpress_user, password=self.config.wordpress_password,
                               database=self.config.wordpress_database, read_timeout=self.config.timeout,
                               write_timeout=self.config.timeout, cursorclass=pymysql.cursors.DictCursor)

    def detect_schema(self) -> str:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            for table in (f"{self.config.wordpress_prefix}wc_orders", f"{self.config.wordpress_prefix}posts"):
                cursor.execute("SELECT 1 FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name = %s", (table,))
                if cursor.fetchone():
                    self.schema = "hpos" if table.endswith("wc_orders") else "legacy"
                    return self.schema
            raise CommerceError("unsupported_schema", "WordPress 订单表结构不支持", "wordpress")
        finally:
            connection.close()

    def _ensure_schema(self) -> str:
        return self.schema or self.detect_schema()

    def find_orders_by_phone(self, phone: str) -> List[Dict[str, Any]]:
        normalized = normalize_phone(phone)
        schema = self._ensure_schema()
        connection = self._connect()
        try:
            cursor = connection.cursor()
            if schema == "hpos":
                sql = (f"SELECT id, status, billing_first_name, billing_last_name, billing_email, billing_phone, "
                       f"date_created_gmt, total, currency, payment_method, transaction_id "
                       f"FROM `{self.config.wordpress_prefix}wc_orders` WHERE billing_phone = %s ORDER BY id DESC LIMIT 20")
            else:
                sql = (f"SELECT p.ID AS id, p.post_status AS status, pm.meta_value AS billing_phone "
                       f"FROM `{self.config.wordpress_prefix}posts` p JOIN `{self.config.wordpress_prefix}postmeta` pm "
                       f"ON pm.post_id = p.ID AND pm.meta_key = %s WHERE p.post_type = %s AND pm.meta_value = %s "
                       f"ORDER BY p.ID DESC LIMIT 20")
                cursor.execute(sql, ("_billing_phone", "shop_order", normalized))
                return [dict(row) for row in cursor.fetchall()]
            cursor.execute(sql, (normalized,))
            return [dict(row) for row in cursor.fetchall()]
        finally:
            connection.close()

    def supplement_order(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Return only missing data from the fallback; never mutate or store the input."""
        if not isinstance(order, dict):
            raise CommerceError("invalid_order", "订单格式不正确", "wordpress")
        order_id = order.get("id")
        if not order_id:
            return dict(order)
        schema = self._ensure_schema()
        connection = self._connect()
        try:
            cursor = connection.cursor()
            if schema == "hpos":
                cursor.execute(
                    f"SELECT id, status, billing_first_name, billing_last_name, billing_company, billing_address_1, "
                    f"billing_address_2, billing_city, billing_state, billing_postcode, billing_country, billing_email, "
                    f"billing_phone, shipping_first_name, shipping_last_name, shipping_company, shipping_address_1, "
                    f"shipping_address_2, shipping_city, shipping_state, shipping_postcode, shipping_country, "
                    f"date_created_gmt, date_updated_gmt, total, currency, payment_method, transaction_id "
                    f"FROM `{self.config.wordpress_prefix}wc_orders` WHERE id = %s LIMIT 1", (int(order_id),)
                )
                row = cursor.fetchone()
                return _merge_order(order, dict(row) if row else {})
            cursor.execute(f"SELECT meta_key, meta_value FROM `{self.config.wordpress_prefix}postmeta` WHERE post_id = %s", (int(order_id),))
            meta = {str(row[0]): row[1] for row in cursor.fetchall()}
            return _merge_order(order, {"meta_data": [{"key": key, "value": value} for key, value in meta.items()]})
        finally:
            connection.close()


def _merge_order(order: Dict[str, Any], supplement: Dict[str, Any]) -> Dict[str, Any]:
    merged = dict(order)
    for key, value in supplement.items():
        if key not in merged or merged[key] in (None, "", [], {}):
            merged[key] = value
    return merged


class PayPalClient:
    def __init__(self, config: CommerceConfig, opener: Optional[Callable] = None):
        self.config = config
        self.http = _HttpClient(opener, config.timeout, (config.paypal_client_id, config.paypal_client_secret))

    def _token(self) -> str:
        if not (self.config.paypal_client_id and self.config.paypal_client_secret):
            raise CommerceError("not_configured", "PayPal 未配置", "paypal")
        auth = base64.b64encode(f"{self.config.paypal_client_id}:{self.config.paypal_client_secret}".encode()).decode()
        raw = self.http.request(self.config.paypal_base_url.rstrip("/") + "/v1/oauth2/token", method="POST",
                                headers={"Accept": "application/json", "Authorization": f"Basic {auth}",
                                         "Content-Type": "application/x-www-form-urlencoded"},
                                data=b"grant_type=client_credentials")
        try:
            token = _json(raw).get("access_token")
        except (ValueError, UnicodeDecodeError) as error:
            raise CommerceError("invalid_response", "PayPal 令牌响应格式不正确", "paypal") from error
        if not token:
            raise CommerceError("invalid_response", "PayPal 未返回访问令牌", "paypal")
        return str(token)

    def verify_transaction(self, transaction_id: str, expected_amount: Any, expected_currency: str) -> Dict[str, Any]:
        if not transaction_id or not re.fullmatch(r"[A-Za-z0-9._:-]{3,200}", str(transaction_id)):
            raise CommerceError("missing_transaction", "缺少有效 PayPal 交易标识", "paypal")
        raw = self.http.request(self.config.paypal_base_url.rstrip("/") + "/v2/checkout/orders/" + str(transaction_id),
                                headers={"Accept": "application/json", "Authorization": f"Bearer {self._token()}"})
        try:
            body = _json(raw)
        except (ValueError, UnicodeDecodeError) as error:
            raise CommerceError("invalid_response", "PayPal 响应格式不正确", "paypal") from error
        status = str(body.get("status", "")).lower()
        amount, currency = _paypal_amount(body)
        if not status or status not in PAYPAL_STATUSES:
            raise CommerceError("invalid_response", "PayPal 返回未知交易状态", "paypal")
        if not _money_equal(amount, expected_amount) or str(currency).upper() != str(expected_currency).upper():
            return {"status": status, "amount": amount, "currency": currency, "transaction_id": str(transaction_id), "result": "mismatch"}
        return {"status": status, "amount": amount, "currency": currency, "transaction_id": str(transaction_id), "result": "ok"}

    def test_connection(self) -> bool:
        return bool(self._token())


def _paypal_amount(body: Dict[str, Any]):
    for purchase in body.get("purchase_units", []) or []:
        payments = _as_dict(purchase).get("payments", {})
        captures = _as_dict(payments).get("captures", []) or []
        if captures:
            capture = _as_dict(captures[0])
            amount = _as_dict(capture.get("amount"))
            return amount.get("value", ""), amount.get("currency_code", "")
    amount = _as_dict(body.get("amount"))
    return amount.get("value", ""), amount.get("currency_code", "")


def _money_equal(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)).quantize(Decimal("0.01")) == Decimal(str(right)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError):
        return False


class VerificationService:
    def __init__(self, database_path: Any, secret: str, admin_unlock_callback: Optional[Callable] = None):
        self.database_path = Path(database_path)
        self.secret = secret or "commerce-verification"
        self.admin_unlock_callback = admin_unlock_callback

    def _state(self, sender: str, create: bool = True):
        key = _hash(sender, self.secret)
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute("SELECT id, sender_hash, email_failure_count, email_locked FROM order_verification_states WHERE sender_hash = ?", (key,)).fetchone()
            if not row and create:
                connection.execute("INSERT INTO order_verification_states(sender_hash) VALUES (?)", (key,))
                connection.commit()
                row = connection.execute("SELECT id, sender_hash, email_failure_count, email_locked FROM order_verification_states WHERE sender_hash = ?", (key,)).fetchone()
            return row
        finally:
            connection.close()

    def _set_email_attempt(self, sender: str, success: bool) -> Dict[str, Any]:
        key = _hash(sender, self.secret)
        now = _now()
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute("SELECT id, email_failure_count, email_locked FROM order_verification_states WHERE sender_hash = ?", (key,)).fetchone()
            if not row:
                connection.execute("INSERT INTO order_verification_states(sender_hash, last_attempt_at) VALUES (?, ?)", (key, now))
                row = (None, 0, 0)
            count = 0 if success else int(row[1]) + 1
            locked = 0 if success else int(row[2]) or int(count >= 3)
            connection.execute("UPDATE order_verification_states SET email_failure_count = ?, email_locked = ?, last_attempt_at = ? WHERE sender_hash = ?", (count, locked, now, key))
            connection.commit()
            return {"verified": success, "locked": bool(locked), "attempts": count}
        finally:
            connection.close()

    @staticmethod
    def _billing(order: Dict[str, Any]) -> Dict[str, Any]:
        billing = _as_dict(order.get("billing"))
        if not billing:
            billing = {str(item.get("key", ""))[9:]: item.get("value") for item in order.get("meta_data", []) if isinstance(item, dict) and str(item.get("key", "")).startswith("_billing_")}
        return billing

    def verify_by_phone(self, sender_phone: str, orders: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        sender = normalize_phone(sender_phone)
        matches = []
        for order in orders or []:
            try:
                if normalize_phone(self._billing(order).get("phone")) == sender:
                    matches.append(order)
            except ValueError:
                continue
        if len(matches) == 1:
            return {"verified": True, "method": "phone", "order": matches[0]}
        if len(matches) > 1:
            return {"verified": False, "method": "phone", "selection_required": True, "orders": [_safe_order_summary(item) for item in matches]}
        return {"verified": False, "method": "phone", "reason": "no_match"}

    def verify_by_order_id(self, sender_phone: str, order: Dict[str, Any]) -> Dict[str, Any]:
        try:
            sender = normalize_phone(sender_phone)
            billing_phone = normalize_phone(self._billing(order).get("phone"))
            if sender == billing_phone:
                return {"verified": True, "method": "phone", "order": order}
        except ValueError:
            pass
        return {"verified": False, "method": "order_id", "reason": "phone_mismatch"}

    def verify_by_email(self, sender_phone: str, order: Dict[str, Any], email: str) -> Dict[str, Any]:
        state = self._state(sender_phone)
        if state and bool(state[3]):
            return {"verified": False, "locked": True, "method": "email", "reason": "email_locked"}
        try:
            expected = normalize_email(self._billing(order).get("email"))
            actual = normalize_email(email)
        except ValueError:
            actual = ""
            expected = "!invalid"
        result = self._set_email_attempt(sender_phone, actual == expected)
        result.update({"method": "email", "order": order if actual == expected else None})
        if actual == expected:
            result["verified"] = True
        elif result["locked"] and self.admin_unlock_callback:
            self.admin_unlock_callback(sender_phone)
        return result

    def unlock_email_verification(self, state_id: int, admin_id: str) -> bool:
        if not str(admin_id or "").strip():
            raise PermissionError("需要已认证管理员")
        connection = sqlite3.connect(self.database_path)
        try:
            cursor = connection.execute("UPDATE order_verification_states SET email_failure_count = 0, email_locked = 0, unlocked_at = ?, unlocked_by = ? WHERE id = ?", (_now(), str(admin_id)[:100], int(state_id)))
            connection.commit()
            return bool(cursor.rowcount)
        finally:
            connection.close()


def _safe_order_summary(order: Dict[str, Any]) -> Dict[str, Any]:
    return {"order_id": str(order.get("id", "")), "status": str(order.get("status", "")), "created": str(order.get("date_created", "")), "total": str(order.get("total", "")), "currency": str(order.get("currency", ""))}


class PaymentReconciler:
    def __init__(self, paypal_client: PayPalClient):
        self.paypal = paypal_client

    def reconcile(self, order: Dict[str, Any]) -> Dict[str, Any]:
        method = str(order.get("payment_method", "")).lower()
        method_title = str(order.get("payment_method_title", "")).lower()
        if method not in PAYPAL_METHODS and "paypal" not in method and "paypal" not in method_title:
            return {"result": "not_applicable", "status": ""}
        transaction_id = str(order.get("transaction_id", ""))
        if not transaction_id:
            for item in order.get("meta_data", []) or []:
                if isinstance(item, dict) and str(item.get("key", "")).lower() in {"_paypal_transaction_id", "paypal_transaction_id", "_transaction_id"}:
                    transaction_id = str(item.get("value", ""))
                    if transaction_id:
                        break
        if not transaction_id:
            return {"result": "needs_manual_review", "reason": "missing_transaction"}
        result = self.paypal.verify_transaction(transaction_id, order.get("total", ""), order.get("currency", ""))
        if result.get("result") != "ok":
            return dict(result, result="needs_manual_review", reason="amount_or_currency_mismatch")
        wc_status = str(order.get("status", "")).lower()
        paypal_status = str(result.get("status", "")).lower()
        if wc_status in {"processing", "completed", "paid"} and paypal_status not in {"approved", "completed", "captured"}:
            return dict(result, result="needs_manual_review", reason="status_conflict")
        return dict(result, result="ok")


class OrderReplyRenderer:
    def render(self, order: Dict[str, Any], payment: Optional[Dict[str, Any]] = None) -> str:
        billing = _as_dict(order.get("billing"))
        shipping = _as_dict(order.get("shipping"))
        lines = [f"订单号：{order.get('id', '')}", f"订单状态：{order.get('status', '')}", f"下单时间：{order.get('date_created', '')}", f"更新时间：{order.get('date_modified', '')}"]
        lines.append("商品：")
        for item in order.get("line_items", []) or []:
            if not isinstance(item, dict):
                continue
            lines.append(f"- {item.get('name', '')} × {item.get('quantity', '')}：{item.get('total', '')}")
        lines.extend([f"商品小计：{order.get('total', '')} {order.get('currency', '')}", f"折扣：{order.get('discount_total', '')}", f"税费：{order.get('total_tax', '')}", f"运费：{order.get('shipping_total', '')}", f"付款方式：{order.get('payment_method_title') or order.get('payment_method', '')}", f"付款状态：{order.get('status', '')}"])
        if payment and payment.get("result") == "ok":
            lines.extend([f"PayPal 交易号：{payment.get('transaction_id', '')}", f"PayPal 核对状态：{payment.get('status', '')}"])
        for label, address in (("账单地址", billing), ("收货地址", shipping)):
            full = " ".join(str(address.get(key, "")).strip() for key in ("first_name", "last_name", "address_1", "address_2", "city", "state", "postcode", "country") if str(address.get(key, "")).strip())
            lines.append(f"{label}：{full or '未提供'}")
        name = " ".join(str(billing.get(key, "")).strip() for key in ("first_name", "last_name") if str(billing.get(key, "")).strip())
        lines.extend([f"姓名：{name or '未提供'}", f"邮箱：{billing.get('email', '未提供')}", f"电话：{billing.get('phone', '未提供')}"])
        shipping_lines = []
        for key in ("shipping_provider", "carrier", "tracking_number", "tracking"):
            if order.get(key):
                shipping_lines.append(f"{key}：{order[key]}")
        if shipping_lines:
            lines.extend(shipping_lines)
        return "\n".join(lines)


class ManualOrderCaseService:
    def __init__(self, database_path: Any, secret: str, send_message: Optional[Callable[[str, str], Any]] = None,
                 admin_phone: str = ""):
        self.database_path = Path(database_path)
        self.secret = secret or "commerce-verification"
        self.send_message = send_message
        self.admin_phone = admin_phone

    def _dedupe(self, chat_id: str, query_type: str, clue: str) -> str:
        return _hash(f"{chat_id}|{query_type}|{clue}", self.secret)

    def create_or_get_case(self, chat_id: str, query_type: str, clue: str, failure_code: str) -> Dict[str, Any]:
        key = self._dedupe(chat_id, query_type, clue)
        now = _now()
        safe_clue = redact_sensitive(clue).replace("\n", " ")[:200]
        customer_hash = _hash(chat_id, self.secret)
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("INSERT OR IGNORE INTO manual_order_cases(case_key, chat_id, customer_hash, query_type, safe_clue, failure_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (key, str(chat_id), customer_hash, str(query_type)[:50], safe_clue, str(failure_code)[:80], now))
            row = connection.execute("SELECT id, case_key, chat_id, query_type, safe_clue, failure_code, status, admin_answer, created_at, answered_at, closed_at, customer_reply_sent_at FROM manual_order_cases WHERE case_key = ?", (key,)).fetchone()
            connection.commit()
        finally:
            connection.close()
        return _case_dict(row)

    def _notify_once(self, key: str, kind: str, target: str, text: str, case_id: int) -> Dict[str, Any]:
        connection = sqlite3.connect(self.database_path)
        try:
            cursor = connection.execute("INSERT OR IGNORE INTO order_notification_dedupe(dedupe_key, notification_type, case_id, status, created_at) VALUES (?, ?, ?, 'pending', ?)", (key, kind, case_id, _now()))
            connection.commit()
            if not cursor.rowcount:
                return {"sent": False, "duplicate": True}
            if not self.send_message:
                connection.execute("UPDATE order_notification_dedupe SET status = 'failed' WHERE dedupe_key = ?", (key,))
                connection.commit()
                return {"sent": False, "reason": "sender_unavailable"}
            try:
                message_id = self.send_message(target, text)
                connection.execute("UPDATE order_notification_dedupe SET status = 'sent', message_id = ?, sent_at = ? WHERE dedupe_key = ?", (str(message_id or "")[:200], _now(), key))
                connection.commit()
                return {"sent": True, "message_id": str(message_id or "")}
            except Exception as error:
                connection.execute("UPDATE order_notification_dedupe SET status = 'failed' WHERE dedupe_key = ?", (key,))
                connection.commit()
                raise CommerceError("notification_failed", redact_sensitive(error), "waha") from error
        finally:
            connection.close()

    def notify_customer_waiting(self, case: Dict[str, Any]) -> Dict[str, Any]:
        return self._notify_once(f"case:{case['id']}:customer-waiting", "customer_waiting", case["chat_id"], "订单正在人工核对，请稍候。", int(case["id"]))

    def notify_admin(self, case: Dict[str, Any]) -> Dict[str, Any]:
        if not self.admin_phone:
            return {"sent": False, "reason": "admin_not_configured"}
        text = f"订单查询需要人工核对\n客户：{_mask_phone(case['chat_id'])}\n线索：{case['safe_clue']}\n原因：{case['failure_code']}\n工单：#{case['id']}"
        return self._notify_once(f"case:{case['id']}:admin", "admin_notification", self.admin_phone, text, int(case["id"]))

    def save_admin_result(self, case_id: int, answer: str, close: bool = False) -> Dict[str, Any]:
        answer = str(answer or "").strip()
        if not answer:
            raise ValueError("管理员答复不能为空")
        status = "closed" if close else "answered"
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("UPDATE manual_order_cases SET admin_answer = ?, status = ?, answered_at = ?, closed_at = ? WHERE id = ?", (answer[:4000], status, _now(), _now() if close else None, int(case_id)))
            connection.commit()
            row = connection.execute("SELECT id, case_key, chat_id, query_type, safe_clue, failure_code, status, admin_answer, created_at, answered_at, closed_at, customer_reply_sent_at FROM manual_order_cases WHERE id = ?", (int(case_id),)).fetchone()
        finally:
            connection.close()
        if not row:
            raise KeyError("工单不存在")
        return _case_dict(row)

    def confirm_and_reply(self, case_id: int, reply_text: Optional[str] = None) -> Dict[str, Any]:
        connection = sqlite3.connect(self.database_path)
        try:
            row = connection.execute("SELECT id, case_key, chat_id, query_type, safe_clue, failure_code, status, admin_answer, created_at, answered_at, closed_at, customer_reply_sent_at FROM manual_order_cases WHERE id = ?", (int(case_id),)).fetchone()
            if not row:
                raise KeyError("工单不存在")
            case = _case_dict(row)
            if case["customer_reply_sent_at"]:
                return {"sent": False, "duplicate": True}
            text = str(reply_text or case["admin_answer"] or "").strip()
            if not text:
                raise ValueError("尚未填写客户回复")
            if not self.send_message:
                raise CommerceError("sender_unavailable", "消息发送器不可用", "waha")
            message_id = self.send_message(case["chat_id"], text)
            connection.execute("UPDATE manual_order_cases SET customer_reply_sent_at = ?, status = 'closed', closed_at = ? WHERE id = ?", (_now(), _now(), int(case_id)))
            connection.execute("UPDATE order_notification_dedupe SET status = 'sent', message_id = ?, sent_at = ? WHERE dedupe_key = ?", (str(message_id or "")[:200], _now(), f"case:{case_id}:customer-confirm"))
            connection.execute("INSERT OR IGNORE INTO order_notification_dedupe(dedupe_key, notification_type, case_id, status, message_id, created_at, sent_at) VALUES (?, 'customer_confirm', ?, 'sent', ?, ?, ?)", (f"case:{case_id}:customer-confirm", int(case_id), str(message_id or "")[:200], _now(), _now()))
            connection.commit()
            return {"sent": True, "message_id": str(message_id or "")}
        finally:
            connection.close()


def _case_dict(row) -> Dict[str, Any]:
    fields = ("id", "case_key", "chat_id", "query_type", "safe_clue", "failure_code", "status", "admin_answer", "created_at", "answered_at", "closed_at", "customer_reply_sent_at")
    return dict(zip(fields, row))


class OrderQueryCoordinator:
    def __init__(self, database_path: Any, config: CommerceConfig, woocommerce: Optional[WooCommerceClient] = None,
                 wordpress: Optional[WordPressReadOnlyRepository] = None, paypal: Optional[PayPalClient] = None,
                 verification: Optional[VerificationService] = None, reconciler: Optional[PaymentReconciler] = None,
                 renderer: Optional[OrderReplyRenderer] = None, cases: Optional[ManualOrderCaseService] = None):
        self.database_path = Path(database_path)
        self.config = config
        self.woocommerce = woocommerce or WooCommerceClient(config)
        self.wordpress = wordpress or WordPressReadOnlyRepository(config)
        self.paypal = paypal or PayPalClient(config)
        self.verification = verification or VerificationService(self.database_path, config.verification_secret)
        self.reconciler = reconciler or PaymentReconciler(self.paypal)
        self.renderer = renderer or OrderReplyRenderer()
        self.cases = cases or ManualOrderCaseService(self.database_path, config.verification_secret, admin_phone=config.admin_whatsapp)

    def _audit(self, request: Dict[str, Any], result: str, method: str, source: str, summary: str = ""):
        connection = sqlite3.connect(self.database_path)
        try:
            connection.execute("INSERT INTO order_query_audit(created_at, session_hash, verification_method, result_code, source, safe_summary) VALUES (?, ?, ?, ?, ?, ?)", (_now(), _hash(str(request.get("chat_id", "")), self.config.verification_secret), method, result, source, redact_sensitive(summary)[:500]))
            connection.commit()
        finally:
            connection.close()

    def _manual(self, request: Dict[str, Any], reason: str, clue: str, source: str = "") -> Dict[str, Any]:
        case = self.cases.create_or_get_case(str(request.get("chat_id", "")), str(request.get("query_type", "order_status")), clue, reason)
        self.cases.notify_customer_waiting(case)
        self.cases.notify_admin(case)
        self._audit(request, "manual", "", source, reason)
        return {"action": "manual", "reason": reason, "case": case}

    def handle(self, request: Any) -> Dict[str, Any]:
        request = dict(request) if isinstance(request, dict) else vars(request)
        chat_id = str(request.get("chat_id", ""))
        sender_phone = str(request.get("sender_phone") or request.get("from_phone") or chat_id.split("@", 1)[0])
        order_id = request.get("selected_order_id") or request.get("order_id")
        email = request.get("email")
        source = "woocommerce"
        order = None
        candidates: List[Dict[str, Any]] = []
        try:
            if order_id:
                order = self.woocommerce.get_order(order_id)
                if _order_needs_supplement(order):
                    order = self.wordpress.supplement_order(order)
                verification = self.verification.verify_by_order_id(sender_phone, order)
                if not verification.get("verified") and email:
                    verification = self.verification.verify_by_email(sender_phone, order, email)
                if not verification.get("verified"):
                    if verification.get("locked"):
                        return self._manual(request, "email_locked", str(order_id), source)
                    return {"action": "verification_required", "method": "email", "message": "请提供订单账单邮箱以完成验证。"}
            else:
                candidates = self.woocommerce.list_orders_by_billing_phone(sender_phone)
                if not candidates:
                    candidates = self.wordpress.find_orders_by_phone(sender_phone)
                    source = "wordpress"
                verification = self.verification.verify_by_phone(sender_phone, candidates)
                if verification.get("selection_required") and not request.get("selected_order_id"):
                    return {"action": "select_order", "orders": verification["orders"]}
                if verification.get("verified"):
                    order = verification["order"]
                elif email and candidates:
                    for candidate in candidates:
                        try:
                            if str(candidate.get("id")) == str(request.get("selected_order_id")):
                                order = candidate
                                break
                        except Exception:
                            pass
                    if order:
                        verification = self.verification.verify_by_email(sender_phone, order, email)
                if not verification.get("verified"):
                    return self._manual(request, "order_not_found_or_unverified", str(order_id or email or ""), source)
                if _order_needs_supplement(order):
                    order = self.wordpress.supplement_order(order)
            payment = self.reconciler.reconcile(order)
            if payment.get("result") == "needs_manual_review":
                return self._manual(request, payment.get("reason", "payment_review"), str(order.get("id", "")), "paypal")
            reply = self.renderer.render(order, payment)
            self._audit(request, "success", verification.get("method", ""), source, "order query completed")
            return {"action": "reply", "order": order, "payment": payment, "text": reply}
        except CommerceError as error:
            return self._manual(request, error.code, str(order_id or email or ""), error.source)
        except (ValueError, KeyError) as error:
            return self._manual(request, "invalid_request", str(order_id or email or ""), source)


def _order_needs_supplement(order: Dict[str, Any]) -> bool:
    return not (_as_dict(order.get("billing")).get("phone") and order.get("currency") and order.get("total"))


def apply_migration(connection: sqlite3.Connection) -> None:
    """Apply this module's idempotent migration to an existing connection."""
    migration = Path(__file__).with_name("migrations") / "002_commerce_order_query.sql"
    connection.executescript(migration.read_text(encoding="utf-8"))
