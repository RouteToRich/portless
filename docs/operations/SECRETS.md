# Secrets and key rotation

For the engineer who deploys PortLess and the founder who owns the provider accounts.

## Where secrets live

- **Production:** the hosting platform's secret store / environment (never a `.env` file in the
  image, never in git — the pre-commit hook refuses `.env`, `*.creds` and the LLM tracking file).
  Move to a dedicated secrets manager (cloud KMS-backed) before real money; to-do section 7.
- **Local development:** `.env` (git-ignored), Mongo + LLM gateway login only; provider keys
  stay placeholders so live calls fail visibly.
- Check what an environment sets without printing values:
  `python scripts/env_parity.py staging=staging.env production=prod.env`.

## Inventory and rotation

| Secret | Owner | Rotate | How to rotate without downtime |
|---|---|---|---|
| `TAZAPAY_API_KEY` / `_API_SECRET` | founder | yearly, and on staff change | create the new key in Tazapay → set it → redeploy → revoke the old key |
| `TAZAPAY_WEBHOOK_SECRET`, `ESCROW_COM_WEBHOOK_SECRET`, `PAYONEER_WEBHOOK_SECRET`, `MAERSK_WEBHOOK_SECRET`, `*_WEBHOOK_SECRET` | engineer | yearly | change at the provider and in the environment in the same window; refused signatures show in `GET /health/failures`; missed events are picked up by the provider polls |
| `ESCROW_COM_API_KEY` | founder | yearly | as Tazapay |
| `PAYONEER_CLIENT_ID` / `_SECRET` | founder | yearly | as Tazapay |
| `QIMA_API_ACCESS_TOKEN`, `QIMA_PASSWORD` | founder | on QIMA's schedule / staff change | new token → set → redeploy |
| `MAERSK_CONSUMER_KEY` / `_CLIENT_SECRET` | founder | yearly | Maersk developer portal: second app key, switch, delete the old |
| `LLM_GATEWAY_PASSWORD` / `LLM_GATEWAY_TOKEN` | engineer | every 90 days | change the service account password in auth-service, set, redeploy (the client re-signs-in on a 401) |
| `APIFY_TOKEN`, `TAVILY_API_KEY`, `DATA_GOV_IN_API_KEY`, `API_SETU_KEY`, `COMTRADE_SUBSCRIPTION_KEY` | engineer | yearly | create new, set, redeploy, revoke old |
| `GMAIL_APP_PASSWORD` / IMAP password | engineer | every 90 days | new app password, set, redeploy; sends retry on failure |
| `GOOGLE_DRIVE_REFRESH_TOKEN` | engineer | when revoked | `python scripts/google_drive_authorize.py` |
| `FIELD_ENCRYPTION_KEY` | engineer | only with a re-encryption run | keep the old key readable until every bank record is re-encrypted with the new one |
| `ESCROW_PROOF_LINK_SECRET`, `UNSUBSCRIBE_LINK_SECRET` | engineer | yearly | rotating invalidates links already sent: unsubscribe links in sent e-mails stop working — announce a grace period or keep the old secret as fallback |
| `HITL_NOTIFY_WEBHOOK_SECRET`, `HITL_TELEGRAM_BOT_TOKEN` | engineer | yearly | set and redeploy |
| NATS `.creds` | engineer | yearly | issue new creds in Synadia, deploy, revoke |
| `MONGO_URI` password | engineer | every 90 days | add a second DB user, switch, delete the first |

## On a suspected leak

1. Pause money (Kill Switch) if a money key is involved.
2. Revoke the key at the provider first, then set the new one.
3. Read the audit log and the provider's own activity log for the exposure window.
4. Record the incident and add the cause to the to-do list.
