---
name: portless-review-board
description: Review board for the PortLess trading app — eight members with different roles (commodity trader, CFO, compliance & legal, operations, buyer and supplier view, CEO / growth, engineer / SRE / security, operator UX) review the backend, the console, the e-mails and documents, the tests and the to-do list; a judge merges and ranks the findings by money at stake and risk; every accepted finding becomes a task in `todo` (right priority and area, owner, why it makes or saves money, never twice); the report goes to docs/reviews/<date>-board.md. Trigger with /portless-review-board [member] [--report-only] [--max N].
---

# /portless-review-board — find every gap that costs PortLess money, and put it on the list

**Modes**

| Invocation | What happens |
|---|---|
| `/portless-review-board` | All eight members review in turn, then the judge, then the accepted findings are appended to `todo`. |
| `/portless-review-board <member>` | One member only (`trader`, `cfo`, `compliance`, `operations`, `counterparty`, `ceo`, `engineer`, `operator`), then the judge on its findings. |
| `… --report-only` | Write the report, add nothing to `todo`. |
| `… --max N` | At most N findings per member (default 12) — the most valuable ones. |

## Rules for every member

- **Read before you judge.** Start from `CLAUDE.md`, the `todo` (open *and* done items — a finding already listed is not new), then the code your brief points to. In this repo: `services/`, `agents/`, `shared/orchestration/`, `shared/connectors/`, `tests/`, `docs/testing/`; the console is `../portless-ui/src`. Where the backend is running, `GET /health/readiness`, `/health/watchers` and the automation results (`/automation/runs`) are evidence too.
- **Never read or print secrets** (`.env`, `*.creds`, tokens). Real deal data only through the app's own endpoints, never copied into the report.
- **Every finding has evidence**: a file and line, an endpoint and its answer, a console screen, an e-mail or document text, or a test that shows it. No evidence → no finding.
- **Say what it costs.** Each finding states the money at stake (a lost deal, a margin leak, a fine, a frozen payment, an operator hour) as concretely as the code allows, and the fix in one or two sentences.
- **Specific, not generic.** "Add monitoring" is not a finding; "the escrow watcher's auto-release has no alert when it fails (services/escrow_guard/service.py:1760) — a lapsed window keeps the supplier's last 10 % unpaid" is.
- **Stay in your lane** (your brief), but note a cross-role issue you trip over in one line for the judge.

## The flow

1. **Members** — for each member in `members/` (or the one named): read its brief, review, and write its findings as JSON lines in `docs/reviews/<date>-board/<member>.jsonl`, one object per finding:
   ```json
   {"member": "cfo", "title": "…", "evidence": "services/escrow_guard/service.py:2010 …", "impact": "…", "money": "high|medium|low", "risk": "high|medium|low", "priority": "P0|P1|P2", "owner": "Eng|Ops|Legal|Biz|QA", "area": "4", "fix": "…", "why_money": "one line: how it makes or saves money"}
   ```
2. **Judge** — follow `judge.md`: merge duplicates across members, drop what is already in `todo`, drop findings without evidence, rank by money × risk, and mark each one `accepted` or `rejected` (with the reason). Write `docs/reviews/<date>-board/judged.json`.
3. **Report** — `docs/reviews/<date>-board.md`: a scorecard per member (0–10 per lens with one sentence), the ranked accepted findings (B-01 …, with evidence and fix), the rejected ones with reasons, and the three moves that would raise profit per deal the most.
4. **To-do** (unless `--report-only`) — run
   ```bash
   python -X utf8 .claude/skills/portless-review-board/scripts/append_findings.py docs/reviews/<date>-board/judged.json
   ```
   It appends every accepted finding under `### 22.2 Review-board findings (<date>)` in the right priority section, skips anything whose title already matches an open or done item, tags each line `(review-board B-nn)` and recounts the metrics block.
5. **Learn** — add the lessons of this run to the members' checklists (`members/*.md`, section "Learned"), so the next run starts from them.
6. **Tell the user** the counts (findings per member, accepted, added to `todo`), the top five by money, and where the report is.

Re-run the board after each batch of fixes and compare the reports (scorecards should rise, the same finding must not come back).
