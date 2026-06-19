"""Shared, cross-cutting package importable by every PortLess service.

Contains only transport-agnostic, framework-light building blocks:
- ``domain``     : enums, DTOs, the deal state machine, the event catalog.
- ``clients``    : the base async REST client used for inter-service calls.
- ``connectors`` : uniform external-adapter interfaces (quote/book/status).
"""
