# Disaster recovery

For the founder and the engineer who restores PortLess after a loss of the hosting region, the
database or the document store. Targets are proposals until the founder signs them off (to-do
"Decisions needed").

## Targets

| What | RPO (data we may lose) | RTO (time to be back) | Why |
|---|---|---|---|
| Money records (escrow, ledger, payouts, approvals, audit log) | 0 — rebuilt from the providers | 4 hours | The escrow and payout providers hold the money and its history; PortLess re-reads them. |
| Deals, negotiations, e-mail history | 5 minutes (Atlas continuous backup) | 4 hours | Replies are also in the mailbox and can be re-imported. |
| Directory, scans, price book, caches | 24 hours | 1 day | Rebuilt by the next scans; costs Apify runs, not money. |
| Trade documents (PI, PO, CI …) | 0 — Google Drive | 1 day | Each record keeps the SHA-256; a document can be re-rendered from the deal. |

## What must exist before an incident

- MongoDB Atlas continuous backup with point-in-time restore on the production cluster, and a
  restore drill at least every quarter (to-do section 7).
- The Google Drive document folder (`GOOGLE_DRIVE_ROOT_FOLDER`) shared with a second account.
- Provider credentials and webhook secrets in the secrets manager, not only on the host.
- A second region / host where the backend image can start (the app is one container:
  `docker compose up` / `make up`).

## Region or host lost

1. **Pause** is not possible while the backend is down — nothing moves money without it, and
   providers keep the money safe meanwhile.
2. Start the backend image in the standby region with the same environment.
3. Point it at the Atlas cluster (or the restored copy). `ENV_PROFILE=production` refuses to
   boot while a readiness check fails — fix those, never override with real money.
4. **Pause immediately after boot** (`POST /hitl-approval/kill-switch`), then reconcile:
   `GET /escrow-guard/reconciliation/providers`, poll every open escrow
   (`POST /escrow-guard/escrows/{id}/poll`) and every open QC / freight order.
5. Update the provider webhook URLs and `PORTLESS_PUBLIC_BASE_URL` if the address changed.
6. Resume once reconciliation is clean.

## Database lost or corrupted

1. Restore Atlas to the last good point in time into a new cluster.
2. Start the backend paused against it; run the reconciliation as above — the providers are the
   truth for money; anything they show that the restored data does not becomes an exception for
   the desk.
3. Verify the audit log chain: `GET /hitl-approval/audit/verify`. A break marks the restore point;
   note it in the incident report.
4. Re-import mail received since the restore point (IMAP poll starts from the mailbox).

## Document store lost

Documents are re-rendered from the deal facts (`POST /docugen/…` per type, idempotent per deal ×
type); compare the new SHA-256 with the one on the record — a difference means the facts changed
and the document must be re-issued, not silently replaced.

## After every recovery

Write what happened, how long it took against the targets, and what to change; add the changes
to the to-do list.
