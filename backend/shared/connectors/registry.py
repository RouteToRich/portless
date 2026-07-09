"""Central registry mapping connector names to their adapter classes.

Workers and the agent runtime resolve providers by name through this registry,
so adding a new integration is a one-line change here.
"""
from __future__ import annotations

from .base import Connector
from .demand_signals import AlibabaRfqConnector, EuTedConnector, GdeltDisruptionConnector
from .escrow import EscrowComConnector, TazapayEscrowConnector
from .payments import PayoneerConnector, StripeTreasuryConnector
from .regulatory import (
    AllianzTradeConnector,
    DgftConnector,
    GstnConnector,
    McaConnector,
    UdyamConnector,
    ViesConnector,
)
from .shields import (
    CogoportConnector,
    FreightosConnector,
    IcegateConnector,
    SgsConnector,
)
from .trade_intel import (
    Access2MarketsConnector,
    CensusTradeConnector,
    ComtradeConnector,
    ImportGeniusConnector,
    IndiamartConnector,
    MacmapConnector,
    TradeIndiaConnector,
    TrademoConnector,
    VolzaConnector,
    WitsTariffConnector,
)

CONNECTOR_REGISTRY: dict[str, type[Connector]] = {
    # -------------------------------------------------------------------------
    # GROUP 1 — Global Opportunity Discovery (Trade-Min Agent)
    # Used to detect import surges, identify active overseas buyers, and build
    # the initial arbitrage opportunity list before a deal is created.
    # -------------------------------------------------------------------------

    # UN Comtrade: global trade volumes, HS code flow data, and YoY demand
    # growth across 200+ countries.  Detects surging import demands such as
    # "Turmeric imports in Poland grew 40 % YoY".  FREE (500 calls/day).
    ComtradeConnector.name: ComtradeConnector,

    # World Bank WITS (TRAINS): applied import tariffs at HS-6 — the FREE,
    # no-key replacement for ITC MacMap. Powers the Phase 3 tariff gate.
    WitsTariffConnector.name: WitsTariffConnector,

    # EU Access2Markets: FREE EU tariffs + NTBs + rules of origin. Preferred for
    # EU destinations in Phase 3 (richer than WITS); falls back to WITS when the
    # ACCESS2MARKETS_API_URL endpoint is not configured.
    Access2MarketsConnector.name: Access2MarketsConnector,

    # US Census International Trade: FREE (key) high-resolution US import
    # statistics; augments Comtrade for the US market in Phase 1.
    CensusTradeConnector.name: CensusTradeConnector,

    # -------------------------------------------------------------------------
    # LEGACY / PAID — retired from the default pipeline, kept for future use.
    # Re-enable by setting their API keys + restoring them to the Phase 1/3/4
    # connector lists in services/trade_min/service.py. See
    # workflow-implementation.md →
    # "Replaced / Legacy Trade-Min connectors (kept for future use)".
    # -------------------------------------------------------------------------

    # ITC Market Access Map (PAID subscription) — superseded by WITS for the
    # tariff gate. Richer NTBs / rules-of-origin if a subscription is bought.
    MacmapConnector.name: MacmapConnector,

    # Volza (PAID, per-record) — named buyers from Bill-of-Lading manifests.
    # No free global equivalent; live-intent signals (Alibaba RFQ, EU TED,
    # GDELT) cover actionable leads instead.
    VolzaConnector.name: VolzaConnector,

    # Trademo Intel (PAID) — buyer risk profiling + trade frequencies.
    TrademoConnector.name: TrademoConnector,

    # ImportGenius / Panjiva (PAID, enterprise) — US/LatAm BoL manifests.
    ImportGeniusConnector.name: ImportGeniusConnector,

    # -------------------------------------------------------------------------
    # GROUP 1b — Live Demand Signals (Trade-Min Agent)
    # Identify buyers who are actively sourcing RIGHT NOW (Level 3 intent) or
    # are forced to source due to a supply disruption (Level 4 intent).
    # -------------------------------------------------------------------------

    # Alibaba International B2B RFQ feed: buyers actively posting sourcing
    # requests for a product category.  Strongest possible buying-intent signal
    # — the buyer is searching for a supplier this week.
    AlibabaRfqConnector.name: AlibabaRfqConnector,

    # GDELT news disruption monitor: detects export bans, port strikes, or
    # sanctions in competitor origin countries.  When Vietnam faces an export
    # restriction on pepper, every European buyer who sourced from Vietnam is
    # now emergency-sourcing — the highest-urgency trade window for PortLess.
    GdeltDisruptionConnector.name: GdeltDisruptionConnector,

    # EU TED procurement portal: active formal tenders from EU government
    # bodies, hospitals, and state enterprises.  Defined budgets, published
    # quantities, and hard deadlines — the most commercially predictable of
    # all buying-intent signals.
    EuTedConnector.name: EuTedConnector,

    # -------------------------------------------------------------------------
    # GROUP 2 — Domestic Supplier Discovery & RFQ (Sourcing Agent)
    # Used to find verified Indian manufacturers, dispatch automated RFQs, and
    # aggregate competitive quotes for a matched trade opportunity.
    # -------------------------------------------------------------------------

    # IndiaMART: India's largest wholesale B2B directory.  Automates RFQ
    # dispatch to verified manufacturers and aggregates supplier quotes for
    # the matched HS code.
    IndiamartConnector.name: IndiamartConnector,

    # TradeIndia: fallback wholesale directory for supplier profiles and
    # competitor listings.  Dispatches secondary RFQs when IndiaMART yields
    # insufficient quotes or when alternative sourcing is needed.
    TradeIndiaConnector.name: TradeIndiaConnector,

    # -------------------------------------------------------------------------
    # GROUP 3 — Regulatory & Credential Verification (Sourcing + Trade-Min)
    # Used to validate supplier and buyer credentials against government and
    # international registries before any financial commitment is made.
    # -------------------------------------------------------------------------

    # GSTN (GST Suvidha Provider): validates a supplier's active GSTIN status,
    # filing history, and registered legal name.  Blocks non-compliant or
    # blacklisted suppliers from entering the deal pipeline.
    GstnConnector.name: GstnConnector,

    # DGFT Developer Portal: verifies a supplier's Importer-Exporter Code (IEC)
    # status and active export licences (EPCG, LUT, Advance Authorization).
    # Confirms the supplier is cleared to export and not on the DEN watch-list.
    DgftConnector.name: DgftConnector,

    # API Setu / MCA: checks Corporate Identification Numbers (CIN), director
    # listings, and LLP/Pvt Ltd registration status.  Ensures the entity is a
    # legitimate registered company, not a broker or shell.
    McaConnector.name: McaConnector,

    # Udyam / MSME Registry (via Signzy / Zoop): confirms micro, small, or
    # medium enterprise certification.  Filters out retail brokers and ensures
    # RFQs are sent directly to real manufacturing units.
    UdyamConnector.name: UdyamConnector,

    # EU VIES (European Commission): validates European buyer VAT numbers,
    # company registration, and address in real-time.  Prevents onboarding of
    # fake or unregistered EU entities at zero cost.
    ViesConnector.name: ViesConnector,

    # Allianz Trade (Euler Hermes): fetches dynamic corporate risk grades and
    # auto-underwritten trade credit insurance limits for overseas buyers.
    # Flags deals where 100 % advance escrow is mandatory due to high buyer risk.
    AllianzTradeConnector.name: AllianzTradeConnector,

    # -------------------------------------------------------------------------
    # GROUP 4 — Shielded Execution (Shield-Link + DocuGen Agents)
    # Used to book third-party QC inspections, freight legs, and customs
    # clearance — the zero-involvement operational layer of every deal.
    # -------------------------------------------------------------------------

    # SGS / Bureau Veritas: triggers physical cargo inspection at the supplier's
    # warehouse.  Holds escrow payment until a cryptographically signed Clean
    # Report of Findings (CRF) is uploaded, blocking dispatch of failed cargo.
    SgsConnector.name: SgsConnector,

    # Cogoport: provides LCL and FCL freight quotations and container bookings
    # from factory door to destination seaport.  Generates container-tracking
    # webhook URLs for real-time milestone updates.
    CogoportConnector.name: CogoportConnector,

    # Freightos: fetches dynamic spot freight rates under FOB / CIF Incoterms
    # and auto-books empty container space.  Used as the primary rate engine
    # alongside Cogoport for best-rate selection.
    FreightosConnector.name: FreightosConnector,

    # ICEGATE (Indian Customs EDI): submits the Shipping Bill and Let Export
    # Order (LEO) electronically via the CHA workflow.  Captures the shipping
    # bill number and tracks customs clearance status automatically.
    IcegateConnector.name: IcegateConnector,

    # -------------------------------------------------------------------------
    # GROUP 5 — Escrow & Payments (Escrow Guard Agent)
    # Used to lock buyer funds, release staged payouts to suppliers and service
    # providers, and handle multi-currency settlement at deal close.
    # -------------------------------------------------------------------------

    # Stripe Treasury: manages multi-currency virtual escrow accounts.  Locks
    # buyer funds on PI acceptance and releases staged payouts — supplier on
    # CRF sign-off, logistics/CHA on LEO issuance, PortLess margin on BoL.
    StripeTreasuryConnector.name: StripeTreasuryConnector,

    # Payoneer B2B: handles cross-border SWIFT / SEPA transfers and issues
    # multi-currency virtual cards.  Used as the fallback settlement rail when
    # Stripe Treasury is unavailable in a target corridor.
    PayoneerConnector.name: PayoneerConnector,

    # -------------------------------------------------------------------------
    # GROUP 6 — Neutral Escrow (Escrow-Guard, buyer-selectable)
    # Licensed third-party custodians that hold the buyer's funds in a
    # segregated account and release them to PortLess in tranches as the trade
    # advances. The buyer picks which one they trust on the PI. See
    # services/escrow_guard/workflow_escrow_guard.md.
    # -------------------------------------------------------------------------

    # Tazapay: cross-border B2B escrow — local pay-in + INR payout; strongest
    # India-corridor fit. Stub when TAZAPAY_API_KEY absent.
    TazapayEscrowConnector.name: TazapayEscrowConnector,

    # Escrow.com: established licensed escrow with milestone transactions;
    # wire-centric. Stub when ESCROW_COM_API_KEY absent.
    EscrowComConnector.name: EscrowComConnector,
}


def get_connector(name: str) -> Connector:
    """Instantiate a connector adapter by its registered name."""
    try:
        return CONNECTOR_REGISTRY[name]()
    except KeyError as exc:
        raise KeyError(f"Unknown connector: {name!r}") from exc
