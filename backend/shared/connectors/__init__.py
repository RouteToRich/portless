"""External connector interfaces.

Every third-party integration (Comtrade, GSTN, DGFT, SGS, Cogoport, Stripe...)
implements the uniform ``Connector`` interface so the agent/worker layer can
treat them interchangeably — see Appendix 8.1 of the master specification.
"""

from .base import Connector
from .registry import CONNECTOR_REGISTRY, get_connector

__all__ = ["Connector", "CONNECTOR_REGISTRY", "get_connector"]
