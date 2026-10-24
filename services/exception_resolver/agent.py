"""LangGraph-based Exception Resolver Agent.

Graph nodes
───────────
  analyze   — LLM classifies exception, determines severity and remediation plan
  execute   — Runs AUTO_FIX (docugen regen, connector retry) or raises HITL
  finalize  — Persists outcome, emits domain events

Flow
────
  START → analyze → execute → finalize → END
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from shared.llm_gateway import LLMClient, LLMExhaustedException
from .config import settings
from .schemas import (
    AgentAnalysis,
    ExceptionCategory,
    ExceptionRecord,
    ExceptionSeverity,
    ExceptionStatus,
    RemediationDecision,
    RemediationStep,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LangGraph state
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    record: ExceptionRecord
    analysis: AgentAnalysis | None
    execution_result: dict[str, Any]
    error: str | None
    playbook: dict[str, Any] | None


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an AI agent that resolves trade deal exceptions for PortLess — a B2B cross-border commodities trading platform.

Your job:
1. Classify the exception into one of the categories below.
2. Assess severity.
3. Build a concrete remediation plan.
4. Decide the resolution strategy.

EXCEPTION CATEGORIES:
- QC_FAILED          Physical quality inspection failed at supplier site
- FREIGHT_DELAY      Shipment delayed or logistics milestone missed
- PAYMENT_FAILED     Stripe escrow or Payoneer payout failure
- NEGOTIATION_FAILED Deal negotiation completely broke down
- CUSTOMS_REJECTED   ICEGATE customs filing rejected or LEO not issued
- DOCUMENT_ERROR     Document generation or hash verification failed
- CONNECTOR_ERROR    Third-party API (SGS/Cogoport/Freightos/Stripe/Payoneer) failure
- MARGIN_BREACH      PO margin fell below the minimum 8% threshold
- APPROVAL_REJECTED  Human operator rejected a HITL approval gate
- CALLBACK_FAILED    HITL saga callback execution failed after approval
- UNKNOWN            Cannot determine category from available context

DECISIONS:
- AUTO_FIX   System will attempt automated fix (retry, fallback provider, regenerate doc)
- ESCALATE   Raise a HITL approval for operator review before proceeding
- CANCEL     Deal cannot proceed; cancel and notify all parties
- MONITOR    Log and watch; no immediate action needed

Respond ONLY with a valid JSON object matching EXACTLY this schema (no markdown, no prose):
{
  "category": "<ExceptionCategory>",
  "severity": "LOW|MEDIUM|HIGH|CRITICAL",
  "summary": "<one-sentence summary for the operator dashboard>",
  "root_cause": "<2-3 sentences root cause analysis>",
  "remediation_steps": ["<step 1>", "<step 2>", "..."],
  "decision": "AUTO_FIX|ESCALATE|CANCEL|MONITOR",
  "decision_rationale": "<why this decision>",
  "estimated_resolution_hours": <integer>
}
"""

# ---------------------------------------------------------------------------
# Auto-fix handlers (what the agent can do without human approval)
# ---------------------------------------------------------------------------

async def _auto_fix_document_error(record: ExceptionRecord) -> dict[str, Any]:
    """Attempt to regenerate a failed document via DocuGen."""
    try:
        from services.docugen import service as docugen_service
        from services.docugen.schemas import GenerateDocumentRequest
        from shared.domain.enums import DocumentType

        # Resolve the document type from context (accepts either the enum value
        # e.g. "CI" or the enum name e.g. "COMMERCIAL_INVOICE"); default to CI.
        raw = record.context.get("document_type") or record.context.get("doc_type") or "CI"
        try:
            doc_type = DocumentType(raw)
        except ValueError:
            try:
                doc_type = DocumentType[raw]
            except KeyError:
                doc_type = DocumentType.COMMERCIAL_INVOICE

        req = GenerateDocumentRequest(deal_id=record.deal_id, doc_type=doc_type)
        doc = await docugen_service.generate_document(req)
        return {"success": True, "document_id": doc.doc_id, "action": "document_regenerated"}
    except Exception as exc:
        logger.error("Auto-fix document_error failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def _auto_fix_connector_error(record: ExceptionRecord) -> dict[str, Any]:
    """Log connector error; graceful stubs already provide fallback — mark for monitoring."""
    logger.warning(
        "Connector error on deal %s from service %s — graceful stub active, monitoring.",
        record.deal_id,
        record.source_service,
    )
    return {
        "success": True,
        "action": "connector_fallback_active",
        "note": "Graceful stub engaged; deal pipeline continues degraded.",
    }


async def _auto_fix_payment_failed(record: ExceptionRecord) -> dict[str, Any]:
    """Funding shortfall → reminder with the missing amount (playbook)."""
    try:
        from shared.orchestration.playbooks import remind_funding_shortfall
        return await remind_funding_shortfall(record.deal_id)
    except Exception as exc:
        logger.error("Auto-fix payment_failed failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def _auto_fix_negotiation_failed(record: ExceptionRecord) -> dict[str, Any]:
    """Buyer walked away → contact the next buyer candidate (playbook)."""
    try:
        from shared.orchestration.playbooks import next_buyer
        return await next_buyer(record.deal_id)
    except Exception as exc:
        logger.error("Auto-fix negotiation_failed failed: %s", exc)
        return {"success": False, "error": str(exc)}


async def _escalate_to_hitl(
    record: ExceptionRecord, analysis: AgentAnalysis, playbook: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Raise a HITL approval with RESOLVE_EXCEPTION action. ``playbook`` names
    the remediation APPROVE runs (``callbacks._cb_resolve_exception``)."""
    try:
        from services.hitl_approval import service as hitl_service
        from services.hitl_approval.schemas import CreateApprovalRequest

        playbook = playbook or {}
        name = playbook.get("playbook")
        summary = analysis.summary
        if name:
            summary = f"{summary} — APPROVE runs playbook '{name}'; REJECT leaves the deal as it is."
        req = CreateApprovalRequest(
            deal_id=record.deal_id,
            action="RESOLVE_EXCEPTION",
            summary=summary,
            autonomy_level=settings.escalation_autonomy_level,
            risk_flags=[f"playbook:{name}"] if name else [],
            financial_impact_usd=playbook.get("financial_impact_usd"),
            callback_payload={
                "exception_id": record.exception_id,
                "deal_id": record.deal_id,
                "category": analysis.category.value,
                "severity": analysis.severity.value,
                "root_cause": analysis.root_cause,
                "remediation_steps": analysis.remediation_steps,
                **({"playbook": name, "reason": playbook.get("reason")} if name else {}),
            },
            idempotency_key=f"resolve-{name}-{record.deal_id}-{playbook.get('attempt', 0)}" if name else None,
        )
        approval = await hitl_service.create_approval(req)
        return {"success": True, "approval_id": approval.approval_id, "action": "hitl_escalated",
                **({"playbook": name} if name else {})}
    except Exception as exc:
        logger.error("Failed to create HITL escalation for %s: %s", record.exception_id, exc)
        return {"success": False, "error": str(exc)}


_AUTO_FIX_DISPATCH: dict[ExceptionCategory, Any] = {
    ExceptionCategory.DOCUMENT_ERROR: _auto_fix_document_error,
    ExceptionCategory.CONNECTOR_ERROR: _auto_fix_connector_error,
    ExceptionCategory.PAYMENT_FAILED: _auto_fix_payment_failed,
    ExceptionCategory.NEGOTIATION_FAILED: _auto_fix_negotiation_failed,
}


# ---------------------------------------------------------------------------
# Deterministic playbooks (run before the LLM)
# ---------------------------------------------------------------------------

def _playbooks_enabled() -> bool:
    return os.getenv("EXCEPTION_PLAYBOOKS_ENABLED", "true").strip().lower() not in ("0", "false", "no", "off")


def _infer_category(record: ExceptionRecord) -> str:
    ctx = record.context or {}
    cat = str(ctx.get("category") or "").upper()
    if cat:
        return cat
    stage = str(record.source_event or ctx.get("stage") or "").upper()
    if stage.startswith(("ESCROW_PENDING:", "PI_SENT:", "PI_ACCEPTED:")) or ctx.get("shortfall") is not None \
            or ctx.get("action_required") == "fund_shortfall":
        return "PAYMENT_FAILED"
    if stage.startswith("OUTREACH:") or stage.startswith("NEGOTIATION"):
        return "NEGOTIATION_FAILED"
    if stage.startswith("QC_FAILED"):
        return "QC_FAILED"
    return ""


def _age_days(record: ExceptionRecord) -> float | None:
    since = (record.context or {}).get("since")
    if not since:
        return None
    try:
        dt = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
    except ValueError:
        return None


async def _deterministic_analysis(record: ExceptionRecord) -> tuple[AgentAnalysis, dict[str, Any]] | None:
    """A fixed plan for the recurring failures (no LLM round-trip). Returns
    ``(analysis, playbook_meta)`` or None when the LLM should look at it."""
    cat = _infer_category(record)
    ctx = record.context or {}
    deal = record.deal_id

    def _mk(category, severity, summary, root_cause, steps, decision, rationale, hours):
        return AgentAnalysis(
            category=category, severity=severity, summary=summary, root_cause=root_cause,
            remediation_steps=steps, decision=decision, decision_rationale=rationale,
            estimated_resolution_hours=hours, llm_model="playbook",
        )

    if cat == "QC_FAILED":
        from shared.orchestration.playbooks import reinspection_allowed, reinspection_max
        allowed, failed = await reinspection_allowed(deal)
        if allowed:
            return _mk(
                ExceptionCategory.QC_FAILED, ExceptionSeverity.HIGH,
                f"QC failed on deal {deal} (attempt {failed}) — rework + re-inspection",
                "The pre-shipment inspection found the lot off-specification. Escrow tranches stay "
                "frozen while the deal is in DISPUTE.",
                ["Notify the supplier of the rework (inspection notes attached)",
                 "Book a fresh inspection with the buyer-opted provider",
                 "On PASS the pipeline continues (document pack, T2, freight); on FAIL refund / cancel"],
                RemediationDecision.ESCALATE, "Rework costs the supplier time, not PortLess money — "
                "one operator click confirms the supplier agreed.", 72,
            ), {"playbook": "reinspect", "attempt": failed, "reason": ctx.get("error") or record.raw_detail}
        return _mk(
            ExceptionCategory.QC_FAILED, ExceptionSeverity.CRITICAL,
            f"QC failed again on deal {deal} ({failed} failures, limit {reinspection_max()}) — refund or replace supplier",
            "The supplier could not bring the lot to specification within the allowed re-inspections.",
            ["Refund the undisbursed escrow balance to the buyer (DISPUTE → REFUNDED)",
             "Or re-match the deal to the next supplier candidate", "Record the supplier's QC history"],
            RemediationDecision.CANCEL, "Re-inspection limit reached; money decisions need a human.", 48,
        ), {"playbook": None, "attempt": failed}

    if cat == "PAYMENT_FAILED":
        from shared.orchestration.playbooks import funding_grace_days
        stage = str(record.source_event or ctx.get("stage") or "").upper()
        if ctx.get("shortfall") or ctx.get("action_required") == "fund_shortfall" or "PARTIAL" in stage:
            return _mk(
                ExceptionCategory.PAYMENT_FAILED, ExceptionSeverity.MEDIUM,
                f"Escrow for deal {deal} is short by {ctx.get('shortfall') or 'an amount'} — buyer reminded",
                "The provider confirmed less than the Proforma Invoice total.",
                ["Send the buyer the exact missing amount and the same checkout link",
                 "The PO is held until the escrow is fully funded"],
                RemediationDecision.AUTO_FIX, "A reminder moves no money.", 48,
            ), {"playbook": None}
        age = _age_days(record)
        grace = funding_grace_days()
        if age is not None and age >= grace:
            return _mk(
                ExceptionCategory.PAYMENT_FAILED, ExceptionSeverity.HIGH,
                f"Deal {deal} unfunded for {age:.0f} days (grace {grace}) — cancel and refund any partial funds",
                "The buyer accepted the terms but never funded the escrow despite reminders.",
                ["Refund any partially funded amount to the buyer", "Cancel the deal and tell both parties",
                 "Keep the buyer in the funnel for a future lot"],
                RemediationDecision.ESCALATE, "Cancelling a deal and refunding money needs a human.", 24,
            ), {"playbook": "cancel_unfunded", "reason": f"unfunded for {age:.0f} days", "attempt": int(age)}
        return _mk(
            ExceptionCategory.PAYMENT_FAILED, ExceptionSeverity.LOW,
            f"Deal {deal} waiting for buyer funding ({'%.0f' % age if age is not None else '?'} days; grace {grace})",
            "No funds yet; the deal watcher keeps reminding the buyer.",
            [f"Reminders continue; cancellation is proposed after {grace} days"],
            RemediationDecision.MONITOR, "Inside the grace period.", 24 * grace,
        ), {"playbook": None}

    if cat == "NEGOTIATION_FAILED":
        exhausted = str(record.source_event or "").upper().startswith("OUTREACH:") or ctx.get("action_required") == "find_new_buyers_or_cancel"
        if exhausted:
            return _mk(
                ExceptionCategory.NEGOTIATION_FAILED, ExceptionSeverity.HIGH,
                f"No interested buyer left for deal {deal} — add buyers or cancel",
                "Every identified buyer declined or never replied to the outreach.",
                ["Run a new buyer scan / add buyers to the funnel", "Or cancel the deal"],
                RemediationDecision.ESCALATE, "Needs new leads or a cancellation decision.", 72,
            ), {"playbook": None}
        return _mk(
            ExceptionCategory.NEGOTIATION_FAILED, ExceptionSeverity.MEDIUM,
            f"Negotiation with the buyer of deal {deal} failed — contacting the next buyer candidate",
            "The buyer walked away or the rounds ran out.",
            ["Contact the next pending buyer candidate from the outreach funnel",
             "Re-open the negotiation when a buyer replies with interest"],
            RemediationDecision.AUTO_FIX, "Contacting the next lead costs nothing.", 72,
        ), {"playbook": None}

    if cat in ("DOCUMENT_ERROR", "CONNECTOR_ERROR"):
        category = ExceptionCategory(cat)
        return _mk(
            category, ExceptionSeverity.LOW,
            f"{cat.replace('_', ' ').title()} on deal {deal} — automatic retry",
            record.raw_detail or "transient failure",
            ["Regenerate the document" if cat == "DOCUMENT_ERROR" else "Continue on the graceful stub; monitor"],
            RemediationDecision.AUTO_FIX, "Known transient class.", 1,
        ), {"playbook": None}

    return None


# ---------------------------------------------------------------------------
# LangGraph nodes
# ---------------------------------------------------------------------------

async def _node_analyze(state: AgentState) -> dict[str, Any]:
    """Use LLM to classify the exception and build a remediation plan."""
    record = state["record"]

    if _playbooks_enabled():
        try:
            fast = await _deterministic_analysis(record)
        except Exception as exc:  # noqa: BLE001 — fall back to the LLM
            logger.warning("ExceptionAgent: playbook lookup failed for %s: %s", record.exception_id, exc)
            fast = None
        if fast is not None:
            analysis, meta = fast
            logger.info(
                "ExceptionAgent playbook for %s → category=%s decision=%s playbook=%s",
                record.exception_id, analysis.category.value, analysis.decision.value, meta.get("playbook"),
            )
            return {"analysis": analysis, "error": None, "playbook": meta}

    user_msg = (
        f"Deal ID: {record.deal_id}\n"
        f"Source event: {record.source_event or 'deal.exception'}\n"
        f"Source service: {record.source_service or 'unknown'}\n"
        f"Error detail: {record.raw_detail or 'No detail provided'}\n"
        f"Context: {json.dumps(record.context, default=str)}"
    )

    try:
        client = LLMClient()
        response = await client.chat(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            caller=settings.llm_caller_tag,
            max_tokens=settings.llm_max_tokens,
            temperature=0.1,
        )
        raw = response.content.strip()
        # Strip markdown fences if the model wraps the JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        data = json.loads(raw)
        analysis = AgentAnalysis(
            category=ExceptionCategory(data["category"]),
            severity=ExceptionSeverity(data["severity"]),
            summary=data["summary"],
            root_cause=data["root_cause"],
            remediation_steps=data["remediation_steps"],
            decision=RemediationDecision(data["decision"]),
            decision_rationale=data["decision_rationale"],
            estimated_resolution_hours=int(data.get("estimated_resolution_hours", 24)),
            llm_model=response.model,
        )
        logger.info(
            "ExceptionAgent analyzed %s → category=%s severity=%s decision=%s model=%s",
            record.exception_id,
            analysis.category.value,
            analysis.severity.value,
            analysis.decision.value,
            response.model,
        )
        return {"analysis": analysis, "error": None}

    except LLMExhaustedException as exc:
        logger.error("LLM exhausted for exception %s: %s", record.exception_id, exc)
        # Fallback: conservative ESCALATE with UNKNOWN category
        analysis = AgentAnalysis(
            category=ExceptionCategory.UNKNOWN,
            severity=ExceptionSeverity.HIGH,
            summary=f"Exception on deal {record.deal_id} — LLM unavailable, manual review required.",
            root_cause="LLM gateway exhausted; cannot determine root cause automatically.",
            remediation_steps=[
                "Review exception context manually.",
                "Determine appropriate remediation action.",
                "Update deal status accordingly.",
            ],
            decision=RemediationDecision.ESCALATE,
            decision_rationale="LLM unavailable — escalating to operator for manual review.",
            estimated_resolution_hours=4,
            llm_model="fallback",
        )
        return {"analysis": analysis, "error": str(exc)}

    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        logger.error(
            "ExceptionAgent: LLM returned unparseable response for %s: %s",
            record.exception_id,
            exc,
        )
        analysis = AgentAnalysis(
            category=ExceptionCategory.UNKNOWN,
            severity=ExceptionSeverity.HIGH,
            summary=f"Exception on deal {record.deal_id} — parse error, escalating.",
            root_cause=f"LLM response could not be parsed: {exc}",
            remediation_steps=["Manual review required."],
            decision=RemediationDecision.ESCALATE,
            decision_rationale="LLM output was malformed — conservative escalation.",
            estimated_resolution_hours=2,
            llm_model="parse_error",
        )
        return {"analysis": analysis, "error": str(exc)}


async def _node_execute(state: AgentState) -> dict[str, Any]:
    """Execute the remediation decision produced by _node_analyze."""
    record = state["record"]
    analysis = state["analysis"]

    if analysis is None:
        return {"execution_result": {"success": False, "error": "analysis missing"}}

    decision = analysis.decision
    playbook = state.get("playbook") or None

    if decision == RemediationDecision.AUTO_FIX:
        handler = _AUTO_FIX_DISPATCH.get(analysis.category)
        if handler:
            result = await handler(record)
            if not result.get("success") and playbook is not None:
                # Playbook auto-fix found nothing to do → hand to the operator
                # (keeps the record ESCALATED with an approval, not UNRESOLVABLE).
                escalated = await _escalate_to_hitl(record, analysis, playbook)
                result = {**escalated, "auto_fix_fell_through": True, "auto_fix_error": result.get("error")}
        else:
            # No automated handler for this category → fall through to ESCALATE
            logger.info(
                "No AUTO_FIX handler for category %s — escalating instead.",
                analysis.category.value,
            )
            result = await _escalate_to_hitl(record, analysis, playbook)
            result["auto_fix_fell_through"] = True
        return {"execution_result": result}

    if decision == RemediationDecision.ESCALATE:
        result = await _escalate_to_hitl(record, analysis, playbook)
        return {"execution_result": result}

    if decision == RemediationDecision.CANCEL:
        logger.warning(
            "ExceptionAgent recommends CANCEL for deal %s — operator must confirm.",
            record.deal_id,
        )
        result = await _escalate_to_hitl(record, analysis, playbook)
        result["cancel_recommended"] = True
        return {"execution_result": result}

    # MONITOR
    return {
        "execution_result": {
            "success": True,
            "action": "monitoring",
            "note": "Exception logged, no immediate action required.",
        }
    }


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def _build_graph():
    builder = StateGraph(AgentState)
    builder.add_node("analyze", _node_analyze)
    builder.add_node("execute", _node_execute)

    builder.set_entry_point("analyze")
    builder.add_edge("analyze", "execute")
    builder.add_edge("execute", END)

    return builder.compile()


_graph = None


def get_agent_graph():
    global _graph
    if _graph is None:
        _graph = _build_graph()
    return _graph


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_agent(record: ExceptionRecord) -> tuple[AgentAnalysis | None, dict[str, Any]]:
    """Run the exception resolver agent for the given record.

    Returns:
        (analysis, execution_result) — analysis is None only if the graph crashes
        at the analyse node before producing any output.
    """
    initial: AgentState = {
        "record": record,
        "analysis": None,
        "execution_result": {},
        "error": None,
        "playbook": None,
    }
    final = await get_agent_graph().ainvoke(initial)
    return final.get("analysis"), final.get("execution_result", {})
