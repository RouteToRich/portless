"""Persistence for computed match scores.

In-memory in this skeleton; the interface mirrors the ``match_scores`` table
(LLD 4.7) so it can be swapped for a PostgreSQL-backed repository later without
touching the service layer.
"""
from __future__ import annotations

from .schemas import ScoreResponse


class InMemoryMatchScoreRepository:
    """Stores scores by ``match_id`` and tracks the latest score per deal."""

    def __init__(self) -> None:
        self._by_match_id: dict[str, ScoreResponse] = {}
        self._latest_match_id_by_deal: dict[str, str] = {}

    def save(self, score: ScoreResponse) -> None:
        if not score.match_id:
            raise ValueError("score.match_id is required to persist a match score")
        self._by_match_id[score.match_id] = score
        self._latest_match_id_by_deal[score.deal_id] = score.match_id

    def get_latest_for_deal(self, deal_id: str) -> ScoreResponse | None:
        match_id = self._latest_match_id_by_deal.get(deal_id)
        return self._by_match_id.get(match_id) if match_id else None

    def list_all(self) -> list[ScoreResponse]:
        return list(self._by_match_id.values())

    def clear(self) -> None:
        self._by_match_id.clear()
        self._latest_match_id_by_deal.clear()


# Process-wide singleton (swap-in point for a real database-backed repository).
repository = InMemoryMatchScoreRepository()
