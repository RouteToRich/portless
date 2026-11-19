# Operator guide

For the trade desk using the PortLess console. One section per screen: what each number means,
where it comes from, and what to do next. Every number is either **firm** (a written quote, a
PI / PO, a settlement) or an **estimate** — the screen says which.

## Opportunity Radar

**What it is:** product × market corridors worth trading, ranked by expected profit.

| Number | Means | Comes from |
|---|---|---|
| Expected profit | margin per MT × lot × chance of closing | buyer price − landed cost (supplier FOB + freight + inspection + insurance + escrow fee) |
| Buyer price | what the market pays | buyer quote → price book → national monthly statistics / UN Comtrade (the source and month are shown) |
| Supplier price | what an Indian supplier charges | supplier quote → price book → mandi price + markup → India's export average |
| Chance of closing | how likely a deal is | best pair score, verified buyers, the corridor's own signed / failed record |
| Found by | how the opportunity was found | market statistics, national statistics, directory |
| Flags | data warnings | `price_check_needed` (gap too large to be real), `sources_disagree`, quality flags |

**Next step:** *Rescore* checks the best pairs (costs Apify runs); *Start prospect* opens a deal.
Before spending, look at `GET /trade-min/scan/plan` — which corridors are worth a scan today.

## Prospects desk

**What it is:** every open prospect, what it is worth and whose move it is.

- **Waiting on**: us / buyers / buyer / suppliers, with idle days. *Stalled* after
  `PROSPECT_STALL_DAYS` (5); the sourcing agent then asks more suppliers by itself.
- **Next moves** are ranked: confirm a match, send an offer, push the supplier toward the
  walk-away price, best and final, reconfirm an expiring quote, contact buyers, chase quotes.
- **Match board** per prospect: every buyer × supplier pair with margin, chance and blockers. A
  *specification mismatch* blocks a pair when the buyer and supplier name different values of a
  key attribute (variety, size, packing).
- **Supplier steps**: `GET /deals/{id}/supplier-steps` shows each supplier step done by e-mail
  or by your relay, and whether the relay has evidence attached.

**Next step:** work the top move; park a prospect you are waiting on for a known reason; drop one
that is dead (reason required).

## Buyers & Suppliers

**What it is:** the trading book — every company a scan or a person found.

| Field | Means |
|---|---|
| Stage | new → contacted → replied → interested → quoted → in deal → traded (or declined / do not contact) |
| Score | 0–100 with its reasons: check status, usable e-mail, engagement, past deals, QC record, sources, certificates |
| Check | verified / unverified / failed — sanctions, e-mail, GST / IEC; existence evidence (LEI, VIES, CIN) |
| Products | per product what the company wants / supplies, each fact with its source and date; open questions |
| Volume | "Buys ~X MT/yr" when recorded; otherwise a size band, which is only an estimate |

**Next step:** *Add to prospect*; enter what you heard by phone on the company's products
(staff entries are never overwritten); attach a supplier's papers and mark the bank account
verified before its first payout.

## Deal page

**What it is:** one deal from negotiation to PAID.

- **Money**: sell (PI), buy (PO), profit (realised once settled); the escrow ledger shows funded,
  released, fees, payouts and the supplier's balance.
- **QC / freight / escrow status** columns come only from the provider or your relay.
- **E-mails**: every message to and from both sides with its delivery outcome.
- **Claims**: `GET /deals/{id}/claim-options` after a failed QC or complaint — options with what
  each costs PortLess and the supplier.
- **Documents check**: paste the text of a certificate / invoice / draft B/L to compare it with
  the deal (`POST /deals/{id}/documents/check`).

**Never** release money outside its approval; if something looks wrong, pause with the Kill
Switch first (see `RUNBOOKS.md`).
