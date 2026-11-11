"""The deal's escrow status — which provider holds the buyer's money, and where it stands.

``DealRecord.escrow_provider`` / ``escrow_status`` / ``escrow_status_detail``
(``shared.domain.enums.EscrowStatus``) feed the console's Deal Board:

  OPEN_FAILED        the provider refused to open the escrow
  AWAITING_FUNDING   opened; the buyer has not paid
  PARTIALLY_FUNDED   the provider holds less than the PI total
  FUNDED             fully funded, nothing paid out yet
  WITH_PROVIDER      a stage's proof is with the provider, its payout pending
                     (Tazapay checking the documents / the buyer to accept on Escrow.com)
  RELEASING          some stages paid out, more to come
  ACCEPTANCE_WINDOW  goods delivered; the buyer's acceptance window is open
  DISPUTED           the buyer disputed or rejected a stage
  SETTLED            every stage paid out
  REFUNDED           the undisbursed money went back to the buyer

``compute()`` derives it from the escrow record alone, so it can always be
recomputed. ``sync()`` writes it with an atomic field update; Escrow-Guard calls
it on every escrow event, and the deal's chosen provider is set by
``set_choice()`` before the escrow exists.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from shared.domain.enums import EscrowStatus

logger = logging.getLogger(__name__)

LABELS: dict[EscrowStatus, str] = {
    EscrowStatus.OPEN_FAILED: "Escrow not opened",
    EscrowStatus.AWAITING_FUNDING: "Awaiting buyer payment",
    EscrowStatus.PARTIALLY_FUNDED: "Part-funded",
    EscrowStatus.FUNDED: "Funded",
    EscrowStatus.WITH_PROVIDER: "Payout with provider",
    EscrowStatus.RELEASING: "Releasing",
    EscrowStatus.ACCEPTANCE_WINDOW: "Buyer acceptance window",
    EscrowStatus.DISPUTED: "Disputed",
    EscrowStatus.SETTLED: "Settled",
    EscrowStatus.REFUNDED: "Refunded",
}


def _money(x: float, currency: str) -> str:
    return f"{x:,.2f} {currency}"


def compute(hold: Any) -> tuple[EscrowStatus | None, str | None]:
    """(status, one-line detail) from the escrow record; (None, None) without one."""
    from shared.connectors.escrow import buyer_accepts_at_provider, display_name

    if hold is None:
        return None, None
    who = display_name(hold.provider)
    cur = hold.currency
    released = float(hold.released_total or 0.0)
    if hold.status == "FAILED":
        return EscrowStatus.OPEN_FAILED, f"{who} did not open the escrow — retry or choose the other provider"
    if hold.status == "REFUNDED":
        return EscrowStatus.REFUNDED, f"{who} refunded {_money(hold.refunded_total or 0.0, cur)} to the buyer"
    if hold.status == "PENDING":
        return EscrowStatus.AWAITING_FUNDING, f"{who} · waiting for the buyer to pay {_money(hold.amount, cur)}"
    if hold.status == "PARTIALLY_FUNDED":
        return EscrowStatus.PARTIALLY_FUNDED, (f"{who} holds {_money(hold.funded_amount or 0.0, cur)} of "
                                                f"{_money(hold.amount, cur)} — short {_money(hold.shortfall or 0.0, cur)}")
    if hold.disputed:
        return EscrowStatus.DISPUTED, f"{who} · buyer dispute — releases stopped ({_money(released, cur)} paid out)"
    tranches = list(hold.tranches or [])
    if hold.settled_at is not None or (tranches and all(t.status == "RELEASED" for t in tranches)):
        return EscrowStatus.SETTLED, f"{who} · all stages paid out ({_money(released, cur)})"
    submitted = [t for t in tranches if t.status == "SUBMITTED"]
    if submitted:
        waiting = ("the buyer to accept on Escrow.com" if buyer_accepts_at_provider(hold.provider)
                   else f"{who} to verify the documents")
        names = ", ".join(t.tranche_id for t in submitted)
        return EscrowStatus.WITH_PROVIDER, (f"{who} · {names} submitted — waiting for {waiting} "
                                             f"({_money(released, cur)} of {_money(hold.amount, cur)} paid out)")
    if hold.inspection_deadline is not None and any(t.trigger == "ACCEPTED" and t.status != "RELEASED" for t in tranches):
        deadline = hold.inspection_deadline
        return EscrowStatus.ACCEPTANCE_WINDOW, (f"{who} · buyer acceptance window until "
                                                 f"{deadline.strftime('%Y-%m-%d %H:%M')} UTC")
    if released > 0:
        done = ", ".join(t.tranche_id for t in tranches if t.status == "RELEASED")
        return EscrowStatus.RELEASING, f"{who} · {done} paid out ({_money(released, cur)} of {_money(hold.amount, cur)})"
    return EscrowStatus.FUNDED, f"{who} holds {_money(hold.funded_amount or hold.amount, cur)}"


async def sync(deal_id: str | None) -> EscrowStatus | None:
    """Recompute and store the deal's escrow status when it changed. Never raises."""
    if not deal_id:
        return None
    try:
        from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
        from services.escrow_guard.repository import (
            get_repository as eg_repo,  # type: ignore[import]
        )
        deal = await deals_repo().get(deal_id)
        if deal is None:
            return None
        hold = await eg_repo().get_hold_by_deal(deal_id)
        status, detail = compute(hold)
        fields: dict[str, Any] = {}
        if status != deal.escrow_status or detail != deal.escrow_status_detail:
            fields.update({"escrow_status": status.value if status else None, "escrow_status_detail": detail,
                           "escrow_status_at": datetime.now(UTC)})
        if hold is not None and hold.provider != deal.escrow_provider:
            fields["escrow_provider"] = hold.provider
            fields["escrow_provider_source"] = (hold.provider_payload or {}).get("chosen_by") or deal.escrow_provider_source
        if fields:
            await deals_repo().set_fields(deal_id, fields)
            logger.info("EscrowStatus: deal %s → %s (%s)", deal_id, getattr(status, "value", status), detail)
        return status
    except Exception as exc:  # noqa: BLE001 — a status line never breaks the workflow
        logger.warning("EscrowStatus: sync failed for deal %s: %s", deal_id, exc)
        return None


async def set_choice(deal_id: str, provider: str, source: str) -> bool:
    """Record the buyer's / operator's provider choice on the deal before the escrow opens.
    Refuses (False) once an escrow exists or for an unknown / disabled provider."""
    from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
    from services.escrow_guard.repository import get_repository as eg_repo  # type: ignore[import]
    from shared.connectors.escrow import enabled_providers, resolve_escrow_provider

    key = resolve_escrow_provider(provider)
    if key is None or key not in enabled_providers():
        return False
    hold = await eg_repo().get_hold_by_deal(deal_id)
    if hold is not None and hold.status != "FAILED":
        return False
    return bool(await deals_repo().set_fields(deal_id, {"escrow_provider": key, "escrow_provider_source": source}))
