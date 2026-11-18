"""Auto-replies and bounces are not replies (DEF-16)."""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shared.orchestration import auto_mail as am


@pytest.mark.parametrize("kw,expected", [
    ({"from_address": "MAILER-DAEMON@mx.gulf.ae", "subject": "failure"}, "bounce"),
    ({"from_address": "postmaster@gulf.ae", "subject": "x"}, "bounce"),
    ({"from_address": "a@b.com", "subject": "Undeliverable: Basmati rice offer"}, "bounce"),
    ({"from_address": "a@b.com", "subject": "Re: offer", "headers": {"X-Failed-Recipients": "buy@gulf.ae"}}, "bounce"),
    ({"from_address": "a@b.com", "subject": "x", "headers": {"Content-Type": "multipart/report; report-type=delivery-status"}}, "bounce"),
    ({"from_address": "buy@gulf.ae", "subject": "Automatic reply: Basmati rice"}, "auto_reply"),
    ({"from_address": "buy@gulf.ae", "subject": "Out of Office"}, "auto_reply"),
    ({"from_address": "buy@gulf.ae", "subject": "Re: offer", "headers": {"Auto-Submitted": "auto-replied"}}, "auto_reply"),
    ({"from_address": "buy@gulf.ae", "subject": "Re: offer", "headers": {"Precedence": "bulk"}}, "auto_reply"),
    ({"from_address": "buy@gulf.ae", "subject": "Re: offer", "headers": {"Auto-Submitted": "no"}}, None),
    ({"from_address": "buy@gulf.ae", "subject": "Re: offer — yes, interested"}, None),
])
def test_classify(kw, expected):
    assert am.classify(**kw) == expected


def test_failed_recipient():
    assert am.failed_recipient(headers={"X-Failed-Recipients": "Buy@Gulf.ae"}) == "buy@gulf.ae"
    body = "Reporting-MTA: dns; mx\nFinal-Recipient: rfc822; old@gulf.ae\nAction: failed"
    assert am.failed_recipient(body=body) == "old@gulf.ae"
    assert am.failed_recipient(body="could not deliver to x@y.ae", own=("trade@portless.in",)) == "x@y.ae"
    assert am.failed_recipient(body="nothing here") is None


async def test_inbound_bounce_marks_the_candidate_and_advances_nothing(monkeypatch):
    from services.email_processor import service as ep
    from services.email_processor.schemas import InboundEmailSource, InboundEmailStatus, InboundRawEmail
    saved = []
    monkeypatch.setattr(ep, "get_repository", lambda: SimpleNamespace(
        find_by_message_id=AsyncMock(return_value=None), save=AsyncMock(side_effect=lambda r: saved.append(r))))
    monkeypatch.setattr(ep, "_is_self_echo", lambda a: False)
    mark = AsyncMock(return_value=2)
    monkeypatch.setattr(am, "mark_bounced", mark)
    mapper = AsyncMock()
    monkeypatch.setattr(ep, "map_email", mapper)
    raw = InboundRawEmail(from_address="MAILER-DAEMON@mx.gulf.ae", to_address="trade@portless.in",
                          subject="Undeliverable: offer", body_text="Final-Recipient: rfc822; old@gulf.ae",
                          message_id="<m1>", source=InboundEmailSource.MANUAL, received_at=datetime.now(UTC))
    out = await ep._process_inbound(raw)
    assert out.status == InboundEmailStatus.BOUNCE and "old@gulf.ae" in out.message and "2 candidate" in out.message
    mark.assert_awaited_once_with("old@gulf.ae", "Undeliverable: offer")
    mapper.assert_not_awaited()


async def test_mark_bounced_updates_every_candidate_at_the_address(monkeypatch):
    import services.deals.repository as dr
    import services.deals.service as ds
    deals = [SimpleNamespace(deal_id="D1", buyer_candidates=[SimpleNamespace(candidate_id="C1", email="Old@gulf.ae"),
                                                              SimpleNamespace(candidate_id="C2", email="new@gulf.ae")])]
    monkeypatch.setattr(dr, "get_repository", lambda: SimpleNamespace(list_active=AsyncMock(return_value=deals)))
    upd = AsyncMock()
    monkeypatch.setattr(ds, "update_buyer_candidate", upd)
    assert await am.mark_bounced("old@gulf.ae", "Undeliverable") == 1
    assert upd.await_args.args[:2] == ("D1", "C1") and upd.await_args.args[2]["email_status"] == "bounced"
