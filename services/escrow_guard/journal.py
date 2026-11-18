"""Double-entry journal for one deal, derived from the escrow hold and the
payouts — the accountant's view of the money chain (``GET
/escrow-guard/deals/{deal_id}/journal``, ``?format=csv`` for the books).

Accounts (debit-positive):

  escrow_trust      money sitting with the escrow provider (asset)
  buyer_deposits    what PortLess owes the buyer until goods are accepted (liability)
  portless_cash     cash PortLess actually received (asset)
  provider_fees     escrow provider fees (expense)
  sales_revenue     recognised as each tranche releases (income)
  cogs              Purchase Order value (expense)
  supplier_payable  what PortLess owes the supplier (liability)

Events → entries

  escrow funded          Dr escrow_trust          Cr buyer_deposits   funded amount
  PO issued              Dr cogs                  Cr supplier_payable PO value
  tranche released       Dr portless_cash (net)   Cr escrow_trust     net
                         Dr provider_fees (fee)   Cr escrow_trust     fee
                         Dr buyer_deposits        Cr sales_revenue    gross
  refund                 Dr buyer_deposits        Cr escrow_trust     refunded
  supplier payout done   Dr supplier_payable      Cr portless_cash    amount
  supplier chargeback     Dr supplier_payable      Cr trade_costs      re-inspection after its failed QC
  FX on a payout (gain)  Dr supplier_payable      Cr fx_gain_loss     INR at PO rate − USD paid
  FX on a payout (loss)  Dr fx_gain_loss          Cr supplier_payable the opposite
                         (only when the PO rate and the payout's bank rate are recorded — fx_book.py)
  escrow interest (ours) Dr portless_cash          Cr interest_income  credited by the provider / bank
  escrow interest (buyer's) Dr portless_cash       Cr buyer_deposits   owed back to the buyer (escrow_interest.py)

Every entry balances by construction; ``balanced`` additionally checks the
trial balance and that the escrow_trust balance equals the ledger's
outstanding amount (what the provider should still hold).
"""
from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

ACCOUNTS: dict[str, str] = {
    "escrow_trust": "Escrow (provider trust account)",
    "buyer_deposits": "Buyer deposits (liability)",
    "portless_cash": "PortLess cash",
    "provider_fees": "Escrow provider fees (expense)",
    "sales_revenue": "Sales revenue",
    "cogs": "Cost of goods (supplier)",
    "supplier_payable": "Supplier payable (liability)",
    "trade_costs": "Freight, inspection, insurance, customs and documents (expense)",
    "service_payables": "Payable to freight, inspection, insurance and customs providers (liability)",
    "fx_gain_loss": "Exchange gain (credit) / loss (debit) on supplier payouts",
    "interest_income": "Interest on funds held in escrow",
}

_COST_LABELS = {
    "ocean_freight": "Ocean freight", "marine_insurance": "Marine cargo insurance", "inspection": "Pre-shipment inspection",
    "customs_agent": "Export customs agent (CHA)", "export_documents": "Export documents and certificates",
    "inland_transport": "Inland transport to port", "origin_handling": "Origin terminal handling",
    "bank_charges": "Bank charges", "payout_fees": "Supplier payout fees",
    "escrow_fee_above_estimate": "Escrow fee above the quoted rate",
}

_TOLERANCE = 0.01


class JournalEntry(BaseModel):
    seq: int
    at: datetime | None = None
    event: str
    debit: str
    credit: str
    amount: float
    currency: str
    ref: str | None = None
    memo: str | None = None


class DealJournal(BaseModel):
    deal_id: str
    currency: str
    escrow_id: str | None = None
    provider: str | None = None
    entries: list[JournalEntry] = Field(default_factory=list)
    balances: dict[str, float] = Field(default_factory=dict)
    debits_total: float = 0.0
    credits_total: float = 0.0
    balanced: bool = True
    issues: list[str] = Field(default_factory=list)
    accounts: dict[str, str] = Field(default_factory=lambda: dict(ACCOUNTS))
    generated_at: datetime


class _Builder:
    def __init__(self, currency: str) -> None:
        self.currency = currency
        self.entries: list[JournalEntry] = []

    def add(self, at: datetime | None, event: str, debit: str, credit: str, amount: float,
            ref: str | None = None, memo: str | None = None) -> None:
        amount = round(float(amount or 0.0), 2)
        if amount <= 0:
            return
        self.entries.append(JournalEntry(
            seq=len(self.entries) + 1, at=at, event=event, debit=debit, credit=credit,
            amount=amount, currency=self.currency, ref=ref, memo=memo,
        ))


def _fee_share(hold: Any, gross: float) -> float:
    """The fee attributable to one release: the hold's quoted fee %, else the
    recorded fees_total pro-rated over the released total."""
    if getattr(hold, "fee_pct", None):
        return round(gross * float(hold.fee_pct) / 100.0, 2)
    released = float(getattr(hold, "released_total", 0.0) or 0.0)
    fees = float(getattr(hold, "fees_total", 0.0) or 0.0)
    if released > 0 and fees > 0:
        return round(fees * gross / released, 2)
    return 0.0


def journal_from(hold: Any, ledger: Any, *, po_issued_at: datetime | None = None,
                 now: datetime | None = None, fx: dict[str, Any] | None = None,
                 chargebacks: list[dict[str, Any]] | None = None,
                 interest: list[dict[str, Any]] | None = None) -> DealJournal:
    """Pure builder (unit-testable): hold = EscrowHoldRecord, ledger = DealLedger."""
    from datetime import timezone
    now = now or datetime.now(timezone.utc)
    b = _Builder(hold.currency)

    funded = hold.funded_amount if hold.funded_amount is not None else (
        hold.amount if hold.status in ("FUNDED", "RELEASED", "REFUNDED") else 0.0
    )
    if funded:
        b.add(hold.funded_at, "escrow_funded", "escrow_trust", "buyer_deposits", funded,
              ref=hold.ext_ref, memo=f"{hold.provider} escrow funded" + (
                  f" (short by {hold.shortfall:.2f})" if hold.shortfall else ""))

    charged = round(sum(float(c.get("amount_usd") or 0) for c in (chargebacks or [])), 2)
    owed = getattr(ledger, "supplier_owed", None)
    if owed is not None and charged:
        owed = round(owed + charged, 2)  # the PO value; the chargebacks are posted below
    released_tranches = sorted(
        [t for t in (hold.tranches or []) if t.status == "RELEASED"],
        key=lambda t: (t.released_at or now),
    )
    if owed:
        first_release = released_tranches[0].released_at if released_tranches else None
        b.add(po_issued_at or first_release, "po_issued", "cogs", "supplier_payable", owed,
              memo="Purchase Order value owed to the supplier")

    for t in released_tranches:
        gross = t.amount if t.amount is not None else round(hold.amount * t.pct, 2)
        fee = _fee_share(hold, gross)
        net = round(gross - fee, 2)
        ref = t.ext_ref or t.tranche_id
        b.add(t.released_at, f"tranche_released:{t.tranche_id}", "portless_cash", "escrow_trust", net,
              ref=ref, memo=f"{t.tranche_id} ({t.trigger}) net of fees")
        b.add(t.released_at, f"provider_fee:{t.tranche_id}", "provider_fees", "escrow_trust", fee,
              ref=ref, memo=f"{hold.provider} fee on {t.tranche_id}")
        b.add(t.released_at, f"revenue_recognised:{t.tranche_id}", "buyer_deposits", "sales_revenue", gross,
              ref=ref, memo=f"{t.tranche_id} earned")

    if hold.refunded_total:
        refund_at = None
        try:
            refund_at = datetime.fromisoformat(str((hold.provider_payload or {}).get("refund", {}).get("refunded_at")))
        except Exception:  # noqa: BLE001
            refund_at = hold.updated_at if hold.status == "REFUNDED" else None
        b.add(refund_at, "refund_to_buyer", "buyer_deposits", "escrow_trust", hold.refunded_total,
              memo="Undisbursed balance refunded to the buyer")

    for c in chargebacks or []:
        b.add(None, f"supplier_chargeback:{c.get('kind')}", "supplier_payable", "trade_costs", c.get("amount_usd"),
              ref=c.get("ref"), memo=f"Charged to the supplier: {c.get('reason')}")

    for i in interest or []:
        credit = "interest_income" if i.get("belongs_to") == "portless" else "buyer_deposits"
        at = None
        try:
            at = datetime.fromisoformat(str(i.get("at")))
        except Exception:  # noqa: BLE001
            at = None
        b.add(at, f"escrow_interest:{i.get('interest_id')}", "portless_cash", credit, i.get("amount_usd"),
              ref=i.get("ref"), memo=f"Interest on escrow funds {i.get('period') or ''}".strip()
              + ("" if credit == "interest_income" else " — owed to the buyer"))

    # PortLess's own costs of moving and protecting the goods, as incurred.
    basis = getattr(ledger, "portless_costs_basis", None) or {}
    for key, usd in (getattr(ledger, "portless_costs", None) or {}).items():
        b.add(None, f"trade_cost:{key}", "trade_costs", "service_payables", usd,
              memo=f"{_COST_LABELS.get(key, key)} ({basis.get(key, 'estimate')})")

    for p in sorted((getattr(ledger, "payouts", None) or []), key=lambda p: (p.completed_at or p.created_at)):
        if p.status != "COMPLETED":
            continue
        b.add(p.completed_at or p.created_at, f"supplier_payout:{p.kind}", "supplier_payable", "portless_cash",
              p.amount, ref=p.ext_ref or p.payout_id, memo=f"Payout {p.payout_id} to {p.payee_id}")
        if fx:
            from shared.orchestration.fx_book import gain_loss
            rate = (fx.get("payout") or {}).get(p.payout_id)
            diff = gain_loss(p.amount, rate, fx.get("po"))
            memo = f"INR {p.amount * rate:,.2f} at {rate} (PO rate {fx.get('po')})" if rate else None
            if diff > 0:
                b.add(p.completed_at or p.created_at, f"fx_gain:{p.payout_id}", "supplier_payable", "fx_gain_loss",
                      diff, ref=p.payout_id, memo=f"Exchange gain — {memo}")
            elif diff < 0:
                b.add(p.completed_at or p.created_at, f"fx_loss:{p.payout_id}", "fx_gain_loss", "supplier_payable",
                      -diff, ref=p.payout_id, memo=f"Exchange loss — {memo}")

    balances: dict[str, float] = {k: 0.0 for k in ACCOUNTS}
    debits = credits = 0.0
    for e in b.entries:
        balances[e.debit] = round(balances.get(e.debit, 0.0) + e.amount, 2)
        balances[e.credit] = round(balances.get(e.credit, 0.0) - e.amount, 2)
        debits += e.amount
        credits += e.amount
    issues: list[str] = []
    if abs(sum(balances.values())) > _TOLERANCE:
        issues.append(f"trial balance off by {sum(balances.values()):.2f}")
    expected_trust = round(float(getattr(ledger, "outstanding", 0.0) or 0.0), 2)
    if funded and abs(balances.get("escrow_trust", 0.0) - expected_trust) > _TOLERANCE:
        issues.append(
            f"escrow_trust balance {balances.get('escrow_trust', 0.0):.2f} != ledger outstanding {expected_trust:.2f}"
        )
    for issue in (getattr(ledger, "issues", None) or []):
        issues.append(f"ledger: {issue}")
    return DealJournal(
        deal_id=hold.deal_id, currency=hold.currency, escrow_id=hold.escrow_id, provider=hold.provider,
        entries=b.entries, balances=balances, debits_total=round(debits, 2), credits_total=round(credits, 2),
        balanced=not issues, issues=issues, generated_at=now,
    )


async def build_journal(deal_id: str) -> DealJournal:
    """Journal for one deal from the live escrow hold + ledger + PO."""
    from .repository import get_repository
    from .service import HoldNotFoundError, get_deal_ledger

    hold = await get_repository().get_hold_by_deal(deal_id)
    if hold is None:
        raise HoldNotFoundError(deal_id)
    ledger = await get_deal_ledger(deal_id)
    po_at = None
    try:
        from services.dealmaker.repository import get_repository as _dm_repo  # type: ignore[import]
        po = await _dm_repo().get_po_by_deal(deal_id)
        po_at = getattr(po, "issued_at", None) if po else None
    except Exception:  # noqa: BLE001
        po_at = None
    from shared.orchestration import chargebacks, escrow_interest, fx_book
    return journal_from(hold, ledger, po_issued_at=po_at, fx=await fx_book.rates(deal_id),
                        chargebacks=await chargebacks.listed(deal_id),
                        interest=await escrow_interest.listed(deal_id))


def journal_csv(journal: DealJournal) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["deal_id", "seq", "at", "event", "debit_account", "credit_account", "amount", "currency", "ref", "memo"])
    for e in journal.entries:
        w.writerow([journal.deal_id, e.seq, e.at.isoformat() if e.at else "", e.event, e.debit, e.credit,
                    f"{e.amount:.2f}", e.currency, e.ref or "", e.memo or ""])
    return buf.getvalue()


def ledger_name(account: str) -> str:
    """The accountant's ledger for a PortLess account: ``JOURNAL_LEDGER_<ACCOUNT>`` (e.g.
    ``JOURNAL_LEDGER_PORTLESS_CASH="HDFC Current A/c 1234"``), else the account's label."""
    import os
    return os.getenv(f"JOURNAL_LEDGER_{account.upper()}", "").strip() or ACCOUNTS.get(account, account)


def accounting_csv(journals: list[DealJournal], target: str) -> str:
    """The journals in the import layout of the accountant's software, one debit and one credit
    row per entry (ledger names from ``ledger_name``):

    - ``tally``: Date (dd-mm-yyyy), Voucher Type, Voucher Number, Ledger Name, Debit, Credit, Narration;
    - ``zoho``: Journal Date (yyyy-mm-dd), Journal Number, Reference Number, Notes, Account, Debits, Credits, Currency.
    """
    if target not in ("tally", "zoho"):
        raise ValueError("target must be tally or zoho")
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if target == "tally":
        w.writerow(["Date", "Voucher Type", "Voucher Number", "Ledger Name", "Debit", "Credit", "Narration"])
    else:
        w.writerow(["Journal Date", "Journal Number", "Reference Number", "Notes", "Account", "Debits", "Credits", "Currency"])
    for j in journals:
        for e in j.entries:
            number = f"{j.deal_id}-{e.seq:03d}"
            narration = " · ".join(x for x in (e.event.replace("_", " "), e.memo, e.ref) if x)
            amount = f"{e.amount:.2f}"
            if target == "tally":
                day = e.at.strftime("%d-%m-%Y") if e.at else ""
                w.writerow([day, "Journal", number, ledger_name(e.debit), amount, "", narration])
                w.writerow([day, "Journal", number, ledger_name(e.credit), "", amount, narration])
            else:
                day = e.at.strftime("%Y-%m-%d") if e.at else ""
                w.writerow([day, number, e.ref or j.deal_id, narration, ledger_name(e.debit), amount, "", e.currency])
                w.writerow([day, number, e.ref or j.deal_id, narration, ledger_name(e.credit), "", amount, e.currency])
    return buf.getvalue()


async def build_all_journals() -> list[DealJournal]:
    from .repository import get_repository
    out: list[DealJournal] = []
    for hold in await get_repository().list_holds():
        try:
            out.append(await build_journal(hold.deal_id))
        except Exception:  # noqa: BLE001
            continue
    return out
