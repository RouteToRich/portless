"""Deal timeline + evidence pack — one place that shows, and can hand over,
everything that happened on a trade.

  build_timeline(deal_id)      every dated fact from every service, merged and
                               sorted: status changes, gates (created/decided),
                               RFQs/quotes, e-mails, PI/PO, documents, QC /
                               freight / customs orders and milestones, escrow
                               funding / tranches / payouts / acceptance,
                               exceptions, buyer outreach
  build_evidence_pack(deal_id) the timeline plus the underlying records (deal,
                               parties, PI/PO, document hashes, ledger +
                               double-entry journal, orders, approvals,
                               exceptions, e-mails) with a SHA-256 over the
                               whole pack — what a bank, insurer, auditor or
                               dispute resolver asks for
  render_evidence_html(pack)   self-contained HTML of the same

Every source is optional: a service whose repository is unavailable adds a
warning instead of failing the pack.
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _iso(dt: Any) -> str | None:
    if dt is None:
        return None
    if isinstance(dt, datetime):
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).isoformat()
    return str(dt)


def _dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, list):
        return [_dump(o) for o in obj]
    return obj


def _ev(at: Any, kind: str, title: str, *, detail: str | None = None, ref: str | None = None,
        actor: str | None = None, source: str = "") -> dict[str, Any] | None:
    iso = _iso(at)
    if iso is None:
        return None
    return {"at": iso, "kind": kind, "title": title, "detail": detail, "ref": ref, "actor": actor, "source": source}


async def _collect(deal_id: str) -> tuple[dict[str, Any], list[str]]:
    """Fetch every record set for the deal; missing services become warnings."""
    data: dict[str, Any] = {}
    warnings: list[str] = []

    async def _get(name: str, coro_factory):
        try:
            data[name] = await coro_factory()
        except Exception as exc:  # noqa: BLE001
            data[name] = None
            warnings.append(f"{name}: {exc}")

    async def _deal():
        from services.deals.service import get_deal  # type: ignore[import]
        return await get_deal(deal_id)

    async def _approvals():
        from services.hitl_approval.repository import get_repository  # type: ignore[import]
        return await get_repository().list_by_deal(deal_id)

    async def _pi():
        from services.dealmaker.repository import get_repository  # type: ignore[import]
        return await get_repository().get_pi_by_deal(deal_id)

    async def _po():
        from services.dealmaker.repository import get_repository  # type: ignore[import]
        return await get_repository().get_po_by_deal(deal_id)

    async def _docs():
        from services.docugen.repository import get_repository  # type: ignore[import]
        return await get_repository().list_by_deal(deal_id)

    async def _orders():
        from services.shield_link.repository import get_repository  # type: ignore[import]
        return await get_repository().list_by_deal(deal_id)

    async def _hold():
        from services.escrow_guard.repository import get_repository  # type: ignore[import]
        return await get_repository().get_hold_by_deal(deal_id)

    async def _ledger():
        from services.escrow_guard.service import get_deal_ledger  # type: ignore[import]
        return await get_deal_ledger(deal_id)

    async def _journal():
        from services.escrow_guard.journal import build_journal  # type: ignore[import]
        return await build_journal(deal_id)

    async def _exceptions():
        from services.exception_resolver.repository import get_repository  # type: ignore[import]
        return await get_repository().list_by_deal(deal_id)

    async def _emails():
        from agents.negotiation_agent.email_history import get_email_repository  # type: ignore[import]
        return await get_email_repository().list_by_deal(deal_id)

    async def _negotiation():
        from agents.negotiation_agent.repository import get_repository  # type: ignore[import]
        repo = get_repository()
        session = await repo.get_session_by_deal(deal_id)
        rounds = await repo.list_rounds(session.negotiation_id) if session else []
        return {"session": session, "rounds": rounds}

    async def _rfqs():
        from services.sourcing.repository import get_repository  # type: ignore[import]
        return await get_repository().list_rfqs(deal_id)

    async def _quotes():
        from services.sourcing.repository import get_repository  # type: ignore[import]
        return await get_repository().list_quotes(deal_id=deal_id)

    await _get("deal", _deal)
    if data.get("deal") is None:
        return data, warnings
    for name, fn in (("approvals", _approvals), ("pi", _pi), ("po", _po), ("documents", _docs), ("orders", _orders),
                     ("hold", _hold), ("exceptions", _exceptions), ("emails", _emails),
                     ("negotiation", _negotiation), ("rfqs", _rfqs), ("quotes", _quotes)):
        await _get(name, fn)
    if data.get("hold") is not None:
        await _get("ledger", _ledger)
        await _get("journal", _journal)
    else:
        data["ledger"] = None
        data["journal"] = None
    return data, warnings


def _timeline_from(data: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any] | None] = []
    deal = data.get("deal")
    if deal is None:
        return []
    events.append(_ev(deal.created_at, "deal", "Deal created", detail=f"HS {deal.hs_code} → {deal.market}",
                      actor=deal.operator_id, source="deals"))
    for h in getattr(deal, "status_history", None) or []:
        status = getattr(h.status, "value", h.status)
        events.append(_ev(h.transitioned_at, "status", f"→ {status}", detail=h.reason, source="deals"))
    for c in getattr(deal, "buyer_candidates", None) or []:
        events.append(_ev(c.contacted_at, "outreach", f"Buyer outreach: {c.legal_name}", detail=c.email, ref=c.candidate_id, source="outreach"))
        events.append(_ev(c.followed_up_at, "outreach", f"Outreach follow-up: {c.legal_name}", ref=c.candidate_id, source="outreach"))
        events.append(_ev(c.replied_at, "outreach", f"Buyer replied ({c.status}): {c.legal_name}", detail=c.reply_preview, ref=c.candidate_id, source="outreach"))
    for r in data.get("rfqs") or []:
        events.append(_ev(r.dispatched_at, "sourcing", f"RFQ sent to {len(r.supplier_ids)} supplier(s)", detail=r.status, ref=r.rfq_id, source="sourcing"))
    for q in data.get("quotes") or []:
        events.append(_ev(getattr(q, "received_at", None), "sourcing", f"Quote {q.unit_price_usd:.2f} {q.currency}/MT from {q.company_name or q.supplier_id}",
                          ref=q.quote_id, source="sourcing"))
    neg = data.get("negotiation") or {}
    for rd in neg.get("rounds") or []:
        price = f" @ {rd.inbound_price:.2f}" if rd.inbound_price is not None else ""
        events.append(_ev(rd.created_at, "negotiation", f"Negotiation round {rd.round_no} ({rd.party}{price})",
                          detail=(rd.outbound_message or "")[:160], ref=rd.round_id, source="negotiation"))
    for e in data.get("emails") or []:
        direction = getattr(e.direction, "value", e.direction)
        status = getattr(e.status, "value", e.status)
        events.append(_ev(e.sent_at or e.created_at, "email", f"E-mail {direction} ({status}): {e.subject[:80]}",
                          detail=f"{e.from_address} → {e.to_address}", ref=e.email_id, source="email"))
    for a in data.get("approvals") or []:
        events.append(_ev(a.created_at, "gate", f"Gate {a.action} ({a.autonomy_level})", detail=a.summary, ref=a.approval_id, source="hitl"))
        if a.resolved_at:
            events.append(_ev(a.resolved_at, "gate", f"Gate {a.action}: {a.status}", detail=a.resolution_note,
                              ref=a.approval_id, actor=a.resolved_by, source="hitl"))
        for s in getattr(a, "approvals", None) or []:
            events.append(_ev(s.approved_at, "gate", f"Gate {a.action}: approval signature", ref=a.approval_id, actor=s.approved_by, source="hitl"))
    pi, po = data.get("pi"), data.get("po")
    if pi:
        events.append(_ev(pi.issued_at, "document", f"Proforma Invoice issued ({pi.total_value:,.2f} {pi.currency})",
                          detail=f"valid until {_iso(pi.expires_at)}", ref=pi.pi_id, source="dealmaker"))
    if po:
        events.append(_ev(po.issued_at, "document", f"Purchase Order issued ({po.total_value:,.2f} {po.currency})",
                          detail=f"margin {po.net_margin_pct}% (floor {po.margin_floor_pct}%)", ref=po.po_id, source="dealmaker"))
        events.append(_ev(po.confirmed_at, "document", "Purchase Order confirmed by supplier", ref=po.po_id, source="dealmaker"))
    for d in data.get("documents") or []:
        dt = getattr(d.doc_type, "value", d.doc_type)
        events.append(_ev(d.rendered_at or d.created_at, "document", f"Document {dt} {d.status.lower()}",
                          detail=f"sha256 {d.content_hash[:16]}…" if d.content_hash else None, ref=d.doc_id, source="docugen"))
    for o in data.get("orders") or []:
        events.append(_ev(o.created_at, "operations", f"{o.order_type} order {o.status.lower()} with {o.provider}", ref=o.ext_ref or o.tpo_id, source="shield_link"))
        if o.qc_completed_at:
            events.append(_ev(o.qc_completed_at, "operations", f"Inspection result: {o.qc_result}", detail=o.qc_report_url, ref=o.ext_ref or o.tpo_id, source="shield_link"))
        if getattr(o, "last_milestone", None):
            events.append(_ev(o.last_milestone_at, "operations", f"Carrier milestone: {o.last_milestone}",
                              detail=getattr(o, "last_milestone_source", None), ref=o.ext_ref or o.tpo_id, source="shield_link"))
    hold = data.get("hold")
    if hold:
        events.append(_ev(hold.created_at, "money", f"Escrow opened with {hold.provider} ({hold.amount:,.2f} {hold.currency})", ref=hold.escrow_id, source="escrow"))
        events.append(_ev(hold.funded_at, "money", f"Escrow funded ({(hold.funded_amount or hold.amount):,.2f} {hold.currency})"
                          + (f", short by {hold.shortfall:,.2f}" if hold.shortfall else ""), ref=hold.escrow_id, source="escrow"))
        for t in hold.tranches or []:
            events.append(_ev(t.released_at, "money", f"Tranche {t.tranche_id} released ({(t.amount or 0):,.2f} {hold.currency}, {t.trigger})",
                              detail=", ".join(t.proof_refs) if t.proof_refs else None, ref=t.ext_ref or t.tranche_id, source="escrow"))
        events.append(_ev(hold.delivery_confirmed_at, "money", "Delivery confirmed", ref=hold.escrow_id, source="escrow"))
        events.append(_ev(hold.inspection_opened_at, "money", f"Buyer acceptance window opened ({hold.inspection_trigger})",
                          detail=f"until {_iso(hold.inspection_deadline)}", ref=hold.escrow_id, source="escrow"))
        events.append(_ev(hold.buyer_accepted_at, "money", "Buyer accepted the goods (final tranche released)", ref=hold.escrow_id, source="escrow"))
        events.append(_ev(hold.disputed_at, "money", "Buyer dispute raised", ref=hold.escrow_id, source="escrow"))
        events.append(_ev(hold.settled_at, "money", "Escrow settled", ref=hold.escrow_id, source="escrow"))
    ledger = data.get("ledger")
    for p in (getattr(ledger, "payouts", None) or []):
        events.append(_ev(p.created_at, "money", f"Supplier payout requested ({p.amount:,.2f} {p.currency}, {p.kind})", ref=p.ext_ref or p.payout_id, source="escrow"))
        events.append(_ev(p.completed_at, "money", f"Supplier payout {p.status.lower()} ({p.amount:,.2f} {p.currency})", ref=p.ext_ref or p.payout_id, source="escrow"))
    for x in data.get("exceptions") or []:
        cat = x.category.value if x.category else "?"
        events.append(_ev(x.raised_at, "exception", f"Exception {cat}: {x.raw_detail[:100] or x.source_event}", ref=x.exception_id, source="exception_resolver"))
        events.append(_ev(x.resolved_at, "exception", f"Exception {x.status.value.lower()}", detail=x.resolution_note, ref=x.exception_id, source="exception_resolver"))
    out = [e for e in events if e]
    out.sort(key=lambda e: e["at"])
    return out


async def build_timeline(deal_id: str) -> dict[str, Any]:
    data, warnings = await _collect(deal_id)
    if data.get("deal") is None:
        raise LookupError(f"deal {deal_id} not found")
    events = _timeline_from(data)
    return {"deal_id": deal_id, "status": getattr(data["deal"].status, "value", data["deal"].status),
            "events": events, "count": len(events), "warnings": warnings}


def _pack_hash(pack: dict[str, Any]) -> str:
    body = json.dumps({k: v for k, v in pack.items() if k != "content_hash"}, sort_keys=True, default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


async def build_evidence_pack(deal_id: str) -> dict[str, Any]:
    data, warnings = await _collect(deal_id)
    deal = data.get("deal")
    if deal is None:
        raise LookupError(f"deal {deal_id} not found")
    neg = data.get("negotiation") or {}
    session = neg.get("session")
    pack: dict[str, Any] = {
        "deal_id": deal_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "deal": _dump(deal),
        "parties": _dump(getattr(deal, "parties", None) or []),
        "buyer_candidates": _dump(getattr(deal, "buyer_candidates", None) or []),
        "sourcing": {"rfqs": _dump(data.get("rfqs") or []), "quotes": _dump(data.get("quotes") or [])},
        "negotiation": {
            "session": _dump(session),
            "rounds": _dump(neg.get("rounds") or []),
            "emails": [
                {k: v for k, v in _dump(e).items() if k not in ("body", "original_body")}
                | {"body_sha256": hashlib.sha256((e.body or "").encode("utf-8")).hexdigest()}
                for e in (data.get("emails") or [])
            ],
        },
        "proforma_invoice": _dump(data.get("pi")),
        "purchase_order": _dump(data.get("po")),
        "documents": [
            {k: v for k, v in _dump(d).items() if k != "context_snapshot"} for d in (data.get("documents") or [])
        ],
        "orders": _dump(data.get("orders") or []),
        "approvals": [
            {k: v for k, v in _dump(a).items() if k != "callback_payload"} for a in (data.get("approvals") or [])
        ],
        "exceptions": _dump(data.get("exceptions") or []),
        "escrow": _dump(data.get("hold")),
        "ledger": _dump(data.get("ledger")),
        "journal": _dump(data.get("journal")),
        "timeline": _timeline_from(data),
        "warnings": warnings,
    }
    pack["content_hash"] = _pack_hash(pack)
    return pack


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _h(v: Any) -> str:
    return html.escape("" if v is None else str(v))


def _table(rows: list[dict[str, Any]], cols: list[str]) -> str:
    if not rows:
        return "<p class='muted'>none</p>"
    head = "".join(f"<th>{_h(c)}</th>" for c in cols)
    body = "".join("<tr>" + "".join(f"<td>{_h(r.get(c))}</td>" for c in cols) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def _kv(d: dict[str, Any] | None, keys: list[str]) -> str:
    if not d:
        return "<p class='muted'>none</p>"
    return "<dl>" + "".join(f"<dt>{_h(k)}</dt><dd>{_h(d.get(k))}</dd>" for k in keys if k in d) + "</dl>"


def render_evidence_html(pack: dict[str, Any]) -> str:
    deal = pack.get("deal") or {}
    ledger = pack.get("ledger") or {}
    journal = pack.get("journal") or {}
    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>Evidence pack {_h(pack['deal_id'])}</title>",
        "<style>body{font:14px/1.45 system-ui,sans-serif;margin:32px;color:#1a1a1a;max-width:1100px}"
        "h1{font-size:22px}h2{font-size:16px;margin-top:28px;border-bottom:1px solid #ddd;padding-bottom:4px}"
        "table{border-collapse:collapse;width:100%;font-size:12.5px}th,td{border:1px solid #e3e3e3;padding:4px 6px;"
        "text-align:left;vertical-align:top}th{background:#f4f4f4}dl{display:grid;grid-template-columns:220px 1fr;gap:2px 12px}"
        "dt{color:#555}.muted{color:#888}.hash{font-family:ui-monospace,monospace;font-size:12px}</style></head><body>",
        f"<h1>Evidence pack — deal {_h(pack['deal_id'])}</h1>",
        f"<p>Status <b>{_h(deal.get('status'))}</b> · HS {_h(deal.get('hs_code'))} → {_h(deal.get('market'))} · "
        f"generated {_h(pack.get('generated_at'))}</p>",
        f"<p class='hash'>SHA-256 {_h(pack.get('content_hash'))}</p>",
        "<h2>Parties</h2>",
        _table(pack.get("parties") or [], ["role", "legal_name", "country", "email", "gstin", "iec", "vat_number",
                                           "sanctions_status", "contact_source"]),
        "<h2>Commercial documents</h2>",
        "<h3>Proforma Invoice</h3>",
        _kv(pack.get("proforma_invoice"), ["pi_id", "status", "agreed_price", "currency", "quantity_mt", "total_value",
                                           "incoterm", "port_of_destination", "payment_terms", "issued_at", "expires_at",
                                           "content_hash", "storage_uri"]),
        "<h3>Purchase Order</h3>",
        _kv(pack.get("purchase_order"), ["po_id", "status", "agreed_price", "currency", "quantity_mt", "total_value",
                                         "incoterm", "port_of_loading", "delivery_deadline", "payment_terms",
                                         "net_margin_pct", "issued_at", "content_hash", "storage_uri"]),
        "<h3>Shipping documents</h3>",
        _table(pack.get("documents") or [], ["doc_id", "doc_type", "status", "rendered_at", "content_hash", "storage_uri"]),
        "<h2>Inspection, freight, customs</h2>",
        _table(pack.get("orders") or [], ["tpo_id", "order_type", "provider", "status", "ext_ref", "qc_result",
                                          "container_number", "vessel", "eta", "last_milestone", "delivered_at"]),
        "<h2>Money</h2>",
        _kv(ledger, ["escrow_id", "provider", "status", "principal", "funded_amount", "shortfall", "released_total",
                     "refunded_total", "outstanding", "fees_total", "net_to_portless", "payouts_total",
                     "supplier_owed", "supplier_balance_due", "portless_costs", "portless_costs_total", "margin_before_costs", "realized_margin", "reconciled", "issues"]),
        "<h3>Tranches</h3>",
        _table(ledger.get("tranches") or [], ["tranche_id", "trigger", "pct", "amount", "status", "released_at", "proof_refs", "ext_ref"]),
        "<h3>Payouts</h3>",
        _table(ledger.get("payouts") or [], ["payout_id", "kind", "amount", "currency", "status", "ext_ref", "completed_at"]),
        "<h3>Double-entry journal</h3>",
        _table(journal.get("entries") or [], ["seq", "at", "event", "debit", "credit", "amount", "currency", "ref", "memo"]),
        f"<p>Balanced: <b>{_h(journal.get('balanced'))}</b> · debits {_h(journal.get('debits_total'))} · "
        f"credits {_h(journal.get('credits_total'))}</p>",
        "<h2>Approvals (human decisions)</h2>",
        _table(pack.get("approvals") or [], ["approval_id", "action", "autonomy_level", "status", "financial_impact_usd",
                                             "resolved_by", "resolved_at", "resolution_note", "required_approvals"]),
        "<h2>Exceptions</h2>",
        _table(pack.get("exceptions") or [], ["exception_id", "category", "severity", "status", "raised_at", "resolved_at", "resolution_note"]),
        "<h2>Correspondence</h2>",
        _table((pack.get("negotiation") or {}).get("emails") or [], ["email_id", "direction", "party", "from_address",
                                                                        "to_address", "subject", "status", "sent_at", "body_sha256"]),
        "<h2>Timeline</h2>",
        _table(pack.get("timeline") or [], ["at", "kind", "title", "detail", "ref", "actor", "source"]),
    ]
    if pack.get("warnings"):
        parts.append("<h2>Warnings</h2><ul>" + "".join(f"<li>{_h(w)}</li>" for w in pack["warnings"]) + "</ul>")
    parts.append("</body></html>")
    return "".join(parts)
