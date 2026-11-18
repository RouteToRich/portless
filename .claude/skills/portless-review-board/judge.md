# Judge — merge, rank, accept

You receive every member's findings (`docs/reviews/<date>-board/*.jsonl`). Your job is a short,
correct list the founder can act on, not a long one.

1. **Evidence check** — open the cited file / endpoint / screen. A finding whose evidence does not
   show the problem is `rejected: evidence does not show it`.
2. **Already known** — search `todo` (open and done items) for the same gap. Listed → `rejected:
   already in todo (<line>)`; listed but the finding adds a new fact → keep it as a sharper item and
   say which line it extends.
3. **Merge** — the same root cause seen by several members becomes one finding; keep the strongest
   evidence and list the members.
4. **Rank** — score = money (high 3 · medium 2 · low 1) × risk (3 · 2 · 1); ties: P0 before P1 before
   P2, then fewer days to fix. Money means what PortLess gains or stops losing per deal or per month.
5. **Priority** — P0 only when money, the law or data can be lost on the first real trade; P1 before
   running more than a few deals a month; P2 improvements.
6. **Accept** at most 40 findings per run; the rest are `rejected: below the cut` (they come back next
   run if still true).
7. Write `judged.json`:
   ```json
   {"date": "YYYY-MM-DD", "findings": [{"id": "B-01", "status": "accepted", "title": "…", "members": ["cfo","trader"],
     "evidence": "…", "fix": "…", "why_money": "…", "priority": "P1", "owner": "Eng", "area": "4",
     "money": "high", "risk": "medium", "score": 6}, {"id": "B-02", "status": "rejected", "title": "…", "reason": "…"}]}
   ```
