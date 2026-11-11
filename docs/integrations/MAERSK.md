# Maersk — ocean booking, tracking, VGM, bill of lading

`FREIGHT_PROVIDER=maersk` (default) makes PortLess book and run the shipment on
Maersk's APIs. Quotes for pricing stay with `FREIGHT_QUOTE_PROVIDER` (Maersk
rates come from the contract, `MAERSK_RATE_USD_PER_CONTAINER`).

## Contract

From the Maersk API definitions mirrored at `artifacts.apis.io/maersk-line`
(Maersk's own portal pages render client-side and could not be read). Maersk's
Booking, Track & Trace and eBL APIs follow the DCSA standards. Every path can be
overridden (`MAERSK_*_PATH`) if the account's documentation differs.

| Call | Request | Used for |
|---|---|---|
| `POST /customer-identity/oauth/v2/access_token` | form `grant_type=client_credentials`, `client_id` (= consumer key), `client_secret`; header `Consumer-Key` | bearer token, cached, refreshed once on a 401 |
| `POST /dcsa/bkg/v2/bookings` | DCSA BookingRequest: `carrierServiceCode`, `receiptTypeAtOrigin`/`deliveryTypeAtDestination` `CY`, `cargoMovementType…` `FCL`, `requestedEquipments[]` (`ISOEquipmentCode`, `units`, `equipmentReferences` = the containers stuffed under the inspector), `commodities[]` (`commodityType`, `HSCodes`, `cargoGrossWeight` KGM), `shipmentLocations` POL/POD (UN/LOCODE), `references` (PO, deal), `remarks` (trade brief), `partyContactDetails`, `serviceContractReference` | the booking (`carrierBookingReference`, `bookingStatus`) |
| `GET /dcsa/bkg/v2/bookings/{ref}` | — | status, confirmed containers, transport plan (vessel, voyage, ETD/ETA), transport document reference |
| `DELETE /dcsa/bkg/v2/bookings/{ref}` | — | cancel |
| `GET /track-and-trace-private/events?carrierBookingReference=` | — | DCSA events → timeline + milestones |
| `GET /dcsa/ebl/v3/transport-documents/{tdr}` | — | B/L status DRAFT / APPROVED / ISSUED |
| `PATCH /dcsa/ebl/v3/transport-documents/{tdr}/approve` | — | shipper approval of the draft |
| `POST /export-shipments/vgm/v1/shipments/{tdr}/vgm` | `{transportDocumentReference, containers: [{equipmentReference, verifiedGrossMass, massUnit KGM, weighingMethod METHOD2, weighingDate, responsibleParty {partyName, signatory, contactEmail}}]}` | the SOLAS VGM |
| webhook `POST /shield-link/carrier/maersk/webhook?token=<MAERSK_WEBHOOK_SECRET>` | Maersk booking-status notification | no published signature → token + the booking is always re-read |

## The shipment, step by step

1. **QC passed + supervised loading recorded** → `BOOK_FREIGHT` gate (L0) →
   booking with the containers the inspector sealed, the supplier's port and the
   buyer's port → deal `IN_TRANSIT`, `freight.booked`.
2. **Booking confirmed** (webhook / `FREIGHT_POLL_MINUTES` poll / "Track now") →
   containers, vessel, voyage, ETD / ETA on the order.
3. **VGM** → `SUBMIT_VGM` gate (L0): per container, cargo = loaded MT ÷ boxes, +
   the box's tare from Maersk (or the ISO default). A container-number mismatch
   between the inspector's record and Maersk is flagged on the gate. APPROVE submits.
4. **Draft B/L** → `APPROVE_TRANSPORT_DOCUMENT` gate (L0): APPROVE approves it;
   **issued** → `freight.transport_document_issued`, the buyer is e-mailed the B/L
   number, and the number is proof for the export tranche (T3).
5. **Customs** → `FILE_CUSTOMS` gate (waits for a planned PEO to pass).
6. **Voyage**: ACT `DEPA` → DEPARTED; `ARRI` at the port of discharge → ARRIVED
   (deal `CLEARED`, T3 + payout gates); a transshipment arrival is ignored;
   `GTOT` at destination / `DROP` → DELIVERED → the buyer's acceptance window opens.
   Estimated / planned events are shown, never applied.
7. REJECTED / CANCELLED booking → `deal.exception` (`rebook_freight`).

The deal carries `freight_status` / `freight_status_detail` (Deal Board chip):
Not booked · Booking requested · Booking confirmed · VGM / B/L pending · B/L
issued · Loaded · Sailed · Arrived · Discharged · Delivered · Booking rejected /
failed. `GET /shield-link/deals/{id}/shipment` feeds the Deal page's tracking card.

## Settings

```
FREIGHT_PROVIDER=maersk  FREIGHT_QUOTE_PROVIDER=cogoport  FREIGHT_POLL_MINUTES=60
MAERSK_CONSUMER_KEY / MAERSK_CLIENT_SECRET / MAERSK_CUSTOMER_CODE / MAERSK_SERVICE_CONTRACT
MAERSK_CARRIER_SERVICE_CODE (required to book) / MAERSK_WEBHOOK_SECRET / MAERSK_DEFAULT_EQUIPMENT (42GP)
MAERSK_BASE_URL / MAERSK_TOKEN_URL / MAERSK_BOOKING_PATH / MAERSK_EVENTS_PATH / MAERSK_EBL_PATH / MAERSK_VGM_PATH
CARRIER_SANDBOX_PROVIDERS / CARRIER_SANDBOX_URL   # simulator — automation + tests only (readiness fails in production)
```

## Not confirmed from public documentation

- The exact token form and Maersk's webhook payload / signature.
- Booking 2.0 fields beyond the mirrored minimum (`shipmentLocations`,
  `references`, `equipmentReferences`, `remarks`) are DCSA fields Maersk is
  expected to accept; the Track & Trace response's event-code field names follow
  DCSA (`transportEventTypeCode`, `equipmentEventTypeCode`).
- Shipping instructions (the input to the draft B/L) are not in the mirrored
  APIs — submit them on the Maersk portal; PortLess picks the draft up.

## Simulator

`services/carrier_sandbox` (`make carrier-sandbox`, port 8016, or in process)
implements the calls above with their validation (Consumer-Key + bearer, required
booking fields, VGM only for the booking's containers, approve only a DRAFT,
cancel refused after departure) and controls: `/maersk/_sandbox/bookings/{ref}/confirm|reject|advance`
(`{"to": "ARRI"}`), `/maersk/_sandbox/transport-documents/{tdr}/issue`, `auto`
(confirm at once; issue the B/L once approved and the VGM is in). The automation
worker sets `CARRIER_SANDBOX_PROVIDERS=maersk`.
