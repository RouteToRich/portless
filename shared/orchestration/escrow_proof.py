"""The proof sheet an escrow provider receives for a tranche.

Tazapay verifies release documents by downloading them from a URL
(``POST /v3/document`` ``url``), and PortLess's own documents live in a private
Google Drive. So each tranche gets one signed, expiring, read-only link:

  ``{PORTLESS_PUBLIC_BASE_URL}/escrow-guard/proof/{escrow_id}/{tranche_id}?exp=…&sig=…``

``sig`` = HMAC-SHA256(``ESCROW_PROOF_LINK_SECRET``, ``escrow_id:tranche_id:exp``);
the link works for ``ESCROW_PROOF_LINK_DAYS`` (default 30). The page lists the
stage, the amount, every proof reference and the SHA-256 of each buyer-side
document (PI, sales contract, commercial invoice, packing list, certificate of
origin, bill of lading reference, inspection report). It never names the
supplier or its price — the buyer may see what the provider holds.

Without ``PORTLESS_PUBLIC_BASE_URL`` / ``ESCROW_PROOF_LINK_SECRET`` no link is
made and only proofs that are themselves URLs (QC report, tracking page) go to
the provider; readiness ``escrow_proof_links`` warns about it.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# Buyer-side documents only — the supplier-facing ones (PO, remittance advice)
# would name the supplier and its price.
_BUYER_SIDE_DOCS = {
    "SALES_CONTRACT", "COMMERCIAL_INVOICE", "PACKING_LIST", "CERTIFICATE_OF_ORIGIN", "COO_APPLICATION",
    "SHIPPING_INSTRUCTIONS", "INSURANCE_DECLARATION", "DELIVERY_ACCEPTANCE_CERTIFICATE", "INSPECTION_ORDER",
}


def _secret() -> str:
    from shared.integration_mode import is_placeholder
    value = os.getenv("ESCROW_PROOF_LINK_SECRET", "").strip()
    return "" if is_placeholder(value) else value


def _base() -> str:
    from shared.integration_mode import is_placeholder
    value = os.getenv("PORTLESS_PUBLIC_BASE_URL", "").strip().rstrip("/")
    return "" if is_placeholder(value) else value


def _sign(escrow_id: str, tranche_id: str, exp: int) -> str:
    return hmac.new(_secret().encode(), f"{escrow_id}:{tranche_id}:{exp}".encode(), hashlib.sha256).hexdigest()


def signed_url(escrow_id: str, tranche_id: str, *, now: float | None = None) -> str | None:
    if not _secret() or not _base():
        return None
    days = max(1, int(os.getenv("ESCROW_PROOF_LINK_DAYS", "30") or 30))
    exp = int((now or time.time()) + days * 86400)
    return f"{_base()}/escrow-guard/proof/{escrow_id}/{tranche_id}?exp={exp}&sig={_sign(escrow_id, tranche_id, exp)}"


def verify(escrow_id: str, tranche_id: str, exp: str | int | None, sig: str | None, *,
           now: float | None = None) -> bool:
    if not _secret() or not sig or exp in (None, ""):
        return False
    try:
        exp_i = int(exp)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if exp_i < (now or time.time()):
        return False
    return hmac.compare_digest(sig, _sign(escrow_id, tranche_id, exp_i))


async def _documents(deal_id: str) -> list[dict[str, Any]]:
    docs: list[dict[str, Any]] = []
    try:
        from services.dealmaker.repository import get_repository as dm_repo  # type: ignore[import]
        pi = await dm_repo().get_pi_by_deal(deal_id)
        if pi is not None:
            docs.append({"type": "PROFORMA_INVOICE", "ref": pi.pi_id, "sha256": getattr(pi, "content_hash", None)})
    except Exception as exc:  # noqa: BLE001
        logger.debug("EscrowProof: PI lookup failed for %s: %s", deal_id, exc)
    try:
        from services.docugen.repository import get_repository as dg_repo  # type: ignore[import]
        repo = dg_repo()
        records = await repo.list_by_deal(deal_id) if hasattr(repo, "list_by_deal") else []
        for r in records or []:
            kind = str(getattr(getattr(r, "doc_type", None), "value", getattr(r, "doc_type", "")) or "")
            if kind.upper() in _BUYER_SIDE_DOCS:
                docs.append({"type": kind.upper(), "ref": getattr(r, "doc_id", None) or getattr(r, "document_id", None),
                             "sha256": getattr(r, "content_hash", None)})
    except Exception as exc:  # noqa: BLE001
        logger.debug("EscrowProof: document lookup failed for %s: %s", deal_id, exc)
    return docs


async def render(escrow_id: str, tranche_id: str) -> str | None:
    """The proof sheet as HTML, or None when the escrow / tranche does not exist."""
    from services.escrow_guard.repository import get_repository  # type: ignore[import]
    from shared.connectors.escrow import display_name

    hold = await get_repository().get_hold(escrow_id)
    if hold is None:
        return None
    tranche = next((t for t in hold.tranches if t.tranche_id == tranche_id), None)
    if tranche is None:
        return None
    docs = await _documents(hold.deal_id)
    e = html.escape
    proof_rows = "".join(f"<li><code>{e(str(p))}</code></li>" for p in tranche.proof_refs if p) or "<li>—</li>"
    doc_rows = "".join(
        f"<tr><td>{e(d['type'])}</td><td><code>{e(str(d.get('ref') or ''))}</code></td>"
        f"<td><code>{e(str(d.get('sha256') or '—'))}</code></td></tr>" for d in docs
    ) or "<tr><td colspan=3>—</td></tr>"
    amount = tranche.amount if tranche.amount is not None else round(hold.amount * tranche.pct, 2)
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>PortLess release proof</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#111}"
        "td,th{border:1px solid #ccc;padding:4px 8px;text-align:left;font-size:13px}table{border-collapse:collapse}"
        "code{font-size:12px;word-break:break-all}</style></head><body>"
        f"<h1>Release proof — {e(tranche.tranche_id)}</h1>"
        f"<p>Escrow <code>{e(hold.escrow_id)}</code> at {e(display_name(hold.provider))} "
        f"(provider reference <code>{e(hold.ext_ref or '')}</code>) for deal <code>{e(hold.deal_id)}</code>.</p>"
        f"<p>Stage: <b>{e(tranche.trigger)}</b> · amount {amount:,.2f} {e(hold.currency)} "
        f"({tranche.pct * 100:.0f}% of {hold.amount:,.2f}).</p>"
        f"<h2>Proof references</h2><ul>{proof_rows}</ul>"
        f"<h2>Trade documents (SHA-256)</h2><table><tr><th>Document</th><th>Reference</th><th>SHA-256</th></tr>{doc_rows}</table>"
        "<p style='color:#666;font-size:12px'>Issued by PortLess for the escrow provider's verification. "
        "The document hashes match the files PortLess holds; the provider may request any file.</p>"
        "</body></html>"
    )
