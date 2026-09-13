import json
import sqlite3
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path


class BusinessContextService:
    """Reads and updates the standalone AI business database only."""

    def __init__(self, database_path):
        self.database_path = Path(database_path)

    def _connect(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _number(value):
        try:
            number = Decimal(str(value))
        except (InvalidOperation, TypeError):
            return 0
        return int(number) if number == number.to_integral_value() else float(number)

    @staticmethod
    def _now_is_valid(row, now):
        return (row["valid_from"] is None or row["valid_from"] <= now) and (
            row["valid_until"] is None or now <= row["valid_until"]
        )

    def get_business_context(self, customer_id=None, product_id=None, variant_id=None, country_code=None):
        now = int(time.time())
        connection = self._connect()
        try:
            settings = {
                row["key"]: self._typed_value(row["value"], row["value_type"])
                for row in connection.execute(
                    "SELECT key, value, value_type FROM business_settings WHERE is_active = 1"
                )
            }
            product = self._product(connection, product_id)
            variant = None
            if variant_id is not None:
                variant = connection.execute(
                    "SELECT * FROM product_variants WHERE id = ? AND is_active = 1", (variant_id,)
                ).fetchone()
                if not variant:
                    raise KeyError("产品版本不存在或已停用")
            elif product:
                variant = None
            if product is None:
                raise KeyError("没有可用产品")

            price_source = variant or product
            original_price = Decimal(str(price_source["original_price"]))
            current_price = Decimal(str(price_source["current_price"]))
            coupon = self._coupon(connection, product["id"], variant["id"] if variant else None, now)
            discount = Decimal("0")
            coupon_data = None
            if coupon:
                if coupon["discount_type"] == "percentage":
                    discount = (current_price * Decimal(str(coupon["discount_value"])) / Decimal("100"))
                else:
                    discount = Decimal(str(coupon["discount_value"]))
                discount = max(Decimal("0"), min(discount, current_price))
                coupon_data = {
                    "code": coupon["code"],
                    "discountType": coupon["discount_type"],
                    "discount": self._number(discount),
                }

            quote = self._quote(connection, customer_id, product["id"], variant["id"] if variant else None, now)
            quote_data = None
            if quote:
                quote_total = Decimal(str(quote["total_price"]))
                quote_data = {
                    "quantity": self._number(quote["quantity"]),
                    "currency": quote["currency"],
                    "unitPrice": self._number(quote["unit_price"]),
                    "shippingFee": self._number(quote["shipping_fee"]),
                    "discount": self._number(quote["discount"]),
                    "totalPrice": self._number(quote_total),
                    "notes": quote["notes"],
                }

            shipping = self._shipping(
                connection,
                country_code,
                product["id"],
                variant["id"] if variant else None,
                settings,
            )
            package_rows = connection.execute(
                "SELECT item_name, quantity, unit FROM product_package_items "
                "WHERE product_id = ? AND (variant_id IS NULL OR variant_id = ?) AND is_active = 1 "
                "ORDER BY sort_order, id",
                (product["id"], variant["id"] if variant else 0),
            ).fetchall()
            payments = self._payments(connection, country_code)
            return {
                "business": {
                    "websiteUrl": settings.get("business.website_url", ""),
                    "shippingOrigin": settings.get("business.shipping_origin_country", ""),
                    "defaultCurrency": settings.get("business.default_currency", "USD"),
                },
                "product": {
                    "id": product["id"],
                    "sku": variant["sku"] if variant else product["sku"],
                    "name": variant["name"] if variant else product["name"],
                    "shortName": product["short_name"],
                    "description": product["description"],
                    "url": product["product_url"],
                },
                "pricing": {
                    "currency": price_source["currency"],
                    "originalPrice": self._number(original_price),
                    "currentPrice": self._number(current_price),
                    "coupon": coupon_data,
                    "finalPrice": self._number(current_price - discount),
                    "specialCustomerQuote": quote_data,
                },
                "packageContents": [
                    {"item": row["item_name"], "quantity": self._number(row["quantity"]), "unit": row["unit"]}
                    for row in package_rows
                ],
                "payments": payments,
                "shipping": shipping,
                "country": self._country(connection, country_code),
            }
        finally:
            connection.close()

    @staticmethod
    def _typed_value(value, value_type):
        if value_type == "number":
            return BusinessContextService._number(value)
        if value_type == "boolean":
            return str(value).lower() in {"1", "true", "yes"}
        if value_type == "json":
            return json.loads(value)
        return value

    @staticmethod
    def _product(connection, product_id):
        if product_id is not None:
            return connection.execute(
                "SELECT * FROM products WHERE id = ? AND is_active = 1", (product_id,)
            ).fetchone()
        return connection.execute(
            "SELECT * FROM products WHERE is_active = 1 ORDER BY id LIMIT 1"
        ).fetchone()

    @staticmethod
    def _coupon(connection, product_id, variant_id, now):
        rows = connection.execute(
            "SELECT * FROM coupons WHERE is_active = 1 AND "
            "(product_id IS NULL OR product_id = ?) AND (variant_id IS NULL OR variant_id = ?) "
            "ORDER BY CASE WHEN variant_id IS NOT NULL THEN 0 WHEN product_id IS NOT NULL THEN 1 ELSE 2 END, id",
            (product_id, variant_id),
        ).fetchall()
        return next((row for row in rows if BusinessContextService._now_is_valid(row, now)), None)

    @staticmethod
    def _quote(connection, customer_id, product_id, variant_id, now):
        if not customer_id:
            return None
        rows = connection.execute(
            "SELECT * FROM customer_quotes WHERE customer_id = ? AND product_id = ? AND is_active = 1 "
            "AND (variant_id IS NULL OR variant_id = ?) ORDER BY id DESC",
            (str(customer_id), product_id, variant_id),
        ).fetchall()
        return next((row for row in rows if row["valid_until"] is None or row["valid_until"] >= now), None)

    @staticmethod
    def _shipping(connection, country_code, product_id, variant_id, defaults=None):
        candidates = []
        if country_code:
            candidates.extend([
                (country_code, product_id, variant_id),
                (country_code, product_id, None),
                (country_code, None, None),
            ])
        candidates.append((None, None, None))
        for country, product, variant in candidates:
            row = connection.execute(
                "SELECT * FROM shipping_rules WHERE is_active = 1 AND "
                "country_code IS ? AND product_id IS ? AND variant_id IS ? LIMIT 1",
                (country, product, variant),
            ).fetchone()
            if row:
                return {
                    "supported": bool(row["shipping_supported"]),
                    "included": bool(row["shipping_included"]),
                    "fee": BusinessContextService._number(row["shipping_fee"] or 0),
                    "currency": row["currency"],
                    "etaMinDays": row["eta_min_days"],
                    "etaMaxDays": row["eta_max_days"],
                    "trackingSupported": bool(row["tracking_supported"]),
                    "notes": row["notes"],
                }
        defaults = defaults or {}
        return {
            "supported": True,
            "included": bool(defaults.get("business.default_shipping_included", True)),
            "fee": 0,
            "currency": defaults.get("business.default_currency", "USD"),
            "etaMinDays": int(defaults.get("business.default_shipping_min_days", 7)),
            "etaMaxDays": int(defaults.get("business.default_shipping_max_days", 10)),
            "trackingSupported": True,
            "notes": "",
        }

    @staticmethod
    def _payments(connection, country_code):
        rows = connection.execute(
            "SELECT p.code, p.name, p.description, p.is_supported, c.is_supported AS country_supported "
            "FROM payment_methods p LEFT JOIN country_payment_methods c "
            "ON c.payment_method_id = p.id AND c.country_code = ? WHERE p.is_active = 1 ORDER BY p.id",
            (country_code,),
        ).fetchall()
        supported, unsupported = [], []
        for row in rows:
            enabled = bool(row["country_supported"] if row["country_supported"] is not None else row["is_supported"])
            (supported if enabled else unsupported).append(row["name"])
        return {"supported": supported, "unsupported": unsupported}

    @staticmethod
    def _country(connection, country_code):
        if not country_code:
            return None
        row = connection.execute(
            "SELECT country_code, country_name, currency_code, default_language, shipping_supported "
            "FROM countries WHERE country_code = ? AND is_active = 1", (country_code.upper(),)
        ).fetchone()
        return dict(row) if row else None

    def list_records(self, resource):
        tables = {"settings": "business_settings", "products": "products", "coupons": "coupons", "payments": "payment_methods", "shipping": "shipping_rules"}
        table = tables.get(resource)
        if not table:
            raise KeyError("不支持的业务资源")
        connection = self._connect()
        try:
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id").fetchall()]
        finally:
            connection.close()

    def save_record(self, resource, payload, record_id=None):
        tables = {"settings": "business_settings", "products": "products", "coupons": "coupons", "payments": "payment_methods", "shipping": "shipping_rules"}
        table = tables.get(resource)
        if not table or not isinstance(payload, dict):
            raise ValueError("业务数据格式不正确")
        allowed = {
            "settings": {"key", "value", "value_type", "description", "is_active"},
            "products": {"sku", "name", "short_name", "description", "currency", "original_price", "current_price", "product_url", "is_active"},
            "coupons": {"code", "discount_type", "discount_value", "currency", "product_id", "variant_id", "valid_from", "valid_until", "is_active"},
            "payments": {"code", "name", "description", "is_supported", "is_active"},
            "shipping": {"country_code", "product_id", "variant_id", "shipping_supported", "shipping_included", "shipping_fee", "currency", "eta_min_days", "eta_max_days", "tracking_supported", "notes", "is_active"},
        }[resource]
        fields = [field for field in payload if field in allowed]
        if not fields:
            raise ValueError("没有可保存的业务字段")
        now = int(time.time())
        connection = self._connect()
        try:
            if resource == "settings" and record_id is None and payload.get("key"):
                existing = connection.execute(
                    "SELECT id FROM business_settings WHERE key = ?", (payload["key"],)
                ).fetchone()
                if existing:
                    record_id = existing["id"]
            values = [payload[field] for field in fields]
            if record_id is None:
                fields += ["created_at", "updated_at"]
                values += [now, now]
                marks = ", ".join("?" for _ in fields)
                cursor = connection.execute(f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({marks})", values)
                record_id = cursor.lastrowid
            else:
                fields += ["updated_at"]
                values += [now]
                assignments = ", ".join(f"{field} = ?" for field in fields)
                cursor = connection.execute(f"UPDATE {table} SET {assignments} WHERE id = ?", values + [record_id])
                if cursor.rowcount == 0:
                    raise KeyError("业务记录不存在")
            connection.commit()
            return dict(connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone())
        finally:
            connection.close()

    def delete_record(self, resource, record_id):
        tables = {"settings": "business_settings", "products": "products", "coupons": "coupons", "payments": "payment_methods", "shipping": "shipping_rules"}
        table = tables.get(resource)
        if not table:
            raise KeyError("不支持的业务资源")
        connection = self._connect()
        try:
            cursor = connection.execute(f"DELETE FROM {table} WHERE id = ?", (record_id,))
            connection.commit()
            if not cursor.rowcount:
                raise KeyError("业务记录不存在")
        finally:
            connection.close()


def seed_business_database(connection):
    now = int(time.time())
    settings = [
        ("business.website_url", "https://www.6spring.com/products/g5-plus/", "string", "客服使用的网站地址"),
        ("business.shipping_origin_country", "China", "string", "发货国家"),
        ("business.default_currency", "USD", "string", "默认货币"),
        ("business.default_shipping_min_days", "7", "number", "默认最短物流天数"),
        ("business.default_shipping_max_days", "10", "number", "默认最长物流天数"),
        ("business.default_shipping_included", "true", "boolean", "默认是否包邮"),
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO business_settings(key, value, value_type, description, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
        [(key, value, value_type, description, now, now) for key, value, value_type, description in settings],
    )
    connection.execute(
        "INSERT OR IGNORE INTO products(sku, name, short_name, description, currency, original_price, current_price, product_url, is_active, created_at, updated_at) "
        "VALUES ('G5-PLUS', 'G5 Plus', 'G5 Plus', '', 'USD', 118, 98, 'https://www.6spring.com/products/g5-plus/', 1, ?, ?)",
        (now, now),
    )
    product_id = connection.execute("SELECT id FROM products WHERE sku = 'G5-PLUS'").fetchone()[0]
    package_items = [
        (product_id, None, "G5 Plus 主体", 1, "piece", 1),
        (product_id, None, "replacement bands", 3, "piece", 2),
        (product_id, None, "magazines / ball storage rods", 2, "piece", 3),
        (product_id, None, "9mm steel balls", 1, "kg", 4),
        (product_id, None, "tweezers", 1, "piece", 5),
        (product_id, None, "screwdriver", 1, "piece", 6),
        (product_id, None, "necessary small accessories", 1, "set", 7),
    ]
    connection.executemany(
        "INSERT INTO product_package_items(product_id, variant_id, item_name, quantity, unit, sort_order, is_active, created_at, updated_at) "
        "SELECT ?, ?, ?, ?, ?, ?, 1, ?, ? WHERE NOT EXISTS (SELECT 1 FROM product_package_items WHERE product_id = ? AND variant_id IS NULL AND item_name = ?)",
        [(product, variant, item, quantity, unit, order, now, now, product, item) for product, variant, item, quantity, unit, order in package_items],
    )
    connection.execute(
        "INSERT OR IGNORE INTO coupons(code, discount_type, discount_value, currency, product_id, is_active, created_at, updated_at) VALUES ('6springg5', 'fixed', 10, 'USD', ?, 1, ?, ?)",
        (product_id, now, now),
    )
    payment_methods = [
        ("paypal", "PayPal", "", 1),
        ("website_checkout", "Website Checkout", "", 1),
        ("cod", "Cash on Delivery", "", 0),
        ("orange_money", "Orange Money", "", 0),
        ("mpesa", "M-Pesa", "", 0),
        ("bank_transfer", "Bank Transfer", "", 0),
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO payment_methods(code, name, description, is_supported, is_active, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)",
        [(code, name, description, supported, now, now) for code, name, description, supported in payment_methods],
    )
    connection.execute(
        "INSERT INTO shipping_rules(country_code, product_id, variant_id, shipping_supported, shipping_included, shipping_fee, currency, eta_min_days, eta_max_days, tracking_supported, notes, is_active, created_at, updated_at) "
        "SELECT NULL, NULL, NULL, 1, 1, 0, 'USD', 7, 10, 1, '', 1, ?, ? WHERE NOT EXISTS (SELECT 1 FROM shipping_rules WHERE country_code IS NULL AND product_id IS NULL AND variant_id IS NULL)",
        (now, now),
    )
    countries = [
        ("PK", "Pakistan", "PKR"), ("BD", "Bangladesh", "BDT"), ("IN", "India", "INR"),
        ("SA", "Saudi Arabia", "SAR"), ("KW", "Kuwait", "KWD"), ("NG", "Nigeria", "NGN"),
        ("CM", "Cameroon", "XAF"), ("CI", "Côte d'Ivoire", "XOF"), ("RO", "Romania", "RON"),
        ("MA", "Morocco", "MAD"),
    ]
    connection.executemany(
        "INSERT OR IGNORE INTO countries(country_code, country_name, currency_code, shipping_supported, is_active) VALUES (?, ?, ?, 1, 1)",
        countries,
    )
