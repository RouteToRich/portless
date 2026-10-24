"""Negotiation Agent — intelligent multi-round negotiation with buyer & seller.

This agent is triggered after Match Engine finds a qualifying match (CMS ≥ 75).
It uses static trade context, optional live market data, monitored inboxes, and
persisted negotiation memory to conduct autonomous negotiations on behalf of
the merchant exporter, with human-in-the-loop at key decision points.
"""
