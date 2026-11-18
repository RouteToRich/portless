"""Roles inside PortLess staff (todo: trade desk, money approver, compliance officer, admin).

Every allowlisted staff member (``AUTH_PORTLESS_EMAILS``) runs deals. Two decisions need a
named role once its list is set (empty list = every staff member, as before):

* **Money approver** — ``AUTH_MONEY_APPROVER_EMAILS``: APPROVE a money-moving gate.
* **Compliance officer** — ``AUTH_COMPLIANCE_OFFICER_EMAILS``: APPROVE a compliance gate
  (``CONFIRM_TRADE_COMPLIANCE``, ``CONFIRM_COUNTERPARTY_RISK``), confirm an HS code's export
  policy, clear a compliance hold, clear a possible sanctions match.

Saying no is never restricted: any staff member may REJECT a gate or fail a screening.
``GET /identity/me``-style callers read ``roles_of(email)``.
"""
from __future__ import annotations

import os

COMPLIANCE_ACTIONS = frozenset({"CONFIRM_TRADE_COMPLIANCE", "CONFIRM_COUNTERPARTY_RISK"})


def _list(name: str) -> set[str]:
    return {e.strip().lower() for e in os.getenv(name, "").split(",") if e.strip()}


def money_approvers() -> set[str]:
    return _list("AUTH_MONEY_APPROVER_EMAILS")


def compliance_officers() -> set[str]:
    return _list("AUTH_COMPLIANCE_OFFICER_EMAILS")


def is_compliance_officer(email: str | None) -> bool:
    officers = compliance_officers()
    return not officers or (bool(email) and email.strip().lower() in officers)


def is_money_approver(email: str | None) -> bool:
    approvers = money_approvers()
    return not approvers or (bool(email) and email.strip().lower() in approvers)


def roles_of(email: str | None) -> list[str]:
    """The decision roles this staff member holds (both lists empty → everyone holds both)."""
    out = ["trade_desk"]
    if is_money_approver(email):
        out.append("money_approver")
    if is_compliance_officer(email):
        out.append("compliance_officer")
    return out


class RoleRequired(Exception):
    def __init__(self, role: str, what: str) -> None:
        super().__init__(f"{what} needs a {role.replace('_', ' ')} "
                         f"({'AUTH_COMPLIANCE_OFFICER_EMAILS' if role == 'compliance_officer' else 'AUTH_MONEY_APPROVER_EMAILS'})")
        self.role = role


def require_compliance_officer(user: object, what: str) -> None:
    email = getattr(user, "email", None)
    if not is_compliance_officer(email):
        raise RoleRequired("compliance_officer", what)
