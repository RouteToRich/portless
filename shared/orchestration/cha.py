"""Customs house agent (CHA / licensed customs broker) hand-off per shipment (todo 5.2).

ICEGATE filing is a stub, so a licensed customs broker files the Shipping Bill. This module runs
everything before and after that filing:

1. **CHA list per port** (Mongo ``cha_agents``, ``GET / PUT /deals/cha/agents``): firm, customs
   broker licence number under CBLR 2018 and its validity, ports (customs port codes or names),
   contact, charges per shipment / per container. ``choose`` picks a CHA with a valid licence for the
   deal's port of loading; a lapsed licence is never used.
2. **KYC of PortLess** (CBLR 2018 reg. 10): the CHA must hold PortLess's IEC, GSTIN, PAN, AD code,
   LUT and a signed authorisation letter. ``record_kyc`` notes when it was sent; ``kyc_status`` says
   what is missing and whether a registration changed after it was sent (send it again).
3. **The pack** (``send_pack``, at QC_PASSED or by staff): one e-mail to the CHA with a
   ``[CHA:<deal>]`` subject token — the documents issued so far, the inspector's loading record
   (containers, seals, loaded MT) and **which export incentives to declare** on the Shipping Bill
   (RoDTEP / RoSCTL / drawback from ``services/trade_min/incentives.py``; a claim not declared is lost).
4. **Checklist approval**: the CHA's reply on that thread with the Shipping Bill checklist opens
   ``APPROVE_SB_CHECKLIST`` (L0); only after APPROVE does PortLess tell the CHA to file.
5. **Shipping Bill record**: number, date, port code, LEO date and the incentive declarations it
   carried — read from the CHA's reply (``parse_shipping_bill``) or entered by staff — feed the
   realisation tracking and ``incentive_credits``; an incentive counted on the cost sheet but not
   declared raises ``INCENTIVE:not_declared`` before LEO.
6. **The CHA's invoice** (agency, CFS, THC, examination) against the cost-sheet ``customs_agent``
   line: above it by more than ``CHA_OVERRUN_PCT`` (20) → ``MARGIN_BREACH`` exception.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

ACTION = "APPROVE_SB_CHECKLIST"
TOKEN = re.compile(r"\[CHA:(DEAL-[A-Z0-9]{6,20})\]", re.IGNORECASE)
KYC_FIELDS = ("iec", "gstin", "pan", "ad_code", "lut_arn")
_memory: dict[str, dict[str, Any]] = {}
_kyc: dict[str, dict[str, Any]] = {}


def _collection(name: str = "cha_agents") -> Any:
    try:
        from shared.mongo import collection
        return collection(name)
    except Exception:  # noqa: BLE001
        return None


def subject_token(deal_id: str) -> str:
    return f"[CHA:{deal_id}]"


def parse_token(subject: str | None) -> str | None:
    m = TOKEN.search(subject or "")
    return m.group(1).upper() if m else None


# ── agents ──────────────────────────────────────────────────────────────────

async def put_agent(fields: dict[str, Any], *, by: str) -> dict[str, Any]:
    firm = (fields.get("firm") or "").strip()
    licence = (fields.get("licence_no") or "").strip()
    email = (fields.get("email") or "").strip().lower()
    if not firm or not licence:
        raise ValueError("firm and licence_no (customs broker licence) are required")
    if "@" not in email:
        raise ValueError("a real e-mail address for the CHA is required")
    valid = fields.get("licence_valid_until")
    if valid:
        date.fromisoformat(str(valid))
    ports = [str(p).strip().upper() for p in (fields.get("ports") or []) if str(p).strip()]
    if not ports:
        raise ValueError("list the ports the CHA files at")
    agent_id = fields.get("agent_id") or f"CHA-{uuid.uuid4().hex[:8].upper()}"
    row = {"agent_id": agent_id, "firm": firm, "licence_no": licence, "licence_valid_until": valid,
           "ports": ports, "email": email, "phone": fields.get("phone"),
           "charges_per_shipment_usd": float(fields.get("charges_per_shipment_usd") or 0) or None,
           "charges_per_container_usd": float(fields.get("charges_per_container_usd") or 0) or None,
           "updated_by": by, "updated_at": datetime.now(timezone.utc).isoformat()}
    _memory[agent_id] = row
    col = _collection()
    if col is not None:
        try:
            await col.replace_one({"agent_id": agent_id}, dict(row), upsert=True)
        except Exception as exc:  # noqa: BLE001
            logger.error("CHA: agent %s not stored (kept in memory): %s", agent_id, exc)
    return row


async def agents() -> list[dict[str, Any]]:
    col = _collection()
    if col is not None:
        try:
            rows = [d async for d in col.find({}, {"_id": 0})]
            for r in rows:
                _memory[r["agent_id"]] = r
        except Exception as exc:  # noqa: BLE001
            logger.warning("CHA: agent list unreadable: %s", exc)
    return sorted(_memory.values(), key=lambda r: r["firm"])


def licence_ok(agent: dict[str, Any], today: date | None = None) -> bool:
    valid = agent.get("licence_valid_until")
    if not valid:
        return False
    return date.fromisoformat(str(valid)) >= (today or datetime.now(timezone.utc).date())


def _port_matches(agent: dict[str, Any], port: str | None) -> bool:
    p = (port or "").strip().upper()
    return bool(p) and any(p == x or p in x or x in p for x in agent.get("ports") or [])


async def choose(port: str | None, today: date | None = None) -> dict[str, Any] | None:
    """A CHA with a valid licence at the port (cheapest per shipment first)."""
    rows = [a for a in await agents() if _port_matches(a, port) and licence_ok(a, today)]
    return min(rows, key=lambda a: a.get("charges_per_shipment_usd") or 1e9) if rows else None


# ── KYC ─────────────────────────────────────────────────────────────────────

async def record_kyc(agent_id: str, *, sent_on: str, authorisation_ref: str, by: str) -> dict[str, Any]:
    date.fromisoformat(sent_on)
    if not (authorisation_ref or "").strip():
        raise ValueError("the signed authorisation letter reference is required")
    row = {"agent_id": agent_id, "sent_on": sent_on, "authorisation_ref": authorisation_ref.strip(),
           "recorded_by": by, "recorded_at": datetime.now(timezone.utc).isoformat()}
    _kyc[agent_id] = row
    col = _collection("cha_kyc")
    if col is not None:
        try:
            await col.replace_one({"agent_id": agent_id}, dict(row), upsert=True)
        except Exception:  # noqa: BLE001
            pass
    return row


async def kyc_status(agent_id: str) -> dict[str, Any]:
    """What the CHA holds of PortLess's KYC: missing facts, and whether a registration changed since."""
    from shared.documents import issuer
    missing = [k for k in KYC_FIELDS if k in issuer.missing(KYC_FIELDS)]
    row = _kyc.get(agent_id)
    col = _collection("cha_kyc")
    if row is None and col is not None:
        try:
            row = await col.find_one({"agent_id": agent_id}, {"_id": 0})
        except Exception:  # noqa: BLE001
            row = None
    resend = False
    if row:
        try:
            from shared import registrations
            held = await registrations.held()
            resend = any(str(r.get("recorded_at") or "")[:10] > row["sent_on"]
                         for k, r in held.items() if k in ("iec", "gstin", "lut", "ad_code"))
        except Exception:  # noqa: BLE001
            resend = False
    return {"agent_id": agent_id, "sent": bool(row), "sent_on": (row or {}).get("sent_on"),
            "missing_facts": missing, "resend_needed": resend,
            "ok": bool(row) and not missing and not resend}


# ── pack ────────────────────────────────────────────────────────────────────

async def incentives_to_declare(hs_code: str | None) -> list[str]:
    """The export incentive schemes the Shipping Bill must declare for this product."""
    try:
        from services.trade_min import incentives  # type: ignore[import]
        entry = await incentives.entry_for(hs_code or "")
    except Exception:  # noqa: BLE001
        entry = None
    if entry is None or not getattr(entry, "eligible_confirmed", False):
        return []
    return incentives.schemes(entry)


def pack_body(deal: Any, agent: dict[str, Any], documents: list[str], schemes: list[str], loading: dict[str, Any]) -> str:
    from shared import company_profile
    from shared.product_label import product_label
    lines = [
        f"Dear {agent['firm']},",
        "",
        f"Please prepare the Shipping Bill for our deal {deal.deal_id}: "
        f"{product_label(getattr(deal, 'hs_code', '') or '') or 'the goods'} (HS {getattr(deal, 'hs_code', '')}), "
        f"{getattr(deal, 'quantity_mt', '') or ''} MT, destination {getattr(deal, 'market', '') or ''}.",
        "",
        "Documents attached / linked:",
        *[f"  - {d}" for d in documents or ["commercial invoice, packing list, SDF, Shipping Bill checklist"]],
    ]
    if loading:
        lines += ["", "Inspector's loading record: containers " + ", ".join(loading.get("containers") or [])
                  + "; seals " + ", ".join(loading.get("seals") or [])
                  + (f"; loaded {loading.get('loaded_mt')} MT" if loading.get("loaded_mt") else "")]
    if schemes:
        lines += ["", "Please DECLARE these export incentives on the Shipping Bill (a claim not declared is lost): "
                  + ", ".join(s.upper() for s in schemes) + "."]
    lines += ["", "Send us the Shipping Bill checklist on this thread for approval; please do not file before "
              "we approve it. After filing, reply with the Shipping Bill number, date, port code and LEO date.",
              "", company_profile.signature()]
    return "\n".join(lines)


async def _deal(deal_id: str) -> Any:
    from services.deals.repository import get_repository  # type: ignore[import]
    deal = await get_repository().get(deal_id)
    if deal is None:
        raise LookupError(f"deal {deal_id} not found")
    return deal


async def _save(deal_id: str, cha: dict[str, Any]) -> None:
    from services.deals.repository import get_repository  # type: ignore[import]
    await get_repository().set_fields(deal_id, {"cha": cha})


async def _loading(deal_id: str) -> dict[str, Any]:
    try:
        from shared.documents.facts import deal_facts
        f = await deal_facts(deal_id)
        return {"containers": f.get("container_numbers") or [], "seals": f.get("seal_numbers") or [],
                "loaded_mt": f.get("loaded_quantity_mt")}
    except Exception:  # noqa: BLE001
        return {}


async def _documents(deal_id: str) -> list[str]:
    try:
        from services.docugen.service import list_deal_documents  # type: ignore[import]
        docs = await list_deal_documents(deal_id)
        return [f"{getattr(d.doc_type, 'value', d.doc_type)} ({d.doc_id})" for d in getattr(docs, "documents", [])]
    except Exception:  # noqa: BLE001
        return []


async def send_pack(deal_id: str, *, by: str, port: str | None = None) -> dict[str, Any]:
    """E-mail the CHA its pack (idempotent: a pack already sent is returned unchanged)."""
    deal = await _deal(deal_id)
    cha = dict(getattr(deal, "cha", None) or {})
    if cha.get("pack_sent_at"):
        return cha
    port = port or cha.get("port") or _port_of_loading(deal)
    agent = await choose(port)
    if agent is None:
        raise LookupError(f"no CHA with a valid licence at {port or 'the port of loading'} — add one (PUT /deals/cha/agents)")
    kyc = await kyc_status(agent["agent_id"])
    schemes = await incentives_to_declare(getattr(deal, "hs_code", None))
    body = pack_body(deal, agent, await _documents(deal_id), schemes, await _loading(deal_id))
    subject = f"Shipping Bill — {deal_id} {subject_token(deal_id)}"
    delivered = False
    try:
        from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
        from services.email_gateway.sender import send_email  # type: ignore[import]
        result = await send_email(SendEmailRequest(to_address=agent["email"], subject=subject, body=body,
                                                   email_id=f"CHA{deal_id.replace('-', '')[-10:]}"))
        delivered = str(getattr(getattr(result, "outcome", None), "value", "")) not in ("logged", "failed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("CHA: pack for %s not sent: %s", deal_id, exc)
    cha.update(agent_id=agent["agent_id"], agent_firm=agent["firm"], agent_email=agent["email"], port=port,
               pack_sent_at=datetime.now(timezone.utc).isoformat(), pack_sent_by=by, pack_delivered=delivered,
               incentives_to_declare=schemes, kyc_ok=kyc["ok"], status="pack_sent")
    await _save(deal_id, cha)
    return cha


def _port_of_loading(deal: Any) -> str | None:
    po = getattr(deal, "port_of_loading", None)
    if po:
        return po
    sheet = getattr(deal, "cost_sheet", None) or {}
    route = (sheet.get("supplier_route") if isinstance(sheet, dict) else None) or {}
    return route.get("port") if isinstance(route, dict) else None


# ── replies ─────────────────────────────────────────────────────────────────

_SB_NO = re.compile(r"\b(?:shipping\s*bill|s\.?\s?b\.?)\s*(?:no\.?|number|#)?\s*[:\-]?\s*(\d{6,8})\b", re.IGNORECASE)
_DATE = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[./-]\d{1,2}[./-]\d{4})"
_SB_DATE = re.compile(r"\b(?:shipping\s*bill|s\.?\s?b\.?)\s*date\s*[:\-]?\s*" + _DATE, re.IGNORECASE)
_LEO = re.compile(r"\bLEO(?:\s*date)?\s*[:\-]?\s*" + _DATE, re.IGNORECASE)
_PORT = re.compile(r"\bport\s*code\s*[:\-]?\s*([A-Z]{5}\d?)\b", re.IGNORECASE)
_CHECKLIST = re.compile(r"\bcheck\s?list\b", re.IGNORECASE)
_SCHEMES = {"rodtep": re.compile(r"\bRoDTEP\b", re.I), "rosctl": re.compile(r"\bRoSCTL\b", re.I),
            "drawback": re.compile(r"\b(?:duty\s+)?draw\s?back\b", re.I)}


def _iso(raw: str | None) -> str | None:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(raw, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_shipping_bill(text: str | None) -> dict[str, Any] | None:
    """Pure: Shipping Bill number / date / LEO date / port code and declared schemes from a CHA e-mail."""
    t = text or ""
    m = _SB_NO.search(t)
    if not m:
        return None
    d, leo, port = _SB_DATE.search(t), _LEO.search(t), _PORT.search(t)
    return {"number": m.group(1), "date": _iso(d.group(1)) if d else None,
            "leo_date": _iso(leo.group(1)) if leo else None, "port_code": port.group(1).upper() if port else None,
            "declared": [k for k, rx in _SCHEMES.items() if rx.search(t)]}


async def handle_reply(deal_id: str, from_address: str, body: str | None) -> dict[str, Any]:
    """A reply on the ``[CHA:<deal>]`` thread: the Shipping Bill record, else the checklist → gate."""
    deal = await _deal(deal_id)
    cha = dict(getattr(deal, "cha", None) or {})
    sender = (from_address or "").strip().lower()
    if not cha.get("agent_email") or sender.split("@")[-1] != cha["agent_email"].split("@")[-1]:
        return {"handled": False, "reason": "sender is not the CHA this deal's pack went to"}
    sb = parse_shipping_bill(body)
    if sb:
        return {"handled": True, "shipping_bill": await record_shipping_bill(deal_id, sb, by=f"cha:{sender}")}
    if _CHECKLIST.search(body or ""):
        approval = await open_checklist_gate(deal, cha, body or "")
        return {"handled": True, "gate": getattr(approval, "approval_id", None)}
    return {"handled": False, "reason": "no Shipping Bill number or checklist in the reply"}


async def open_checklist_gate(deal: Any, cha: dict[str, Any], body: str) -> Any:
    from shared.orchestration.continuations import _create_gate
    cha.update(checklist_received_at=datetime.now(timezone.utc).isoformat(), status="checklist_received")
    await _save(deal.deal_id, cha)
    return await _create_gate(
        deal.deal_id, ACTION, f"{cha.get('agent_firm') or 'CHA'} sent the Shipping Bill checklist for {deal.deal_id} — "
        "check HS code, FOB value, incentives declared, then approve filing",
        None, {"deal_id": deal.deal_id, "agent_email": cha.get("agent_email"), "checklist": body[:4000],
               "incentives_to_declare": cha.get("incentives_to_declare") or []},
        f"cha-checklist-{deal.deal_id}-{cha.get('checklist_received_at', '')[:16]}", autonomy_level="L0", deal=deal,
        risk_flags=["incentive_not_in_checklist"] if any(
            not _SCHEMES[s].search(body) for s in cha.get("incentives_to_declare") or [] if s in _SCHEMES) else None)


async def approve_filing(deal_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """APPROVE callback: tell the CHA to file."""
    deal = await _deal(deal_id)
    cha = dict(getattr(deal, "cha", None) or {})
    try:
        from services.email_gateway.schemas import SendEmailRequest  # type: ignore[import]
        from services.email_gateway.sender import send_email  # type: ignore[import]
        from shared import company_profile
        await send_email(SendEmailRequest(
            to_address=cha.get("agent_email") or payload.get("agent_email"),
            subject=f"Re: Shipping Bill — {deal_id} {subject_token(deal_id)} — checklist approved",
            body="The checklist is approved — please file the Shipping Bill and reply with its number, date, "
                 "port code and LEO date.\n\n" + company_profile.signature(),
            email_id=f"CHAOK{deal_id.replace('-', '')[-10:]}"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("CHA: filing approval for %s not e-mailed: %s", deal_id, exc)
    cha.update(checklist_approved_at=datetime.now(timezone.utc).isoformat(), status="filing_approved")
    await _save(deal_id, cha)
    return {"deal_id": deal_id, "status": "filing_approved"}


async def record_shipping_bill(deal_id: str, sb: dict[str, Any], *, by: str) -> dict[str, Any]:
    if not re.fullmatch(r"\d{6,8}", str(sb.get("number") or "")):
        raise ValueError("the Shipping Bill number is 6–8 digits")
    deal = await _deal(deal_id)
    cha = dict(getattr(deal, "cha", None) or {})
    declared = sorted({str(s).lower() for s in sb.get("declared") or []})
    row = {"number": str(sb["number"]), "date": sb.get("date"), "port_code": sb.get("port_code"),
           "leo_date": sb.get("leo_date"), "declared": declared, "recorded_by": by,
           "recorded_at": datetime.now(timezone.utc).isoformat()}
    cha.update(shipping_bill=row, status="leo" if row["leo_date"] else "filed")
    missing = [s for s in cha.get("incentives_to_declare") or [] if s not in declared]
    cha["incentives_not_declared"] = missing
    await _save(deal_id, cha)
    if missing:
        from shared.orchestration.continuations import _publish_exception
        await _publish_exception(deal_id, "INCENTIVE:not_declared",
                                 f"Shipping Bill {row['number']} does not declare {', '.join(m.upper() for m in missing)} "
                                 "— amend it before LEO or the claim is lost",
                                 {"category": "DOCUMENT_ERROR", "shipping_bill": row, "missing": missing,
                                  "action_required": "amend_shipping_bill"})
    return row


async def record_invoice(deal_id: str, *, amount_usd: float, items: dict[str, float] | None, by: str) -> dict[str, Any]:
    """The CHA's invoice against the cost sheet's customs-agent line."""
    if amount_usd is None or amount_usd < 0:
        raise ValueError("amount_usd must be zero or more")
    deal = await _deal(deal_id)
    cha = dict(getattr(deal, "cha", None) or {})
    estimate = _estimate(deal)
    over = bool(estimate) and amount_usd > estimate * (1 + float(os.getenv("CHA_OVERRUN_PCT", "20") or 20) / 100.0)
    cha["invoice"] = {"amount_usd": float(amount_usd), "items": items or {}, "estimate_usd": estimate,
                      "over_estimate": over, "recorded_by": by, "recorded_at": datetime.now(timezone.utc).isoformat()}
    await _save(deal_id, cha)
    if over:
        from shared.orchestration.continuations import _publish_exception
        await _publish_exception(deal_id, "CHA:cost_overrun",
                                 f"CHA invoice {amount_usd:,.2f} USD vs {estimate:,.2f} USD in the cost sheet",
                                 {"category": "MARGIN_BREACH", "invoice": cha["invoice"],
                                  "action_required": "review_margin"})
    return cha["invoice"]


def _estimate(deal: Any) -> float | None:
    sheet = getattr(deal, "cost_sheet", None) or {}
    lines = sheet.get("lines") if isinstance(sheet, dict) else None
    qty = (sheet.get("quantity_mt") if isinstance(sheet, dict) else None) or getattr(deal, "quantity_mt", None) or 0
    for line in lines or []:
        if line.get("key") == "customs_agent" and qty:
            return round(float(line.get("usd_per_mt") or 0) * float(qty), 2)
    return float(os.getenv("TRADE_COST_CHA_USD_PER_SHIPMENT", "150") or 150)


def reset_for_tests() -> None:
    _memory.clear()
    _kyc.clear()
