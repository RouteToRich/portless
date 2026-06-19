"""In-memory persistence for detected trade opportunities.

Interface mirrors a future ``opportunities`` table (LLD 4.7) so the repository
can be swapped for a PostgreSQL-backed implementation without touching the
service layer.
"""
from __future__ import annotations

from .schemas import OpportunityResponse


class InMemoryOpportunityRepository:
    """Stores opportunities keyed by ``opportunity_id``."""

    def __init__(self) -> None:
        self._store: dict[str, OpportunityResponse] = {}

    def save(self, opp: OpportunityResponse) -> None:
        if not opp.opportunity_id:
            raise ValueError("opportunity_id is required")
        self._store[opp.opportunity_id] = opp

    def get(self, opportunity_id: str) -> OpportunityResponse | None:
        return self._store.get(opportunity_id)

    def list_all(self) -> list[OpportunityResponse]:
        return list(self._store.values())

    def clear(self) -> None:
        self._store.clear()


# Process-wide singleton (swap-in point for a real database-backed repository).
repository = InMemoryOpportunityRepository()
