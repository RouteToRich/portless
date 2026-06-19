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

export interface DealException {
  exception_id: string;
  deal_id: string;
  category?: string | null;
  status: string;
  remediation_plan: string[];
}

export type DownstreamHealth = Record<string, string>;

export interface CreateDealRequest {
  operator_id: string;
  hs_code: string;
  autonomy_profile?: AutonomyLevel;
  margin_floor_pct?: number | null;
  parties?: Party[];
}
