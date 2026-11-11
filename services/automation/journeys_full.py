"""UC-15 — the whole positive flow, opportunity to cash, on every provider simulator.

Nothing is written into a record by hand and no provider result is injected:
the deal starts from a stored trade-min opportunity (what a scan leaves), every
step runs through the real continuation, gate, e-mail or provider read, and the
real LLM drafts PortLess's e-mails and plays the buyer and the supplier.

  trade-min opportunity → deal (Match Engine) → buyer outreach + RFQ by e-mail
  → buyer interested + supplier quote → match board → CONFIRM_MATCH
  → LLM negotiation (port, packing, inspection phases, escrow provider named by the buyer)
  → agreement → trade compliance → supplier firm order → PI → PI accepted → sales contract
  → Tazapay escrow (simulator) funded → PO → PO acknowledged with the goods-ready date
  → QIMA DUPRO + PSI (simulator) → supervised loading + seal → T1 / T2 submitted and paid out by Tazapay
  → Maersk booking (simulator) → VGM gate → B/L approval gate → eBL issued → PEO (QIMA) → customs
  → departed → arrived (CLEARED, T3, payouts up to the retention) → delivered (acceptance window)
  → buyer accepts by e-mail → T4 → settled → supplier balance → PAID
  → ledger reconciled, journal balanced, every document issued, timeline, evidence pack.

The one thing not on a simulator: the Indian registries (GSTN / DGFT / MCA /
Udyam) have no sandbox, so the sourcing verification answers "active" here.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from services.deals.schemas import CreateDealRequest
from services.deals.service import create_deal, get_deal
from services.email_processor.schemas import InboundEmailSource, InboundRawEmail
from services.email_processor.service import process_inbound
from services.trade_min.schemas import BuyerSignal, EnrichedOpportunity, SellerCandidate

from . import journeys as j
from . import seed as fx

S = "UC-15"
HS = "10063020"
MARKET = "AE"


def _v(x: Any) -> str:
    return str(getattr(x, "value", x))


async def _gates(deal_id: str) -> list[str]:
    return sorted(g.action for g in await j.pending(deal_id))


async def _approve_all(deal_id: str, *actions: str, rounds: int = 3) -> list[str]:
    """APPROVE every pending gate of these actions until none is left (a gate can open the next)."""
    done: list[str] = []
    for _ in range(rounds):
        hit = False
        for action in actions:
            for gate in await j.approve(deal_id, action, by="operator@qa.invalid"):
                done.append(gate.action)
                hit = True
        if not hit:
            break
        await j.slow_settle()
    return done


def _sandbox_registry() -> Any:
    """GSTN / DGFT / MCA / Udyam have no sandbox: answer every lookup as an active, clean firm."""
    from services.sourcing import service as sourcing

    original = sourcing._safe_call

    async def active(_connector: str, _method: str, _request: dict) -> dict:
        return {"status": "ACTIVE", "on_denied_list": False, "age_years": 8, "registered": True}

    sourcing._safe_call = active
    return lambda: setattr(sourcing, "_safe_call", original)


async def _inbound(sender: str, subject: str, body: str) -> Any:
    result = await process_inbound(InboundRawEmail(
        from_address=sender, to_address="trade@portless-qa.invalid", subject=subject, body_text=body,
        message_id=f"<{uuid.uuid4().hex}@qa.invalid>", source=InboundEmailSource.IMAP,
        received_at=datetime.now(timezone.utc),
    ))
    await j.slow_settle()
    j.note(f"\n_System: {_v(result.status)} — {result.message[:220]}_\n")
    j.say(f"   inbound from {sender}: {_v(result.status)} — {result.message[:130]}")
    return result


def _mail_to(address: str, contains: str = "") -> list[dict[str, Any]]:
    return [m for m in j.EMAILS if m["to"] == address and contains.lower() in (m["subject"] + m["body"]).lower()]


async def _qc_orders(deal_id: str) -> list[Any]:
    return [o for o in await j.sl_repo().list_by_deal(deal_id) if o.order_type == "QC"]


async def _inspect(deal_id: str, phase: str, qima: httpx.AsyncClient) -> Any:
    """QIMA's team validates the order, the inspector passes the goods; PortLess learns it by polling QIMA."""
    from services.shield_link import qc as qc_lifecycle
    order = next((o for o in await _qc_orders(deal_id) if (o.inspection_type or "PSI") == phase), None)
    j.check(S, f"the {phase} is booked on QIMA (simulator)", order is not None and bool(order.ext_ref),
            str([(o.inspection_type, o.status, o.ext_ref) for o in await _qc_orders(deal_id)]))
    if order is None:
        return None
    (await qima.post(f"/_sandbox/qima/orders/{order.ext_ref}/validate", json={"inspection_date": order.inspection_date})).raise_for_status()
    (await qima.post(f"/_sandbox/qima/orders/{order.ext_ref}/inspect", json={"result": "PASS"})).raise_for_status()
    await qc_lifecycle.poll_qc(deal_id, force=True)
    await j.slow_settle()
    order = next(o for o in await _qc_orders(deal_id) if o.tpo_id == order.tpo_id)
    j.say(f"   {phase}: order {order.ext_ref} status={order.status} result={getattr(order, 'result', None)}")
    return order


async def _tranches(deal_id: str) -> dict[str, str]:
    hold = await j.eg_repo().get_hold_by_deal(deal_id)
    return {t.tranche_id: _v(t.status) for t in hold.tranches} if hold else {}


async def _payouts(deal_id: str) -> None:
    for _ in range(5):
        if not await j.approve(deal_id, "RELEASE_SUPPLIER_PAYOUT", by="operator@qa.invalid"):
            break
        await j.complete_payouts(deal_id)
        await j.slow_settle()


async def uc_full_flow() -> None:
    j.say(f"\n{'=' * 78}\n{S}: the whole positive flow, opportunity to cash, on every provider simulator\n{'=' * 78}")
    restore_registry = _sandbox_registry()
    from services.carrier_sandbox import main as carrier_sim
    from services.carrier_sandbox import maersk as maersk_sim
    from services.qc_sandbox import main as qc_sim
    from services.shield_link import freight
    maersk_sim.STATE.auto = False  # Maersk confirms, moves the vessel and issues the B/L only when this journey says so
    qima = httpx.AsyncClient(transport=httpx.ASGITransport(app=qc_sim.app), base_url="http://qc-sandbox.local", timeout=30)
    try:
        await _run(qima, carrier_sim, freight)
    finally:
        await qima.aclose()
        restore_registry()


async def _run(qima: httpx.AsyncClient, carrier_sim: Any, freight: Any) -> None:
    uid = uuid.uuid4().hex[:6]
    now = datetime.now(timezone.utc)
    buyers = [
        {"id": "B1", "name": fx.company("buyer", f"Gulf Staples Trading {uid} LLC").name, "mail": f"purchasing.{uid}@gulfstaples-{uid}.invalid"},
        {"id": "B2", "name": fx.company("buyer", f"Emirates Foodstuff {uid} LLC").name, "mail": f"imports.{uid}@emiratesfood-{uid}.invalid"},
    ]
    suppliers = [
        {"id": "S1", "name": fx.company("supplier", f"Cauvery Rice Mills {uid} Pvt Ltd").name, "mail": f"export.{uid}@cauvery-{uid}.invalid", "fob": 900.0},
        {"id": "S2", "name": fx.company("supplier", f"Godavari Agro {uid} Pvt Ltd").name, "mail": f"sales.{uid}@godavari-{uid}.invalid", "fob": 940.0},
    ]

    # ── 1. The opportunity a scan left, and the deal made from it ─────────
    j.say(f"\n[{S}] trade-min opportunity → deal (Match Engine) → outreach + RFQ")
    from services.trade_min.repository import get_repository as tm_repo
    opp = EnrichedOpportunity(
        opportunity_id=f"OPP-UC15-{uid}", hs_code=HS, market=MARKET, scanned_at=now,
        demand_index=72.0, price_gap_pct=28.0, source="apify_export_markets", net_price_gap_pct=22.0,
        arbitrage_rank=65.0, confidence_score=72.0, price_benchmark_usd_per_mt=1240.0,
        buyer_signals=[BuyerSignal(buyer_name=b["name"], buyer_country=MARKET, company_id=b["id"], signal_type="manifest",
                                   source="apify_export_markets", urgency_tier=2, unit_price_usd=1250.0, price_unit="USD/MT",
                                   email=b["mail"], fit_score=80 - i * 5, quantity=20.0, quantity_unit="MT",
                                   pitch_angle="imports milled rice into Jebel Ali every month")
                       for i, b in enumerate(buyers)],
        seller_candidates=[SellerCandidate(candidate_id=s["id"], company_name=s["name"], location_city="Thanjavur",
                                           location_state="Tamil Nadu", hs_code=HS, estimated_fob_price_usd=s["fob"],
                                           annual_capacity_mt=6000.0, source="apify_export_markets", email=s["mail"],
                                           gstin="33AAAAA0000A1Z5", iec="0400000000", supplier_to_port_km=340.0,
                                           export_shipments_12m=24, fit_score=80)
                           for s in suppliers],
    )
    await (await tm_repo()).save(opp)
    deal = await create_deal(CreateDealRequest(operator_id="qa", hs_code=HS, market=MARKET, opportunity_id=opp.opportunity_id,
                                               margin_floor_pct=8.0), org_id="qa-org-a")
    deal_id = deal.deal_id
    j.note(f"\n\n# Use case {S}\n\nDeal `{deal_id}` from opportunity `{opp.opportunity_id}`\n")
    await j.slow_settle()
    deal = await get_deal(deal_id)
    j.check(S, "the opportunity becomes a prospect at MATCHING with its buyers as candidates",
            _v(deal.status) == "MATCHING" and len(deal.buyer_candidates) == 2, f"{_v(deal.status)}, {len(deal.buyer_candidates)} candidates")
    j.check(S, "the Match Engine scored the suppliers", bool(deal.match_results),
            str([(m.supplier_id, m.decision, m.composite_score) for m in deal.match_results]))
    j.check(S, "no buyer party while the outreach funnel is open",
            not any(_v(p.role).lower() == "buyer" for p in deal.parties), str([(p.role, p.legal_name) for p in deal.parties]))
    intros = {b["id"]: _mail_to(b["mail"], "[BUY:") for b in buyers}
    j.check(S, "each buyer gets an introduction with its own [BUY:] token", all(intros.values()),
            str({k: len(v) for k, v in intros.items()}) + f"; gates {await _gates(deal_id)}")
    rfqs = {s["id"]: _mail_to(s["mail"], "[RFQ:") for s in suppliers}
    j.check(S, "each supplier gets the RFQ by e-mail with an [RFQ:] token", all(rfqs.values()),
            str({k: len(v) for k, v in rfqs.items()}) + f"; gates {await _gates(deal_id)}")
    if not intros["B1"] or not rfqs["S1"]:
        return
    j.note(f"\n**PortLess → {buyers[0]['mail']}** (template)\n\n> {intros['B1'][-1]['subject']}\n\n{intros['B1'][-1]['body']}\n")

    # ── 2. The buyer says yes, the supplier quotes ─────────────────────────
    j.say(f"\n[{S}] buyer interested + supplier quote → match board")
    intro = intros["B1"][-1]
    body = await j.persona_reply("purchasing manager", buyers[0]["name"], intro["body"],
                                 "Say you are interested. You need 20 MT of long-grain white rice, 5% broken, CIF Jebel Ali, "
                                 "and your target is USD 1,250 per MT. Ask for their offer.")
    j.note(f"\n**{buyers[0]['mail']} → PortLess** (LLM playing the buyer)\n\n{body}\n")
    await _inbound(buyers[0]["mail"], f"Re: {intro['subject']}", body)
    deal = await get_deal(deal_id)
    c1 = next(c for c in deal.buyer_candidates if c.candidate_id == "B1")
    j.check(S, "the buyer's reply is read as interested, with its requirement for the match board",
            c1.status == "interested" and c1.requirement is not None,
            f"{c1.status}; requirement {c1.requirement.model_dump(exclude_none=True) if c1.requirement else None}")

    rfq = rfqs["S1"][-1]
    body = await j.persona_reply("export sales manager", suppliers[0]["name"], rfq["body"],
                                 "Quote USD 900 per MT FOB Chennai for 20 MT of long-grain white rice, 5% broken. MOQ 10 MT, "
                                 "capacity 500 MT a month, ready 20 days after order. Write: GSTIN 33AAAAA0000A1Z5, IEC 0400000000.")
    j.note(f"\n**{suppliers[0]['mail']} → PortLess** (LLM playing the supplier)\n\n{body}\n")
    await _inbound(suppliers[0]["mail"], f"Re: {rfq['subject']}", body)
    from services.sourcing.repository import get_repository as src_repo
    quotes = await src_repo().list_quotes(deal_id=deal_id)
    deal = await get_deal(deal_id)
    j.check(S, "the e-mailed quote enters the quote pipeline and the supplier's offer reaches the deal",
            bool(deal.supplier_offers) or bool(quotes),
            f"offers {[(o.company_name, o.fob_price_usd_per_mt) for o in deal.supplier_offers]}; quotes {len(quotes)}; gates {await _gates(deal_id)}")

    # ── 3. Match board → CONFIRM_MATCH → negotiation ──────────────────────
    from shared.orchestration import matchmaking
    if not await j.pending(deal_id, "CONFIRM_MATCH"):
        await matchmaking.evaluate(deal_id)
        await j.slow_settle()
    if await j.pending(deal_id, "SEND_INDICATIVE_OFFER") and not await j.pending(deal_id, "CONFIRM_MATCH"):
        j.say("   the board proposes an indicative offer first")
        await _approve_all(deal_id, "SEND_INDICATIVE_OFFER")
        offer_mail = _mail_to(buyers[0]["mail"])[-1]
        body = await j.persona_reply("purchasing manager", buyers[0]["name"], offer_mail["body"],
                                     "Say the range works for you and you want to proceed with 20 MT.")
        await _inbound(buyers[0]["mail"], f"Re: {offer_mail['subject']}", body)
        await matchmaking.evaluate(deal_id)
        await j.slow_settle()
    gates = await j.pending(deal_id, "CONFIRM_MATCH")
    j.check(S, "the match board proposes the buyer × supplier pair for an operator (CONFIRM_MATCH)", bool(gates),
            f"gates {await _gates(deal_id)}")
    deal = await get_deal(deal_id)
    s1_offers = {o.offer_id for o in deal.supplier_offers if o.supplier_id == "S1"}
    ours = [g for g in gates if g.callback_payload.get("offer_id") in s1_offers and g.callback_payload.get("candidate_id") == "B1"]
    j.check(S, "one CONFIRM_MATCH is for buyer B1 × supplier S1", bool(ours),
            str([(g.callback_payload.get("candidate_id"), g.callback_payload.get("offer_id")) for g in gates]))
    for gate in ours[:1]:
        await j.resolve_approval(gate.approval_id, j.ResolveApprovalRequest(decision="APPROVE", resolved_by="operator@qa.invalid"))
        await j.slow_settle()
    # Offers from other prospects of the same HS code are on the board too: the
    # confirmed pair closes their gates itself (superseded), no operator click.
    j.check(S, "confirming the match closes every other proposed pair of the deal",
            not await j.pending(deal_id, "CONFIRM_MATCH") and not await j.pending(deal_id, "SEND_INDICATIVE_OFFER"),
            str(await _gates(deal_id)))
    await _approve_all(deal_id, "CONFIRM_SUPPLIER_SELECTION")
    deal = await get_deal(deal_id)
    supplier_party = next((p for p in deal.parties if _v(p.role).lower() == "supplier"), None)
    buyer_party = next((p for p in deal.parties if _v(p.role).lower() == "buyer"), None)
    j.check(S, "the confirmed match sets the buyer and the supplier at their own addresses and opens the negotiation",
            _v(deal.status) == "NEGOTIATING" and supplier_party is not None and supplier_party.email == suppliers[0]["mail"]
            and buyer_party is not None and buyer_party.email == buyers[0]["mail"],
            f"{_v(deal.status)}; buyer {getattr(buyer_party, 'email', None)}; supplier {getattr(supplier_party, 'email', None)}")
    session = await j.neg_repo().get_session_by_deal(deal_id)
    if session is None or _v(deal.status) != "NEGOTIATING":
        return
    who = {"name": S, "buyer_company": buyers[0]["name"], "buyer_mail": buyers[0]["mail"],
           "supplier_company": suppliers[0]["name"], "supplier_mail": suppliers[0]["mail"],
           "negotiation_id": session.negotiation_id, "supplier_ask": suppliers[0]["fob"]}

    # ── 4. LLM negotiation ────────────────────────────────────────────────
    j.say(f"\n[{S}] LLM negotiation: port, packing, inspection phases, escrow provider")
    await j.send_pending_drafts(deal_id, who)
    plan = [
        ("buyer", "Say the price is too high and counter at USD 1,200 per MT for 20 MT. Repeat these sentences word for word: "
                  "Our destination port is Jebel Ali, UAE. We want a during production inspection, a pre-shipment inspection "
                  "and an inspection before customs clearance. We will pay through Tazapay escrow. The goods must be packed in 25 kg "
                  "PP woven bags with our company name printed on each bag. Do not ask any question."),
        ("supplier", "Say you can improve slightly and offer USD 910 per MT FOB Chennai."),
        ("buyer", "Say you will meet them closer and counter at USD 1,225 per MT."),
    ]
    for party, instruction in plan:
        offered = (await j.session_of(who)).last_offer_to_buyer
        if party == "buyer" and "counter at USD 1,225" in instruction and offered and offered <= 1225:
            # PortLess's own pricing already came down to the buyer's planned counter:
            # a real buyer takes the offer instead of countering with the same number.
            instruction = (f"Say you accept the offer of USD {offered:,.0f} per MT and wish to proceed. "
                           "Do not ask any question.")
        if not await j.last_sent(who, party):
            j.check(S, f"PortLess wrote to the {party} before the {party} replies", False)
            continue
        await j.reply(deal_id, who, party, instruction)
        await j.send_pending_drafts(deal_id, who)
        if await j.pending(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT"):
            break
    if not await j.pending(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT"):
        await j.reply(deal_id, who, "buyer", "Say clearly: we accept your offer and your terms and wish to proceed. Repeat word for word: "
                                             "Our destination port is Jebel Ali, UAE. The goods must be packed in 25 kg PP woven bags "
                                             "with our company name printed on each bag. Do NOT write any price. Do not ask any question and "
                                             "do not ask for documents or details.")
        await j.send_pending_drafts(deal_id, who)
    session = await j.session_of(who)
    j.say(f"   negotiation: status={session.status} round={session.round_no} agreed={session.agreed_price}")
    j.check(S, "the buyer's acceptance waits for the operator", bool(await j.pending(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT")),
            f"{_v((await get_deal(deal_id)).status)}; negotiation {session.status}; gates {await _gates(deal_id)}")
    deal = await get_deal(deal_id)
    j.check(S, "the buyer's inspection phases were read from its e-mail (DUPRO, PSI, PEO)",
            list(deal.inspection_types or []) == ["DUPRO", "PSI", "PEO"], str(deal.inspection_types))
    j.check(S, "the buyer's escrow provider was read from its e-mail (Tazapay)",
            (deal.escrow_provider or "") == "tazapay", f"{deal.escrow_provider} ({getattr(deal, 'escrow_provider_source', None)})")
    from shared.orchestration import supplier_address
    address_before, known_before = await supplier_address.current(deal_id)
    asked_in_negotiation = [e for e in await j.drafts(who) if e.party == "supplier" and e.body
                            and _v(e.direction).lower() == "outbound" and "address" in e.body.lower()]
    j.check(S, "the negotiation asks the supplier for its facility address (the scan only knew the town)",
            bool(asked_in_negotiation), f"address on file: {address_before}")
    await _approve_all(deal_id, "CONFIRM_NEGOTIATION_AGREEMENT")
    await _approve_all(deal_id, "CONFIRM_TRADE_COMPLIANCE", "CONFIRM_COUNTERPARTY_RISK")

    # ── 5. Supplier commits → PI → PI accepted ────────────────────────────
    j.say(f"\n[{S}] supplier firm order → PI → PI accepted → escrow")
    from shared.orchestration import supplier_commitment
    commitment = await supplier_commitment.get(deal_id)
    j.check(S, "the agreement first asks the supplier to confirm the order at its own price",
            commitment is not None and commitment.status == "requested", f"{commitment.status if commitment else None}; gates {await _gates(deal_id)}")
    if commitment is None or commitment.current is None:
        return
    asked = commitment.current.price_usd_per_mt
    order_request = next((m for m in reversed(j.EMAILS) if m["to"] == who["supplier_mail"] and "Order confirmation" in m["subject"]), None)
    j.check(S, "the order request is in the supplier's currency (INR with the USD price) and asks for the facility "
               "address only if none is known yet",
            bool(order_request) and "INR " in order_request["body"] and f"(USD {asked:,.2f})" in order_request["body"]
            and (("full address of the facility" in order_request["body"]) == (not known_before)),
            f"address before: {address_before} (complete={known_before}); " + (order_request or {}).get("body", "")[:300])
    await j.reply(deal_id, who, "supplier", f"Say clearly: we confirm the order at USD {asked:,.0f} per MT FOB, packing as requested. "
                                            "Port of loading Chennai, ready 26 days after the purchase order. Then write this line "
                                            "exactly: Factory address: Plot 14, SIPCOT Industrial Estate, Thanjavur, Tamil Nadu 613005")
    supplier_party = next((p for p in (await get_deal(deal_id)).parties if _v(p.role).lower() == "supplier"), None)
    j.check(S, "the supplier's facility address is read from its own e-mail onto the deal",
            supplier_party is not None and "613005" in (supplier_party.address or ""), str(getattr(supplier_party, "address", None)))
    await _approve_all(deal_id, "CONFIRM_SUPPLIER_ORDER")
    j.check(S, "the confirmed order issues the PI", await j.status(deal_id) == "PI_SENT", f"{await j.status(deal_id)}; gates {await _gates(deal_id)}")
    pi = await j.dm_repo().get_pi_by_deal(deal_id)
    j.check(S, "the PI carries the agreed price, the buyer's port and its escrow provider",
            pi is not None and abs(pi.agreed_price - (session.agreed_price or 0)) < 0.01 and pi.port_of_destination == "Jebel Ali, AE",
            f"PI {getattr(pi, 'agreed_price', None)} / {getattr(pi, 'port_of_destination', None)}; agreed {session.agreed_price}")
    await j.reply(deal_id, who, "buyer", "Reply exactly in substance: We accept the Proforma Invoice. Do not add conditions.")
    await _approve_all(deal_id, "CONFIRM_PI_ACCEPTANCE")
    hold = await j.eg_repo().get_hold_by_deal(deal_id)
    j.check(S, "PI accepted opens the buyer's escrow on Tazapay (simulator) at the PI value",
            await j.status(deal_id) == "ESCROW_PENDING" and hold is not None and hold.provider == "tazapay"
            and pi is not None and abs(hold.amount - pi.total_value) < 0.01,
            f"{await j.status(deal_id)}; {getattr(hold, 'provider', None)} {getattr(hold, 'amount', None)}")
    if hold is None:
        return
    confirmed = [m for m in j.EMAILS if "order confirmed" in m["subject"].lower()]
    j.check(S, "both parties get the order-confirmed e-mail, neither naming the other",
            {m["to"] for m in confirmed} >= {who["buyer_mail"], who["supplier_mail"]}
            and not any(j.leak_free(m["body"], who, "buyer" if m["to"] == who["buyer_mail"] else "supplier") for m in confirmed),
            str([m["to"] for m in confirmed]))

    # ── 6. Funded → PO → acknowledgement → QC booked ─────────────────────
    j.say(f"\n[{S}] buyer pays on Tazapay → PO → supplier acknowledges with its goods-ready date")
    await j.fund(deal_id, hold.amount, who["buyer_company"], MARKET, f"ev-{S}-{uid}")
    await j.slow_settle()
    j.check(S, "PortLess reads the payment from Tazapay and asks for the PO", await j.status(deal_id) == "ESCROW_FUNDED"
            and bool(await j.pending(deal_id, "ISSUE_PO")), f"{await j.status(deal_id)}; gates {await _gates(deal_id)}")
    await _approve_all(deal_id, "ISSUE_PO")
    j.check(S, "PO issued", await j.status(deal_id) == "PO_ISSUED", await j.status(deal_id))
    ready_on = (datetime.now(timezone.utc) + timedelta(days=26)).date()
    await j.reply(deal_id, who, "supplier", "Say clearly: we acknowledge the purchase order and start production on Monday. Also say "
                                            f"that the goods will be fully produced and packed, ready for inspection on "
                                            f"{ready_on.day} {ready_on:%B %Y} — write that date exactly as given.")
    rec = await supplier_commitment.get(deal_id)
    j.check(S, "the supplier's own e-mail gives the goods-ready date", bool(rec and rec.qc_ready_date == ready_on.isoformat()),
            f"{rec.qc_ready_date if rec else None} by {rec.qc_ready_by if rec else None}")
    booked = [(o.inspection_type or "PSI", o.status) for o in await _qc_orders(deal_id)]
    phases = sorted(ph for ph, st in booked if st != "FAILED")
    j.check(S, "DUPRO and PSI are booked on QIMA (PEO waits for the freight)", phases == ["DUPRO", "PSI"],
            f"{booked}; status {await j.status(deal_id)}")
    await _approve_all(deal_id, "RELEASE_ESCROW_TRANCHE")
    j.check(S, "T1 is submitted to Tazapay with its proof and booked once Tazapay pays it out",
            (await _tranches(deal_id)).get("T1") == "RELEASED", str(await _tranches(deal_id)))

    # ── 7. Inspections on QIMA, supervised loading ────────────────────────
    j.say(f"\n[{S}] QIMA DUPRO + PSI (simulator, polled) → supervised loading + seal")
    await _inspect(deal_id, "DUPRO", qima)
    j.check(S, "a passed DUPRO does not move the deal", await j.status(deal_id) == "SUPPLIER_DISPATCH_READY", await j.status(deal_id))
    await _inspect(deal_id, "PSI", qima)
    deal = await get_deal(deal_id)
    j.check(S, "the PSI result read from QIMA passes the deal", _v(deal.status) == "QC_PASSED" and _v(deal.qc_status) == "PASSED",
            f"{_v(deal.status)} / qc {_v(deal.qc_status)}")
    from services.shield_link import service as sl_service
    from services.shield_link.schemas import LoadingRecordRequest
    await sl_service.record_loading(deal_id, LoadingRecordRequest(
        container_numbers=["MSKU1111111"], seal_numbers=[f"SEAL-{uid.upper()}"], loaded_quantity_mt=20.0, hs_code=HS,
        goods_description="Long-grain white rice, 5% broken, 25 kg PP bags", inspector_ref="QIMA", photos=["https://qa.invalid/seal.jpg"],
        source="inspector"))
    await j.slow_settle()
    j.check(S, "the inspector's container + seal record opens T2 and freight", {"BOOK_FREIGHT", "RELEASE_ESCROW_TRANCHE"} <= set(await _gates(deal_id)),
            str(await _gates(deal_id)))
    await _approve_all(deal_id, "RELEASE_ESCROW_TRANCHE")
    j.check(S, "T2 paid out by Tazapay", (await _tranches(deal_id)).get("T2") == "RELEASED", str(await _tranches(deal_id)))

    # ── 8. Maersk: booking, VGM, B/L, PEO, customs ───────────────────────
    j.say(f"\n[{S}] Maersk booking → VGM → B/L → eBL → PEO → customs (simulators)")
    await _approve_all(deal_id, "BOOK_FREIGHT")
    tpo = next((o for o in await j.sl_repo().list_by_deal(deal_id) if o.order_type.upper() == "FREIGHT"), None)
    j.check(S, "freight booked on Maersk (simulator), deal IN_TRANSIT",
            tpo is not None and tpo.provider == "maersk" and await j.status(deal_id) == "IN_TRANSIT",
            f"{getattr(tpo, 'provider', None)}; {await j.status(deal_id)}")
    if tpo is None:
        return
    await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/confirm")
    await freight.sync_deal(deal_id)
    await j.slow_settle()
    j.check(S, "Maersk confirms; the VGM and the draft B/L wait for an operator",
            {"SUBMIT_VGM", "APPROVE_TRANSPORT_DOCUMENT"} <= set(await _gates(deal_id)), str(await _gates(deal_id)))
    await _approve_all(deal_id, "SUBMIT_VGM", "APPROVE_TRANSPORT_DOCUMENT")
    await carrier_sim.control(f"/maersk/_sandbox/transport-documents/{tpo.ext_ref}/issue")
    await freight.sync_deal(deal_id)
    await j.slow_settle()
    tpo = await j.sl_repo().get(tpo.tpo_id)
    j.check(S, "VGM submitted and the eBL issued", tpo.vgm_status == "SUBMITTED" and tpo.ebl_status == "ISSUED",
            f"vgm {tpo.vgm_status}, ebl {tpo.ebl_status}")
    await _inspect(deal_id, "PEO", qima)
    await _approve_all(deal_id, "FILE_CUSTOMS")
    orders = await j.sl_repo().list_by_deal(deal_id)
    j.check(S, "customs filed after the PEO passed", any(o.order_type.upper() == "CHA" for o in orders),
            f"{[(o.order_type, o.inspection_type, o.status) for o in orders]}; gates {await _gates(deal_id)}")

    # ── 9. Voyage → arrival → payouts up to the retention ────────────────
    j.say(f"\n[{S}] departed → arrived (CLEARED) → T3 → supplier payouts")
    await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/advance", {"to": "DEPA"})
    await freight.sync_deal(deal_id)
    await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/advance", {"to": "ARRI"})
    await freight.sync_deal(deal_id)
    await j.slow_settle()
    j.check(S, "arrival read from Maersk clears the deal", await j.status(deal_id) == "CLEARED", await j.status(deal_id))
    await _approve_all(deal_id, "RELEASE_ESCROW_TRANCHE")
    j.check(S, "T3 paid out by Tazapay", (await _tranches(deal_id)).get("T3") == "RELEASED", str(await _tranches(deal_id)))
    await _payouts(deal_id)
    j.check(S, "the supplier is not paid in full before the buyer accepts", await j.status(deal_id) == "CLEARED", await j.status(deal_id))

    # ── 10. Delivery → acceptance → settlement → PAID ─────────────────────
    j.say(f"\n[{S}] delivered → buyer accepts → T4 → settled → PAID")
    await carrier_sim.control(f"/maersk/_sandbox/bookings/{tpo.ext_ref}/advance", {"to": "GTOT"})
    await freight.sync_deal(deal_id)
    await j.slow_settle()
    hold = await j.eg_repo().get_hold_by_deal(deal_id)
    j.check(S, "delivery read from Maersk opens the buyer's acceptance window",
            getattr(hold, "inspection_window_opened_at", None) is not None or bool(_mail_to(who["buyer_mail"], "accept")),
            f"window {getattr(hold, 'inspection_window_opened_at', None)}")
    await j.reply(deal_id, who, "buyer", "Say the cargo arrived in good condition and state plainly: we accept the goods.")
    j.check(S, "the buyer's acceptance needs confirmation", bool(await j.pending(deal_id, "CONFIRM_BUYER_ACCEPTANCE")), str(await _gates(deal_id)))
    await _approve_all(deal_id, "CONFIRM_BUYER_ACCEPTANCE")
    await _approve_all(deal_id, "RELEASE_ESCROW_TRANCHE")
    await _payouts(deal_id)

    # ── 11. The books and the record ──────────────────────────────────────
    final = await j.status(deal_id)
    ledger = await j.eg.get_deal_ledger(deal_id)
    j.check(S, "deal ends PAID with every tranche paid out and the books reconciled",
            final == "PAID" and ledger.reconciled and set((await _tranches(deal_id)).values()) == {"RELEASED"},
            f"{final}; reconciled={ledger.reconciled}; {await _tranches(deal_id)}")
    j.check(S, "the supplier received exactly the PO value and PortLess kept a margin",
            abs(ledger.payouts_total - ledger.supplier_owed) < 0.01 and (ledger.realized_margin or 0) > 0,
            f"paid {ledger.payouts_total} of {ledger.supplier_owed}; margin {ledger.realized_margin}")
    journal = await j.build_journal(deal_id)
    j.check(S, "the double-entry journal balances", bool(getattr(journal, "balanced", False)), str(getattr(journal, "balanced", None)))
    docs = {(_v(d.doc_type)) for d in await j.dg_repo().list_by_deal(deal_id) if d.status == "RENDERED"}
    expected = {"SALES_CONTRACT", "INSPECTION_ORDER", "CI", "PL", "CoO", "SDF", "SHIPPING_BILL", "SHIPPING_INSTRUCTIONS",
                "INSURANCE_DECLARATION", "ACCEPTANCE_CERTIFICATE", "REMITTANCE_ADVICE", "SETTLEMENT_STATEMENT"}
    j.check(S, "every trade document was issued along the way", expected <= docs, f"missing {sorted(expected - docs)}")
    deal = await get_deal(deal_id)
    j.check(S, "the Deal Board statuses end PASSED / DELIVERED",
            _v(deal.qc_status) == "PASSED" and _v(getattr(deal, "freight_status", "")) == "DELIVERED",
            f"qc {_v(deal.qc_status)}, freight {_v(getattr(deal, 'freight_status', None))}, escrow {_v(getattr(deal, 'escrow_status', None))}")
    timeline = await j.evidence.build_timeline(deal_id)
    pack = await j.evidence.build_evidence_pack(deal_id)
    j.check(S, "the timeline and the evidence pack (with its content hash) are complete",
            len(timeline.get("events") or timeline.get("timeline") or []) > 20 and len(pack.get("content_hash") or "") == 64,
            f"{len(timeline.get('events') or timeline.get('timeline') or [])} events; warnings {pack.get('warnings')}")
    await j.assert_outbound_clean(S, who)
    from shared import currency as _currency
    rate = _currency.fallback_rate("INR") or 96.0
    misquoted = [e.email_id for e in await j.drafts(who) if e.party == "supplier" and e.body
                 and _v(e.direction).lower() == "outbound" and f"INR {suppliers[0]['fob'] * rate:,.0f}" in e.body
                 and "your quotation" in e.body.lower()]
    j.check(S, "the supplier's USD quotation is never restated as an INR amount", not misquoted, str(misquoted))
    our_prices = [e.body for e in await j.drafts(who) if e.party == "supplier" and e.body and _v(e.direction).lower() == "outbound"
                  and "per MT" in e.body]
    j.check(S, "PortLess's prices to the Indian supplier are written in INR", all("INR" in b for b in our_prices),
            str([b[:160] for b in our_prices if "INR" not in b][:2]))
    j.check(S, "no gate left waiting", not await j.pending(deal_id), str(await _gates(deal_id)))
    open_exc = [e for e in await j.exceptions_of(deal_id) if _v(e.status) not in ("RESOLVED",)]
    j.check(S, "no exception left open", not open_exc, str([(_v(e.status), getattr(e, "category", None), str(getattr(e, "message", ""))[:80]) for e in open_exc]))
    j.check(S, "no mail addressed a real mailbox", all(m["to"].endswith(".invalid") for m in j.EMAILS), "")
    j.say(f"   final: PI {pi.total_value if pi else None}, supplier {ledger.supplier_owed}, fees {ledger.fees_total}, margin {ledger.realized_margin}")
