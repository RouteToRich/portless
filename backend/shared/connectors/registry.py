"""Central registry mapping connector names to their adapter classes.

Workers and the agent runtime resolve providers by name through this registry,
so adding a new integration is a one-line change here.
"""
from __future__ import annotations

from .base import Connector
from .payments import PayoneerConnector, StripeTreasuryConnector
from .regulatory import (
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
    ComtradeConnector,
    IndiamartConnector,
    TradeIndiaConnector,
    VolzaConnector,
)

CONNECTOR_REGISTRY: dict[str, type[Connector]] = {
    # Trade intelligence
    ComtradeConnector.name: ComtradeConnector,
    VolzaConnector.name: VolzaConnector,
    IndiamartConnector.name: IndiamartConnector,
    TradeIndiaConnector.name: TradeIndiaConnector,
    # Regulatory / verification
    GstnConnector.name: GstnConnector,
    DgftConnector.name: DgftConnector,
    McaConnector.name: McaConnector,
    UdyamConnector.name: UdyamConnector,
    ViesConnector.name: ViesConnector,
    # Shields: QC / freight / customs
    SgsConnector.name: SgsConnector,
    CogoportConnector.name: CogoportConnector,
    FreightosConnector.name: FreightosConnector,
    IcegateConnector.name: IcegateConnector,
    # Payments / escrow
    StripeTreasuryConnector.name: StripeTreasuryConnector,
    PayoneerConnector.name: PayoneerConnector,
}


def get_connector(name: str) -> Connector:
    """Instantiate a connector adapter by its registered name."""
    try:
        return CONNECTOR_REGISTRY[name]()
    except KeyError as exc:
        raise KeyError(f"Unknown connector: {name!r}") from exc
