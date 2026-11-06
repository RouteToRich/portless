# Profit gaps — opportunity → negotiation

Product / business review of PortLess up to the end of the negotiation phase (Radar →
prospect → buyer outreach → supplier RFQ → match board → negotiation). Later phases (PI,
escrow, PO, QC, freight, customs, payout) will be reviewed separately.

Each gap is ranked by its expected effect on **margin per deal × number of deals**.
Points about Indian export rules (incentives, GST) must be checked per product before
being relied on. Figures marked "typically" are rules of thumb, not measured results.

Status: suggestions — nothing below is implemented yet.

---

## Top 6 — biggest gain for the least work

| # | Gap | Why it costs money today | Suggestion |
|---|---|---|---|
| 1 | **Landed cost leaves out real costs** | Only FOB, freight, inspection, insurance and the escrow fee are counted. Customs broker fees, origin port handling, documentation and certificate fees, bank charges, courier, and the cost of money tied up are missing — and so is the cost of each prospect (Apify runs, LLM calls, operator time). The margin looks bigger than it is; small lots can lose money while showing "8 %". | Add fixed costs per shipment and per prospect to `shared/orchestration/landed_cost.py`, and a **minimum profit per deal in dollars** (e.g. ≥ $1,500) next to the 8 % floor. |
| 2 | **Export incentives are not in the margin** | A merchant exporter can often claim RoDTEP (a percentage of FOB) or duty drawback, and buy at the reduced 0.1 % GST rate for merchant exports instead of paying full GST and waiting for a refund. None of it is counted, so offers are priced too cautiously or money is left unclaimed. | Add an incentive line per HS code to the margin calculation and to the PI / PO paperwork (check eligibility per product). |
| 3 | **Suppliers do not compete** | Five suppliers quote once and the best quote is taken as it is. | The supplier agent uses competing quotes as leverage ("we hold a verified quote at X — can you improve?", without names), plus a best-and-final round. Typically 2–5 % off the buying price. |
| 4 | **One side's acceptance can trigger the agreement** | An outright acceptance from either the buyer or the supplier can open `CONFIRM_NEGOTIATION_AGREEMENT`. That risks committing to a supplier without the buyer's firm yes. | Open the agreement gate only when **the buyer has accepted** and the supplier's price is quoted and still valid. |
| 5 | **We anchor low** | The indicative offer starts at the minimum + 3 % (or the market median). Buyers accepting at once means money was left on the table. The acceptance-based nudge needs 5 responses and is global, not per market. | Start at the high end of the market range and concede in shrinking steps. Learn the nudge per market and buyer segment, and treat an instant acceptance as a sign the price was too low. |
| 6 | **E-mail deliverability** | Cold outreach from a personal Gmail risks the spam folder or a suspended account, and Gmail caps daily sending. | A business domain with SPF / DKIM / DMARC, a sending-volume warm-up, and reply tracking. Every other number depends on e-mails arriving. |

---

## What to trade (Radar / opportunity selection)

| Gap | Suggestion |
|---|---|
| The Radar ranks by price-gap percentage on country averages | Rank by **expected profit**: margin after full landed cost × realistic volume × chance of closing (price book + landed cost make this possible now). |
| No price for the buyer's alternative | Add Comtrade unit values of **competing origins** into the same market (e.g. Vietnamese / Brazilian pepper into Germany) — the buyer's real price ceiling. |
| Standard (MFN) duty only | Use **free-trade-agreement** rates (e.g. India–UAE CEPA): a lower duty for the buyer lets PortLess charge more; the preferential certificate of origin goes into the document pack. |
| HS-code averages mix grades | Grades per catalogue line (crude vs refined, ASTA grades) with a price book per grade. |
| No compliance check before outreach | A checklist per product × market: export policy, APEDA / Spices Board registration, phytosanitary certificate, EU pesticide residue limits (MRL) for spices. A rejected shipment costs more than a lost deal. |
| Exchange rate | The buyer pays USD, the supplier is paid INR. Lock or buffer the rate at agreement, or ask suppliers to quote in USD. |

## Buyer side — higher price, more wins, repeat orders

| Gap | Suggestion |
|---|---|
| Buyers ranked only by the search actor's fit score | Rank by import volume and regularity (bill-of-lading data) — a regular importer is worth many deals. |
| One deal per prospect, no account management | Repeat-order potential, reorder reminders on the buyer's buying cycle, a customer list with lifetime value. |
| One introduction template, one follow-up | A/B test templates per market and segment, a second channel (LinkedIn / WhatsApp), reply rate tracked in the outcome data. |
| No pricing by segment or volume | Premium for pharma / USP grades over technical, volume tiers, an urgency premium for short delivery windows. |
| No sample / trial-order step | Many buyers ask for a sample first; a paid-sample step stops those deals dying silently. |
| Escrow is the only payment option | Offer a letter of credit at sight as an alternative for importers who refuse escrow, with its cost priced in. |

## Supplier side — lower buying price

| Gap | Suggestion |
|---|---|
| Traders and manufacturers treated the same | Identify manufacturers (Udyam registration, factory address, manufacturing activity code) and prefer them — traders add their own margin. |
| No combined buying | When several prospects need the same HS code, send one combined RFQ for the bigger volume (cross-prospect offers already exist). |
| Payment terms not used as a lever | Offer faster payment (from the first escrow tranche) in exchange for a discount. |
| Quotes go stale | Ask for a fresh quote before the PI when the price book shows the market moved. |

## Matching and negotiation

| Gap | Suggestion |
|---|---|
| Margin floor is a fixed 8 % | Vary it by risk: higher for a new buyer, a risky country or a small lot. |
| Only price is negotiated | Trade other terms instead of price: payment terms, lead time, packaging, incoterm. |
| Backup buyers not used as leverage | When a second buyer is interested at a higher price, the buyer agent can say so (softly), and the match board can switch pairs. |
| Quantity taken exactly as asked | Optimise to a full container (e.g. 22 MT instead of 20) so fixed freight and inspection costs spread over more tonnes. |
| Freight is a regional estimate | Use live freight quotes (Freightos / Cogoport connectors exist) in the landed cost, and add a **freight markup** on CIF deals — PortLess books the freight, so it can earn on it. |

---

## Numbers to track (mostly not tracked yet)

- **Pipeline per corridor:** opportunities scanned → prospects → buyers interested → quotes → matches → signed, and the time spent at each stage.
- **Cost per prospect** (Apify, LLM, operator time) and **profit per corridor**, so loss-making corridors are stopped early.
- **Pricing quality:** realised margin vs predicted, how often the first price was given away too cheaply (instant acceptances), supplier discount gained per extra round.

## Suggested order

1. **Make the margin honest** — no deal secretly loses money: #1 full landed cost, #2 incentives, #4 stricter agreement trigger.
2. **Earn more per deal:** #3 competing supplier quotes, #5 higher anchoring, freight markup, container fill.
3. **Win more deals:** #6 deliverability, buyer ranking by volume, template testing, samples, LC option.
4. **Repeat business and smarter choices:** account management, Radar ranked by expected profit, competing origins, free-trade rates.
