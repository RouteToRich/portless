"""PortLess side of the actors that are specified but not built yet (todo "Apify actors", A.16, A.17,
A.29, A.30, A.31, A.33, A.34, A.38).

Each connector maps the row fields of its actor spec (todo, section "Apify actors") onto a PortLess
shape. Until the actor exists on the ``rich_minds`` account it must not be called: a slot runs only
with the token **and** ``APIFY_ACTOR_<KEY>_BUILT=true`` (set when ``/actor-deploy`` has published it).
Without both, ``quote()`` returns the empty stub result (``stub: True``) and costs nothing.

Normalisers are pure (``row`` dict → PortLess dict); the tests run them on rows written to the spec.
"""
from __future__ import annotations

import os
from typing import Any

from .apify import ApifyActorConnector, _first, _safe_float, _safe_int, iso2


def _built(key: str) -> bool:
    return os.getenv(f"APIFY_ACTOR_{key}_BUILT", "").strip().lower() in ("1", "true", "yes", "on")


def _hs(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _hs_list(values: Any) -> list[str]:
    return [h for h in (_hs(v) for v in (values or [])) if len(h) >= 4]


class _PendingActor(ApifyActorConnector):
    """A slot whose actor is specified but may not be built yet."""

    external = True

    @property
    def configured(self) -> bool:
        return bool(self._token) and _built(self.actor_key)

    def describe(self) -> dict[str, Any]:
        return {**super().describe(), "built": _built(self.actor_key)}

    async def _rows(self, actor_input: dict[str, Any], max_items: int | None = None) -> list[dict[str, Any]] | None:
        if not self.configured:
            return None
        return await self.run({**self._common_input(max_items), **actor_input}, max_items=max_items)


# ── A.29 trade-regulation-watch-ai ──────────────────────────────────────────

class ApifyRegulationWatchConnector(_PendingActor):
    name = "apify_regulation_watch"
    actor_key = "TRADE_REGULATION_WATCH"
    default_slug = "trade-regulation-watch-ai"
    empty_key = "items"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows({"sourceMode": "live", "since": request.get("since"),
                                 "sources": request.get("sources") or []}, max_items=int(request.get("max_items") or 200))
        if rows is None:
            return self._empty()
        items = [self.item(r) for r in rows]
        return {"items": items, "total": len(items), "actor": self.actor_ref}

    @staticmethod
    def item(r: dict[str, Any]) -> dict[str, Any]:
        return {"source": r.get("source"), "number": r.get("number"), "date": r.get("date"),
                "title": (r.get("title") or "")[:500], "type": r.get("type") or "other",
                "url": _first(r.get("pdfUrl"), r.get("sourceUrl")), "text": (r.get("pdfText") or "")[:4000],
                "hs_codes": _hs_list(r.get("hsCodes")), "products": [str(p) for p in r.get("products") or []],
                "effective_from": r.get("effectiveFrom"), "hash": r.get("textHash") or r.get("rowId")}


# ── A.30 india-trade-portal-ai ──────────────────────────────────────────────

class ApifyIndiaTradePortalConnector(_PendingActor):
    name = "apify_india_trade_portal"
    actor_key = "INDIA_TRADE_PORTAL"
    default_slug = "india-trade-portal-ai"
    empty_key = "requirements"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows({"sourceMode": "live", "hsCodes": request.get("hs_codes") or [],
                                 "countries": request.get("countries") or []})
        if rows is None:
            return self._empty()
        out = [self.requirement(r) for r in rows]
        return {"requirements": out, "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def requirement(r: dict[str, Any]) -> dict[str, Any]:
        prefs = [{"agreement": p.get("agreement"), "rate_pct": _safe_float(p.get("ratePct")),
                  "rules_of_origin": p.get("rulesOfOrigin")} for p in r.get("preferentialTariffs") or [] if isinstance(p, dict)]
        return {"hs_code": _hs(r.get("hsCode")), "country": iso2(r.get("country")),
                "mfn_pct": _safe_float(r.get("mfnTariffPct")), "preferential": prefs,
                "sps": list(r.get("spsMeasures") or []), "tbt": list(r.get("tbtMeasures") or []),
                "other_ntm": list(r.get("otherNtm") or []), "licensing": r.get("importLicensing"),
                "documents": list(r.get("borderDocuments") or []), "url": _first(r.get("pageUrl"), r.get("sourceUrl")),
                "read_on": _first(r.get("readOn"), r.get("fetchedAt")), "hashes": dict(r.get("sectionHashes") or {}),
                "changed": list(r.get("changed_sections") or r.get("changedSections") or [])}


# ── A.31 destination-rule-watch-ai ──────────────────────────────────────────

class ApifyDestinationRulesConnector(_PendingActor):
    name = "apify_destination_rules"
    actor_key = "DESTINATION_RULE_WATCH"
    default_slug = "destination-rule-watch-ai"
    empty_key = "rules"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows({"sourceMode": "live", "countries": request.get("countries") or [],
                                 "hsCodes": request.get("hs_codes") or [], "since": request.get("since")})
        if rows is None:
            return self._empty()
        out = [self.rule(r) for r in rows]
        return {"rules": out, "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def rule(r: dict[str, Any]) -> dict[str, Any]:
        return {"country": iso2(r.get("country")), "regulator": r.get("regulator"), "hs_codes": _hs_list(r.get("hsCodes")),
                "products": list(r.get("products") or []), "measure": (r.get("measure") or "")[:1000],
                "measure_type": r.get("measureType") or "other", "published_on": r.get("publishedOn"),
                "in_force_from": r.get("inForceFrom"), "url": _first(r.get("link"), r.get("sourceUrl"))}


# ── A.33 import-refusals-ai ─────────────────────────────────────────────────

class ApifyImportRefusalsConnector(_PendingActor):
    name = "apify_import_refusals"
    actor_key = "IMPORT_REFUSALS"
    default_slug = "import-refusals-ai"
    empty_key = "refusals"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows({"sourceMode": "live", "originCountry": "IN", "hsCodes": request.get("hs_codes") or [],
                                 "products": request.get("products") or [], "since": request.get("since")},
                                max_items=int(request.get("max_items") or 200))
        if rows is None:
            return self._empty()
        out = [self.refusal(r) for r in rows]
        return {"refusals": out, "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def refusal(r: dict[str, Any]) -> dict[str, Any]:
        return {"country": iso2(r.get("country")) or r.get("country"), "authority": r.get("authority"), "date": r.get("date"),
                "product": r.get("product"), "hs_code": _hs(r.get("hsCode")), "exporter": r.get("exporterName"),
                "exporter_city": r.get("exporterCity"), "hazard": (r.get("hazard") or "").lower() or None,
                "hazard_detail": r.get("hazardDetail"), "action": r.get("action"), "reference": r.get("reference"),
                "url": _first(r.get("link"), r.get("sourceUrl"))}


# ── A.34 india-exporter-registries-ai (lookup mode) ─────────────────────────

class ApifyRegistryLookupConnector(_PendingActor):
    name = "apify_registry_lookup"
    actor_key = "INDIA_EXPORTER_REGISTRIES"
    default_slug = "india-exporter-registries-ai"
    empty_key = "lookups"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        lookups = [{"kind": x["kind"], "number": x["number"]} for x in request.get("lookups") or []
                   if x.get("kind") and x.get("number")]
        if not lookups:
            return self._empty()
        rows = await self._rows({"sourceMode": "list", "mode": "lookup", "lookups": lookups}, max_items=len(lookups))
        if rows is None:
            return self._empty()
        out = [self.lookup(r) for r in rows]
        return {"lookups": out, "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def lookup(r: dict[str, Any]) -> dict[str, Any]:
        status = str(r.get("status") or "").lower()
        return {"kind": r.get("kind"), "number": str(r.get("number") or "").strip(),
                "status": status if status in ("active", "expired", "suspended", "not_found") else "unknown",
                "valid_until": r.get("validUntil"), "company": r.get("companyName"), "address": r.get("address"),
                "category": r.get("licenceCategory"), "checked_at": _first(r.get("checkedAt"), r.get("fetchedAt")),
                "url": r.get("sourceUrl")}


# ── A.38 multi-carrier-tracking-ai (courier AWBs) ───────────────────────────

class ApifyCourierTrackingConnector(_PendingActor):
    name = "apify_courier_tracking"
    actor_key = "COURIER_TRACKING"
    default_slug = "multi-carrier-tracking-ai"
    empty_key = "parcels"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        lines = [f"{p['awb']}, {p.get('courier') or ''}".rstrip(", ") for p in request.get("parcels") or [] if p.get("awb")]
        if not lines:
            return self._empty()
        rows = await self._rows({"sourceMode": "live", "mode": "courier", "trackingNumbers": lines}, max_items=len(lines))
        if rows is None:
            return self._empty()
        out = [self.parcel(r) for r in rows]
        return {"parcels": out, "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def parcel(r: dict[str, Any]) -> dict[str, Any]:
        status = str(r.get("status") or "").lower()
        return {"awb": str(r.get("trackingNumber") or "").strip(), "courier": r.get("courier"),
                "status": status or "unknown", "last_scan": r.get("lastScan"), "last_scan_at": r.get("lastScanAt"),
                "delivered_at": r.get("deliveredAt"), "signed_by": r.get("signedBy"),
                "delivered": status == "delivered" or bool(r.get("deliveredAt"))}


# ── A.16 buy-leads-ai / A.17 food-tenders-ai ────────────────────────────────

class ApifyBuyLeadsConnector(_PendingActor):
    name = "apify_buy_leads"
    actor_key = "BUY_LEADS"
    default_slug = "buy-leads-ai"
    empty_key = "leads"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows({"sourceMode": "live", "products": request.get("products") or [],
                                 "countries": request.get("countries") or [],
                                 "postedWithinDays": int(request.get("posted_within_days") or 14)})
        if rows is None:
            return self._empty()
        out = [self.lead(r) for r in rows]
        return {"leads": out, "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def lead(r: dict[str, Any]) -> dict[str, Any]:
        qty = _safe_float(r.get("quantityMt"))
        return {"source": f"buy_lead:{r.get('board') or 'board'}", "kind": "buy_request",
                "buyer_name": r.get("buyerName"), "country": iso2(r.get("country")), "product": r.get("product"),
                "hs_code": _hs(r.get("hsCode")) or None, "quantity_mt": qty, "quantity_raw": r.get("quantity"),
                "unit": r.get("unit"), "incoterm": r.get("incoterm"), "destination": r.get("destination"),
                "posted_on": r.get("postedOn"), "deadline": r.get("deadline"), "url": _first(r.get("leadUrl"), r.get("sourceUrl")),
                "contact_shown": bool(r.get("contactShown")), "flags": ["unverified_lead"]}


class ApifyFoodTendersConnector(_PendingActor):
    name = "apify_food_tenders"
    actor_key = "FOOD_TENDERS"
    default_slug = "food-tenders-ai"
    empty_key = "leads"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows({"sourceMode": "live", "products": request.get("products") or [],
                                 "countries": request.get("countries") or []})
        if rows is None:
            return self._empty()
        out = [self.tender(r) for r in rows]
        return {"leads": out, "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def tender(r: dict[str, Any]) -> dict[str, Any]:
        qty = _safe_float(r.get("quantity"))
        unit = str(r.get("unit") or "").lower()
        if qty is not None and unit in ("kg", "kgs", "kilogram", "kilograms"):
            qty = qty / 1000.0
        elif unit not in ("", "mt", "t", "ton", "tons", "tonne", "tonnes", "metric ton", "metric tons"):
            qty = None
        return {"source": "food_tender", "kind": "tender", "buyer_name": r.get("buyer"), "country": iso2(r.get("country")),
                "product": _first(r.get("product"), r.get("title")), "hs_code": None, "quantity_mt": qty,
                "quantity_raw": r.get("quantity"), "unit": r.get("unit"), "deadline": r.get("deadline"),
                "posted_on": r.get("publishedOn"), "awarded_to": r.get("awardedTo"),
                "award_value_usd": _safe_float(r.get("awardValueUsd")), "url": _first(r.get("tenderUrl"), r.get("sourceUrl")),
                "urgency_tier": 3, "flags": []}


PENDING_CONNECTORS: tuple[type[_PendingActor], ...] = (
    ApifyRegulationWatchConnector, ApifyIndiaTradePortalConnector, ApifyDestinationRulesConnector,
    ApifyImportRefusalsConnector, ApifyRegistryLookupConnector, ApifyCourierTrackingConnector,
    ApifyBuyLeadsConnector, ApifyFoodTendersConnector,
)

__all__ = [c.__name__ for c in PENDING_CONNECTORS] + ["PENDING_CONNECTORS", "_safe_int"]
