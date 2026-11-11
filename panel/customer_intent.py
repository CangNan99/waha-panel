"""Small, deterministic customer-intent classifiers used by automation."""

from __future__ import annotations

import re
import unicodedata


_PURCHASE_RE = re.compile(
    r"(?:购买|买|下单|订购|付款|支付|付费|订阅|续费|开通|充值|结账|"
    r"\bbuy\b|\bpurchase\b|\border\b|\bcheckout\b|\bpay(?:ment)?\b|"
    r"\bsubscribe\b|\brenew\b)",
    re.IGNORECASE,
)
_HIGH_INTEREST_RE = re.compile(
    r"(?:感兴趣|有兴趣|怎么买|怎么下单|"
    r"马上|立即|现在可以|ready\s+to\s+buy|very\s+interested|how\s+to\s+get)",
    re.IGNORECASE,
)


def _normalize(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()


def classify_purchase_intent(text):
    """Return at most one stable system label for the current customer text."""
    normalized = _normalize(text)
    if not normalized:
        return []
    if _HIGH_INTEREST_RE.search(normalized):
        return ["高意向"]
    if _PURCHASE_RE.search(normalized):
        return ["购买意向"]
    return []


__all__ = ["classify_purchase_intent"]
