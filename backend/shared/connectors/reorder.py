"""Reorder Window Engine: predict when an active buyer is due for their next order.

This is a pure-computation utility — no external API call is made.  It operates
entirely on Volza buyer-insights data already fetched by
``VolzaConnector.status()``.

Usage example::

    from shared.connectors import get_connector
    from shared.connectors.reorder import ReorderWindowEngine

    volza   = get_connector("volza")
    profile = await volza.status("7839201")       # fetch buyer insights from Volza
    engine  = ReorderWindowEngine()
    window  = engine.compute(profile)

    # {
    #   "volza_buyer_id":        "7839201",
    #   "buyer_name":            "Iberia Spices SL",
    #   "shipments_analysed":    12,
    #   "avg_reorder_days":      91,
    #   "last_shipment_date":    "2026-04-15",
    #   "predicted_next_order":  "2026-07-15",
    #   "days_until_next_order": 25,
    #   "urgency_score":         0,
    #   "urgency_label":         "UPCOMING",
    #   "confidence":            "HIGH"
    # }

How it fits the demand-signal pipeline::

    VolzaConnector.quote()          → find buyers who bought before
          ↓
    VolzaConnector.status(id)       → fetch full shipment history
          ↓
    ReorderWindowEngine.compute()   → predict next order date + urgency
          ↓
    Trade-Min Agent ranks by        → OVERDUE buyers first → highest conversion
    urgency_label + urgency_score

Urgency labels (OVERDUE buyers are highest priority):

    OVERDUE   — past predicted reorder date  (urgency_score = days overdue, > 0)
    IMMINENT  — within 14 days of predicted date
    UPCOMING  — 15 – 30 days out
    WATCHING  — 31 – 90 days out
    DORMANT   — 90 + days out
    UNKNOWN   — insufficient data to predict
"""
from __future__ import annotations

import statistics
from datetime import date, datetime, timedelta
from typing import Any

# ISO-8601 date formats returned by Volza (most to least specific).
_DATE_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def _parse_date(value: str) -> date | None:
    """Attempt to parse a date string using known Volza timestamp formats."""
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


class ReorderWindowEngine:
    """Compute predicted reorder dates from Volza buyer-insights shipment history.

    The engine needs at least 2 shipment records to calculate an average
    inter-order interval.  With fewer records it falls back gracefully:

        >= 3 intervals  → HMEAN of all gaps, confidence = HIGH
        == 2 intervals  → MEAN of two gaps,  confidence = MEDIUM
        == 1 interval   → single observed gap, confidence = LOW
        0 intervals     → 90-day default,       confidence = INSUFFICIENT_DATA
    """

    DEFAULT_INTERVAL_DAYS: int = 90

    def compute(self, buyer_profile: dict[str, Any]) -> dict[str, Any]:
        """Compute the reorder window from a ``VolzaConnector.status()`` response.

        Parameters
        ----------
        buyer_profile:
            The dict returned by ``VolzaConnector.status()``, containing at
            minimum a ``buyer_details`` key and a ``shipments`` list.  Each
            shipment must have a ``shipment_date`` field.

        Returns
        -------
        dict with the following keys:

            volza_buyer_id        (str)       Volza company identifier
            buyer_name            (str)       Company display name
            shipments_analysed    (int)       Number of dated shipments used
            avg_reorder_days      (int)       Computed average inter-order gap
            last_shipment_date    (str|None)  ISO date of most recent shipment
            predicted_next_order  (str|None)  ISO date of predicted next order
            days_until_next_order (int|None)  Positive = future; negative = overdue
            urgency_score         (int)       Days overdue (0 if not yet overdue)
            urgency_label         (str)       OVERDUE / IMMINENT / UPCOMING / WATCHING /
                                              DORMANT / UNKNOWN
            confidence            (str)       HIGH / MEDIUM / LOW / INSUFFICIENT_DATA
        """
        details: dict[str, Any] = buyer_profile.get("buyer_details") or {}
        shipments: list[dict[str, Any]] = buyer_profile.get("shipments") or []

        buyer_name = details.get("buyer_name", "")
        volza_id = str(details.get("volza_buyer_id", ""))

        # --- 1. Extract and sort all available shipment dates ---
        raw_dates = [s.get("shipment_date", "") for s in shipments if s.get("shipment_date")]
        parsed: list[date] = sorted(filter(None, (_parse_date(d) for d in raw_dates)))

        # --- 2. Compute inter-order intervals (skip same-day duplicates) ---
        intervals: list[int] = [
            (parsed[i] - parsed[i - 1]).days
            for i in range(1, len(parsed))
            if (parsed[i] - parsed[i - 1]).days > 0
        ]

        if len(intervals) >= 3:
            avg_days = round(statistics.mean(intervals))
            confidence = "HIGH"
        elif len(intervals) == 2:
            avg_days = round(statistics.mean(intervals))
            confidence = "MEDIUM"
        elif len(intervals) == 1:
            avg_days = intervals[0]
            confidence = "LOW"
        else:
            avg_days = self.DEFAULT_INTERVAL_DAYS
            confidence = "INSUFFICIENT_DATA"

        # --- 3. Predict next order date ---
        last_date = parsed[-1] if parsed else None

        if last_date:
            next_order: date | None = last_date + timedelta(days=avg_days)
            days_until: int | None = (next_order - date.today()).days
        else:
            next_order = None
            days_until = None

        # --- 4. Assign urgency label and score ---
        if days_until is None:
            urgency_score = 0
            urgency_label = "UNKNOWN"
        elif days_until < 0:
            urgency_score = abs(days_until)   # positive integer = days overdue
            urgency_label = "OVERDUE"
        elif days_until <= 14:
            urgency_score = 0
            urgency_label = "IMMINENT"
        elif days_until <= 30:
            urgency_score = 0
            urgency_label = "UPCOMING"
        elif days_until <= 90:
            urgency_score = 0
            urgency_label = "WATCHING"
        else:
            urgency_score = 0
            urgency_label = "DORMANT"

        return {
            "volza_buyer_id":        volza_id,
            "buyer_name":            buyer_name,
            "shipments_analysed":    len(parsed),
            "avg_reorder_days":      avg_days,
            "last_shipment_date":    last_date.isoformat() if last_date else None,
            "predicted_next_order":  next_order.isoformat() if next_order else None,
            "days_until_next_order": days_until,
            "urgency_score":         urgency_score,
            "urgency_label":         urgency_label,
            "confidence":            confidence,
        }

    def batch_compute(
        self,
        buyer_profiles: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Compute reorder windows for a list of buyer profiles and sort by urgency.

        Profiles are returned sorted so that OVERDUE (highest urgency_score first),
        then IMMINENT, UPCOMING, WATCHING, DORMANT, UNKNOWN buyers appear in order.
        Use this when the Trade-Min Agent needs to rank a full buyer list for
        prioritised outreach.
        """
        _LABEL_PRIORITY = {
            "OVERDUE":           0,
            "IMMINENT":          1,
            "UPCOMING":          2,
            "WATCHING":          3,
            "DORMANT":           4,
            "UNKNOWN":           5,
            "INSUFFICIENT_DATA": 6,
        }

        results = [self.compute(p) for p in buyer_profiles]
        return sorted(
            results,
            key=lambda r: (
                _LABEL_PRIORITY.get(r["urgency_label"], 99),
                -r["urgency_score"],   # most overdue first within OVERDUE bucket
            ),
        )
