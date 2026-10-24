"""Static trade context for negotiation prompts, keyed by product HS code."""

_FALLBACK_PLAYBOOKS: dict[str, str] = {
    "1006": (
        "Rice (HS 1006): Global benchmark is Thailand 5% broken (FOB Bangkok). "
        "Standard payment: LC at sight. Lead time: 21–30 days. "
        "Quality: moisture ≤ 14%, broken ≤ 5%. "
        "Concession rhythm: supplier opens +3–5%, buyer counters −8%. "
        "Convergence in ~3 rounds. Incoterm: FOB or CIF."
    ),
    "1001": (
        "Wheat (HS 1001): CME CBOT front-month is the reference. "
        "Protein premium: +$8–12/MT for hard red winter. "
        "Payment: open account 90 days (established) or LC (new buyers). "
        "Lead time: 14–21 days Black Sea; 30–35 days Australia. MOQ: 500 MT."
    ),
    "6101": (
        "Apparel (HS 61): Factory-gate pricing dominates. "
        "Buyer MOQ: 500–1000 pcs/SKU. Lead time: 45–60 days. "
        "Payment: 30% TT advance + 70% on BL. "
        "OEKO-TEX 100 required by EU buyers. Concession: 2–3%/round."
    ),
    "DEFAULT": (
        "Generic trade context: anchor buyer −10% of market, supplier +12–15%. "
        "Standard convergence: 3–5 rounds for commodities. "
        "Payment: LC at sight first transaction; open account after 3 shipments. "
        "Incoterm default: FOB origin / CIF destination."
    ),
}


def trade_context_for_product(hs_code: str) -> str:
    for prefix_len in (4, 2):
        hit = _FALLBACK_PLAYBOOKS.get(hs_code[:prefix_len])
        if hit:
            return hit
    return _FALLBACK_PLAYBOOKS["DEFAULT"]
