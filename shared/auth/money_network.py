"""Where and from which device staff may APPROVE money (todo: device / IP restrictions for staff
approving money).

* ``AUTH_MONEY_APPROVER_NETWORKS`` — comma-separated IP addresses / CIDR blocks (office, VPN).
  The caller's address is the TCP peer, or with ``TRUSTED_PROXY_HOPS`` the forwarded address
  (``shared.rate_limit.client_address`` — never the caller's own claim).
* ``AUTH_MONEY_APPROVER_DEVICE_KEYS`` — comma-separated device keys; the console sends the one
  stored on a registered device in ``X-PortLess-Device``.

Each list empty = no restriction (as before). Only APPROVE of a money-moving gate is checked —
saying no is never restricted. Pure helpers; never raise.
"""
from __future__ import annotations

import hmac
import ipaddress
import os
from typing import Any


def _list(name: str) -> list[str]:
    return [p.strip() for p in os.getenv(name, "").split(",") if p.strip()]


def networks() -> list[Any]:
    out = []
    for raw in _list("AUTH_MONEY_APPROVER_NETWORKS"):
        try:
            out.append(ipaddress.ip_network(raw, strict=False))
        except ValueError:
            continue
    return out


def address_allowed(address: str | None) -> bool:
    nets = networks()
    if not nets and not _list("AUTH_MONEY_APPROVER_NETWORKS"):
        return True
    try:
        ip = ipaddress.ip_address((address or "").strip())
    except ValueError:
        return False
    return any(ip in n for n in nets)


def device_allowed(key: str | None) -> bool:
    keys = _list("AUTH_MONEY_APPROVER_DEVICE_KEYS")
    if not keys:
        return True
    given = (key or "").strip()
    return bool(given) and any(hmac.compare_digest(given, k) for k in keys)


def request_address(request: Any) -> str:
    from shared.rate_limit import client_address
    peer = getattr(getattr(request, "client", None), "host", None)
    return client_address(request.headers.get("x-forwarded-for"), peer)


def check(request: Any) -> str | None:
    """None when this request may approve money, else the reason it may not."""
    addr = request_address(request)
    if not address_allowed(addr):
        return (f"money gates can be approved only from the approved networks (AUTH_MONEY_APPROVER_NETWORKS); "
                f"this request came from {addr or 'an unknown address'}")
    if not device_allowed(request.headers.get("x-portless-device")):
        return "money gates can be approved only from a registered device (X-PortLess-Device)"
    return None
