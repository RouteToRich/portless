"""Export certificates the shipment needs before it sails (DEF-17 APEDA, todo: phytosanitary
certificate, certificate of origin).

Required per deal (``required(deal)``):

* **phytosanitary** — plant products (HS chapters 06–14: produce, spices, grain, oilseeds);
* **apeda_rcac** — APEDA registration of the export contract (RCAC), required for **basmati**
  (HS 1006 30 20 / 1006 30 21 / 1006 30 29 — APEDA-notified) before shipment;
* **certificate_of_origin** — issued by the authorised body (the system only issues the
  application): asked by most importers and needed for any preferential duty;
* anything the catalogue lists for the product (``ProductLine.export_certificates``).

Staff record each issued certificate (``POST /deals/{id}/export-certificates``: kind, number,
issue date, issuer, evidence — audited). The shipment check (``compliance.check_shipment``,
before freight and the QC-stage tranche) raises ``shipment_cert_<kind>`` (review) for each one
still missing. ``EXPORT_CERTS_REQUIRED`` (default on; pinned off in the test suite and the
automation catalogue, like the other pre-shipment requirements).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

KINDS = {
    "phytosanitary": "Phytosanitary certificate (Plant Quarantine)",
    "apeda_rcac": "APEDA registration of the export contract (RCAC)",
    "certificate_of_origin": "Certificate of origin (authorised body)",
    "fumigation": "Fumigation certificate",
    "health": "Health / quality certificate",
    "preferential_coo": "Preferential certificate of origin under the trade agreement",
    "attestation": "Invoice / certificate of origin attested (chamber or embassy)",
    "consular_invoice": "Consular invoice",
}


def country_kinds(market: str | None, hs_code: str | None = None) -> list[str]:
    """What the destination asks of the exporter (country profile, todo 24): the trade agreement's
    certificate of origin form, attestation / legalisation, a consular invoice. Documents the
    importer files itself (e.g. FDA prior notice) are listed for the buyer, never required here."""
    if not market:
        return []
    try:
        from shared import country_profiles
        p = country_profiles.get(market)
    except Exception:  # noqa: BLE001
        p = None
    out: list[str] = []
    if p is not None and p.coo_form:
        out.append("preferential_coo")
    elif hs_code:
        try:  # the India Trade Portal says a preferential duty applies (destination_rules.py)
            from shared.orchestration import destination_rules
            if destination_rules.preferential(hs_code, market):
                out.append("preferential_coo")
        except Exception:  # noqa: BLE001
            pass
    for doc in (p.documents if p is not None else []):
        d = doc.lower()
        if "importer" in d:
            continue
        if ("attest" in d or "legalis" in d or "legaliz" in d) and "attestation" not in out:
            out.append("attestation")
        if "consular invoice" in d and "consular_invoice" not in out:
            out.append("consular_invoice")
    return out
_BASMATI = ("10063020", "10063021", "10063029")
_memory: list[dict[str, Any]] = []


def enabled() -> bool:
    return os.getenv("EXPORT_CERTS_REQUIRED", "true").strip().lower() in ("1", "true", "yes", "on")


def required(hs_code: str | None, market: str | None = None) -> list[str]:
    hs = "".join(ch for ch in (hs_code or "") if ch.isdigit())
    out = ["certificate_of_origin"]
    out += [k for k in country_kinds(market, hs_code) if k not in out]
    if hs[:2] and 6 <= int(hs[:2]) <= 14:
        out.append("phytosanitary")
    if hs.startswith(_BASMATI) or hs[:8] in _BASMATI:
        out.append("apeda_rcac")
    try:
        from shared import catalogue
        line = catalogue.line_for(hs_code or "")
        for kind in getattr(line, "export_certificates", None) or []:
            if kind in KINDS and kind not in out:
                out.append(kind)
    except Exception:  # noqa: BLE001
        pass
    return out


def _collection() -> Any:
    try:
        from shared.mongo import collection
        return collection("export_certificates")
    except Exception:  # noqa: BLE001
        return None


async def record(deal_id: str, kind: str, *, number: str, issued_on: str | None, issuer: str | None,
                 evidence: str | None, by: str) -> dict[str, Any]:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if not number or len(number.strip()) < 3:
        raise ValueError("the certificate number is required")
    row = {"deal_id": deal_id, "kind": kind, "number": number.strip(), "issued_on": issued_on, "issuer": issuer,
           "evidence": evidence, "recorded_by": by, "recorded_at": datetime.now(timezone.utc).isoformat()}
    col = _collection()
    if col is None:
        _memory[:] = [r for r in _memory if not (r["deal_id"] == deal_id and r["kind"] == kind)] + [row]
    else:
        await col.replace_one({"_id": f"{deal_id}:{kind}"}, {"_id": f"{deal_id}:{kind}", **row}, upsert=True)
    return row


async def recorded(deal_id: str) -> list[dict[str, Any]]:
    col = _collection()
    if col is None:
        return [r for r in _memory if r["deal_id"] == deal_id]
    try:
        out = []
        async for d in col.find({"deal_id": deal_id}):
            d.pop("_id", None)
            out.append(d)
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("ExportCerts: %s unreadable: %s", deal_id, exc)
        return []


async def status(deal: Any) -> dict[str, Any]:
    need = required(getattr(deal, "hs_code", None), getattr(deal, "market", None))
    have = {r["kind"]: r for r in await recorded(deal.deal_id)}
    return {"deal_id": deal.deal_id, "required": [{"kind": k, "label": KINDS[k], "certificate": have.get(k)} for k in need],
            "missing": [k for k in need if k not in have], "enforced": enabled()}


def reset_for_tests() -> None:
    _memory.clear()
