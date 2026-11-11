"""Neutral escrow providers — Tazapay and Escrow.com, the buyer chooses one.

Both hold the buyer's money in the provider's own segregated account. PortLess
never holds the principal and **never moves it by itself**: a tranche leaves the
escrow only when the provider pays it out.

  Tazapay (``tazapay``) — Tazapay API v3 (developer.tazapay.com/openapi.yaml)
      POST /v3/escrow                 open the escrow; ``data.url`` is the buyer's hosted pay page
      GET  /v3/escrow/{id}            ``payment_status`` / ``amount_paid`` (minor units)
      POST /v3/escrow/{id}/expire     close an escrow nobody paid
      POST /v3/document               a release document (``reference`` = escrow id, ``type``, ``url``)
      POST /v3/refund                 money back to the buyer (``payin`` = escrow id)
      Auth ``Basic base64(API_KEY:API_SECRET)``; amounts in the currency's minor unit.
      There is no release call: PortLess submits each tranche's documents and
      **Tazapay verifies them and pays out**. Webhook: header ``signature`` (also
      ``webhook-signature``) = base64(HMAC-SHA256(secret, event_id + raw body + created_at)).

  Escrow.com (``escrow_com``) — API 2017-09-01 (escrow.com/api/docs)
      POST  /transaction                          a milestone transaction: one ``milestone`` item per tranche
      GET   /transaction/{id}                     items[].status (shipped / accepted / rejected …), schedule status
      PATCH /transaction/{id}/item/{item_id}      ``{"action": "ship", "shipping_information": …}`` — PortLess
                                                  delivers a milestone with its proof
      PATCH /transaction/{id}                     ``{"action": "cancel", "cancel_information": …}``
      Auth ``Basic base64(email:api_key)``. **The buyer accepts each milestone on
      Escrow.com** (or it is accepted when its inspection period lapses) and
      Escrow.com disburses — a partner cannot accept for the buyer. Webhooks are
      not signed: the payload is only a hint, the transaction is always fetched
      before anything is applied (Escrow.com's own advice).

So ``release()`` answers ``SUBMITTED`` (proof is with the provider) and the
tranche counts as released only when ``interpret_status()`` reads the
provider's payout — from a webhook, the escrow watcher's poll or a staff relay.

``INTEGRATION_MODE`` (``shared/integration_mode.py``):
  • live (default) — real HTTP with the configured credentials; a placeholder
    key is sent as-is and the provider's refusal surfaces as ``ConnectorError``.
  • sandbox — ``ESCROW_SANDBOX_PROVIDERS=tazapay,escrow_com`` (or ``all``) sends
    the same requests to the provider simulator ``services.escrow_sandbox`` (in
    process via ASGI, or at ``ESCROW_SANDBOX_URL``); any other provider answers
    from the old one-line stubs (the pytest default).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from shared.integration_mode import sandbox as stub_mode

from .base import Connector
from .payments import ConnectorError

logger = logging.getLogger(__name__)

_TIMEOUT = 20.0

# ---------------------------------------------------------------------------
# Catalogue — what the buyer is offered, what each provider does
# ---------------------------------------------------------------------------

ESCROW_PROVIDER_CATALOG: dict[str, dict[str, Any]] = {
    "tazapay": {
        "display_name": "Tazapay",
        "release_authority": "Tazapay checks PortLess's documents for each stage and pays it out",
        "buyer_pays_by": "card, bank transfer or local payment methods on Tazapay's checkout page",
        "buyer_accepts_at_provider": False,
        "fee_pct": 1.0,
        "aliases": ("tazapay", "taza pay", "taza-pay"),
    },
    "escrow_com": {
        "display_name": "Escrow.com",
        "release_authority": "the buyer accepts each stage on Escrow.com (or its inspection period lapses) before Escrow.com pays it out",
        "buyer_pays_by": "wire transfer to Escrow.com",
        "buyer_accepts_at_provider": True,
        "fee_pct": 0.89,
        "aliases": ("escrow.com", "escrow com", "escrowcom", "escrow_com"),
    },
}

_TRANCHE_TITLES = {
    "PO_ISSUED": "Production advance — purchase order issued",
    "QC_PASSED": "Quality cleared — independent inspection passed",
    "CLEARED": "Exported — bill of lading and customs clearance",
    "ACCEPTED": "Delivered — buyer accepts the goods",
}

# Tazapay document ``type`` per tranche (free text in v3; a request from Tazapay
# names the exact document, which then takes precedence).
_TAZAPAY_DOC_TYPE = {
    "PO_ISSUED": "purchase_order",
    "QC_PASSED": "inspection_report",
    "CLEARED": "bill_of_lading",
    "ACCEPTED": "delivery_proof",
}


def enabled_providers() -> list[str]:
    raw = os.getenv("ESCROW_PROVIDERS_ENABLED", "tazapay,escrow_com")
    names = [p.strip().lower() for p in raw.split(",") if p.strip()]
    return [p for p in names if p in ESCROW_PROVIDER_CATALOG] or list(ESCROW_PROVIDER_CATALOG)


def default_provider() -> str:
    name = os.getenv("ESCROW_DEFAULT_PROVIDER", "").strip().lower() or "tazapay"
    enabled = enabled_providers()
    return name if name in enabled else enabled[0]


def resolve_escrow_provider(name: str | None) -> str | None:
    """Catalogue key for a provider name a buyer or operator wrote; None if unknown."""
    if not name:
        return None
    text = str(name).strip().lower()
    if text in ESCROW_PROVIDER_CATALOG:
        return text
    for key, meta in ESCROW_PROVIDER_CATALOG.items():
        if any(alias == text for alias in meta["aliases"]):
            return key
    return None


_NEGATION = re.compile(r"\b(not|no|never|don't|do not|won't|avoid|without|rather than|instead of)\b[^.,;!?\n]{0,24}$")


def detect_escrow_provider_mention(text: str | None) -> str | None:
    """The one escrow provider a buyer's reply names as its choice, else None.

    Deterministic: an alias of exactly one provider, not negated just before it
    ("not Tazapay", "rather than Escrow.com"). Quoted history (lines starting
    with ">" or after "On … wrote:") is ignored."""
    if not text:
        return None
    lines = []
    for line in text.splitlines():
        if re.match(r"^\s*on .{0,200}wrote:\s*$", line, re.IGNORECASE):
            break
        if not line.lstrip().startswith(">"):
            lines.append(line)
    lowered = "\n".join(lines).lower()
    found: set[str] = set()
    for key, meta in ESCROW_PROVIDER_CATALOG.items():
        for alias in meta["aliases"]:
            for m in re.finditer(rf"(?<![\w.]){re.escape(alias)}(?![\w])", lowered):
                if not _NEGATION.search(lowered[max(0, m.start() - 40):m.start()]):
                    found.add(key)
    enabled = enabled_providers()
    found &= set(enabled)
    return found.pop() if len(found) == 1 else None


def options_summary() -> str:
    """Buyer-facing one-liner per enabled provider, for the e-mail draft."""
    return " | ".join(f"{ESCROW_PROVIDER_CATALOG[k]['display_name']} ({ESCROW_PROVIDER_CATALOG[k]['release_authority']}; "
                      f"pay by {ESCROW_PROVIDER_CATALOG[k]['buyer_pays_by']})" for k in enabled_providers())


def display_name(provider: str | None) -> str:
    meta = ESCROW_PROVIDER_CATALOG.get(provider or "")
    return meta["display_name"] if meta else (provider or "").replace("_", " ").title()


def buyer_accepts_at_provider(provider: str | None) -> bool:
    return bool(ESCROW_PROVIDER_CATALOG.get(provider or "", {}).get("buyer_accepts_at_provider"))


# ---------------------------------------------------------------------------
# Sandbox switch (provider simulator)
# ---------------------------------------------------------------------------

SANDBOX_PROVIDERS = ("tazapay", "escrow_com")
_SANDBOX_HOST = "http://escrow-sandbox.local"


def sandbox_providers() -> set[str]:
    raw = os.getenv("ESCROW_SANDBOX_PROVIDERS", "").strip().lower()
    if raw in ("all", "*", "true", "1", "yes"):
        return set(SANDBOX_PROVIDERS)
    return {p.strip() for p in raw.split(",") if p.strip() in SANDBOX_PROVIDERS}


def in_simulator(provider: str) -> bool:
    """The provider runs on the simulator — only ever in INTEGRATION_MODE=sandbox."""
    return stub_mode() and provider in sandbox_providers()


def _simulator_client(prefix: str, auth: tuple[str, str]) -> httpx.AsyncClient:
    url = os.getenv("ESCROW_SANDBOX_URL", "").strip().rstrip("/")
    if url:
        return httpx.AsyncClient(base_url=f"{url}{prefix}", auth=auth, timeout=_TIMEOUT)
    from services.escrow_sandbox.main import app as sandbox_app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=sandbox_app), base_url=f"{_SANDBOX_HOST}{prefix}",
                             auth=auth, timeout=_TIMEOUT)


def _error(provider: str, what: str, resp: httpx.Response) -> ConnectorError:
    return ConnectorError(f"{provider} {what} failed: HTTP {resp.status_code} — {resp.text[:300]}")


def _minor(amount: Any) -> int:
    return round(float(amount or 0) * 100)


def _major(amount: Any) -> float | None:
    try:
        return round(float(amount) / 100.0, 2)
    except (TypeError, ValueError):
        return None


def _flag(container: Any, key: str) -> bool:
    return isinstance(container, dict) and bool(container.get(key))


# ---------------------------------------------------------------------------
# Tazapay — API v3
# ---------------------------------------------------------------------------

class TazapayEscrowConnector(Connector):
    """Tazapay escrow (``POST /v3/escrow``). Requires TAZAPAY_API_KEY / _API_SECRET."""

    name = "tazapay"

    def __init__(self) -> None:
        self._api_key = os.getenv("TAZAPAY_API_KEY", "")
        self._api_secret = os.getenv("TAZAPAY_API_SECRET", "")
        self._webhook_secret = os.getenv("TAZAPAY_WEBHOOK_SECRET", "")
        env = os.getenv("TAZAPAY_ENV", "production").strip().lower()
        default = "https://service-sandbox.tazapay.com" if env == "sandbox" else "https://service.tazapay.com"
        base = (os.getenv("TAZAPAY_BASE_URL", "").strip() or default).rstrip("/")
        self._base_url = base.removesuffix("/v3")
        self._webhook_url = os.getenv("TAZAPAY_WEBHOOK_URL", "").strip()
        self._release_events = {
            e.strip() for e in os.getenv(
                "TAZAPAY_RELEASE_EVENTS", "escrow.verification.succeeded,escrow.released,escrow.release.succeeded",
            ).split(",") if e.strip()
        }

    # -- plumbing -----------------------------------------------------------

    def _stubbed(self) -> bool:
        return stub_mode() and not in_simulator(self.name)

    def _client(self) -> httpx.AsyncClient:
        auth = (self._api_key, self._api_secret)
        if in_simulator(self.name):
            return _simulator_client("/tazapay", (self._api_key or "sandbox-key", self._api_secret or "sandbox-secret"))
        return httpx.AsyncClient(base_url=self._base_url, auth=auth, timeout=_TIMEOUT)

    @staticmethod
    def _data(resp: httpx.Response) -> dict[str, Any]:
        body = resp.json()
        return body.get("data") if isinstance(body, dict) and isinstance(body.get("data"), dict) else body

    def _stub_ref(self) -> str:
        return f"tzp_stub_{uuid4().hex[:12]}"

    # -- catalogue ----------------------------------------------------------

    async def list_payment_methods(self, corridor: dict[str, Any] | None = None) -> list[str]:
        return ["card", "bank_transfer", "local_payment", "wire"]

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        """Tazapay has no fee-quote endpoint; its contracted fee is configured."""
        fee = float(os.getenv("TAZAPAY_FEE_PCT", ESCROW_PROVIDER_CATALOG["tazapay"]["fee_pct"]))
        return {"provider": "tazapay", "fee_pct": fee, "source": "configured"}

    # -- open ---------------------------------------------------------------

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """Open the escrow. Returns ext_ref (``esc_…``) + the buyer's pay page."""
        if self._stubbed():
            ref = self._stub_ref()
            logger.warning("TazapayEscrowConnector: INTEGRATION_MODE=sandbox — stub escrow %s", ref)
            return {
                "ext_ref": ref, "provider": "tazapay", "status": "AWAITING_FUNDING",
                "amount": request.get("amount", 0), "currency": request.get("currency", "USD"),
                "checkout_url": f"https://pay.tazapay.com/checkout/{ref}", "stub": True,
            }
        buyer = request.get("buyer") or {}
        body: dict[str, Any] = {
            "invoice_currency": str(request.get("currency", "USD")).upper(),
            "amount": _minor(request.get("amount")),
            "customer_details": {
                "name": buyer.get("name") or "Buyer",
                "email": buyer.get("email") or "",
                "country": (buyer.get("country") or "").upper(),
            },
            "transaction_description": (request.get("description") or "PortLess trade")[:200],
            "reference_id": request.get("reference", ""),
            "metadata": {
                "deal_id": request.get("deal_id", ""),
                "escrow_id": request.get("escrow_id", ""),
                "note": (request.get("note") or "")[:500],
                "tranches": json.dumps([
                    {"tranche_id": t.get("tranche_id"), "trigger": t.get("trigger"), "amount": t.get("amount")}
                    for t in request.get("tranches") or []
                ]),
            },
        }
        if buyer.get("phone"):
            body["customer_details"]["phone"] = buyer["phone"]
        for key in ("success_url", "cancel_url", "expires_at"):
            if request.get(key):
                body[key] = request[key]
        if self._webhook_url:
            body["webhook_url"] = self._webhook_url
        async with self._client() as client:
            resp = await client.post("/v3/escrow", json=body)
        if resp.status_code not in range(200, 300):
            raise _error("Tazapay", "create-escrow", resp)
        data = self._data(resp)
        return {
            "ext_ref": data.get("id", ""),
            "provider": "tazapay",
            "status": data.get("status", "active"),
            "payment_status": data.get("payment_status", "unpaid"),
            "amount": request.get("amount", 0),
            "currency": data.get("invoice_currency") or request.get("currency", "USD"),
            "checkout_url": data.get("url"),
        }

    # -- release (submit the tranche's documents; Tazapay pays out) ---------

    async def release(self, request: dict[str, Any]) -> dict[str, Any]:
        ext_ref = request.get("ext_ref", "")
        if self._stubbed():
            return {"ext_ref": ext_ref, "status": "RELEASED", "amount": request.get("amount", 0),
                    "tranche_id": request.get("tranche_id"), "stub": True}
        trigger = request.get("trigger") or ""
        documents = [d for d in request.get("documents") or [] if d.get("url")]
        if not documents:
            raise ConnectorError(
                f"Tazapay release of {request.get('tranche_id')}: no release document to submit — Tazapay downloads "
                "each document from a URL; set PORTLESS_PUBLIC_BASE_URL and ESCROW_PROOF_LINK_SECRET so every stage "
                "gets a signed proof sheet link")
        uploaded: list[str] = []
        async with self._client() as client:
            for doc in documents:
                doc_type = doc.get("type") or _TAZAPAY_DOC_TYPE.get(trigger, "other")
                body = {
                    "url": doc.get("url") or "",
                    "type": doc_type,
                    "reference": ext_ref,
                    "description": (request.get("note") or "")[:300],
                    "metadata": json.dumps({"tranche_id": request.get("tranche_id"), "trigger": trigger,
                                            "amount": request.get("amount"), "proof_ref": doc.get("name") or ""}),
                }
                if doc_type == "other":
                    body["name"] = doc.get("name") or "proof"
                resp = await client.post("/v3/document", json=body)
                if resp.status_code not in range(200, 300):
                    raise _error("Tazapay", "upload-document", resp)
                uploaded.append(self._data(resp).get("id", ""))
        return {"ext_ref": ext_ref, "status": "SUBMITTED", "tranche_id": request.get("tranche_id"),
                "amount": request.get("amount"), "documents": uploaded}

    async def refund(self, request: dict[str, Any]) -> dict[str, Any]:
        """Undisbursed money back to the buyer (``POST /v3/refund``)."""
        ext_ref = request.get("ext_ref", "")
        if self._stubbed():
            return {"ext_ref": ext_ref, "status": "REFUNDED", "amount": request.get("amount", 0), "stub": True}
        body: dict[str, Any] = {"payin": ext_ref, "reason": request.get("reason", "") or "refund"}
        if request.get("amount") is not None:
            body["amount"] = _minor(request["amount"])
        if request.get("currency"):
            body["currency"] = str(request["currency"]).lower()
        async with self._client() as client:
            resp = await client.post("/v3/refund", json=body)
        if resp.status_code not in range(200, 300):
            raise _error("Tazapay", "refund", resp)
        data = self._data(resp)
        return {"ext_ref": ext_ref, "status": "REFUNDED", "refund_id": data.get("id"), "provider_payload": data}

    async def cancel(self, ext_ref: str, reason: str = "") -> dict[str, Any]:
        """Close an escrow nobody paid (``POST /v3/escrow/{id}/expire``)."""
        if self._stubbed():
            return {"ext_ref": ext_ref, "status": "expired", "stub": True}
        async with self._client() as client:
            resp = await client.post(f"/v3/escrow/{ext_ref}/expire")
        if resp.status_code not in range(200, 300):
            raise _error("Tazapay", "expire-escrow", resp)
        return self._data(resp)

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if self._stubbed():
            return {"ext_ref": ext_ref, "status": "AWAITING_FUNDING", "stub": True}
        async with self._client() as client:
            resp = await client.get(f"/v3/escrow/{ext_ref}")
        if resp.status_code not in range(200, 300):
            raise _error("Tazapay", "fetch-escrow", resp)
        return self._data(resp)

    # -- reading the provider -----------------------------------------------

    def interpret_status(self, body: dict[str, Any], tranches: list[Any],
                         event: dict[str, Any] | None = None) -> dict[str, Any]:
        """What the provider says has happened, in PortLess terms.

        Funding: ``payment_status`` paid / partially paid and ``amount_paid``
        (minor units). Releases: a submitted document Tazapay marked verified
        (``transaction_documents[].status``) or a release webhook event, read
        back to the tranche through the document's ``metadata.tranche_id``."""
        out: dict[str, Any] = {"funded": False, "funded_amount": None, "released": [], "rejected": [],
                               "cancelled": False, "refunded": False, "provider_status": None}
        if not isinstance(body, dict) or body.get("stub"):
            return out
        payment_status = str(body.get("payment_status") or "").lower()
        out["provider_status"] = body.get("status") or payment_status or None
        paid = _major(body.get("amount_paid"))
        if payment_status in ("paid", "partially_paid", "captured", "succeeded") or (paid or 0) > 0:
            out["funded"] = True
            out["funded_amount"] = paid
        if str(body.get("status") or "").lower() == "expired" and not out["funded"]:
            out["cancelled"] = True
        if payment_status == "refunded":
            out["refunded"] = True
        known = {getattr(t, "tranche_id", None) for t in tranches}

        def tranche_of(doc: Any) -> str | None:
            meta = doc.get("metadata") if isinstance(doc, dict) else None
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except ValueError:
                    meta = None
            tid = meta.get("tranche_id") if isinstance(meta, dict) else None
            return tid if tid in known else None

        for doc in body.get("transaction_documents") or []:
            if not isinstance(doc, dict):
                continue
            state = str(doc.get("status") or doc.get("verification_status") or "").lower()
            tid = tranche_of(doc)
            if tid and state in ("verified", "approved", "accepted", "released"):
                out["released"].append(tid)
            elif tid and state in ("failed", "rejected"):
                out["rejected"].append(tid)
        if event and str(event.get("type") or "") in self._release_events:
            tid = tranche_of(event.get("data") or {})
            if tid and tid not in out["released"]:
                out["released"].append(tid)
        return out

    @staticmethod
    def event_reference(body: dict[str, Any]) -> str | None:
        """The escrow a webhook is about: ``data.id`` (esc_…), else ``data.reference`` / ``data.payin``."""
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return None
        for key in ("id", "reference", "payin", "source"):
            value = data.get(key)
            if isinstance(value, str) and value.startswith("esc_"):
                return value
        return data.get("reference") or data.get("payin") or data.get("id")

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """``base64(HMAC-SHA256(secret, event_id + raw body + created_at))`` in
        header ``signature`` (the guide also calls it ``webhook-signature``)."""
        if not self._webhook_secret:
            logger.warning("TazapayEscrowConnector: TAZAPAY_WEBHOOK_SECRET not set — accepting all webhooks")
            return True
        lowered = {k.lower(): v for k, v in headers.items()}
        sig = lowered.get("signature") or lowered.get("webhook-signature") or ""
        if not sig:
            return False
        return hmac.compare_digest(sig.strip(), tazapay_signature(self._webhook_secret, body))


def tazapay_signature(secret: str, body: bytes) -> str:
    """Tazapay's documented webhook signature for a raw body."""
    try:
        parsed = json.loads(body)
    except ValueError:
        parsed = {}
    event_id = str(parsed.get("id", "")) if isinstance(parsed, dict) else ""
    created_at = str(parsed.get("created_at", "")) if isinstance(parsed, dict) else ""
    message = event_id.encode() + body + created_at.encode()
    return base64.b64encode(hmac.new(secret.encode(), message, hashlib.sha256).digest()).decode()


# ---------------------------------------------------------------------------
# Escrow.com — API 2017-09-01, milestone transaction
# ---------------------------------------------------------------------------

class EscrowComConnector(Connector):
    """Escrow.com milestone transaction. HTTP Basic auth (account e-mail + API key)."""

    name = "escrow_com"

    def __init__(self) -> None:
        self._api_key = os.getenv("ESCROW_COM_API_KEY", "")
        self._email = os.getenv("ESCROW_COM_EMAIL", "")
        self._webhook_secret = os.getenv("ESCROW_COM_WEBHOOK_SECRET", "")
        env = os.getenv("ESCROW_COM_ENV", "production").strip().lower()
        default = ("https://api.escrow-sandbox.com/2017-09-01" if env == "sandbox"
                   else "https://api.escrow.com/2017-09-01")
        self._base_url = (os.getenv("ESCROW_COM_BASE_URL", "").strip() or default).rstrip("/")
        self._inspection_days = max(1, min(30, int(os.getenv("ESCROW_COM_INSPECTION_DAYS", "1") or 1)))

    def _stubbed(self) -> bool:
        return stub_mode() and not in_simulator(self.name)

    def _client(self) -> httpx.AsyncClient:
        auth = (self._email, self._api_key)
        if in_simulator(self.name):
            return _simulator_client("/escrow-com/2017-09-01",
                                     (self._email or "portless@sandbox.invalid", self._api_key or "sandbox-key"))
        return httpx.AsyncClient(base_url=self._base_url, auth=auth, timeout=_TIMEOUT)

    def _stub_ref(self) -> str:
        return f"esc_stub_{uuid4().hex[:12]}"

    async def list_payment_methods(self, corridor: dict[str, Any] | None = None) -> list[str]:
        return ["wire_transfer", "credit_card", "paypal"]

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        fee = float(os.getenv("ESCROW_COM_FEE_PCT", ESCROW_PROVIDER_CATALOG["escrow_com"]["fee_pct"]))
        return {"provider": "escrow_com", "fee_pct": fee, "source": "configured"}

    async def book(self, request: dict[str, Any]) -> dict[str, Any]:
        """One milestone item per tranche, buyer pays, PortLess (``me``) is the seller."""
        if self._stubbed():
            ref = self._stub_ref()
            logger.warning("EscrowComConnector: INTEGRATION_MODE=sandbox — stub escrow %s", ref)
            return {
                "ext_ref": ref, "provider": "escrow_com", "status": "awaiting_payment",
                "amount": request.get("amount", 0), "currency": request.get("currency", "USD"),
                "checkout_url": f"https://www.escrow.com/pay/{ref}", "stub": True,
            }
        buyer = request.get("buyer") or {}
        buyer_email = (buyer.get("email") or "").strip()
        if not buyer_email:
            raise ConnectorError("Escrow.com create-transaction: the buyer's e-mail address is required")
        tranches = request.get("tranches") or [
            {"tranche_id": "T1", "trigger": "ACCEPTED", "amount": request.get("amount", 0)}]
        window_days = max(1, min(30, int(request.get("acceptance_window_days") or 3)))
        due = (datetime.now(UTC) + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        items = []
        for t in tranches:
            trigger = t.get("trigger") or ""
            days = window_days if trigger == "ACCEPTED" else self._inspection_days
            items.append({
                "title": f"{t.get('tranche_id')} — {_TRANCHE_TITLES.get(trigger, trigger)}"[:200],
                "description": (f"{request.get('description') or 'PortLess trade'} · {t.get('tranche_id')} "
                                f"({trigger})")[:500],
                "type": "milestone",
                "reference": str(t.get("tranche_id")),
                "inspection_period": days * 86400,
                "quantity": 1,
                "schedule": [{
                    "amount": round(float(t.get("amount") or 0), 2),
                    "payer_customer": buyer_email,
                    "beneficiary_customer": "me",
                    "due_date": due,
                }],
            })
        body = {
            "parties": [
                {"role": "buyer", "customer": buyer_email},
                {"role": "seller", "customer": "me"},
            ],
            "currency": str(request.get("currency", "usd")).lower(),
            "description": (request.get("description") or "PortLess trade")[:256],
            "reference": str(request.get("reference", ""))[:24],
            "items": items,
        }
        async with self._client() as client:
            resp = await client.post("/transaction", json=body)
        if resp.status_code not in range(200, 300):
            raise _error("Escrow.com", "create-transaction", resp)
        data = resp.json()
        tranche_items: dict[str, str] = {}
        for item, t in zip(data.get("items") or [], tranches):
            tid = item.get("reference") or t.get("tranche_id")
            tranche_items[str(tid)] = str(item.get("id", ""))
        buyer_party = next((p for p in data.get("parties") or [] if p.get("role") == "buyer"), {})
        return {
            "ext_ref": str(data.get("id", "")),
            "provider": "escrow_com",
            "status": "cancelled" if data.get("is_cancelled") else "awaiting_payment",
            "amount": request.get("amount", 0),
            "currency": request.get("currency", "USD"),
            "checkout_url": buyer_party.get("next_step") or None,
            "tranche_items": tranche_items,
        }

    async def release(self, request: dict[str, Any]) -> dict[str, Any]:
        """Deliver the milestone (``ship``) with its proof; the buyer accepts it on Escrow.com."""
        ext_ref = request.get("ext_ref", "")
        if self._stubbed():
            return {"ext_ref": ext_ref, "status": "RELEASED", "amount": request.get("amount", 0),
                    "tranche_id": request.get("tranche_id"), "stub": True}
        item_id = request.get("item_id")
        if not item_id:
            raise ConnectorError(f"Escrow.com release of {request.get('tranche_id')}: no milestone item id on the escrow")
        proofs = [str(p) for p in request.get("proof_refs") or [] if p]
        proofs += [str(d.get("url") or d.get("name")) for d in request.get("documents") or [] if d.get("url") or d.get("name")]
        body = {
            "action": "ship",
            "shipping_information": {
                "tracking_information": {
                    "carrier": "PortLess proof of milestone",
                    "tracking_id": "; ".join(proofs)[:500] or str(request.get("tranche_id")),
                    "notes": (request.get("note") or "")[:500],
                },
            },
        }
        async with self._client() as client:
            resp = await client.patch(f"/transaction/{ext_ref}/item/{item_id}", json=body)
        if resp.status_code not in range(200, 300):
            raise _error("Escrow.com", "ship-milestone", resp)
        return {"ext_ref": ext_ref, "status": "SUBMITTED", "tranche_id": request.get("tranche_id"),
                "item_id": item_id, "amount": request.get("amount")}

    async def refund(self, request: dict[str, Any]) -> dict[str, Any]:
        """Cancel the transaction; Escrow.com returns the undisbursed funds to the buyer.
        A transaction that is complete (or whose cancellation Escrow.com refuses)
        surfaces as ConnectorError — the operator then settles it on Escrow.com."""
        ext_ref = request.get("ext_ref", "")
        if self._stubbed():
            return {"ext_ref": ext_ref, "status": "refunded", "amount": request.get("amount", 0), "stub": True}
        return await self.cancel(ext_ref, request.get("reason", ""))

    async def cancel(self, ext_ref: str, reason: str = "") -> dict[str, Any]:
        if self._stubbed():
            return {"ext_ref": ext_ref, "status": "cancelled", "stub": True}
        body = {"action": "cancel", "cancel_information": {"cancellation_reason": (reason or "cancelled")[:500]}}
        async with self._client() as client:
            resp = await client.patch(f"/transaction/{ext_ref}", json=body)
        if resp.status_code not in range(200, 300):
            raise _error("Escrow.com", "cancel-transaction", resp)
        data = resp.json()
        return {"ext_ref": ext_ref, "status": "REFUNDED" if data.get("is_cancelled", True) else "unknown",
                "provider_payload": data}

    async def status(self, ext_ref: str) -> dict[str, Any]:
        if self._stubbed():
            return {"ext_ref": ext_ref, "status": "awaiting_payment", "stub": True}
        async with self._client() as client:
            resp = await client.get(f"/transaction/{ext_ref}")
        if resp.status_code not in range(200, 300):
            raise _error("Escrow.com", "fetch-transaction", resp)
        return resp.json()

    def interpret_status(self, body: dict[str, Any], tranches: list[Any],
                         event: dict[str, Any] | None = None) -> dict[str, Any]:
        """Funding from the schedules' ``status.secured``; a tranche is released
        once its item is ``accepted`` (Escrow.com then disburses) or its
        schedule is ``disbursed_to_beneficiary``; ``rejected`` is a buyer dispute."""
        out: dict[str, Any] = {"funded": False, "funded_amount": None, "released": [], "rejected": [],
                               "cancelled": False, "refunded": False, "provider_status": None}
        if not isinstance(body, dict) or body.get("stub"):
            return out
        by_item = {str(getattr(t, "provider_item_id", "") or ""): t.tranche_id for t in tranches}
        by_ref = {t.tranche_id: t.tranche_id for t in tranches}
        secured_total, all_secured, any_item = 0.0, True, False
        for item in body.get("items") or []:
            if not isinstance(item, dict) or item.get("type") not in (None, "milestone", "general_merchandise"):
                continue
            any_item = True
            tid = by_item.get(str(item.get("id", ""))) or by_ref.get(str(item.get("reference", "")))
            status = item.get("status") or {}
            schedules = item.get("schedule") or []
            secured = bool(schedules) and all(_flag(s.get("status"), "secured") for s in schedules)
            disbursed = bool(schedules) and all(_flag(s.get("status"), "disbursed_to_beneficiary") for s in schedules)
            if secured:
                secured_total += sum(float(s.get("amount") or 0) for s in schedules)
            else:
                all_secured = False
            if tid and (_flag(status, "accepted") or disbursed):
                out["released"].append(tid)
            elif tid and _flag(status, "rejected"):
                out["rejected"].append(tid)
        if any_item and secured_total > 0:
            out["funded"] = True
            out["funded_amount"] = round(secured_total, 2)
        if body.get("is_cancelled"):
            out["cancelled"] = True
        out["provider_status"] = ("cancelled" if body.get("is_cancelled")
                                  else "funded" if any_item and all_secured else "awaiting_payment")
        return out

    @staticmethod
    def event_reference(body: dict[str, Any]) -> str | None:
        if not isinstance(body, dict):
            return None
        for key in ("transaction_id", "transaction", "id"):
            value = body.get(key)
            if isinstance(value, dict):
                value = value.get("id")
            if value not in (None, ""):
                return str(value)
        return None

    async def webhook_verify(self, headers: dict[str, str], body: bytes) -> bool:
        """Escrow.com does not sign webhooks. With ESCROW_COM_WEBHOOK_SECRET set the
        call must carry it — as ``?token=`` on the URL registered with Escrow.com
        (the route passes it as ``x-portless-webhook-token``) or as an HMAC in
        ``x-escrow-signature``. Either way the transaction is re-fetched before use."""
        if not self._webhook_secret:
            logger.warning("EscrowComConnector: ESCROW_COM_WEBHOOK_SECRET not set — accepting all webhooks")
            return True
        lowered = {k.lower(): v for k, v in headers.items()}
        token = lowered.get("x-portless-webhook-token", "")
        if token and hmac.compare_digest(token, self._webhook_secret):
            return True
        sig = lowered.get("x-escrow-signature", "")
        expected = hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        return bool(sig) and hmac.compare_digest(sig, expected)
