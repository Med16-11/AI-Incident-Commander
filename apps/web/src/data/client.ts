import type {
  ApiHealth,
  Evidence,
  Hypothesis,
  Incident,
  Page,
  PrometheusResult,
  Remediation,
  ServiceDependencies,
  ServiceNode,
  TimelineEntry,
} from "./types";
import { apiErrorFromPayload, ApiError } from "./errors";

export { ApiError };

const API_BASE = import.meta.env.VITE_OPSMIND_API_URL ?? "/api";
const REMEDIATION_BASE = import.meta.env.VITE_OPSMIND_REMEDIATION_URL ?? "/remediation-api";

export function correlationId(): string {
  return crypto.randomUUID();
}

async function request<T>(base: string, path: string, token?: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  headers.set("X-Correlation-ID", correlationId());
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  const response = await fetch(`${base}${path}`, { ...init, headers });
  if (!response.ok) {
    let body: unknown = undefined;
    try {
      body = await response.json();
    } catch {
      // The API may return an empty error body.
    }
    throw apiErrorFromPayload(response.status, body);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  incidents: (token?: string, query = "") => request<Page<Incident>>(API_BASE, `/incidents${query}`, token),
  incident: (id: string, token?: string) => request<Incident>(API_BASE, `/incidents/${id}`, token),
  timeline: (id: string, token?: string) => request<Page<TimelineEntry>>(API_BASE, `/incidents/${id}/timeline?limit=100`, token),
  evidence: (id: string, token?: string) => request<Page<Evidence>>(API_BASE, `/incidents/${id}/evidence?limit=100`, token),
  hypotheses: (id: string, token?: string) => request<Page<Hypothesis>>(API_BASE, `/incidents/${id}/hypotheses?limit=100`, token),
  incidentRemediations: async (id: string, token?: string) => {
    const page = await request<Page<Remediation>>(REMEDIATION_BASE, `/incidents/${id}/remediations?limit=100`, token);
    return { ...page, items: page.items.map((item) => ({ ...item, id: item.action_id ?? item.id })) };
  },
  services: (token?: string) => request<Page<ServiceNode>>(API_BASE, "/services?limit=100", token),
  dependencies: (id: string, token?: string) => request<ServiceDependencies>(API_BASE, `/services/${id}/dependencies`, token),
  approve: (id: string, reason: string, token?: string) => request<Remediation>(REMEDIATION_BASE, `/remediations/${id}/approve`, token, { method: "POST", body: JSON.stringify({ reason }) }),
  reject: (id: string, reason: string, token?: string) => request<Remediation>(REMEDIATION_BASE, `/remediations/${id}/reject`, token, { method: "POST", body: JSON.stringify({ reason }) }),
  remediationAction: (id: string, token?: string) => request<Remediation>(REMEDIATION_BASE, `/remediations/${id}`, token),
  health: () => request<ApiHealth>(API_BASE, "/health/ready"),
  prometheus: async (query: string): Promise<PrometheusResult> => {
    const params = new URLSearchParams({ query });
    const response = await fetch(`/prometheus/api/v1/query?${params}`);
    if (!response.ok) throw new ApiError("Prometheus query failed", response.status);
    return (await response.json()) as PrometheusResult;
  },
};

export async function openIncidentStream(
  incidentId: string,
  token: string | undefined,
  signal: AbortSignal,
  onUpdate: () => void,
): Promise<void> {
  const headers = new Headers({ Accept: "text/event-stream", "X-Correlation-ID": correlationId() });
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE}/incidents/${incidentId}/stream`, {
    headers,
    signal,
  });
  if (!response.ok || !response.body) {
    throw new ApiError(`Live updates unavailable (${response.status})`, response.status);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (!signal.aborted) {
    const { value, done } = await reader.read();
    if (done) {
      if (!signal.aborted) throw new Error("Incident event stream disconnected");
      return;
    }
    buffer += decoder.decode(value, { stream: true });
    let boundary: number;
    while ((boundary = buffer.indexOf("\n\n")) >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      if (frame.split("\n").some((line) => line.startsWith("event: timeline"))) onUpdate();
    }
  }
}
