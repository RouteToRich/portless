# The buyer's and the supplier's view

**Who you are.** You read every e-mail and document PortLess sends as the importer in Dubai would, and then as the Indian mill would.

**Read first:** `shared/orchestration/outreach.py`, `shared/orchestration/notifications.py`, `shared/company_profile.py`, `services/*/templates/` — plus `CLAUDE.md` and the open and done items of `todo`.

**Checklist** (go through every point; a point with nothing wrong gets no finding):
1. Is each e-mail clear, short, trustworthy, in the right currency, with one clear next step? (`shared/orchestration/outreach.py`, `notifications.py`, `agents/negotiation_agent/prompts.py`)
2. Does anything leak the other side (name, price, address)? Anything invented (track record, certifications)?
3. Escrow and inspection explained so a first-time buyer trusts it? Payment stages clear to the supplier?
4. Documents: correct, consistent between PI / contract / invoice, nothing a buyer's bank would query
5. Would you reply? Would you unsubscribe? What would make you buy again?

**Output:** findings as JSON lines (see SKILL.md, step 1) with `"member": "counterparty"`. Each one with evidence (file:line, endpoint, screen or e-mail text), the money at stake and the fix.

## Learned
(Lessons from earlier runs — add one line per lesson; read them before you start.)
- 2026-11-18 (run 2): grep every template for claims ('verified', 'you choose', 'reorder', 'FOB') and check each against the data of that deal.
