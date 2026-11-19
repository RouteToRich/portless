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

# ── Directory and statistics actors (A.1, A.2, A.5, A.9, A.10, A.11, A.12, A.13, A.14, A.15, A.18) ──

class _CompanyListActor(_PendingActor):
    """An actor whose rows are companies for the directory; ``company()`` maps one row."""

    empty_key = "companies"
    role = "buyer"
    source_prefix = "list"

    def actor_input(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"sourceMode": "live", "products": request.get("products") or [], "hsCodes": request.get("hs_codes") or [],
                "countries": request.get("countries") or []}

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows(self.actor_input(request), max_items=int(request.get("max_items") or 100))
        if rows is None:
            return self._empty()
        out = [c for c in (self.company(r) for r in rows) if c.get("name")]
        return {"companies": out, "total": len(out), "role": self.role, "actor": self.actor_ref}

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        return {"name": (r.get("companyName") or r.get("name") or "").strip(), "role": self.role,
                "country": iso2(r.get("country")) or r.get("country"), "city": r.get("city"), "website": r.get("website"),
                "email": r.get("email"), "phone": r.get("phone"), "source": self.source_prefix,
                "evidence_url": _first(r.get("sourceUrl"), r.get("exhibitorUrl"), r.get("memberUrl"), r.get("directoryUrl"),
                                       r.get("listingUrl"), r.get("articleUrl"), r.get("evidenceUrl")),
                "products": list(r.get("products") or r.get("categories") or [])[:10], "flags": []}


class ApifyTradeFairConnector(_CompanyListActor):
    name = "apify_trade_fairs"
    actor_key = "TRADE_FAIR_EXHIBITORS"
    default_slug = "trade-fair-exhibitors-ai"

    def actor_input(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"sourceMode": "live", "fairs": request.get("fairs") or [], "categories": request.get("products") or [],
                "countries": request.get("countries") or [], "role": request.get("role") or "buyer"}

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        c.update(role=r.get("role") or "buyer", source=f"trade_fair:{r.get('fair') or 'fair'}",
                 fair=r.get("fair"), edition=r.get("edition"), stand=r.get("stand"))
        return c


class ApifyExporterRegistriesConnector(_CompanyListActor):
    name = "apify_exporter_registries"
    actor_key = "INDIA_EXPORTER_REGISTRIES_LIST"
    default_slug = "india-exporter-registries-ai"
    role = "supplier"

    def actor_input(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"sourceMode": "live", "mode": "list", "registries": request.get("registries") or [],
                "products": request.get("products") or [], "states": request.get("states") or []}

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        c.update(country="IN", source=f"registry:{r.get('registry') or 'board'}", registry=r.get("registry"),
                 registration_number=r.get("registrationNumber"), registration_type=r.get("registrationType"),
                 valid_until=r.get("validUntil"), state=r.get("state"), address=r.get("address"))
        return c


class ApifyIndiaB2BConnector(_CompanyListActor):
    name = "apify_india_b2b"
    actor_key = "INDIA_B2B_SUPPLIERS"
    default_slug = "india-b2b-suppliers-ai"
    role = "supplier"

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        price = _safe_float(r.get("priceUsdPerMt"))
        c.update(country="IN", source=f"b2b:{r.get('site') or 'listing'}", state=r.get("state"),
                 price_usd_per_mt=price, moq=_safe_float(r.get("moq")), moq_unit=r.get("moqUnit"),
                 gstin=r.get("gstin"), gst_verified=bool(r.get("gstVerified")),
                 years_in_business=_safe_int(r.get("yearsInBusiness")),
                 flags=["masked_phone"] if r.get("maskedPhone") else [])
        return c


class ApifyApmcTradersConnector(_CompanyListActor):
    name = "apify_apmc_traders"
    actor_key = "APMC_TRADERS"
    default_slug = "apmc-traders-ai"
    role = "supplier"

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        c.update(name=(r.get("traderName") or c["name"] or "").strip(), country="IN", source="apmc",
                 licence_number=r.get("licenceNumber"), mandi=r.get("mandi"), state=r.get("state"))
        return c


class ApifyAssociationMembersConnector(_CompanyListActor):
    name = "apify_association_members"
    actor_key = "TRADE_ASSOCIATION_MEMBERS"
    default_slug = "trade-association-members-ai"

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        c.update(source=f"association:{r.get('association') or 'association'}", flags=["association_member"])
        return c


class ApifyImporterListsConnector(_CompanyListActor):
    name = "apify_importer_lists"
    actor_key = "IMPORTER_LIST_EXTRACTOR"
    default_slug = "importer-list-extractor-ai"

    def actor_input(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"sourceMode": "live", "queries": request.get("queries") or [], "urls": request.get("urls") or []}

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        c.update(source="article", mentions=_safe_int(r.get("mentions")) or 1)
        return c


class ApifyBusinessDirectoryConnector(_CompanyListActor):
    name = "apify_business_directories"
    actor_key = "B2B_DIRECTORY_COMPANIES"
    default_slug = "b2b-directory-companies-ai"

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        c.update(source=f"directory:{r.get('directory') or 'directory'}", activity=r.get("activity"),
                 registration_number=r.get("registrationNumber"))
        return c


class ApifyLocalBusinessConnector(_CompanyListActor):
    name = "apify_local_business"
    actor_key = "LOCAL_BUSINESS_LEADS"
    default_slug = "local-business-leads-ai"

    def actor_input(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"sourceMode": "live", "queries": request.get("queries") or [], "maxPerQuery": 10}

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        c.update(source="places", address=r.get("address"), rating=_safe_float(r.get("rating")))
        return c


class ApifyCompetitorClientsConnector(_CompanyListActor):
    name = "apify_competitor_clients"
    actor_key = "COMPETITOR_CLIENTS"
    default_slug = "competitor-clients-ai"

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        c.update(name=(r.get("clientName") or c["name"] or "").strip(), country=iso2(r.get("clientCountry")) or c["country"],
                 source="competitor_client", buys_from=r.get("exporterName"), flags=["buys_from_competitor"])
        return c


class ApifyUsImportRecordsConnector(_CompanyListActor):
    name = "apify_us_import_records"
    actor_key = "US_IMPORT_RECORDS"
    default_slug = "us-import-records-ai"

    def actor_input(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"sourceMode": "live", "products": request.get("products") or [], "hsCodes": request.get("hs_codes") or [],
                "originCountries": ["IN"], "months": 12, "excludeForwarders": True}

    def company(self, r: dict[str, Any]) -> dict[str, Any]:
        c = super().company(r)
        c.update(name=(r.get("consigneeName") or c["name"] or "").strip(), country="US", source="us_customs",
                 address=r.get("consigneeAddress"), shipments_12m=_safe_int(r.get("shipments12m")),
                 annual_volume_mt=_safe_float(r.get("totalMt12m")), typical_shipment_mt=_safe_float(r.get("typicalShipmentMt")),
                 frequency_days=_safe_int(r.get("frequencyDays")), last_import_date=r.get("lastShipmentDate"),
                 flags=["forwarder"] if r.get("isForwarder") else [])
        return c


class ApifyIndiaExportStatsConnector(_PendingActor):
    name = "apify_india_export_stats"
    actor_key = "INDIA_EXPORT_STATS"
    default_slug = "india-export-stats-ai"
    empty_key = "rows"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows({"sourceMode": "live", "hsCodes": request.get("hs_codes") or [],
                                 "countries": request.get("countries") or [], "frequency": "monthly"},
                                max_items=int(request.get("max_items") or 500))
        if rows is None:
            return self._empty()
        out = [self.row(r) for r in rows]
        return {"rows": out, "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def row(r: dict[str, Any]) -> dict[str, Any]:
        qty = _safe_float(r.get("quantityMt"))
        value = _safe_float(r.get("valueUsd"))
        uv = _safe_float(r.get("unitValueUsdPerMt")) or (round(value / qty, 2) if value and qty else None)
        return {"hs_code": _hs(r.get("hsCode")), "country": iso2(r.get("country")), "period": str(r.get("period") or ""),
                "value_usd": value, "quantity_mt": qty, "unit_value_usd_per_mt": uv, "provisional": bool(r.get("provisional")),
                "flags": list(r.get("flags") or [])}


DIRECTORY_CONNECTORS: tuple[type[_CompanyListActor], ...] = (
    ApifyTradeFairConnector, ApifyExporterRegistriesConnector, ApifyIndiaB2BConnector, ApifyApmcTradersConnector,
    ApifyAssociationMembersConnector, ApifyImporterListsConnector, ApifyBusinessDirectoryConnector,
    ApifyLocalBusinessConnector, ApifyCompetitorClientsConnector, ApifyUsImportRecordsConnector,
)
PENDING_CONNECTORS = PENDING_CONNECTORS + DIRECTORY_CONNECTORS + (ApifyIndiaExportStatsConnector,)


class ApifyCountryRiskConnector(_PendingActor):
    """A.32 ``country-risk-feed-ai``: ECGC rating, escrow providers' countries, attestation rules."""

    name = "apify_country_risk"
    actor_key = "COUNTRY_RISK_FEED"
    default_slug = "country-risk-feed-ai"
    empty_key = "countries"

    async def quote(self, request: dict[str, Any]) -> dict[str, Any]:
        rows = await self._rows({"sourceMode": "live", "countries": request.get("countries") or []}, max_items=300)
        if rows is None:
            return self._empty()
        out = [self.country(r) for r in rows]
        return {"countries": [c for c in out if c["country"]], "total": len(out), "actor": self.actor_ref}

    @staticmethod
    def country(r: dict[str, Any]) -> dict[str, Any]:
        att = r.get("attestation") if isinstance(r.get("attestation"), dict) else {}
        rating = str(r.get("ecgcRating") or "").strip().upper() or None
        return {"country": iso2(r.get("country") or r.get("iso2")), "ecgc_rating": rating,
                "ecgc_rating_date": r.get("ecgcRatingDate"), "escrow_providers": [str(x) for x in r.get("escrowProviders") or []],
                "attestation_documents": [str(x) for x in att.get("documents") or []] if att.get("required") else [],
                "fetched_at": r.get("fetchedAt")}


PENDING_CONNECTORS = PENDING_CONNECTORS + (ApifyCountryRiskConnector,)


__all__ = [c.__name__ for c in PENDING_CONNECTORS] + ["PENDING_CONNECTORS", "_safe_int"]
