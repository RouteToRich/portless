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


async def _escalate_to_hitl(record: ExceptionRecord, analysis: AgentAnalysis) -> dict[str, Any]:
    """Raise a HITL approval with RESOLVE_EXCEPTION action."""
    try:
        from services.hitl_approval import service as hitl_service
        from services.hitl_approval.schemas import CreateApprovalRequest

        req = CreateApprovalRequest(
            deal_id=record.deal_id,
            action="RESOLVE_EXCEPTION",
            summary=analysis.summary,
            autonomy_level=settings.escalation_autonomy_level,
            callback_payload={
                "exception_id": record.exception_id,
                "deal_id": record.deal_id,
                "category": analysis.category.value,
                "severity": analysis.severity.value,
                "root_cause": analysis.root_cause,
                "remediation_steps": analysis.remediation_steps,
            },
        )
        approval = await hitl_service.create_approval(req)
        return {"success": True, "approval_id": approval.approval_id, "action": "hitl_escalated"}
    except Exception as exc:
        logger.error("Failed to create HITL escalation for %s: %s", record.exception_id, exc)
        return {"success": False, "error": str(exc)}


_AUTO_FIX_DISPATCH: dict[ExceptionCategory, Any] = {
    ExceptionCategory.DOCUMENT_ERROR: _auto_fix_document_error,
    ExceptionCategory.CONNECTOR_ERROR: _auto_fix_connector_error,
}


# ---------------------------------------------------------------------------
# LangGraph nodes
# ---------------------------------------------------------------------------

async def _node_analyze(state: AgentState) -> dict[str, Any]:
    """Use LLM to classify the exception and build a remediation plan."""
    record = state["record"]

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

    if decision == RemediationDecision.AUTO_FIX:
        handler = _AUTO_FIX_DISPATCH.get(analysis.category)
        if handler:
            result = await handler(record)
        else:
            # No automated handler for this category → fall through to ESCALATE
            logger.info(
                "No AUTO_FIX handler for category %s — escalating instead.",
                analysis.category.value,
            )
            result = await _escalate_to_hitl(record, analysis)
            result["auto_fix_fell_through"] = True
        return {"execution_result": result}

    if decision == RemediationDecision.ESCALATE:
        result = await _escalate_to_hitl(record, analysis)
        return {"execution_result": result}

    if decision == RemediationDecision.CANCEL:
        logger.warning(
            "ExceptionAgent recommends CANCEL for deal %s — operator must confirm.",
            record.deal_id,
        )
        result = await _escalate_to_hitl(record, analysis)
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
    }
    final = await get_agent_graph().ainvoke(initial)
    return final.get("analysis"), final.get("execution_result", {})
