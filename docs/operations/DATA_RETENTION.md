# Data retention

For the founder and the lawyer / CA confirming PortLess's record keeping. The periods below are
settings with proposed defaults — they need the lawyer / CA's confirmation (to-do 3.7).

| Data | Kept for | Why | Setting / mechanism |
|---|---|---|---|
| Deals past the PI (contracts), PI / PO / CI / PL / Shipping Bill, escrow ledger, journal, payouts | at least 8 years after the deal | GST (CGST Act s. 36), customs, FEMA export realisation | `RECORD_RETENTION_YEARS` (8); the evidence pack (`GET /deals/{id}/evidence-pack`) is the hand-over copy |
| Audit log (approvals, money relays, Kill Switch) | as long as the deals it covers | proof of who decided what | insert-only, hash-chained; never pruned |
| Trade documents in Google Drive | as the deal | the document itself | Drive folder per deal; SHA-256 on the record |
| Personal contact data of counterparties never contacted | 730 days after last seen | DPDP Act 2023 — keep only what is needed | `PERSONAL_DATA_RETENTION_DAYS`; `POST /trade-min/personal-data/retention` |
| Personal data on request | erased at once, except what signed deals' statutory records need | DPDP right to erasure vs statutory retention | `POST /trade-min/{buyers|suppliers}/{id}/erase` reports what is held and until when |
| Prospects (no contract) | closed after 60 days untouched; records kept | trading history | `PROSPECT_RETENTION_DAYS` |
| Scan data, price book, caches | rebuilt by the next scans | no legal need | TTLs per cache |

## Rules

- Nothing a statutory record needs is deleted before its retention ends — an erase request on
  such a company removes the directory copy and keeps the deal's own records.
- Backups (Atlas point-in-time, Drive) must outlive the longest retention period above.
- Deleting anything after the period is a deliberate, recorded action (audit log), never a side
  effect of a clean-up.
