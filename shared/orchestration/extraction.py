"""Read a counterparty's e-mail into structured fields.

Buyer reply → ``BuyerRequirement`` (grade, quantity, port, incoterm, target price,
delivery window, certifications…). Supplier reply → offer fields (FOB price, MOQ,
capacity, lead time, port of loading, grade, certifications, payment terms).

The shared LLM Gateway reads the text into strict JSON; when it is not
configured or fails, a deterministic parser picks up what the patterns can
(prices, quantities, incoterms, lead times, MOQ). Never raises: an unreadable
reply returns an empty result and the match board shows the field as missing.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_INCOTERM = re.compile(r"\b(EXW|FOB|FCA|CFR|CNF|C&F|CIF|CPT|CIP|DAP|DPU|DDP)\b", re.IGNORECASE)
_PRICE = re.compile(
    r"(?:USD|US\$|\$)\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:/|per)?\s*(MT|metric ton(?:ne)?s?|tons?|tonnes?|kg|kilo(?:gram)?s?)?"
    r"|([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:USD|US\$|\$)\s*(?:/|per)?\s*(MT|metric ton(?:ne)?s?|tons?|tonnes?|kg|kilo(?:gram)?s?)?",
    re.IGNORECASE)
_BARE_PRICE = re.compile(r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*(?:/|per)\s*(MT|metric ton(?:ne)?s?|tons?|tonnes?|kg|kilo(?:gram)?s?)\b", re.IGNORECASE)
_QTY = re.compile(r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*(MT|metric ton(?:ne)?s?|tons?|tonnes?|containers?|FCLs?|x\s?20'?|kg)\b",
                  re.IGNORECASE)
_MOQ = re.compile(r"\b(?:MOQ|minimum order(?: quantity)?)\D{0,15}([0-9][0-9,]*(?:\.[0-9]+)?)\s*(MT|tons?|tonnes?|containers?|kg)?",
                  re.IGNORECASE)
_CAPACITY = re.compile(r"\b([0-9][0-9,]*(?:\.[0-9]+)?)\s*(MT|tons?|tonnes?)\s*(?:per|/|a)\s*month\b", re.IGNORECASE)
_LEAD = re.compile(r"\b(?:lead time|delivery|ship(?:ment)?|dispatch)\D{0,25}([0-9]{1,3})\s*(?:-\s*[0-9]{1,3}\s*)?(days?|weeks?)\b",
                   re.IGNORECASE)
_PORT = re.compile(r"\b(?:(?i:port of (?:discharge|destination|loading)?)\s*:?\s*|to |at |destination:?\s*|POD:?\s*|POL:?\s*)"
                   r"([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)\s*(?:port|harbou?r)?\b")
_WITHIN = re.compile(r"\bwithin\s+([0-9]{1,3})\s*(days?|weeks?|months?)\b", re.IGNORECASE)
_CERTS = re.compile(r"\b(USP|BP|EP|IP|FCC|ISO\s?\d{4,5}|HACCP|FSSAI|Halal|Kosher|GMP|REACH|organic|non-GMO|COA|MSDS|SGS)\b",
                    re.IGNORECASE)
_GRADE = re.compile(r"\b(?:grade|spec(?:ification)?|purity|quality)\s*:?\s*([^\n;,]{2,40}?)(?=[\n;,]|\.\s|\.$|$)",
                    re.IGNORECASE)
_PORT_AFTER_TERM = re.compile(r"\b(?:CIF|CFR|CNF|C&F|DAP|DDP|CPT|CIP)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)")
_POL_AFTER_FOB = re.compile(r"\b(?:FOB|FCA)\s+([A-Z][a-z]+(?:\s[A-Z][a-z]+)?)")
_GRADE_HINT = re.compile(r"\b(\d{2}(?:\.\d+)?\s?%|refined|crude|technical|pharma(?:ceutical)?|food grade|industrial|"
                         r"ASTA\s?\d+|bold|extra bold|premium|USP|BP)\b", re.IGNORECASE)
_PAYMENT = re.compile(r"\b(\d{1,3}\s?%\s*(?:advance|TT|T/T)[^\n.]{0,40}|LC at sight|L/C[^\n.]{0,30}|CAD|DP at sight|"
                      r"T/T[^\n.]{0,30}|escrow)\b", re.IGNORECASE)


def _num(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _to_mt(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    u = (unit or "mt").lower()
    if u.startswith("kg") or u.startswith("kilo"):
        return round(value / 1000.0, 3)
    if u.startswith("container") or u.startswith("fcl") or u.startswith("x"):
        return round(value * 20.0, 3)  # one 20' FCL ≈ 20 MT of bulk agri / chemicals
    return value


def _price_per_mt(value: float | None, unit: str | None) -> float | None:
    if value is None:
        return None
    u = (unit or "").lower()
    if u.startswith("kg") or u.startswith("kilo"):
        return round(value * 1000.0, 2)
    if not u and value < 20:  # "$1.15" with no unit is a per-kg price
        return round(value * 1000.0, 2)
    return value


def _days(n: str, unit: str) -> int | None:
    v = _num(n)
    if v is None:
        return None
    u = unit.lower()
    return int(v * 7) if u.startswith("week") else int(v * 30) if u.startswith("month") else int(v)


def _first_price(text: str) -> float | None:
    for m in _PRICE.finditer(text):
        value = _num(m.group(1) or m.group(3) or "")
        unit = m.group(2) or m.group(4)
        price = _price_per_mt(value, unit)
        if price and 20 <= price <= 200000:
            return price
    # "We can pay 1200 per MT": no currency, but only a price reads that way.
    for m in _BARE_PRICE.finditer(text):
        price = _price_per_mt(_num(m.group(1)), m.group(2))
        if price and 20 <= price <= 200000:
            return price
    return None


def _quantity(text: str) -> float | None:
    for m in _QTY.finditer(text):
        if _MOQ.search(text[max(0, m.start() - 25):m.end()]):
            continue
        q = _to_mt(_num(m.group(1)), m.group(2))
        if q and 0.1 <= q <= 100000:
            return q
    return None


def _grade(text: str) -> str | None:
    m = _GRADE.search(text)
    if m:
        return m.group(1).strip()
    hints = list(dict.fromkeys(h.group(1) for h in _GRADE_HINT.finditer(text)))
    return " ".join(hints[:3]) or None


_DESTINATION_PORT = re.compile(
    r"\b(?:(?i:destination\s+port|port\s+of\s+(?:discharge|destination)|POD)\s*(?i:is|will\s+be|:|-)?\s*"
    r"|(?:CIF|CFR|CNF|C&F|DAP|DDP|CPT|CIP)\s+)"
    r"([A-Z][a-zA-Z]+(?: [A-Z][a-zA-Z]+){0,2})"
)
_NOT_A_PORT = {"your", "our", "the", "destination", "port", "price", "basis", "terms", "incoterm", "incoterms", "to", "at"}


def parse_destination_port(text: str | None) -> str | None:
    """The port named outright in *text* ("destination port is X", "CIF X",
    "port of discharge: X"). Stricter than the requirement parse: this value
    is printed on a Proforma Invoice, so a loose "to Monday" must not match."""
    for match in _DESTINATION_PORT.finditer(text or ""):
        port = match.group(1).strip()
        if port.split()[0].lower() not in _NOT_A_PORT:
            return port
    return None


def parse_buyer(text: str) -> dict[str, Any]:
    """Deterministic buyer-requirement parse (the gateway-down fallback)."""
    t = text or ""
    out: dict[str, Any] = {}
    if (inc := _INCOTERM.search(t)):
        out["incoterm"] = inc.group(1).upper().replace("CNF", "CFR").replace("C&F", "CFR")
    if (price := _first_price(t)) is not None:
        out["target_price_usd_per_mt"] = price
        out["target_price_incoterm"] = out.get("incoterm")
    if (q := _quantity(t)) is not None:
        out["quantity_mt"] = q
    if (port := _PORT_AFTER_TERM.search(t) or _PORT.search(t)):
        out["destination_port"] = port.group(1).strip()
    if (w := _WITHIN.search(t)):
        out["delivery_window"] = w.group(0)
        out["delivery_by_days"] = _days(w.group(1), w.group(2))
    certs = list(dict.fromkeys(c.group(1).upper() for c in _CERTS.finditer(t)))
    if certs:
        out["certifications"] = certs
    if (g := _grade(t)):
        out["grade"] = g
    if (pay := _PAYMENT.search(t)):
        out["payment_terms"] = pay.group(1).strip()
    return out


def parse_supplier(text: str) -> dict[str, Any]:
    """Deterministic supplier-offer parse (the gateway-down fallback)."""
    t = text or ""
    out: dict[str, Any] = {}
    if (price := _first_price(t)) is not None:
        out["fob_price_usd_per_mt"] = price
    if (m := _MOQ.search(t)):
        out["moq_mt"] = _to_mt(_num(m.group(1)), m.group(2))
    if (m := _CAPACITY.search(t)):
        out["capacity_mt_per_month"] = _num(m.group(1))
    if (m := _LEAD.search(t)):
        out["lead_time_days"] = _days(m.group(1), m.group(2))
    if (m := re.search(r"\b(?:(?i:port of loading)|POL|ex)\s*:?\s*([A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?)", t)
            or _POL_AFTER_FOB.search(t)):
        out["port_of_loading"] = m.group(1).strip()
    certs = list(dict.fromkeys(c.group(1).upper() for c in _CERTS.finditer(t)))
    if certs:
        out["certifications"] = certs
    if (g := _grade(t)):
        out["grade"] = g
    if (pay := _PAYMENT.search(t)):
        out["payment_terms"] = pay.group(1).strip()
    return out


_BUYER_FIELDS = {
    "grade": "string|null", "packaging": "string|null", "quantity_mt": "number|null",
    "delivery_window": "string|null", "delivery_by_days": "integer|null (days from today the goods must arrive)",
    "incoterm": "EXW|FOB|CFR|CIF|DAP|DDP|null", "destination_port": "string|null",
    "target_price_usd_per_mt": "number|null (convert per-kg to per-MT; null if no price is named)",
    "target_price_incoterm": "string|null", "certifications": "string[]", "payment_terms": "string|null",
    "notes": "string|null (anything else that matters for an offer, max 200 chars)",
}
_SUPPLIER_FIELDS = {
    "grade": "string|null", "fob_price_usd_per_mt": "number|null (FOB India, per metric ton; convert per-kg)",
    "moq_mt": "number|null", "capacity_mt_per_month": "number|null", "lead_time_days": "integer|null",
    "port_of_loading": "string|null", "certifications": "string[]", "payment_terms": "string|null",
    "validity_days": "integer|null",
}


async def _llm(kind: str, text: str, fields: dict[str, str], product: str | None) -> dict[str, Any] | None:
    try:
        from shared.llm_gateway import LLMClient, LLMConfigurationError, LLMExhaustedException
    except Exception:  # noqa: BLE001
        return None
    system = (
        f"You read one business e-mail from a {kind} in an export trade"
        + (f" of {product}" if product else "")
        + " and return ONLY a JSON object with these keys: "
        + json.dumps(fields)
        + ". Use null when the e-mail does not say it — never guess. Quantities in metric tons."
    )
    try:
        from shared.orchestration.memory import context_for
        known = await context_for(purpose="product grades specifications units", product=product,
                                  top_k=4, max_chars=1000)
    except Exception:  # noqa: BLE001
        known = ""
    if known:
        system += ("\nProduct knowledge PortLess holds (for naming grades consistently; facts in the e-mail win):\n"
                   + known)
    try:
        resp = await LLMClient().chat(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": text[:6000]}],
            caller=f"extract_{kind}", temperature=0.0, max_tokens=500,
        )
    except (LLMConfigurationError, LLMExhaustedException) as exc:
        logger.info("Extraction: gateway unavailable (%s) — pattern parser only", type(exc).__name__)
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Extraction: gateway call failed: %s", exc)
        return None
    raw = (resp.content or "").strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        raw = raw[4:] if raw.startswith("json") else raw
    try:
        data = json.loads(raw.strip())
    except (ValueError, TypeError):
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        try:
            data = json.loads(m.group(0)) if m else None
        except (ValueError, TypeError):
            data = None
    return data if isinstance(data, dict) else None


def _clean(data: dict[str, Any], allowed: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in allowed:
        v = data.get(k)
        if v in (None, "", [], "null"):
            continue
        if k == "certifications":
            v = [str(x) for x in (v if isinstance(v, list) else [v]) if x]
        elif k.endswith(("_mt", "_per_mt", "_month")):
            v = _num(str(v)) if not isinstance(v, (int, float)) else float(v)
            if v is None or v <= 0:
                continue
        elif k.endswith("_days"):
            try:
                v = int(float(v))
            except (TypeError, ValueError):
                continue
        else:
            v = str(v).strip()[:200]
        out[k] = v
    return out


async def extract_buyer(text: str, *, product: str | None = None) -> dict[str, Any]:
    """Buyer reply → requirement fields (``source``/``confidence`` included)."""
    pattern = parse_buyer(text)
    llm = await _llm("buyer", text, _BUYER_FIELDS, product)
    if llm:
        fields = {**pattern, **_clean(llm, _BUYER_FIELDS)}
        fields["confidence"] = 0.85
    else:
        fields = pattern
        fields["confidence"] = 0.6 if pattern else 0.0
    fields["source"] = "email"
    fields["updated_at"] = datetime.now(timezone.utc)
    return fields


async def extract_supplier(text: str, *, product: str | None = None) -> dict[str, Any]:
    """Supplier reply → offer fields."""
    pattern = parse_supplier(text)
    llm = await _llm("supplier", text, _SUPPLIER_FIELDS, product)
    return {**pattern, **_clean(llm, _SUPPLIER_FIELDS)} if llm else pattern
