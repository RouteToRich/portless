"""QC inspection lifecycle after the booking.

One function moves a QC order — ``apply_update`` — whichever way the news
arrives:

  * ``poll_qc``            API providers (QIMA): order status + report search,
                           report PDFs fetched and kept (deal watcher, every
                           ``QC_POLL_MINUTES``; ``POST /shield-link/deals/{id}/qc/poll``)
  * webhook                ``POST /shield-link/qc/webhook`` (signed; portal relay)
  * ``record_qc_status``   operator reading SGS SMART / BV OneSource Connect
                           (``POST /shield-link/qc/{ref}/status``)

Status path: REQUESTED → CONFIRMED (date fixed, inspector assigned) → INSPECTED
→ REPORT_ISSUED with PASS / FAIL (deal → QC_PASSED / QC_FAILED, as before) or
PENDING (the report leaves the decision to the client → an operator decides).
A result is applied once; a repeated poll or webhook is ignored.

Reports: every file the provider publishes is downloaded and stored next to the
deal's other documents (Google Drive ``<root>/<deal_id>/``, local disk only as
the logged fallback) with its SHA-256; the first report's hash is the CRF hash
the QC escrow tranche cites when the provider gives none.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from shared.connectors.qc_providers import (
    CANCELLED,
    INSPECTED,
    REPORT_ISSUED,
    ConnectorError,
    get_qc_connector,
)
from shared.domain.events import EventTopic

from .repository import get_repository
from .schemas import (
    DealQcResponse,
    QcOrderView,
    QcRescheduleRequest,
    QcStatusUpdateRequest,
    ThirdPartyOrderRecord,
)

logger = logging.getLogger(__name__)

_TERMINAL = ("COMPLETED", "FAILED", "CANCELLED")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Report storage
# ---------------------------------------------------------------------------

def _local_dir(deal_id: str) -> Path:
    base = Path(os.getenv("SHIELD_LINK_STORAGE_DIR", "") or Path(tempfile.gettempdir()) / "portless-docs" / "shield_link")
    p = base / re.sub(r"[^A-Za-z0-9_.-]", "_", deal_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


async def store_report(deal_id: str, filename: str, data: bytes, mime: str) -> str:
    """Keep a provider report: Google Drive first, local disk as the loud fallback."""
    from shared.connectors.google_drive import GoogleDriveError, get_connector as drive_connector
    drive = drive_connector()
    if drive.is_configured():
        try:
            result = await asyncio.to_thread(drive.upload_document, deal_id, filename, data, mime)
            return result["web_view_link"] or f"gdrive://{result['file_id']}"
        except GoogleDriveError as exc:
            logger.error("ShieldLink: Drive upload of %s failed (deal %s) — local disk: %s", filename, deal_id, exc)
    else:
        logger.warning("ShieldLink: Google Drive not configured — QC report %s (deal %s) written to local disk",
                       filename, deal_id)
    dest = _local_dir(deal_id) / re.sub(r"[^A-Za-z0-9_.-]", "_", filename)
    dest.write_bytes(data)
    return dest.as_uri()


async def load_report(storage_uri: str) -> tuple[bytes, str]:
    if storage_uri.startswith("file://"):
        from urllib.parse import urlparse
        from urllib.request import url2pathname
        path = Path(url2pathname(urlparse(storage_uri).path))
        if not path.is_file():
            raise FileNotFoundError(f"Stored QC report missing on disk: {path}")
        return path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    from services.docugen.service import load_stored_document  # type: ignore[import]
    return await load_stored_document(storage_uri)


async def _keep(tpo: ThirdPartyOrderRecord, data: bytes, mime: str, *, name: str, file_type: str,
                source: str, provider_file_id: str | None = None) -> dict[str, Any]:
    digest = hashlib.sha256(data).hexdigest()
    ext = mimetypes.guess_extension(mime) or (".pdf" if data[:5] == b"%PDF-" else ".bin")
    filename = name if "." in name[-6:] else f"{name}{ext}"
    uri = await store_report(tpo.deal_id, f"QC-{tpo.tpo_id}-{filename}", data, mime)
    return {"name": filename, "file_type": file_type, "storage_uri": uri, "sha256": digest, "size": len(data),
            "mime": mime, "source": source, "provider_file_id": provider_file_id, "stored_at": _now().isoformat()}


async def _fetch_documents(tpo: ThirdPartyOrderRecord, *, refs: list[dict[str, Any]], source: str,
                           product_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """Download and keep the provider's report files (never raises)."""
    connector = get_qc_connector(tpo.provider)
    have = {d.get("provider_file_id") or d.get("source_url") for d in tpo.qc_documents}
    kept: list[dict[str, Any]] = []
    try:
        files = list(refs)
        if not files and connector.pollable and tpo.ext_ref:
            files = [{"file_id": f["file_id"], "name": f.get("file_name"), "type": f.get("file_type")}
                     for f in await connector.documents(tpo.ext_ref, product_ids)]
        for f in files:
            ref = f.get("file_id") or f.get("url")
            if not ref or ref in have:
                continue
            data, mime = await connector.download(ref)
            doc = await _keep(tpo, data, mime, name=f.get("name") or f"report-{len(kept) + 1}",
                              file_type=f.get("type") or "INSPECTION_REPORT", source=source,
                              provider_file_id=f.get("file_id"))
            if f.get("url"):
                doc["source_url"] = f["url"]
            kept.append(doc)
    except (ConnectorError, OSError, Exception) as exc:  # noqa: BLE001 — a missing PDF never blocks the result
        logger.error("ShieldLink: QC report fetch failed for %s (deal %s): %s", tpo.tpo_id, tpo.deal_id, exc)
    return kept


# ---------------------------------------------------------------------------
# The one place a QC order changes
# ---------------------------------------------------------------------------

def _history(tpo: ThirdPartyOrderRecord, status: str, source: str, **extra: Any) -> list[dict[str, Any]]:
    entry = {"at": _now().isoformat(), "status": status, "source": source,
             **{k: v for k, v in extra.items() if v not in (None, "", [], {})}}
    return [*tpo.status_history, entry]


async def apply_update(tpo: ThirdPartyOrderRecord, **kwargs: Any) -> dict[str, Any]:
    """Apply a provider update (see ``_apply_update``) and recompute the deal's QC status."""
    from shared.orchestration import qc_status
    try:
        return await _apply_update(tpo, **kwargs)
    finally:
        await qc_status.sync(tpo.deal_id)


async def _apply_update(
    tpo: ThirdPartyOrderRecord,
    *,
    source: str,
    status: str | None = None,
    result: str | None = None,
    provider_order_number: str | None = None,
    inspection_date: str | None = None,
    status_code: str | None = None,
    status_text: str | None = None,
    report_url: str | None = None,
    crf_hash: str | None = None,
    notes: str | None = None,
    documents: list[dict[str, Any]] | None = None,
    product_ids: list[str] | None = None,
    defects: dict[str, Any] | None = None,
    loading: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Record what the provider says about the order and, for a PASS / FAIL
    report, move the deal (QC_PASSED / QC_FAILED) exactly once."""
    from . import service as sl
    repo = get_repository()
    if tpo.qc_result in ("PASS", "FAIL") and result:
        return {"status": "ignored", "reason": "result already recorded", "result": tpo.qc_result, "deal_id": tpo.deal_id}
    new_status = REPORT_ISSUED if result else (status or tpo.provider_status)
    changed = (new_status != tpo.provider_status or (inspection_date and inspection_date != tpo.inspection_date)
               or (provider_order_number and provider_order_number != tpo.provider_order_number))
    update: dict[str, Any] = {
        "provider_status": new_status,
        "provider_order_number": provider_order_number or tpo.provider_order_number,
        "provider_status_code": status_code or tpo.provider_status_code,
        "provider_status_text": status_text or tpo.provider_status_text,
        "inspection_date": (inspection_date or tpo.inspection_date),
        "qc_defects": defects or tpo.qc_defects,
        "updated_at": _now(),
    }
    if source == "poll":
        update["last_polled_at"] = _now()
    if changed or result:
        update["status_history"] = _history(tpo, new_status or "", source, result=result,
                                            inspection_date=inspection_date, notes=notes)
    if new_status == CANCELLED and tpo.status not in _TERMINAL:
        update["status"] = "CANCELLED"
    elif new_status in ("CONFIRMED", INSPECTED) and tpo.status in ("BOOKED", "PENDING"):
        update["status"] = "IN_PROGRESS"

    if not result:
        await repo.save(tpo.model_copy(update=update))
        if changed:
            await sl._emit(EventTopic.QC_STATUS_UPDATED, tpo.deal_id, {
                "tpo_id": tpo.tpo_id, "ext_ref": tpo.ext_ref, "provider": tpo.provider, "provider_status": new_status,
                "inspection_date": update["inspection_date"], "provider_order_number": update["provider_order_number"],
                "source": source,
            })
        return {"status": "ok", "provider_status": new_status or "", "deal_id": tpo.deal_id}

    docs = await _fetch_documents(tpo, refs=documents or [], source=source, product_ids=product_ids)
    all_docs = [*tpo.qc_documents, *docs]
    crf = crf_hash or (all_docs[0]["sha256"] if all_docs else None) or tpo.crf_hash
    now = _now()
    update.update({
        "qc_documents": all_docs,
        "qc_result": result,
        "qc_report_url": report_url or tpo.qc_report_url,
        "crf_hash": crf,
    })
    if result == "PENDING":
        await repo.save(tpo.model_copy(update=update))
        await sl._emit(EventTopic.DEAL_EXCEPTION, tpo.deal_id, {
            "stage": "QC_RESULT_PENDING", "category": "QC_RESULT_PENDING",
            "error": f"{tpo.provider.upper()} report {tpo.provider_order_number or tpo.ext_ref} leaves the decision to "
                     "the client — read it and record PASS or FAIL (POST /shield-link/qc/{ref}/status)",
            "action_required": "manual_retry_or_intervention", "tpo_id": tpo.tpo_id, "ext_ref": tpo.ext_ref,
        })
        return {"status": "ok", "result": "PENDING", "deal_id": tpo.deal_id, "documents": len(docs)}

    update.update({"status": "COMPLETED" if result == "PASS" else "FAILED", "qc_completed_at": now})
    updated = tpo.model_copy(update=update)
    await repo.save(updated)
    loading = loading or {}
    ext_ref = tpo.ext_ref or tpo.tpo_id
    if (tpo.inspection_type or "PSI") != "PSI":
        # DUPRO / PEO: a phase result — the deal stays where it is; the PSI
        # remains the release check (continuations.on_qc_phase_result).
        await sl._emit(EventTopic.QC_PHASE_RESULT, tpo.deal_id, {
            "tpo_id": tpo.tpo_id, "ext_ref": ext_ref, "inspection_type": tpo.inspection_type, "result": result,
            "notes": notes, "defects": updated.qc_defects, "provider": tpo.provider,
            "provider_order_number": updated.provider_order_number, "report_url": updated.qc_report_url,
            "crf_hash": crf,
        })
        logger.info("ShieldLink: %s result %s (%s) for deal %s", tpo.inspection_type, result, source, tpo.deal_id)
        return {"status": "ok", "result": result, "inspection_type": tpo.inspection_type, "deal_id": tpo.deal_id}
    if result == "PASS":
        try:
            await sl._transition_deal(tpo.deal_id, "QC_PASSED", f"QC passed — ref {ext_ref}")
        except Exception as exc:  # noqa: BLE001
            logger.error("ShieldLink: QC_PASSED transition failed for deal %s: %s", tpo.deal_id, exc)
        await sl._emit(EventTopic.QC_PASSED, tpo.deal_id, {
            "tpo_id": tpo.tpo_id, "ext_ref": ext_ref, "crf_hash": crf, "report_url": updated.qc_report_url,
            "provider": tpo.provider, "provider_order_number": updated.provider_order_number,
            "documents": [{"name": d["name"], "sha256": d["sha256"]} for d in all_docs],
            "container_numbers": loading.get("container_numbers") or [],
            "seal_numbers": loading.get("seal_numbers") or [],
            "loaded_quantity_mt": loading.get("loaded_quantity_mt"),
            "loaded_hs_code": loading.get("loaded_hs_code"),
            "goods_description": loading.get("goods_description"),
            "photos": loading.get("photos") or [],
        })
    else:
        try:
            await sl._transition_deal(tpo.deal_id, "QC_FAILED", f"QC failed — ref {ext_ref}, notes: {notes}")
        except Exception as exc:  # noqa: BLE001
            logger.error("ShieldLink: QC_FAILED transition failed for deal %s: %s", tpo.deal_id, exc)
        await sl._emit(EventTopic.QC_FAILED, tpo.deal_id, {
            "tpo_id": tpo.tpo_id, "ext_ref": ext_ref, "notes": notes, "defects": updated.qc_defects,
            "provider": tpo.provider, "report_url": updated.qc_report_url,
        })
    logger.info("ShieldLink: QC result %s (%s) processed for deal %s", result, source, tpo.deal_id)
    return {"status": "ok", "result": result, "deal_id": tpo.deal_id}


# ---------------------------------------------------------------------------
# Polling (API providers)
# ---------------------------------------------------------------------------

def _poll_due(tpo: ThirdPartyOrderRecord, now: datetime, force: bool) -> bool:
    if force or tpo.last_polled_at is None:
        return True
    from shared.orchestration import qc_schedule
    last = tpo.last_polled_at if tpo.last_polled_at.tzinfo else tpo.last_polled_at.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(minutes=qc_schedule.poll_minutes())


async def poll_order(tpo: ThirdPartyOrderRecord, *, force: bool = False) -> dict[str, Any]:
    """Ask the provider about one QC order. Never raises."""
    if tpo.order_type != "QC" or tpo.status in _TERMINAL or tpo.qc_result in ("PASS", "FAIL"):
        return {"status": "closed", "tpo_id": tpo.tpo_id}
    connector = get_qc_connector(tpo.provider)
    if not connector.pollable or not tpo.ext_ref or tpo.channel == "stub":
        return {"status": "not_pollable", "tpo_id": tpo.tpo_id, "channel": tpo.channel or connector.channel}
    if not _poll_due(tpo, _now(), force):
        return {"status": "not_due", "tpo_id": tpo.tpo_id}
    try:
        st = await connector.status(tpo.ext_ref)
    except Exception as exc:  # noqa: BLE001
        logger.warning("ShieldLink: QC poll failed for %s (deal %s): %s", tpo.tpo_id, tpo.deal_id, exc)
        await get_repository().save(tpo.model_copy(update={"last_polled_at": _now()}))
        return {"status": "error", "tpo_id": tpo.tpo_id, "error": str(exc)}
    defects = {}
    for r in st.get("reports") or []:
        for k, v in (r.get("defects") or {}).items():
            if isinstance(v, (int, float)):
                defects[k] = max(defects.get(k, 0), v)
    applied = await apply_update(
        tpo, source="poll", status=st.get("provider_status"), result=st.get("result"),
        provider_order_number=st.get("provider_order_number"), inspection_date=st.get("inspection_date"),
        status_code=st.get("provider_status_code"), status_text=st.get("provider_status_text"),
        product_ids=st.get("product_ids"), defects=defects or None,
        notes="; ".join(filter(None, (str(r.get("remark") or "") for r in st.get("reports") or []))) or None,
    )
    return {**applied, "tpo_id": tpo.tpo_id, "provider_status": st.get("provider_status")}


async def poll_qc(deal_id: str, *, force: bool = False) -> dict[str, Any]:
    """Poll every open QC order of the deal (deal watcher / operator)."""
    orders = [o for o in await get_repository().list_by_deal(deal_id) if o.order_type == "QC"]
    if not orders:
        return {"status": "no_booking", "deal_id": deal_id}
    results = [await poll_order(o, force=force) for o in orders]
    return {"status": "ok", "deal_id": deal_id, "orders": results}


# ---------------------------------------------------------------------------
# Operator / portal relay
# ---------------------------------------------------------------------------

async def _find(ref: str) -> ThirdPartyOrderRecord:
    from .service import TpoNotFoundError
    repo = get_repository()
    tpo = await repo.get(ref) or await repo.get_by_ext_ref(ref)
    if tpo is None or tpo.order_type != "QC":
        raise TpoNotFoundError(ref)
    return tpo


def _loading(src: Any) -> dict[str, Any]:
    return {k: getattr(src, k) for k in ("container_numbers", "seal_numbers", "loaded_quantity_mt", "loaded_hs_code",
                                         "goods_description", "photos")}


async def record_qc_status(ref: str, req: QcStatusUpdateRequest) -> dict[str, Any]:
    """What the provider's portal / e-mail says, relayed by an operator."""
    tpo = await _find(ref)
    if req.status == REPORT_ISSUED and req.result is None:
        from .service import InvalidDealState
        raise InvalidDealState(tpo.deal_id, "REPORT_ISSUED without result", "result PASS or FAIL")
    return await apply_update(
        tpo, source=req.source, status=req.status, result=req.result,
        provider_order_number=req.provider_order_number, inspection_date=req.inspection_date,
        report_url=req.report_url, crf_hash=req.crf_hash, notes=req.notes,
        documents=[d.model_dump() for d in req.documents], defects=req.defects or None, loading=_loading(req),
    )


def loading_from_payload(payload: Any) -> dict[str, Any]:
    return _loading(payload)


# ---------------------------------------------------------------------------
# Views, documents, reschedule
# ---------------------------------------------------------------------------

def _view(o: ThirdPartyOrderRecord) -> QcOrderView:
    payload = o.provider_payload or {}
    return QcOrderView(
        tpo_id=o.tpo_id, provider=o.provider, channel=o.channel, status=o.status, provider_status=o.provider_status,
        provider_status_code=o.provider_status_code, provider_status_text=o.provider_status_text,
        provider_order_number=o.provider_order_number, ext_ref=o.ext_ref, goods_ready_date=o.goods_ready_date,
        inspection_date=o.inspection_date, qc_result=o.qc_result, qc_report_url=o.qc_report_url, crf_hash=o.crf_hash,
        qc_defects=o.qc_defects,
        documents=[{k: d.get(k) for k in ("name", "file_type", "sha256", "size", "mime", "source", "stored_at")}
                   for d in o.qc_documents],
        last_polled_at=o.last_polled_at, status_history=o.status_history,
        booking_email=payload.get("booking_email"), portal_url=payload.get("portal_url"), created_at=o.created_at,
    )


def _next_step(orders: list[ThirdPartyOrderRecord], readiness: dict[str, Any]) -> str:
    live = [o for o in orders if o.status not in ("CANCELLED",)]
    if not live:
        if readiness.get("status") == "awaiting":
            return "Waiting for the supplier's goods-ready date (POST /shield-link/deals/{id}/qc-readiness records it)."
        return "No inspection booked yet."
    o = live[-1]
    if o.qc_result in ("PASS", "FAIL"):
        return f"Report issued: {o.qc_result}."
    if o.qc_result == "PENDING":
        return "The report leaves the decision to the client — record PASS or FAIL."
    if o.channel in ("portal", "sandbox") and get_qc_connector(o.provider).channel == "portal":
        return (f"Booked through {o.provider.upper()}'s portal — confirmation and report arrive by webhook or are "
                "recorded with POST /shield-link/qc/{ref}/status.")
    return f"Inspection {o.provider_status or 'REQUESTED'} for {o.inspection_date or 'a date to be confirmed'}; status is polled."


async def get_deal_qc(deal_id: str) -> DealQcResponse:
    orders = [o for o in await get_repository().list_by_deal(deal_id) if o.order_type == "QC"]
    orders.sort(key=lambda o: o.created_at)
    readiness: dict[str, Any] = {}
    try:
        from shared.orchestration import supplier_commitment
        rec = await supplier_commitment.get(deal_id)
        if rec is not None:
            readiness = {"status": rec.qc_ready_status, "ready_date": rec.qc_ready_date, "by": rec.qc_ready_by,
                         "produced_pct": rec.qc_ready_produced_pct, "packed_pct": rec.qc_ready_packed_pct,
                         "po_ack": rec.po_ack}
    except Exception as exc:  # noqa: BLE001
        logger.debug("ShieldLink: readiness lookup failed for %s: %s", deal_id, exc)
    status = label = detail = None
    try:
        from services.deals.repository import get_repository as deals_repo  # type: ignore[import]
        from shared.orchestration import qc_status, supplier_commitment
        deal = await deals_repo().get(deal_id)
        if deal is not None:
            st, detail = qc_status.compute(deal.status, await supplier_commitment.get(deal_id), orders)
            status, label = (st.value, qc_status.LABELS[st]) if st else (None, None)
    except Exception as exc:  # noqa: BLE001
        logger.debug("ShieldLink: QC status for %s unavailable: %s", deal_id, exc)
    return DealQcResponse(deal_id=deal_id, qc_status=status, qc_status_label=label, qc_status_detail=detail,
                          orders=[_view(o) for o in orders], readiness=readiness,
                          next_step=_next_step(orders, readiness))


async def get_qc_document(tpo_id: str, index: int) -> tuple[bytes, str, str]:
    """(bytes, mime, filename) of a kept report."""
    tpo = await _find(tpo_id)
    if index < 0 or index >= len(tpo.qc_documents):
        raise FileNotFoundError(f"QC order {tpo_id} has no document #{index}")
    doc = tpo.qc_documents[index]
    data, mime = await load_report(doc["storage_uri"])
    return data, doc.get("mime") or mime, doc.get("name") or f"{tpo_id}-{index}.pdf"


async def reschedule(tpo_id: str, req: QcRescheduleRequest) -> Any:
    """Cancel the open booking at the provider and book the new date (the
    supplier is late, or the agency moved). The deal does not move."""
    from shared.orchestration import qc_schedule

    from .schemas import QcBookingRequest
    from .service import InvalidDealState, book_qc
    tpo = await _find(tpo_id)
    if tpo.qc_result in ("PASS", "FAIL") or tpo.status in _TERMINAL:
        raise InvalidDealState(tpo.deal_id, f"QC order {tpo.status}", "an open inspection booking")
    connector = get_qc_connector(tpo.provider)
    cancel = await connector.cancel(tpo.ext_ref or tpo.tpo_id, req.reason) if tpo.ext_ref else {"cancelled": False}
    await get_repository().save(tpo.model_copy(update={
        "status": "CANCELLED", "provider_status": CANCELLED, "updated_at": _now(),
        "status_history": _history(tpo, CANCELLED, "reschedule", notes=req.reason, cancelled_at_provider=cancel.get("cancelled")),
    }))
    new_date = qc_schedule.to_date(req.inspection_date)
    return await book_qc(QcBookingRequest(
        deal_id=tpo.deal_id, provider=tpo.provider, goods_ready_date=new_date.isoformat() if new_date else None,
        inspection_date=req.inspection_date, idempotency_key=f"qc-reschedule-{tpo.tpo_id}-{req.inspection_date}",
    ), reinspection=True, previous=tpo)
