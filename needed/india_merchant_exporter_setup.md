# Starting an Export–Import Business in India — Merchant Exporter Playbook

> **Scope.** Everything a **merchant exporter** (a trader who buys finished goods from Indian
> manufacturers and exports them without owning a factory) needs in order to legally ship
> internationally from India: entity setup, statutory registrations, product-specific licences,
> per-shipment certificates, FX/payment compliance, and the incentive schemes worth claiming.
>
> **Status.** Working reference for the Portless domain model — *not legal or tax advice*. Fees,
> thresholds and scheme rates change every Foreign Trade Policy amendment and every Union Budget.
> Verify on the official portals (linked throughout) before acting. Last verified: **July 2026**.

---

## 0. Merchant exporter vs. manufacturer exporter

| | Merchant exporter | Manufacturer exporter |
|---|---|---|
| Owns production | ❌ Buys from an Indian manufacturer | ✅ Makes the goods |
| Factory / plant registrations | Not required | Required (factory licence, pollution NOC, etc.) |
| GST on procurement | **0.1% concessional rate** (conditions below) | N/A — pays input GST, claims ITC |
| Duty Drawback | ✅ Eligible | ✅ Eligible |
| RoDTEP | ✅ Eligible | ✅ Eligible |
| Advance Authorisation | ✅ Only with a **supporting manufacturer** named | ✅ Direct |
| EPCG (capital goods) | ✅ Only tied to a supporting manufacturer | ✅ Direct |
| Cargo handling | Can be structured so the merchant never touches cargo (goods move factory → port) | Handles own cargo |

The last row is the whole Portless thesis: the 0.1% GST notification **requires** goods to move
directly from the supplier's premises to the port/ICD/CFS (or to a registered warehouse for
aggregation) — the merchant exporter never takes physical possession. Handling the cargo yourself
*breaks* the concessional-rate eligibility.

---

## 1. Phase 0 — Entity, PAN, bank, GST (weeks 0–3)

### 1.1 Choose the legal entity

| Structure | When it fits | Trade-offs |
|---|---|---|
| **Proprietorship** | Solo start, low volume, fastest setup (days) | Unlimited personal liability; foreign buyers and banks treat it as less credible; hard to raise capital |
| **One Person Company (OPC)** | Solo founder who wants limited liability | MCA compliance overhead; auto-converts to Pvt Ltd above turnover/capital thresholds |
| **LLP** | 2+ partners, service-ish trade, lower compliance than Pvt Ltd | No equity funding; some buyers/banks prefer companies |
| **Private Limited** ✅ | Recommended for a platform/serious export house | Limited liability, best credibility with foreign buyers, banks, ECGC and investors; MCA + audit compliance |

**On "director role?"** — a Pvt Ltd needs a minimum of **2 directors** (at least one resident in India
for ≥182 days) and 2 shareholders. Each director needs a **DIN** (Director Identification Number) and
a **Class 3 DSC**. An OPC needs 1 director + 1 nominee. The director signing DGFT/ICEGATE filings must
be the one whose DSC is registered on those portals.

**Artifacts produced:** Certificate of Incorporation, MoA/AoA, PAN, TAN, DIN(s), Class 3 DSC(s).

### 1.2 Registrations that follow entity formation

| # | Registration | Issuing body | Mandatory? | Notes |
|---|---|---|---|---|
| 1 | **PAN** (entity-level) | Income Tax Dept | ✅ Yes | IEC is now issued *against* PAN — one IEC per PAN |
| 2 | **TAN** | Income Tax Dept | ✅ If deducting TDS | Auto with company incorporation (SPICe+) |
| 3 | **Current account with an AD Category-I bank** | Any RBI-authorised dealer bank | ✅ Yes | This bank issues your **AD Code** (§2.2). Pick a bank with a real trade-finance desk — it matters later for LC/BRC/ECGC |
| 4 | **GST registration (GSTIN)** | GSTN | ✅ Practically yes | Exports are *zero-rated inter-state supplies*. Threshold exemption is theoretically available but you cannot file LUT, claim refunds, or use the 0.1% merchant-export rate without a GSTIN |
| 5 | **Udyam (MSME) registration** | Ministry of MSME | ⚠️ Optional, strongly advised | Free. Unlocks collateral-free credit (CGTMSE), MSME interest schemes, 45-day payment protection, EPC fee concessions |
| 6 | **Shops & Establishment / trade licence** | State/municipal | ⚠️ State-dependent | Also professional tax registration in states that levy it |
| 7 | **Class 3 Digital Signature Certificate (DSC)** | Licensed CA (eMudhra, Sify, etc.) | ✅ For companies/LLPs | Required for DGFT, ICEGATE, GST and MCA filings. Proprietors can often use Aadhaar e-Sign instead |
| 8 | **EEFC account** (Exchange Earners' Foreign Currency) | Same AD bank | ⚠️ Optional | Hold export proceeds in USD/EUR without forced conversion — useful FX hedge |

---

## 2. Phase 1 — Core mandatory export registrations (weeks 2–5)

These four are the non-negotiable spine. Without all four you **cannot file a shipping bill**.

### 2.1 IEC — Importer Exporter Code ⭐ the licence

| Field | Detail |
|---|---|
| Issued by | **DGFT** (Directorate General of Foreign Trade), Ministry of Commerce |
| Portal | [dgft.gov.in](https://www.dgft.gov.in) — Services → IEC → Apply for IEC (Form **ANF-2A**) |
| Fee | **₹500** government fee (one-time). Agents charge ₹1,500–₹6,000 on top — the self-service flow is genuinely easy |
| Turnaround | **1–3 working days**, fully online |
| Validity | **Lifetime** — but see the annual-update trap below |
| Format | 10-digit code, now identical to your PAN |

**Documents:** entity PAN · Aadhaar / passport of proprietor or authorised signatory · address proof
of the principal place of business (electricity bill, rent agreement + NOC, sale deed) · **cancelled
cheque or banker's certificate** in the prescribed format · Certificate of Incorporation (for
companies/LLPs) · Class 3 DSC or Aadhaar e-Sign.

> ⚠️ **Annual update trap.** Every IEC must be **electronically updated/confirmed between April and
> June each year**, even when nothing has changed. Miss it and DGFT **deactivates** the IEC — your
> shipping bills stop clearing until you reactivate. Put this on a recurring calendar.

One IEC per PAN. Only one is needed for **both** import and export.

### 2.2 AD Code — Authorised Dealer Code + ICEGATE registration

A **14-digit** code your bank issues, tying your IEC ↔ bank account ↔ customs records so that foreign
inward remittances auto-route to the right account and shipping bills can be generated.

1. Request the AD Code letter from your AD Category-I bank on **bank letterhead** (many banks have a
   standard format; ask for the "AD Code letter for customs registration").
2. Register it on **[ICEGATE](https://www.icegate.gov.in)** → *Bank Account Management* →
   *Authorized Dealer Code Registration* (or have your **CHA** do it at the port).
3. **Turnaround:** 1–7 working days depending on the port. **Validity:** lifetime, no renewal.

> CBIC has moved toward a **single PAN-based registration** rather than per-port registration — one
> registration is generally sufficient across ports now. Confirm with your CHA for your actual port of
> export, since practice still varies by Customs house.

**Also register on ICEGATE as an exporter** (separate from AD Code registration). You'll need the
login to track shipping bills, view your **RoDTEP e-scrip ledger**, make e-payments, and file
IGCR/bond documents on the import side.

### 2.3 RCMC — Registration-Cum-Membership Certificate

Issued by the **Export Promotion Council (EPC)** / Commodity Board / Development Authority relevant to
your product line.

| Field | Detail |
|---|---|
| Portal | **e-RCMC on the DGFT Common Digital Platform** — [dgft.gov.in/CP/?opt=e-rcmc](https://www.dgft.gov.in/CP/?opt=e-rcmc). Mandatory electronic filing since **01.04.2022** (Trade Notice 35/2021-22) |
| Prerequisite | IEC must be issued and **linked/updated** on the DGFT portal first |
| Validity | **5 financial years** |
| Fee | Set by each council — typically ₹5,000–₹25,000 + GST; MSME/Udyam concessions common |

**Which council?** Pick by product:

| Product line | Council / Board |
|---|---|
| Anything with no dedicated council (**default for most merchant exporters**) | **FIEO** — Federation of Indian Export Organisations |
| Agri & processed food, cereals, fresh produce, meat | **APEDA** |
| Spices (52 notified varieties) | **Spices Board** |
| Tea / Coffee / Rubber / Tobacco | Respective statutory Boards |
| Marine products | **MPEDA** |
| Engineering goods | **EEPC India** |
| Chemicals, pharma | CHEMEXCIL / PHARMEXCIL |
| Textiles, apparel, cotton | AEPC / TEXPROCIL / SRTEPC |
| Handicrafts | **EPCH** |
| Gems & jewellery | **GJEPC** |
| Leather | CLE |
| Plastics | PLEXCONCIL |
| Electronics & IT | ESC |

> 📌 **Two reasons you still want an RCMC even where it's optional.** DGFT has relaxed RCMC as a
> blanket precondition for *some* FTP benefits (clarified October 2024), but: (a) the **GST 0.1%
> merchant-export concession explicitly requires** registration with an EPC or Commodity Board, and
> (b) councils provide buyer leads, trade-fair subsidies, market-access grants and the credibility
> foreign buyers check. For a merchant exporter, treat RCMC as mandatory.

### 2.4 LUT — Letter of Undertaking (GST RFD-11)

Lets you export **without paying IGST upfront** instead of paying and claiming a refund. This is pure
working-capital preservation — with LUT you never park cash with the department.

| Field | Detail |
|---|---|
| Form | **GST RFD-11**, filed on the GST portal |
| Frequency | **Once per financial year** — file fresh every April |
| Bank guarantee | Not required for most exporters (only for those prosecuted for tax evasion > ₹250 lakh) |
| Alternative | Export **on payment of IGST** and claim refund — simpler paperwork, worse cash flow |

---

## 3. Phase 2 — Product- and destination-specific licences

### 3.1 First: classify the product and check the export policy

1. Find the **8-digit ITC(HS) code** for your product. India uses 8 digits (the first 6 are the
   global WCO HS code; digits 7–8 are the Indian national sub-heading).
2. Look it up in **ITC(HS) Schedule 2 (Export Policy)** on the DGFT site. Every line is one of:

| Policy | Meaning |
|---|---|
| **Free** | Export freely — no licence beyond IEC |
| **Restricted** | Needs a **specific export authorisation** from DGFT per shipment/period |
| **Prohibited** | Cannot be exported at all |
| **STE** | Exportable only through a designated **State Trading Enterprise** (e.g. certain minerals) |

3. Check **ITC(HS) Schedule 1 (Import Policy)** for anything you plan to import.
4. Check the **destination country's** import rules and tariffs — MacMap, the destination customs
   site, and any **FTA/PTA** India has with that country (which decides whether a *preferential*
   Certificate of Origin is worth obtaining).

**Useful data sources:** [tradestat.commerce.gov.in](https://tradestat.commerce.gov.in) (DGCI&S export
statistics — what actually ships, from where, to where), DGFT's **District Export Action Plans** (what
your own district exports), and your EPC's product-market briefs.

### 3.2 Product-specific registrations

| Product category | Registration / licence | Body |
|---|---|---|
| **Any food product** (domestic handling, storage, labelling) | **FSSAI licence** — Central licence for exporters | FSSAI |
| Agri & processed food, fruits, vegetables, cereals, meat, dairy | **APEDA registration** (also serves as RCMC) | APEDA |
| **Spices** (any of the 52 notified varieties) | **CRES** — Certificate of Registration as Exporter of Spices; ~₹5,000 new registration. FSSAI licence is a supporting document | Spices Board |
| Marine / fishery | MPEDA registration + **approved processing plant** | MPEDA / EIC |
| Basmati rice, honey, fish, black pepper and other notified items | **Compulsory pre-shipment inspection** — Export Inspection Council / Export Inspection Agency certificate | EIC / EIA |
| Drugs, cosmetics, medical devices | CDSCO registration + **COPP / WHO-GMP**; Free Sale Certificate | CDSCO |
| Textiles / apparel | AEPC registration; buyer-driven audits (SEDEX, WRAP, GOTS, OEKO-TEX) | AEPC + private |
| Wooden packaging, plants, plant products | **Phytosanitary certificate** + **ISPM-15** treated/fumigated wood stamp | Plant Quarantine, Ministry of Agriculture |
| Hazardous / chemicals / batteries | **MSDS** + DG declaration + IATA/IMDG classification | Shipper + carrier |
| Packaged commodities | **Legal Metrology** packaged-commodity compliance (declarations on the pack) | Legal Metrology Dept |
| Antiques, wildlife, arms, narcotics, certain minerals | Restricted/prohibited — special authorisation or outright ban | DGFT / respective ministry |

### 3.3 Buyer- or market-driven certifications (not statutory, but deal-blocking)

**ISO 9001** · **ISO 22000 / HACCP / BRC / FSSC 22000** (food) · **Halal** (Gulf, Malaysia, Indonesia)
· **Kosher** · **CE marking** (EU) · **FDA registration + FSVP** (US food) · **REACH** (EU chemicals)
· **GOTS / OEKO-TEX** (textiles) · **Fair Trade** · **organic — NPOP/NOP** certification.

These are commercial requirements, not Indian law — but a buyer's RFQ will list them and no
negotiation closes without them. Capture them as **deal-level requirements at RFQ time**, not at
shipment time.

---

## 4. Phase 3 — Optional, high-leverage registrations

| Registration | Why it matters | Body |
|---|---|---|
| **ECGC policy** (Export Credit Insurance) | Answers *"what if payment is never received?"* — covers commercial default (buyer insolvency, protracted default) **and political risk** (war, exchange restrictions, import-ban). Also enables banks to lend against your export receivables. The Shipment/Turnover policy is the standard product. **Take this before your first unsecured (D/A or open-account) shipment** | ECGC Ltd |
| **AEO certification** (T1/T2/T3) | CBIC's *Authorised Economic Operator* status — faster customs clearance, lower inspection rates, deferred duty, priority treatment. **Free** to apply. T1 is achievable by a small exporter with ~2 years of clean records | CBIC |
| **Star Export House** status (1★–5★) | Awarded on export performance over 3 years; brings self-certification of origin, reduced bank guarantees, priority clearance | DGFT |
| **indiausatrade.mea.gov.in enrolment** | MEA's India–US trade facilitation portal — market intel and buyer connects for the US corridor | MEA |
| **Dak Ghar Niryat Kendra (DNK)** enrolment | Post-office export route (§6.4) for small consignments — very relevant for low-value/sample shipments | India Post |

---

## 5. GST mechanics specific to merchant exporters

### 5.1 The 0.1% concessional procurement rate

Under **Notification 40/2017-CT(Rate)** and **41/2017-IT(Rate)**, a registered supplier may charge a
merchant exporter **0.1% GST** (0.05% CGST + 0.05% SGST, or 0.1% IGST) instead of the normal rate.
**All** of these conditions must hold:

1. Supplier issues a **tax invoice** at the 0.1% rate.
2. Merchant exporter **exports within 90 days** of the supplier's tax invoice date.
3. Merchant exporter is **registered with an EPC or Commodity Board** (→ your RCMC).
4. The merchant exporter's **GSTIN and the supplier's tax invoice number** are stated on the
   **shipping bill / bill of export**.
5. Merchant exporter places the **purchase order on the supplier** and sends a copy to the supplier's
   **jurisdictional GST officer**.
6. Goods move **directly from the supplier's place of business to the port/ICD/CFS/airport** — or to a
   **registered warehouse** for aggregation with other suppliers' goods, then to the port.
7. Merchant exporter provides a **copy of the shipping bill + EGM** back to the supplier and their
   jurisdictional officer after export.

Miss condition 2 or 6 and the supplier becomes liable for the differential tax with interest — which
is why supplier contracts should make the 90-day export and direct-movement obligations explicit.

### 5.2 Refunds

| Route | What you get back |
|---|---|
| Export **under LUT** (no IGST paid) + goods procured at 0.1% | Refund of **unutilised ITC** under the zero-rated-supply route |
| Export **on payment of IGST** | Refund of the **IGST paid**, auto-processed off the shipping bill (the shipping bill *is* the refund application) once GSTR-1 and GSTR-3B match |

> ⚠️ A merchant exporter using the 0.1% rate accumulates almost no ITC by design, so the refund is
> small — the benefit is that you never blocked the cash in the first place.

---

## 6. Per-shipment documents & certificates

### 6.1 Commercial set (you/your platform generate)

| Document | Purpose |
|---|---|
| **Proforma Invoice (PI)** | Pre-contract quote to the buyer; basis for the buyer's LC application and advance payment |
| **Purchase Order (PO)** | Your order on the Indian supplier (also a condition for the 0.1% GST rate) |
| **Commercial Invoice (CI)** | The definitive priced document for customs and the bank |
| **Packing List (PL)** | Carton/pallet-level breakdown; weights, dimensions, marks & numbers |
| **Sales contract / Export agreement** | Governing terms, Incoterm, dispute forum, governing law |

### 6.2 Statutory / regulatory set

| Document | Issued by |
|---|---|
| **Shipping Bill** (electronic, via ICEGATE) | Customs — the core export document; nothing clears without it |
| **SDF declaration** (Statutory Declaration Form) | Exporter — declares realisation of proceeds; now embedded in the electronic shipping bill |
| **Certificate of Origin (CoO)** | **Preferential** (for FTA benefit) via the DGFT CoO Common Digital Platform → [coo.dgft.gov.in](https://coo.dgft.gov.in); **Non-preferential** via chambers of commerce / EPCs |
| **Bill of Lading (sea) / Airway Bill (air)** | Carrier or freight forwarder — the title/transport document |
| **Marine Insurance Certificate** | Insurer — mandatory under CIF/CIP terms |
| **Phytosanitary Certificate** | Plant Quarantine (agri/plant goods) |
| **Fumigation Certificate + ISPM-15 stamp** | Approved fumigation agency (wooden packaging) |
| **Health / Sanitary Certificate** | EIC/EIA (food, marine) |
| **Certificate of Analysis / Test Report** | Accredited lab (chemicals, food, pharma) |
| **Pre-shipment Inspection Certificate** | Third-party QC — SGS, QIMA, Bureau Veritas, Intertek, TÜV SÜD (buyer usually names the inspector) |
| **Let Export Order (LEO)** | Customs — final permission to load; issued after assessment on ICEGATE |
| **EGM (Export General Manifest)** | Carrier — proves goods actually left India; triggers IGST refund and RoDTEP |

### 6.3 Banking set

**LC (if applicable)** · **Bill of Exchange / Draft** · **Bank's covering schedule** ·
**e-BRC** (§7.2).

### 6.4 Alternate export channels for small consignments

| Channel | Document | Fits |
|---|---|---|
| **Dak Ghar Niryat Kendra (DNK)** — India Post | **PBE** (Postal Bill of Export) | Samples, low-value e-commerce parcels; ~250+ DNK counters nationwide; RoDTEP/drawback claimable on PBE |
| **Courier mode** (DHL/FedEx/UPS) | **CSB-IV** (non-commercial/free-trade samples) or **CSB-V** (commercial e-commerce exports, allows incentive claims) | Fast small parcels; value caps apply |
| **Regular cargo** | Shipping Bill via CHA | Everything else |

---

## 7. Payment, FX and realisation compliance

### 7.1 Payment terms, ranked by exporter risk

| Term | Risk to exporter | Notes |
|---|---|---|
| **Advance payment (T/T)** | 🟢 Lowest | Ideal for a first-time buyer. Common structure: 30% advance + 70% against BL copy |
| **Confirmed irrevocable LC** | 🟢 Low | Bank guarantees payment on compliant documents. *Confirmed* = an Indian bank also guarantees, removing issuing-bank/country risk. Discrepancy management is the whole game |
| **Unconfirmed LC** | 🟡 Medium | Carries issuing-bank and country risk |
| **D/P — Documents against Payment** | 🟡 Medium | Bank releases title documents only on payment. Buyer can still abandon the cargo at destination |
| **D/A — Documents against Acceptance** | 🔴 High | Buyer gets the documents on *accepting* a time draft (30/60/90 days) and pays later. **Take ECGC cover** |
| **Open account** | 🔴 Highest | Ship first, invoice later. Only for long-standing buyers, and only with ECGC cover |

**Incoterms 2020** most relevant here: **EXW** (buyer collects at supplier's gate) · **FOB** (you
deliver on board at the Indian port; buyer pays freight) · **CFR** (you pay freight) · **CIF** (you
pay freight + insurance to destination port) · **CIP/DAP/DDP** (progressively more of the destination
leg is yours). For a merchant exporter starting out, **FOB or CIF** are the workhorses — DDP exposes
you to foreign customs duty and local tax registration.

### 7.2 e-BRC — Electronic Bank Realisation Certificate

Proof that export proceeds were actually received. Since the DGFT revamp, the flow is **self-service**:
the exporter **links the inward remittance (IRM) to the shipping bill** on the DGFT portal and
self-certifies the e-BRC, rather than waiting for the bank to issue it.

> ⚠️ **No self-certified e-BRC on the DGFT portal → no RoDTEP, no Duty Drawback, no Advance
> Authorisation closure.** This is the single most commonly missed step after a shipment lands.

### 7.3 FEMA realisation timeline

Export proceeds must be realised and repatriated to India within the **RBI-prescribed period —
currently 9 months** from the date of export (extended from 6 months and retained since). Overdue
bills show up in the **EDPMS** (Export Data Processing and Monitoring System) and your AD bank will
chase you; persistent defaults can get your IEC flagged. Where proceeds are short-realised, **CBIC
Circular 20/2026-Customs (10 April 2026)** clarifies that RoDTEP/RoSCTL benefits may still be allowed
in defined cases — including where the shortfall is compensated by **ECGC** — subject to RBI
write-off compliance.

---

## 8. Incentive schemes worth claiming

| Scheme | What it gives | Merchant exporter eligible? |
|---|---|---|
| **RoDTEP** (Remission of Duties and Taxes on Exported Products) | Rebate of embedded central/state/local duties not otherwise refunded. Credited as a **transferable e-scrip** in your ICEGATE ledger; usable against basic customs duty or sellable. Rate is per 8-digit HS code | ✅ Yes — claim by ticking RoDTEP on the shipping bill |
| **Duty Drawback** | Refund of customs duty on imported inputs; **AIR** (All Industry Rate) is the simple route | ✅ Yes |
| **Advance Authorisation** | Duty-free import of inputs against an export obligation | ✅ With a named supporting manufacturer |
| **EPCG** | Duty-free capital-goods import against an export obligation | ✅ Tied to a supporting manufacturer |
| **Market Access Initiative (MAI)** | Subsidised trade-fair participation, buyer-seller meets, market studies — routed via your EPC | ✅ Via RCMC |
| **Transport & Marketing Assistance (TMA)** | Freight subsidy for specified agri products | ✅ For eligible agri lines |
| **Interest Equalisation Scheme** | Interest subvention on pre/post-shipment rupee export credit | ⚠️ **Lapsed in 2024** — check current status before budgeting for it |

> RoDTEP and Drawback **cannot both** be claimed on the same input duty component — the shipping bill
> declaration decides which. Claim both only where the schemes cover different duty elements.

---

## 9. Import side (the "import" half of Export-Import)

Your IEC already covers imports. Additionally:

| Item | Detail |
|---|---|
| **Bill of Entry** | The import counterpart of the shipping bill; filed on ICEGATE |
| **Customs duty** | BCD + Social Welfare Surcharge + **IGST** (creditable as ITC) + any anti-dumping/safeguard duty |
| **ITC(HS) Schedule 1** | Check whether the item is Free / Restricted / Prohibited / STE on import |
| **BIS certification** | Mandatory for goods under the **Compulsory Registration Order** (electronics, steel, toys, etc.) — the *foreign manufacturer* must hold the BIS licence |
| **FSSAI import licence** | Separate Central licence endorsed for import, for any food import |
| **CDSCO import licence** | Drugs, cosmetics, medical devices |
| **Legal Metrology importer registration** | For pre-packaged commodities |
| **IGCR** (Import of Goods at Concessional Rate) | Register on ICEGATE + execute a continuity bond to import inputs at concessional duty |
| **Customs bond / bank guarantee** | For warehousing, provisional assessment, EPCG/AA obligations |

---

## 10. Timeline and indicative cost

| Phase | Step | Typical time | Indicative cost (₹) |
|---|---|---|---|
| 0 | Pvt Ltd incorporation (SPICe+, PAN, TAN, DIN, DSC) | 7–15 days | 8,000 – 25,000 |
| 0 | Current account with AD Category-I bank | 3–7 days | 0 (min. balance applies) |
| 0 | GST registration | 7–15 days | 0 (self) / 2,000–5,000 |
| 0 | Udyam (MSME) | Same day | **0** |
| 1 | **IEC** | **1–3 days** | **500** (govt) |
| 1 | AD Code letter + ICEGATE registration | 1–7 days | 0 – 3,000 (CHA fee) |
| 1 | **RCMC** (FIEO or product council) | 3–15 days | 5,000 – 25,000 + GST |
| 1 | LUT (GST RFD-11) | Same day | **0** |
| 2 | Product licence (FSSAI / APEDA / Spices CRES …) | 15–60 days | 5,000 – 25,000 |
| 3 | ECGC policy | 7–21 days | Premium ~0.05–0.5% of insured turnover |
| 3 | AEO T1 | 30–60 days | **0** (govt fee) |

**Realistic total to first legal shipment: 4–8 weeks; ₹25,000 – ₹75,000** for a Pvt Ltd merchant
exporter on a Free-policy product line. A proprietorship on a Free-policy product can be shipping in
**~2 weeks for under ₹10,000**.

---

## 11. Ongoing compliance calendar

| Cadence | Obligation |
|---|---|
| **Per shipment** | Shipping bill + LEO + EGM; RoDTEP/Drawback declaration; CoO; link IRM → e-BRC after remittance |
| **Monthly** | GSTR-1 and GSTR-3B (export invoices flagged with shipping bill no. + date — a mismatch here is the #1 cause of stuck IGST refunds); TDS payment |
| **Quarterly** | Advance tax; TDS returns |
| **Annually — April** | **File fresh LUT** for the new financial year |
| **Annually — April to June** | ⭐ **DGFT IEC update/confirmation** — non-negotiable, or the IEC is deactivated |
| **Annually** | GSTR-9 / 9C; income tax return; MCA AOC-4 + MGT-7; statutory audit |
| **Every 5 years** | RCMC renewal |
| **Rolling** | EDPMS overdue-bill clearance; realisation within 9 months of export |

---

## 12. Mapping to the Portless platform

How each real-world artifact above maps onto modules in this repo:

| Real-world artifact | Portless module | Notes |
|---|---|---|
| GSTIN, IEC/DGFT, MCA, Udyam verification of supplier & buyer | `services/sourcing` | Already wired — parallel verification in the quote-intake flow |
| ITC(HS) 8-digit classification, destination tariffs, trade stats | `services/trade_min` + `shared/connectors/trade_intel.py`, `regulatory.py` | Comtrade / MacMap / tradestat feeds |
| Proforma Invoice, Purchase Order | `services/dealmaker` | PI/PO issuance + margin guard (≥8%) |
| Commercial Invoice, Packing List, CoO, SDF, Shipping Bill | `services/docugen` | Templates already implemented; **ICEGATE** filing via connector |
| Pre-shipment inspection certificate | `services/shield_link` | Buyer-selectable QC: SGS, QIMA, Bureau Veritas, Intertek, TÜV SÜD |
| Freight booking + BL/AWB, CHA/customs filing | `services/shield_link` | Cogoport / Freightos / ICEGATE connectors |
| Escrow, advance/LC-equivalent milestones, supplier payout | `services/escrow_guard` | Stripe hold + Payoneer payout |
| QC cost, doc cost, freight cost in price build-up | `agents/negotiation_agent` cost envelope | QC provider's quoted cost feeds `cost_envelope.qc_cost` |
| Approval gates on PO / freight / payout / customs filing | `services/hitl_approval` | L0–L3 autonomy; saga callbacks |

**Gaps worth modelling next** (present in the real-world flow, not yet in the platform):

- **RCMC / EPC membership** as an exporter-org attribute — it is a *precondition* for the 0.1% GST rate.
- **The 90-day export clock** from supplier tax-invoice date — a hard deadline that should raise a
  `deal.exception` well before it lapses.
- **e-BRC linking** after remittance — currently no module owns the "link IRM → shipping bill → claim
  RoDTEP" step, and missing it silently forfeits the incentive.
- **ECGC cover status** per deal — should gate whether D/A or open-account terms are even offerable
  during negotiation.
- **IEC annual-update (Apr–Jun) reminder** — a deactivated IEC halts every shipment; cheap to monitor,
  expensive to miss.
- **Direct-movement constraint** (supplier premises → port, no merchant possession) — should be an
  invariant asserted in the freight-booking flow, since violating it breaks the 0.1% concession.

---

## Sources

- [IEC Registration in India 2026 — DGFT process, documents, fees (Vakilsearch)](https://vakilsearch.com/article/iec-import-export-code-registration-india-2026/)
- [Import Export License Fees in India — cost breakdown 2026 (RegisterKaro)](https://www.registerkaro.in/post/import-export-license-fees)
- [DGFT Registration & IEC Code — step-by-step guide 2026 (Eximly)](https://www.eximly.in/blog/complete-guide-dgft-registration-iec-code-2026)
- [DGFT e-RCMC Common Digital Platform](https://www.dgft.gov.in/CP/?opt=e-rcmc) · [e-RCMC User Guide for Exporters (PDF)](https://content.dgft.gov.in/Website/e-RCMC%20User%20Guide%20for%20Exporters.pdf)
- [RCMC online registration with DGFT (IndiaFilings)](https://www.indiafilings.com/learn/rcmc-2)
- [Registration certificate not mandatory for exporters seeking benefits: DGFT (Business Standard)](https://www.business-standard.com/industry/news/registration-certificate-not-mandatory-for-exporters-seeking-benefits-dgft-124100801238_1.html)
- [AD Code registration on ICEGATE — documents & process 2026 (JustStart)](https://juststart.co.in/blog/ad-code-registration-icegate/)
- [IEC and AD Code registration — complete guide for exporters in India (On Pattison, Jan 2026)](https://onpattison.com/news/2026/jan/26/iec-and-ad-code-registration-complete-guide-for-exporters-in-india/)
- [All about merchant exports under GST (ClearTax)](https://cleartax.in/s/merchant-exports-gst)
- [Merchant exporter under GST — benefits, documents & rules (Shiprocket X)](https://www.shiprocket.in/blog/merchant-exporter-under-gst/)
- [Merchant exporter status (RCMC) & concessional GST rate (SetIndiaBiz)](https://www.setindiabiz.com/merchant-exporter-gst)
- [RoDTEP Scheme 2026 — rates & claim guide (Afleo)](https://afleo.com/rodtep-scheme/)
- [Bank Realisation Certificate (BRC) — meaning and importance (Bajaj Finserv)](https://www.bajajfinserv.in/bank-realisation-certificate-brc)
- [CBIC Circular clarifying RoDTEP/RoSCTL eligibility on short realisation of export proceeds (A2Z Taxcorp)](https://a2ztaxcorp.net/cbic-issues-circular-clarifying-eligibility-of-rodtep-and-rosctl-benefits-in-cases-of-short-realisation-of-export-proceeds/)
- [How to start an export business in India — registration to first shipment 2026 (IncorpX)](https://www.incorpx.io/blog/export-business-start-guide-india)
- [APEDA registration for exporters — 2026 guide (Agile Regulatory)](https://www.agileregulatory.com/blogs/apeda-registration-complete-guide-for-exporters-in-india-2026)
- [Spice Board registration (CRES) for exporters — process & benefits (IncorpX)](https://www.incorpx.io/blog/spice-board-registration-exporters-india)
- [FSSAI licence for exporters in India (Agile Regulatory)](https://www.agileregulatory.com/blogs/fssai-license-for-exporters)

**Official portals:** [dgft.gov.in](https://www.dgft.gov.in) · [icegate.gov.in](https://www.icegate.gov.in) ·
[gst.gov.in](https://www.gst.gov.in) · [coo.dgft.gov.in](https://coo.dgft.gov.in) ·
[tradestat.commerce.gov.in](https://tradestat.commerce.gov.in) · [ecgc.in](https://www.ecgc.in) ·
[fieo.org](https://www.fieo.org) · [indiausatrade.mea.gov.in](https://indiausatrade.mea.gov.in)
