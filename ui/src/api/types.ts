// Types mirror the backend Pydantic DTOs (shared/domain/models.py + service schemas).

export type DealStatus =
  | "DRAFT"
  | "MATCHING"
  | "NEGOTIATING"
  | "NEGOTIATION_AGREED"
  | "NEGOTIATION_FAILED"
  | "PI_SENT"
  | "PI_ACCEPTED"
  | "ESCROW_PENDING"
  | "ESCROW_FUNDED"
  | "PO_ISSUED"
  | "SUPPLIER_DISPATCH_READY"
  | "QC_PASSED"
  | "QC_FAILED"
  | "IN_TRANSIT"
  | "CLEARED"
  | "PAID"
  | "DISPUTE"
  | "CANCELLED"
  | "REFUNDED";

export type AutonomyLevel = "L0" | "L1" | "L2" | "L3";

export type EmailStatus = "ok" | "risky" | "invalid" | "unverified";
export type SanctionsStatus = "clear" | "review" | "block" | "unchecked";

export interface Party {
  party_id?: string | null;
  role: "buyer" | "supplier";
  legal_name: string;
  country: string;
  gstin?: string | null;
  iec?: string | null;
  vat_number?: string | null;
  verification_status?: string | null;
  credit_status?: string | null;
  risk_score?: number | null;
  // Contact — filled by the Apify company-contact-finder actor, the opportunity
  // signal, or an operator edit (contact_source = "operator" is never overwritten).
  email?: string | null;
  website?: string | null;
  phone?: string | null;
  address?: string | null;
  contact_source?: string | null;
  contact_confidence?: number | null;
  email_status?: EmailStatus | string | null;
  // Counterparty screening — Apify counterparty-check actor (OFAC/UN/EU/UK, LEI, VIES).
  sanctions_status?: SanctionsStatus | string | null;
  sanctions_reason?: string | null;
  sanctions_checked_at?: string | null;
  lei?: string | null;
  vat_valid?: boolean | null;
  enriched_at?: string | null;
}

// PATCH /deals/{id}/parties/{role} — operator correction of a counterparty.
export interface UpdatePartyRequest {
  legal_name?: string;
  country?: string;
  email?: string;
  website?: string;
  phone?: string;
  address?: string;
  gstin?: string;
  iec?: string;
  vat_number?: string;
  verification_status?: string;
}

// POST /deals/{id}/enrich — Apify contact finder + sanctions screening result.
export interface PartyEnrichment {
  legal_name?: string | null;
  country?: string | null;
  email?: string | null;
  email_status?: string | null;
  website?: string | null;
  phone?: string | null;
  address?: string | null;
  vat_number?: string | null;
  vat_valid?: boolean | null;
  lei?: string | null;
  contact_source?: string | null;
  contact_confidence?: number | null;
  sanctions_status?: string | null;
  sanctions_reason?: string | null;
  screening?: Record<string, unknown> | null;
}

export interface EnrichmentResult {
  deal_id: string;
  buyer?: PartyEnrichment | null;
  supplier?: PartyEnrichment | null;
  actors_used: string[];
  warnings: string[];
  blocked: boolean;
  deal: Deal;
}

/** One seller candidate's Composite Match Score breakdown (Match Engine),
 *  captured on the deal at creation so matching info survives the originating
 *  opportunity being removed from the Opportunity Radar. */
export interface MatchResult {
  match_id: string;
  supplier_id: string;
  company_name?: string;
  composite_score: number;
  gatekeeper_passed: boolean;
  arbitrage_score: number;
  proximity_score: number;
  reliability_score: number;
  decision: "SOURCE" | "DISCARD";
  fob_price_usd?: number | null;
  supplier_to_port_km?: number | null;
}

export interface Deal {
  deal_id: string;
  operator_id: string;
  hs_code: string;
  market?: string | null;
  status: DealStatus;
  autonomy_profile: AutonomyLevel;
  margin_floor_pct?: number | null;
  est_margin_usd?: number | null;
  realized_margin_usd?: number | null;
  parties: Party[];
  // Trade-Min / Match Engine linkage (seeded at deal creation)
  opportunity_id?: string | null;
  match_results?: MatchResult[];
  winning_supplier_id?: string | null;
  winning_cms?: number | null;
  buyer_target_price_usd?: number | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface SellerCandidate {
  candidate_id: string;
  company_name: string;
  location_city?: string | null;
  location_state?: string | null;
  estimated_fob_price_usd?: number | null;
  annual_capacity_mt?: number | null;
  source?: string | null;
  listing_url?: string | null;
  supplier_to_port_km?: number | null;
  // Apify india-supplier actor: contact + credentials
  email?: string | null;
  phone?: string | null;
  website?: string | null;
  gstin?: string | null;
  iec?: string | null;
  fit_score?: number | null;
  price_unit?: string | null;
}

export interface BuyerSignal {
  buyer_name: string;
  buyer_country: string;
  company_id?: string | null;
  signal_type?: string | null;
  source?: string | null;
  unit_price_usd?: number | null;
  price_unit?: string | null;
  estimated_order_value_usd?: number | null;
  annual_volume_mt?: number | null;
  risk_score?: number | null;
  urgency_tier?: number | null;
  contact_url?: string | null;
  // Apify buyer actors: contact + fit
  website?: string | null;
  email?: string | null;
  phone?: string | null;
  address?: string | null;
  fit_score?: number | null;
  fit_reason?: string | null;
  pitch_angle?: string | null;
  quantity?: number | null;
  quantity_unit?: string | null;
  shipments_12m?: number | null;
  hs_codes?: string[];
  last_import_date?: string | null;
}

export interface TariffData {
  applied_duty_pct?: number | null;
  gst_vat_pct?: number | null;
  total_landed_cost_add_pct?: number | null;
  ntb_flags?: string[];
  rules_of_origin?: string | null;
}

export interface Opportunity {
  opportunity_id: string;
  hs_code: string;
  market: string;
  arbitrage_rank: number;
  source?: string | null;
  status?: "ACTIVE" | "MARGINAL" | "EXPIRED" | "CONVERTED" | null;
  seller_candidates?: SellerCandidate[];
  buyer_signals?: BuyerSignal[];
  // Enrichment fields returned by GET /trade-min/opportunities (EnrichedOpportunity)
  currency?: string | null;
  scanned_at?: string | null;
  demand_index?: number | null;
  price_gap_pct?: number | null;
  net_price_gap_pct?: number | null;
  active_rfq_count?: number | null;
  active_tender_count?: number | null;
  urgency_tier?: number | null;
  tariff_data?: TariffData | null;
  tariff_viable?: boolean | null;
  confidence_score?: number | null;
  expires_at?: string | null;
  // What the destination market actually pays per MT (UN Comtrade unit value
  // via the export-market actor) — the real buyer price anchor.
  price_benchmark_usd_per_mt?: number | null;
  price_benchmark_source?: string | null;
}

// GET /trade-min/sources — which demand / buyer / seller / enrichment sources
// the scanner can reach right now (incl. the PortLess-owned Apify actors).
export type SourceMode = "live" | "stub" | "mock" | "open";
export type SourceRole = "demand" | "buyers" | "sellers" | "tariff" | "enrichment" | "screening";

export interface TradeSource {
  name: string;
  label: string;
  role: SourceRole | string;
  provider: string;
  actor?: string | null;
  mode: SourceMode | string;
  detail: string;
}

export interface TradeSourcesResponse {
  mock_mode: boolean;
  apify: { token_present: boolean; owner: string; ai_tier: boolean; max_items: number };
  live_count: number;
  total: number;
  sources: TradeSource[];
}

// GET /health/readiness — production readiness gate (shared/readiness.py).
export type ReadinessStatus = "ok" | "warn" | "fail";

export interface ReadinessCheck {
  id: string;
  category: string;
  label: string;
  status: ReadinessStatus;
  detail: string;
}

export interface ReadinessReport {
  profile: "dev" | "staging" | "production" | string;
  enforced: boolean;
  ready: boolean;
  fail_count: number;
  warn_count: number;
  generated_at: string;
  checks: ReadinessCheck[];
}

export interface Approval {
  approval_id: string;
  deal_id: string;
  action: string;
  summary?: string | null;
  financial_impact_usd?: number | null;
  risk_flags: string[];
  status: string;
  expires_at?: string | null;
  default_on_timeout: string;
  deep_link?: string | null;
}

export interface ApprovalRecord {
  approval_id: string;
  deal_id: string;
  action: string;
  summary: string | null;
  financial_impact_usd: number | null;
  risk_flags: string[];
  autonomy_level: AutonomyLevel;
  status: "PENDING" | "APPROVED" | "REJECTED" | "AUTO_APPROVED" | "TIMED_OUT";
  expires_at: string | null;
  default_on_timeout: "APPROVE" | "REJECT";
  deep_link: string | null;
  callback_payload: Record<string, unknown>;
  decision: "APPROVE" | "REJECT" | null;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  modified_body: string | null;
  created_at: string;
  updated_at: string;
}

export interface DealException {
  exception_id: string;
  deal_id: string;
  category?: string | null;
  status: string;
  remediation_plan: string[];
}

export type DownstreamHealth = Record<string, string>;

// Auth
export interface AuthUser {
  user_id: string;
  email: string;
  name: string;
  org_id: string | null;
  is_portless: boolean;
  created_at?: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user: AuthUser;
}

// Organizations (Portless platform staff only — see /auth/organizations, /auth/users)
export interface Organization {
  org_id: string;
  name: string;
  created_by: string;
  known_emails: string[];
  created_at: string;
}

// RAG knowledge base
export interface RagStats {
  total_chunks: number;
  collection: string;
  db: string;
  embedding_provider: string;
  embedding_model: string;
  file_store_backend: string;
  graph_retrieval_enabled: boolean;
}

export interface RagIngestResponse {
  job_id: string;
  status: string;
  message: string;
}

export interface RagJobStatus {
  job_id: string;
  status: "pending" | "running" | "completed" | "failed";
  inserted_count?: number | null;
  graph_inserted_count?: number | null;
  error?: string | null;
  started_at: string;
  completed_at?: string | null;
}

// A file persisted in the RAG file store (MongoDB GridFS — the sole backend).
export interface RagFile {
  file_id: string;
  filename: string;
  size: number;
  backend: string;
  content_type?: string | null;
  uploaded_at?: string | null;
}

export interface RagFilesResponse {
  backend: string;
  count: number;
  files: RagFile[];
}

// RAG chat / test console — POST /rag/query (full retrieve → generate pipeline)
export interface RagSourceChunk {
  source?: string | null;
  title?: string | null;
  section?: string | null;
  page_number?: number | null;
  chunk_id?: string | null;
  content?: string | null;
}

export interface RagCitationValidation {
  valid: boolean;
  cited_numbers: number[];
  missing_citations: boolean;
  invalid_citations: number[];
  available_citations: number[];
}

export interface RagEvaluationMetrics {
  faithfulness: number;
  context_precision: number;
  answer_relevance: number;
  evaluator: string;
  advanced_notes: string;
}

export interface RagQueryResponse {
  question: string;
  answer: string;
  sources: RagSourceChunk[];
  citation_validation: RagCitationValidation;
  evaluation_metrics: RagEvaluationMetrics;
}

export interface CreateDealRequest {
  operator_id: string;
  hs_code: string;
  market: string;
  autonomy_profile?: AutonomyLevel;
  margin_floor_pct?: number | null;
  opportunity_id?: string | null;
  parties?: Party[];
}

// Negotiation types
export interface NegotiationSession {
  negotiation_id: string;
  deal_id: string;
  trade_match_id: string;
  buyer_id: string;
  seller_id: string;
  product_code: string;
  status: "STARTED" | "ROUND" | "AGREED" | "FAILED" | "HITL_PENDING" | "ESCALATED";
  round_no: number;
  current_bid: number | null;
  current_ask: number | null;
  agreed_price: number | null;
  margin_at_current: number | null;
  hitl_required: boolean;
  created_at: string;
  updated_at: string;
}

export interface NegotiationRound {
  round_id: string;
  negotiation_id: string;
  deal_id: string;
  round_no: number;
  party: "buyer" | "supplier";
  channel: string;
  inbound_price: number | null;
  inbound_message: string | null;
  outbound_message: string;
  orchestrator_routing: string;
  margin_at_round: number | null;
  hitl_required: boolean;
  created_at: string;
}

export interface EmailHistoryRecord {
  email_id: string;
  negotiation_id: string;
  deal_id: string;
  round_no: number;
  direction: "outbound" | "inbound";
  party: "buyer" | "supplier";
  to_address: string;
  from_address: string;
  subject: string;
  body: string;
  original_body: string | null;
  approval_id: string | null;
  status:
    | "draft"
    | "pending_approval"
    | "sent"
    | "modified_sent"
    | "rejected"
    | "failed"
    | "received";
  sent_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface NegotiationResponse {
  round: NegotiationRound;
  session: NegotiationSession;
}

export interface NegotiationEmailThread extends NegotiationSession {
  buyer_target_price: number;
  supplier_quote_price: number;
  emails: EmailHistoryRecord[];
  pending_approval_count: number;
}

export interface RunRoundRequest {
  inbound_price?: number | null;
  inbound_message?: string | null;
  party?: "buyer" | "supplier";
}

// Escrow, QC, Freight, Docugen — mirror services/escrow_guard/schemas.py
export type EscrowStatus =
  | "PENDING"
  | "PARTIALLY_FUNDED"
  | "FUNDED"
  | "RELEASED"
  | "REFUNDED"
  | "FAILED";
export type TrancheTrigger = "PO_ISSUED" | "QC_PASSED" | "CLEARED" | "ACCEPTED";

export interface Tranche {
  tranche_id: string;
  trigger: TrancheTrigger | string;
  pct: number;
  amount?: number | null;
  status: "PENDING" | "RELEASED";
  proof_refs: string[];
  ext_ref?: string | null;
  released_at?: string | null;
}

// GET /escrow-guard/deals/{id}/hold — EscrowResponse
export interface EscrowRecord {
  escrow_id: string;
  deal_id: string;
  provider: string;
  amount: number;
  currency: string;
  status: EscrowStatus | string;
  ext_ref?: string | null;
  client_secret?: string | null;
  funded_at?: string | null;
  funded_amount?: number | null;
  created_at: string;
  payment_method?: string | null;
  checkout_url?: string | null;
  released_total: number;
  refunded_total: number;
  fee_pct?: number | null;
  fees_total: number;
  shortfall?: number | null;
  settled_at?: string | null;
  tranches: Tranche[];
  inspection_deadline?: string | null;
  buyer_accepted_at?: string | null;
  disputed: boolean;
  disputed_at?: string | null;
}

export type PayoutKind = "supplier_payout" | "supplier_balance";

// PayoutResponse
export interface PayoutRecord {
  payout_id: string;
  deal_id: string;
  payee_id: string;
  amount: number;
  currency: string;
  status: "REQUESTED" | "PROCESSING" | "COMPLETED" | "FAILED" | string;
  ext_ref?: string | null;
  completed_at?: string | null;
  created_at: string;
  kind: PayoutKind | string;
  total_owed?: number | null;
}

// GET /escrow-guard/deals/{id}/ledger and GET /escrow-guard/ledgers
export interface DealLedger {
  deal_id: string;
  currency: string;
  provider?: string | null;
  escrow_id?: string | null;
  status?: string | null;
  principal: number;
  funded_amount?: number | null;
  shortfall?: number | null;
  released_total: number;
  refunded_total: number;
  outstanding: number;
  fees_total: number;
  net_to_portless: number;
  payouts_total: number;
  payouts_pending: number;
  available_for_payout: number;
  supplier_owed?: number | null;
  supplier_balance_due?: number | null;
  realized_margin?: number | null;
  tranches: Tranche[];
  payouts: PayoutRecord[];
  inspection_deadline?: string | null;
  disputed: boolean;
  settled: boolean;
  reconciled: boolean;
  issues: string[];
}

export interface QcRecord {
  qc_id: string;
  deal_id: string;
  provider: string;
  status: "BOOKED" | "IN_PROGRESS" | "PASSED" | "FAILED";
  report_url: string | null;
  booked_at: string;
}

export interface FreightRecord {
  tpo_id: string;
  deal_id: string;
  provider: string;
  status: "BOOKED" | "IN_TRANSIT" | "ARRIVED" | "CLEARED";
  bl_number: string | null;
  eta: string | null;
}

export interface DocumentRecord {
  document_id: string;
  deal_id: string;
  document_type: string;
  status: string;
  file_url: string | null;
  created_at: string;
}

// Match score — mirrors shared/domain/models.py MatchScore (the CMS formula's
// actual components: arbitrage / proximity / reliability, not price/margin/risk).
export interface MatchScore {
  composite_score: number;
  arbitrage_score: number;
  proximity_score: number;
  reliability_score: number;
  gatekeeper_passed?: boolean;
}

// DealMaker contracts (PI/PO) for a deal — GET /dealmaker/deals/{id}/documents.
// storage_uri is a Google Drive webViewLink when Drive is configured.
export interface DealmakerContract {
  status: string;
  storage_uri?: string | null;
  total_value?: number | null;
  agreed_price?: number | null;
  issued_at?: string | null;
}
export interface DealmakerDealDocuments {
  deal_id: string;
  pi: (DealmakerContract & { pi_id: string }) | null;
  po: (DealmakerContract & { po_id: string }) | null;
}

// SSE event
export interface ApprovalStreamEvent {
  event: string;
  data: ApprovalRecord;
}

// Resolve payload
export interface ResolveApprovalRequest {
  decision: "APPROVE" | "REJECT";
  resolved_by: string;
  note?: string;
  modified_body?: string;
}
