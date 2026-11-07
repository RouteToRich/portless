"""Deterministic price strategy for every outbound negotiation e-mail.

The LLM writes the words; this module decides the number. Before it existed the
agents were handed only a bound ("never below floor_price" / "never above
implied_max") and hugged it: a buyer whose own target was 980 was offered 930,
a supplier who had quoted 760 was offered 802 — and because each bound assumed
the *other* side stayed put, the two openings together cleared ~4% margin
against an 8% floor.

Two rules fix that, and together they guarantee the floor on any pair of prices
the agents can quote:

1. **Never cross the counterpart's own number.** A buyer is never quoted below
   what they offered; a supplier is never offered more than they asked.
2. **Bounds come from what the other side actually committed to.** The buyer
   floor is computed from the supplier's own last quote (not our counter to
   them), the supplier ceiling from the buyer's own last offer.

   buyer price ≥ (S + costs) / (1 − floor)   and supplier price ≤ S
   ⇒ margin ≥ floor, whatever the supplier finally accepts below S
   (symmetrically for the supplier ceiling).

Opening and concession rhythm:

* Buyer, first offer: their target at or above our ideal (target-margin) price →
  meet their target (the most persuasive opening there is); between floor and
  ideal → halfway between their number and the ideal; below floor → the ideal,
  which leaves room to concede down to the floor.
* Supplier, first offer: ``opening_discount`` (default 4%) under their quote,
  never above the ceiling; never more than twice that discount under their
  quote (no insulting low-ball that loses a verified supplier).
* Later rounds: split the difference between our last offer and their latest
  number; the last round goes straight to their number / our limit and is
  flagged as the final offer. Our offers only ever move toward the counterpart.

Price secrecy: the supplier-side figures are derived from the supplier's own
quote and the existing non-invertible ceiling only; the buyer-side figures from
the buyer's own number and supplier-price-plus-costs, never the raw supplier
price.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


def _up(x: float) -> float:
    return math.ceil(round(x * 100, 6)) / 100


def _down(x: float) -> float:
    return math.floor(round(x * 100, 6)) / 100


@dataclass(frozen=True)
class PriceBand:
    """What one outbound e-mail may quote (USD per MT)."""

    low: float
    high: float
    recommended: float
    final_offer: bool
    basis: str
    """Short internal explanation — goes into the round's reasoning, never to a counterparty."""

    def contains(self, price: float, tolerance: float = 0.005) -> bool:
        slack = max(1.0, self.high * tolerance)
        return self.low - slack <= price <= self.high + slack

    def clamp(self, price: float) -> float:
        return min(max(round(price, 2), self.low), self.high)


def _is_last_round(round_no: int, max_rounds: int) -> bool:
    return max_rounds > 0 and round_no >= max_rounds - 1


def buyer_band(
    *,
    buyer_offer: float,
    supplier_price: float,
    costs: float,
    floor_pct: float,
    target_pct: float,
    last_offer_to_buyer: float | None,
    round_no: int,
    max_rounds: int,
) -> PriceBand:
    """Band for an e-mail to the BUYER (we want the price high)."""
    walk = (supplier_price + costs) / max(1.0 - floor_pct / 100.0, 0.01)
    ideal = (supplier_price + costs) / max(1.0 - target_pct / 100.0, 0.01)
    ideal = max(ideal, walk)
    c = buyer_offer
    low = max(walk, c)

    if last_offer_to_buyer:
        high = max(low, last_offer_to_buyer)
        if _is_last_round(round_no, max_rounds):
            rec, final, basis = low, True, "final round: their number or our floor"
        else:
            rec, final, basis = low + (high - low) * 0.5, False, "split the gap between our last offer and theirs"
    else:
        high = max(ideal, c)
        final = False
        if c >= ideal:
            rec, basis = c, "opening: meet the buyer's own target"
        elif c >= walk:
            rec, basis = (c + ideal) / 2.0, "opening: halfway between their target and our ideal"
        else:
            rec, basis = ideal, "opening: their target is below our floor — anchor at the ideal price"

    rec = min(max(rec, low), high)
    # "Final offer" only after real concession rounds — an opening that meets
    # the buyer's own target is good news, not an ultimatum.
    final = final or bool(last_offer_to_buyer and (high - low) <= max(1.0, 0.002 * high))
    return PriceBand(_up(low), _up(high), _up(rec), final, basis)


def supplier_band(
    *,
    supplier_quote: float,
    ceiling: float,
    last_offer_to_supplier: float | None,
    round_no: int,
    max_rounds: int,
    opening_discount_pct: float = 4.0,
) -> PriceBand:
    """Band for an e-mail to the SUPPLIER (we want the price low).

    ``ceiling`` is the existing non-invertible bound (buyer's own offer less
    floor margin and costs) — this function never sees the buyer's price."""
    d = max(0.0, opening_discount_pct) / 100.0
    c = supplier_quote
    high = min(ceiling, c)

    if last_offer_to_supplier:
        low = min(high, last_offer_to_supplier)
        if _is_last_round(round_no, max_rounds):
            rec, final, basis = high, True, "final round: their quote or our ceiling"
        else:
            rec, final, basis = low + (high - low) * 0.5, False, "split the gap between our last offer and their quote"
    else:
        final = False
        rec = min(c * (1.0 - d), ceiling * (1.0 - d / 2.0))
        low = min(rec, c * (1.0 - 2.0 * d))
        if c <= ceiling:
            basis = f"opening: {opening_discount_pct:g}% under their quote, room to move up to it"
        else:
            basis = "opening: their quote is above our ceiling — counter under the ceiling"

    rec = min(max(rec, low), high)
    final = final or bool(last_offer_to_supplier and (high - low) <= max(1.0, 0.002 * max(high, 1.0)))
    return PriceBand(_down(low), _down(high), _down(rec), final, basis)
