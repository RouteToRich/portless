"""Static trade context for negotiation prompts, keyed by product HS code.

Market practice is background for the drafting agents. Payment terms are NOT
taken from here: every e-mail states PortLess's escrow terms
(briefing.buyer_terms / supplier_terms), so the playbooks only mention what the
market usually does elsewhere."""

_FALLBACK_PLAYBOOKS: dict[str, str] = {
    "1006": (
        "Rice (HS 1006): Global benchmark is Thailand 5% broken (FOB Bangkok). "
        "Market practice elsewhere: LC at sight (PortLess uses escrow instead). Lead time: 21–30 days. "
        "Quality: moisture ≤ 14%, broken ≤ 5%. "
        "Concession rhythm: supplier opens +3–5%, buyer counters −8%. "
        "Convergence in ~3 rounds. Incoterm: FOB or CIF."
    ),
    "1001": (
        "Wheat (HS 1001): CME CBOT front-month is the reference. "
        "Protein premium: +$8–12/MT for hard red winter. "
        "Market practice elsewhere: LC or 90-day open account (PortLess uses escrow instead). "
        "Lead time: 14–21 days Black Sea; 30–35 days Australia. MOQ: 500 MT."
    ),
    "6101": (
        "Apparel (HS 61): Factory-gate pricing dominates. "
        "Buyer MOQ: 500–1000 pcs/SKU. Lead time: 45–60 days. "
        "Market practice elsewhere: 30% TT advance + 70% on BL (PortLess uses escrow instead). "
        "OEKO-TEX 100 required by EU buyers. Concession: 2–3%/round."
    ),
    "DEFAULT": (
        "Generic trade context: anchor buyer −10% of market, supplier +12–15%. "
        "Standard convergence: 3–5 rounds for commodities. "
        "Payment is always PortLess's neutral escrow with staged release — never propose LC or open account. "
        "Incoterm default: FOB origin / CIF destination."
    ),
}


def trade_context_for_product(hs_code: str) -> str:
    for prefix_len in (4, 2):
        hit = _FALLBACK_PLAYBOOKS.get(hs_code[:prefix_len])
        if hit:
            return hit
    return _FALLBACK_PLAYBOOKS["DEFAULT"]
