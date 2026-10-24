"""Business logic for the Match Engine service (LLD 4.3).

CMS = C_supplier * (w1*M_arbitrage + w2*L_proximity + w3*V_reliability)
Decision: CMS >= 75 -> trigger sourcing (SOURCE); CMS < 75 -> auto-discard (DISCARD).

The LLM proposes inputs; this deterministic pure-function guard decides.
"""
from __future__ import annotations

from uuid import uuid4

from shared.bus import get_publisher
from shared.domain.events import DomainEvent, EventTopic

from . import scoring
from .config import DEFAULT_CONFIG
from .repository import repository
from .schemas import ScoreRequest, ScoreResponse, ScoreWeights


def _new_match_id() -> str:
    return f"MATCH-{uuid4().hex[:12]}"


async def score_match(payload: ScoreRequest) -> ScoreResponse:
    """Compute the Composite Match Score, persist it, and emit ``match.scored``."""
    config = DEFAULT_CONFIG

    weights_in = payload.weights or ScoreWeights(
        arbitrage=config.w_arbitrage,
        proximity=config.w_proximity,
        reliability=config.w_reliability,
    )
    weights = scoring.normalize_weights(
        weights_in.arbitrage, weights_in.proximity, weights_in.reliability
    )

    gatekeeper = scoring.gatekeeper_multiplier(
        payload.verification.gst_verified,
        payload.verification.iec_active,
        payload.verification.dgft_denied,
    )
    arbitrage = scoring.arbitrage_score(
        payload.buyer_target_price, payload.supplier_quote_price, config
    )
    proximity = scoring.proximity_score(payload.supplier_to_port_km, config)
    reliability = scoring.reliability_score(
        payload.business_age_years,
        payload.udyam_registered,
        payload.annual_shipment_count,
        config,
    )

    cms = scoring.composite_match_score(
        gatekeeper, arbitrage, proximity, reliability, weights
    )
    decision = "SOURCE" if cms >= config.cms_threshold else "DISCARD"

    response = ScoreResponse(
        match_id=_new_match_id(),
        deal_id=payload.deal_id,
        supplier_id=payload.supplier_id,
        composite_score=round(cms, 2),
        gatekeeper_passed=bool(gatekeeper),
        arbitrage_score=round(arbitrage, 2),
        proximity_score=round(proximity, 2),
        reliability_score=round(reliability, 2),
        currency=payload.currency,
        threshold=config.cms_threshold,
        decision=decision,
        weights=ScoreWeights(
            arbitrage=round(weights[0], 4),
            proximity=round(weights[1], 4),
            reliability=round(weights[2], 4),
        ),
    )

    repository.save(response)

    await get_publisher().publish(
        DomainEvent(
            topic=EventTopic.MATCH_SCORED,
            deal_id=response.deal_id,
            payload={
                "match_id": response.match_id,
                "supplier_id": response.supplier_id,
                "composite_score": response.composite_score,
                "gatekeeper_passed": response.gatekeeper_passed,
                "arbitrage_score": response.arbitrage_score,
                "proximity_score": response.proximity_score,
                "reliability_score": response.reliability_score,
                "decision": response.decision,
                "threshold": response.threshold,
            },
        )
    )
    return response


async def get_score(deal_id: str) -> ScoreResponse | None:
    """Return the latest score for a deal, or ``None`` if it has not been scored."""
    return repository.get_latest_for_deal(deal_id)


async def list_scores() -> list[ScoreResponse]:
    """Return every computed match score."""
    return repository.list_all()
