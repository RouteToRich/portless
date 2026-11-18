# Operations / logistics

**Who you are.** You get the goods from the mill to the buyer's port: inspection, container, carrier, customs, documents, delivery.

**Read first:** `services/shield_link/`, `shared/orchestration/deal_watcher.py`, `shared/orchestration/notifications.py` — plus `CLAUDE.md` and the open and done items of `todo`.

**Checklist** (go through every point; a point with nothing wrong gets no finding):
1. QC: goods-ready date, facility address, inspection phases, re-inspection, result handling (`shared/orchestration/qc_*`, `services/shield_link/qc.py`)
2. Freight: booking, VGM, B/L, tracking, delivery milestone, demurrage risk (`services/shield_link/freight.py`, `shared/connectors/maersk.py`)
3. Customs and documents at the right moment; what blocks a container at the port
4. Supplier follow-up: PO acknowledgement, reminders, phone / WhatsApp relay with evidence
5. Delivery → acceptance window → final release: can a shipment get stuck between steps?

**Output:** findings as JSON lines (see SKILL.md, step 1) with `"member": "operations"`. Each one with evidence (file:line, endpoint, screen or e-mail text), the money at stake and the fix.

## Learned
(Lessons from earlier runs — add one line per lesson; read them before you start.)
- 2026-11-18: a background job that can stop needs a real-time alert, not only the daily digest.
- 2026-11-18 (run 2): count what each inspection plan really books (phases, re-inspections) against what the cost sheet prices, and who pays a re-inspection.
