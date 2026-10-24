# Event-Driven Architecture — Should PortLess Go There?

> **Question:** Should PortLess move from its current REST/in-process orchestration
> to an event-driven architecture (EDA)? This is a **personal, single-user app**,
> so the analysis is weighted toward *simplicity and $0 cost*, not scale.
>
> **TL;DR:** You do **not** need a message broker. But you are already 80% modelled
> for events and 0% wired. The sweet spot for a personal app is a **Tier 1
> in-process async event bus** (replace the existing `NoopPublisher`) — it's
> essentially free, adds no infrastructure, fixes a real gap (nothing consumes
> `negotiation.agreed` today), and cleanly decouples notifications and fan-out.
> Reach for Azure only for the 2–3 things that genuinely benefit from **durable,
> retryable, or push** delivery (webhooks, background jobs, real-time UI).

---

## 1. Where you already are

PortLess is **event-modelled but not event-driven**:

| Already in place | File | State |
|---|---|---|
| Event catalog (24 topics) | `shared/domain/events.py` → `EventTopic` | ✅ Defined |
| Canonical envelope | `shared/domain/events.py` → `DomainEvent{event_id, topic, deal_id, occurred_at, payload}` | ✅ Defined |
| Publisher seam | `shared/bus/publisher.py` → `EventPublisher` protocol + `NoopPublisher` | ⚠️ **No-op** (publishes nowhere) |
| Publish calls | services call `get_publisher().publish(DomainEvent(...))` after committing | ⚠️ Fire into the void |
| Inbound events (already!) | provider **webhooks** (Stripe, SGS, Cogoport, Freightos, Payoneer) | ✅ Real event ingress |
| Push to UI | HITL **SSE** stream (`/hitl-approval/stream`) | ✅ One-way push |
| Polling loops (event *candidates*) | Email Gateway IMAP poll (15 min), HITL timeout watcher (poll), Trade-Min APScheduler cron | ⏳ Time-driven, could be event-driven |

**Consequence of the no-op:** state changes propagate by **direct calls** today
(`dealmaker → deals.update_status`, `shield_link → deals.update_status`,
`hitl_approval.callbacks → dealmaker/shield/escrow`). This works, but it's why
the **`negotiation.agreed` → deal-status transition is currently unwired** — the
event is "published" to `NoopPublisher` and no one is listening. That single gap
is the clearest argument for at least a Tier 1 bus.

---

## 2. What "event-driven" would actually mean here

Two independent axes — don't conflate them:

| Axis | Option A (today) | Option B (event-driven) |
|---|---|---|
| **Coordination** | **Orchestration** — a caller directly invokes the next step | **Choreography** — a service emits an event; interested services react |
| **Transport** | In-process function calls / REST | In-process async bus **or** a message broker (queue/topic) |

For a personal app you can adopt **choreography over an in-process async bus**
and get most of the decoupling benefit with **none** of the broker cost. A broker
only earns its keep when you need durability across restarts, retries with
back-off, or delivery across separate hosts/processes.

---

## 3. Pros & cons for a *personal single-user app*

### Pros of going event-driven
- **Decoupling** — `deals` stops being called by six services; instead it *reacts*
  to `negotiation.agreed`, `escrow.funded`, `qc.passed`, … Fewer import cycles
  (today handled by lazy imports).
- **Fixes the known gap** — a subscriber on `negotiation.agreed/failed` transitions
  the deal without the negotiation agent reaching into `deals`.
- **Fan-out for free** — one event → many reactions (transition + notify UI + audit)
  without the emitter knowing the consumers.
- **Natural retries & durability** (only with a broker) — a failed QC booking or
  payout can be retried/dead-lettered instead of raising inline.
- **Kills polling** (only with push) — Gmail push / webhooks replace the 15-min
  IMAP poll and the HITL timeout poll.
- **Future-proofing** — the modelling already exists; wiring it is low-risk.

### Cons / costs (heavier for a single-user app)
- **Operational complexity** — a broker is one more thing to run, secure, and pay
  for. For one user this is usually **not worth it**.
- **Eventual consistency** — "agreed" and "deal moved" are no longer atomic; the UI
  may briefly show a stale state. Fine here, but a mental-model shift.
- **Harder debugging** — a call stack becomes a trace across handlers; you need
  correlation IDs (you already have `event_id`/`deal_id` — use them).
- **At-least-once → idempotency required** — brokers redeliver; every handler must
  be idempotent (some services already are: escrow "funded" is idempotent, email
  dedupes by `message_id`, TPO/PENDING records exist — good foundation).
- **Ordering** — events can arrive out of order; the deal **state machine already
  guards this** (`can_transition` rejects illegal jumps), which is a big safety net.
- **Cost creep** — Service Bus *Standard* (topics), Event Hubs, and Redis are **not
  free**; only some Azure services have a true free grant (see §5).

### Verdict
- **In-process choreography (Tier 1): do it.** ~Free, low risk, real payoff.
- **Message broker (Tier 2/3): skip for now.** Add only for the specific
  async/durable/push needs in §6, and only the free-tier services in §5.

---

## 4. Recommended tiers

| Tier | What | Infra | Cost | Recommendation |
|---|---|---|---|---|
| **0** | Keep REST/in-process orchestration | none | $0 | Acceptable — nothing is broken |
| **1** | **In-process async event bus** (replace `NoopPublisher`); services subscribe to topics | none | ~$0 | ✅ **Recommended default** |
| **2** | Durable queue + worker for a *few* async/retryable jobs (connector calls, background scans) | Storage Queue / Service Bus Basic + Functions | ~$0–small | Optional, targeted |
| **3** | Full broker with topics/subscriptions across hosts | Service Bus Standard / Event Hubs / Kafka | $10+/mo | ❌ Overkill for personal use |

---

## 5. Azure options for event-driven — free / near-free

> Free tiers change — verify current terms. "Near-$0" = pay-per-op that rounds to
> nothing at personal volume.

| Azure service | Fit | Free / cost | Notes |
|---|---|---|---|
| **Event Grid** | Reactive events, webhook-style fan-out | **100k operations/month free** (always-free grant) | Great for "when X happens, call handler". Push, not a queue |
| **Storage Queues** | Simple durable job queue | Pennies (storage acct); 12-mo free includes storage | Cheapest durable queue; poll or trigger a Function |
| **Azure Functions** | Event **handlers** (queue/EventGrid/timer/HTTP triggers) | **1M executions + 400k GB-s/month free** (consumption) | Replace APScheduler crons with a **Timer trigger**; run reactions serverless |
| **SignalR Service** | Real-time push to a UI | **Free tier: 20 connections, 20k msgs/day** | Replaces HITL SSE polling with managed push |
| **Web PubSub** | Real-time push (WebSocket) to a UI | **Free tier: 20 connections, 20k msgs/day** | Alternative to SignalR |
| **Service Bus — Basic** | Queues (point-to-point) | ~$0.05 / **million** ops (no monthly base) | Near-$0 personally; **queues only** — no topics/subscriptions |
| **Service Bus — Standard** | **Topics + subscriptions** (real pub/sub) | ~$10/mo base | Only if you need broker-grade pub/sub — usually skip |
| **Event Hubs — Basic** | High-throughput streaming | ~$11/mo | Overkill for personal use |
| **Cosmos DB change feed** | DB-as-event-source | Free within Cosmos free tier (1000 RU/s) | If you host on Cosmos, every write is an event |

### The best free "event source" that fits your current design
**MongoDB Atlas M0 change streams.** M0 is a 3-node replica set, so **change
streams work on the free tier**. A tiny watcher (`collection.watch()`) turns every
deal/escrow/email write into an event — **no broker, no extra cost**, and it fits
the existing Mongo persistence. This is the cheapest path to "real" events if you
want durability beyond the in-process bus. (Atlas also has **Triggers** — hosted
functions on change streams — on shared tiers.)

### Non-Azure free alternatives (if you prefer)
- **Upstash QStash** — HTTP-based message queue with a free tier; perfect for
  serverless "publish an event, retry delivery" without running a broker.
- **Upstash Kafka / Redis** — free tiers for pub/sub.
- **CloudAMQP "Little Lemur"** — free managed RabbitMQ (topics/queues).
- **Redis Cloud free 30 MB** — pub/sub + streams.

---

## 6. Where EDA could be implemented in *this* codebase

The concrete "possible places," each with its current mechanism, the event-driven
alternative, the recommended tier, and the payoff.

| # | Place | Today | Event-driven alternative | Tier | Benefit |
|---|---|---|---|---|---|
| 1 | **Publisher seam** | `NoopPublisher` (drops events) | Real `AsyncInProcPublisher` with `subscribe(topic, handler)` | 1 | Unlocks everything below |
| 2 | **negotiation.agreed / failed → deal** | *unwired* (no subscriber) | `deals` subscribes → `update_status(NEGOTIATION_AGREED/FAILED)` | 1 | **Fixes a real gap** |
| 3 | **Deal transitions** (dealmaker/shield/escrow → `deals.update_status`) | Direct in-process calls | Emit `pi.sent`, `escrow.funded`, `qc.passed`… ; `deals` reacts | 1 | Removes lazy-import cycles; one reactor owns transitions |
| 4 | **HITL saga** (`callbacks.py` dispatch on approve) | Orchestrated callbacks | Emit `approval.resolved`; target services react (choreography) | 1–2 | Decouples HITL from downstream services |
| 5 | **deal.exception → Exception Resolver** | Direct call / no subscriber | Emit `deal.exception`; resolver subscribes | 1 | Cleaner failure routing; already an "event" in spirit |
| 6 | **Operator notifications / UI** | HITL SSE + in-process fan-out | Event → SignalR/Web PubSub push (managed) | 1 (SSE) / 2 (managed) | Real-time UI without polling |
| 7 | **Email inbound** | IMAP poll every 15 min | Provider **webhooks** (already built!) or **Gmail push → Pub/Sub** | 2 | Near-instant replies; no polling latency |
| 8 | **Provider webhooks** (Stripe/SGS/Cogoport/Freightos/Payoneer) | HTTP handlers that call services | Normalize into the internal bus as `escrow.funded`, `qc.passed`, … | 1–2 | One event model for inbound + internal |
| 9 | **Scheduled scans** (Trade-Min APScheduler) | In-process cron | **Azure Functions Timer trigger** → emit `scan.requested` | 2 | Survives restarts; serverless; no in-process scheduler |
| 10 | **Long-running / retryable connector calls** (QC book, freight, payout, customs) | Inline `await connector...`, raises on failure | Enqueue job → worker with **retry + dead-letter** | 2 | Resilience without inline failure handling |
| 11 | **Deal audit / analytics** | `status_history` array | Subscribe to all topics → append to an event log / read model | 1 | Free audit trail + future dashboards |

**Sweet-spot subset for a personal app:** implement **#1, #2, #3, #5, #11** on the
Tier-1 in-process bus (zero infra), and *optionally* **#7** by leaning on the
already-built SendGrid/Mailgun webhooks instead of IMAP polling.

---

## 7. Minimal migration path (Tier 1, ~half a day)

The seam is already designed for this — you're filling in `NoopPublisher`.

1. **Implement an in-process async bus** in `shared/bus/publisher.py`:
   - a module-level `dict[EventTopic, list[handler]]`,
   - `subscribe(topic, async_handler)` and an `AsyncInProcPublisher.publish()` that
     `await`s (or `asyncio.create_task`s) each handler, wrapping every handler in
     `try/except` so one bad subscriber can't break the emitter.
2. **Register subscribers at startup** (in `app.py` lifespan): e.g.
   `subscribe(EventTopic.NEGOTIATION_AGREED, deals_on_agreed)`.
3. **Make handlers idempotent** — re-check `can_transition` before acting (the deal
   state machine already rejects illegal/duplicate transitions — reuse it).
4. **Keep direct calls during transition** — publish *and* call, then remove the
   direct call once the subscriber is trusted (strangler pattern).
5. **Correlate** — log `event_id` + `deal_id` in every handler for traceability.
6. **Later, if you need durability/push:** swap the in-process publisher for one
   backed by Storage Queue / Service Bus Basic / Atlas change streams — **call
   sites don't change** (that's the whole point of the `EventPublisher` protocol).

### Cross-cutting concerns to decide up front
- **Idempotency:** every handler must tolerate redelivery (dedupe by `event_id`).
- **Ordering:** rely on the state machine, not on broker ordering guarantees.
- **Error handling:** in-process = log + continue; broker = retry + dead-letter.
- **Observability:** structured logs keyed by `event_id`/`deal_id`; consider an
  event-log collection (place #11) as a poor-man's trace.

---

## 8. Bottom line

- **For a personal, single-user app: don't run a broker.** The complexity and
  (for topics) the cost aren't justified.
- **Do implement the Tier-1 in-process async bus.** It's nearly free, low-risk,
  reuses the modelling you already have, and fixes the unwired
  `negotiation.agreed → deal` transition while decoupling deal transitions and
  notifications.
- **If/when you want durability or push**, the cheapest Azure-friendly options are
  **Event Grid (100k ops/mo free)**, **Functions (1M exec/mo free)** for handlers
  and timers, **SignalR/Web PubSub free tier** for real-time UI, and — the best fit
  for your Mongo-based design — **MongoDB Atlas M0 change streams** as a free,
  durable event source. Avoid Service Bus *Standard*, Event Hubs, and managed Redis
  unless a real need appears.
- **Every change is incremental** thanks to the `EventPublisher` protocol: start
  in-process, and the swap to a real transport later touches one file, not the
  callers.
