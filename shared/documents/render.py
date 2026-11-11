"""One Jinja environment for every trade document: the service's own templates
first, then the shared layout (``shared/documents/templates``)."""
from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import ChoiceLoader, Environment, FileSystemLoader, select_autoescape

from . import issuer

SHARED_TEMPLATES = Path(__file__).parent / "templates"

_ONES = ("", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen")
_TENS = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")
_CURRENCY_WORDS = {"USD": ("US Dollars", "Cents"), "EUR": ("Euros", "Cents"), "INR": ("Rupees", "Paise"),
                   "GBP": ("Pounds Sterling", "Pence"), "AED": ("UAE Dirhams", "Fils")}


def _below_thousand(n: int) -> str:
    words = []
    if n >= 100:
        words.append(f"{_ONES[n // 100]} Hundred")
        n %= 100
    if n >= 20:
        words.append(_TENS[n // 10] + (f"-{_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        words.append(_ONES[n])
    return " ".join(words)


def number_words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    for value, name in ((10**9, "Billion"), (10**6, "Million"), (1000, "Thousand"), (1, "")):
        if n >= value:
            chunk, n = divmod(n, value)
            parts.append(f"{_below_thousand(chunk)} {name}".strip())
    return " ".join(parts)


def amount_words(value: Any, currency: str = "USD") -> str:
    """``25000.5, "USD"`` → ``US Dollars Twenty-Five Thousand and Cents Fifty Only``."""
    try:
        amount = round(float(value or 0), 2)
    except (TypeError, ValueError):
        return ""
    major, minor = _CURRENCY_WORDS.get((currency or "USD").upper(), (currency, "Cents"))
    whole, cents = int(amount), int(round((amount - int(amount)) * 100))
    text = f"{major} {number_words(whole)}"
    if cents:
        text += f" and {minor} {number_words(cents)}"
    return f"{text} Only"


def money(value: Any, currency: str | None = None, decimals: int = 2) -> str:
    try:
        text = f"{float(value or 0):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"
    return f"{currency} {text}" if currency else text


def qty(value: Any, decimals: int = 3) -> str:
    try:
        return f"{float(value or 0):,.{decimals}f}"
    except (TypeError, ValueError):
        return "—"


def fmt_date(value: Any) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return value
    if isinstance(value, (datetime, date)):
        return value.strftime("%d %b %Y")
    return str(value)


def pct(value: Any, decimals: int = 0) -> str:
    try:
        return f"{float(value) * 100:.{decimals}f}%"
    except (TypeError, ValueError):
        return "—"


def environment(service_templates: Path | str | None = None) -> Environment:
    loaders = [FileSystemLoader(str(service_templates))] if service_templates else []
    loaders.append(FileSystemLoader(str(SHARED_TEMPLATES)))
    env = Environment(loader=ChoiceLoader(loaders), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters.update(money=money, qty=qty, date=fmt_date, pct=pct, words=amount_words)
    return env


def tranche_schedule() -> list[dict[str, Any]]:
    """The staged escrow schedule documents print (same source as the escrow itself)."""
    labels = {"PO_ISSUED": "Purchase Order issued", "QC_PASSED": "Independent inspection passed",
              "CLEARED": "Export cleared / goods arrived", "ACCEPTED": "Delivery accepted by buyer"}
    try:
        from services.escrow_guard.service import DEFAULT_TRANCHES  # type: ignore[import]
        return [{"id": t, "trigger": trg, "pct": p, "label": labels.get(trg, trg)} for t, trg, p in DEFAULT_TRANCHES]
    except Exception:  # noqa: BLE001
        return []


def render_document(env: Environment, template: str, ctx: dict[str, Any], *,
                    exporter_fields: tuple[str, ...] = (), required: dict[str, Any] | None = None) -> str:
    """Render ``template``. Any exporter fact in ``exporter_fields`` or value in
    ``required`` that is empty is listed in a DRAFT banner on the document — a
    trade document never prints a made-up value in place of a missing one."""
    reasons = issuer.missing(exporter_fields)
    reasons += [label for label, value in (required or {}).items() if value in (None, "", 0, 0.0, [])]
    full = {
        "exporter": issuer.exporter(),
        "tranches": tranche_schedule(),
        "generated_at": datetime.now(timezone.utc),
        "draft_reasons": reasons,
        **ctx,
    }
    return env.get_template(template).render(**full)
