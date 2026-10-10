export type IncidentState =
  | "DETECTED"
  | "TRIAGING"
  | "INVESTIGATING"
  | "IDENTIFIED"
  | "MITIGATING"
  | "RESOLVED"
  | "CLOSED";

export interface Incident {
  id: string;
  tenant_id: string;
  service_id: string | null;
  title: string;
  description: string | null;
  state: IncidentState;
  severity: string;
  version: number;
  created_at: string;
  updated_at: string;
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

export interface TimelineEntry {
  id: string;
  incident_id: string;
  event_id: string | null;
  evidence_id: string | null;
  sequence: number;
  event_type: string;
  summary: string;
  actor_id: string | null;
  occurred_at: string;
}

export interface Evidence {
  id: string;
  incident_id: string;
  evidence_type: string;
  source: string;
  service: string;
  normalized_summary: string;
  confidence: number;
  external_ref: string | null;
  content: Record<string, unknown>;
  content_hash: string | null;
  observed_at: string;
  collected_at: string;
}

export interface Hypothesis {
  id: string;
  incident_id: string;
  summary: string;
  confidence: number;
  status: string;
  rationale: string | null;
  created_at: string;
  updated_at: string;
}

export interface Remediation {
  id: string;
  action_id?: string;
  incident_id: string;
  action_type: string;
  target: string;
  parameters: Record<string, unknown>;
  risk_level: string;
  status: string;
  requested_by?: string | null;
  approved_by: string | null;
  approved_at?: string | null;
  execution_start?: string | null;
  execution_end?: string | null;
  result?: Record<string, unknown> | null;
  rationale?: string;
  created_at: string;
  updated_at: string;
  approval?: {
    reason: string;
    expires_at: string;
    decision: string;
    approver: string | null;
    timestamp: string | null;
  } | null;
}

export interface ServiceNode {
  id: string;
  name: string;
  environment: string;
  owner_team?: string | null;
  repository_url?: string | null;
  metadata?: Record<string, unknown>;
}

export interface ServiceDependencies {
  service: ServiceNode;
  direct_dependencies: ServiceNode[];
  upstream_services: ServiceNode[];
  downstream_services: ServiceNode[];
  upstream_depth: number;
  downstream_depth: number;
  critical_path: ServiceNode[];
}

export interface ApiHealth {
  status: string;
  service?: string;
}

export interface PrometheusResult {
  status: string;
  data?: { result?: Array<{ metric: Record<string, string>; value: [number, string] }> };
}
