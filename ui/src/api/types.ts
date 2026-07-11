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
}

export interface Deal {
  deal_id: string;
  operator_id: string;
  hs_code: string;
  status: DealStatus;
  autonomy_profile: AutonomyLevel;
  margin_floor_pct?: number | null;
  est_margin_usd?: number | null;
  realized_margin_usd?: number | null;
  parties: Party[];
  created_at?: string | null;
  updated_at?: string | null;
}

export interface Opportunity {
  opportunity_id: string;
  hs_code: string;
  market: string;
  arbitrage_rank: number;
  source?: string | null;
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
  created_at?: string | null;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user: AuthUser;
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
  autonomy_profile?: AutonomyLevel;
  margin_floor_pct?: number | null;
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

// Escrow, QC, Freight, Docugen
export interface EscrowHold {
  hold_id: string;
  deal_id: string;
  amount_usd: number;
  status: "PENDING" | "FUNDED" | "RELEASED" | "REFUNDED";
  stripe_payment_intent_id: string | null;
  funded_at: string | null;
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

// Match score
export interface MatchScore {
  composite_score: number;
  price_score: number;
  margin_score: number;
  risk_score: number;
  logistics_score: number;
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
