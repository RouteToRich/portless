# Escrow providers — Tazapay and Escrow.com

The buyer chooses which neutral escrow holds their money. PortLess never holds
the principal and **never moves it by itself**: for each stage PortLess sends
the proof to the provider, and the provider pays the stage out.

| | Tazapay | Escrow.com |
|---|---|---|
| API | v3 — `developer.tazapay.com/openapi.yaml` | 2017-09-01 — `escrow.com/api/docs` |
| Base URL | `https://service.tazapay.com` (sandbox `https://service-sandbox.tazapay.com`) | `https://api.escrow.com/2017-09-01` (sandbox `https://api.escrow-sandbox.com/2017-09-01`) |
| Auth | Basic `API_KEY:API_SECRET` | Basic `account-email:API_KEY` |
| Open | `POST /v3/escrow` (buyer = `customer_details`, amount in minor units) → `data.url` hosted pay page | `POST /transaction`, buyer + seller (`me`), one `milestone` item per stage |
| Buyer pays | on Tazapay's page (card / bank / local methods) | wire to Escrow.com, from the invitation Escrow.com e-mails the buyer |
| A stage is released | PortLess uploads the stage's documents (`POST /v3/document`, `reference` = escrow id); **Tazapay checks them and pays out** | PortLess ships the milestone (`PATCH /transaction/{id}/item/{item}` `action: ship`); **the buyer accepts it on Escrow.com** (or its inspection period lapses) and Escrow.com pays out — a partner cannot accept for the buyer |
| Last stage (10 %) | delivery document after the buyer's acceptance or the acceptance window lapsing | milestone shipped when the acceptance window opens; the buyer accepts on Escrow.com |
| Refund | `POST /v3/refund` (`payin` = escrow id) | `PATCH /transaction/{id}` `action: cancel` |
| Webhook | `POST /escrow-guard/webhooks/tazapay`; header `signature` = base64(HMAC-SHA256(secret, event id + raw body + created_at)) | `POST /escrow-guard/webhooks/escrow-com?token=<ESCROW_COM_WEBHOOK_SECRET>` (Escrow.com does not sign) |

## How a deal uses it

1. **Choice.** The buyer names a provider in a reply ("we prefer Escrow.com") —
   the negotiation reads it (`detect_escrow_provider_mention`, negation-aware)
   and records it on the deal (`escrow_provider`, `escrow_provider_source=buyer`).
   Staff can set it: `PUT /deals/{id}/escrow-provider` `{provider, reason}`
   (refused once the escrow is open). The first priced buyer e-mail offers the
   choice. Order used: request → the PI the buyer accepted → the deal → the
   negotiation session → `ESCROW_DEFAULT_PROVIDER`; only providers in
   `ESCROW_PROVIDERS_ENABLED`.
2. **PI** prints the provider; **PI accepted** opens the escrow there
   (`create_escrow`), the buyer's funding e-mail names it and says who releases
   each stage.
3. **Funding** comes from the provider: its webhook (re-read — the payload alone
   moves nothing), the escrow watcher's read every `ESCROW_POLL_MINUTES`, or
   `POST /escrow-guard/escrows/{id}/poll`. Less than the PI → `PARTIALLY_FUNDED`
   (no PO). Escrow.com takes money only from the buyer's account, so the buyer
   is recorded as payer; Tazapay does not report the payer, so the compliance
   payer check asks a person (`payer_unknown`, review).
4. **Each stage** (`RELEASE_ESCROW_TRANCHE` gate approved) → `release_tranche`
   sends the proof → tranche `SUBMITTED` (`escrow.tranche_submitted`) → counts
   as released (`escrow.tranche_released`, ledger, supplier payouts) only when
   the provider has paid it out. A refused document / rejected milestone →
   dispute.
5. **Staff relay**: `POST /escrow-guard/escrows/{id}/tranches/{tid}/provider-confirmed`
   `{provider_ref, reason}` for a payout seen on the provider's dashboard — only
   for a stage already `SUBMITTED`, audited on the escrow.

## Release documents

Tazapay downloads every document from a URL, and PortLess's documents are in a
private Drive. Each stage therefore gets a signed, expiring link to a proof
sheet (`GET /escrow-guard/proof/{escrow}/{tranche}?exp=&sig=`, HMAC with
`ESCROW_PROOF_LINK_SECRET`, `ESCROW_PROOF_LINK_DAYS`): stage, amount, proof
references and the SHA-256 of each buyer-side document — never the supplier or
its price. Proofs that are already URLs (QC report, tracking page) are sent too.
Without `PORTLESS_PUBLIC_BASE_URL` + `ESCROW_PROOF_LINK_SECRET` a Tazapay release
is refused with a message saying so.

## Deal board

`DealRecord.escrow_provider` / `escrow_status` / `escrow_status_detail`
(`shared/orchestration/escrow_status.py`): Awaiting buyer payment · Part-funded
· Funded · Payout with provider · Releasing · Buyer acceptance window ·
Disputed · Settled · Refunded · Escrow not opened. The console shows the chip on
every deal card, the table and the deal header; the Deal page escrow card shows
stages "with Tazapay / Escrow.com", the provider's own status and a "Check now"
button.

## Not confirmed from the public documentation

- **Tazapay v3** documents escrow create / fetch / update / expire, document
  upload and refund, but **no release endpoint and no escrow-specific webhook
  event names**: release is Tazapay's own step after document verification.
  PortLess reads a release from a document marked verified in
  `transaction_documents[].status`, or from a webhook whose type is in
  `TAZAPAY_RELEASE_EVENTS` (default `escrow.verification.succeeded,
  escrow.released, escrow.release.succeeded`) carrying the document's
  `metadata.tranche_id`. Confirm both with Tazapay for the account (and whether
  one escrow can be paid out in stages); the staff relay covers the gap.
- **Escrow.com** documents no webhook signature — hence the URL token and the
  re-read; the schedule `status.secured` / `disbursed_to_beneficiary` flags are
  read for funding and payout.

## Settings

```
ESCROW_PROVIDERS_ENABLED=tazapay,escrow_com   ESCROW_DEFAULT_PROVIDER=tazapay   ESCROW_POLL_MINUTES=30
TAZAPAY_API_KEY / _API_SECRET / _WEBHOOK_SECRET / _ENV / _BASE_URL / _WEBHOOK_URL / _FEE_PCT / _RELEASE_EVENTS
ESCROW_COM_API_KEY / _EMAIL / _WEBHOOK_SECRET / _ENV / _BASE_URL / _INSPECTION_DAYS / _FEE_PCT
PORTLESS_PUBLIC_BASE_URL   ESCROW_PROOF_LINK_SECRET   ESCROW_PROOF_LINK_DAYS
ESCROW_SANDBOX_PROVIDERS / ESCROW_SANDBOX_URL   # simulator — automation + tests only (readiness fails in production)
```

Readiness: `escrow_provider` fails while an offered provider has no keys,
`escrow_proof_links` warns without the proof-link settings, `escrow_sandbox`
fails in production while the simulator is on.

## Simulator (automation and tests)

`services/escrow_sandbox` (`make escrow-sandbox`, port 8015, or in process)
implements both contracts above with the providers' validation rules, plus
controls that play the buyer and the provider: `/tazapay/_sandbox/escrow/{id}/pay`,
`/tazapay/_sandbox/document/{id}/verify`, `/escrow-com/_sandbox/transaction/{id}/pay`,
`…/item/{id}/accept|reject`, and `auto_verify` / `auto_accept` (on by default) so
automation journeys flow. The automation worker sets
`ESCROW_SANDBOX_PROVIDERS=tazapay,escrow_com`; seeded deals open their escrow on
the simulator; the journeys fund it there and PortLess learns it by reading the
provider.
