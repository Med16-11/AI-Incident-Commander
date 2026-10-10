import { useState } from "react";
import { Check, Clock3, ShieldAlert, X } from "lucide-react";
import { api } from "../data/client";
import type { Remediation } from "../data/types";
import { useAuth } from "./auth";
import { formatDate, StateBadge } from "./shared";

export function RemediationCard({ action, onChanged }: { action: Remediation; onChanged: () => void }) {
  const { token } = useAuth();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const awaiting = action.status.toLowerCase() === "approval_requested";

  const decide = async (decision: "approve" | "reject") => {
    const reason = window.prompt("Add a short reason for this " + decision + " decision:");
    if (reason === null) return;
    if (!reason.trim()) {
      setMessage("A decision reason is required.");
      return;
    }
    setBusy(true);
    setMessage("");
    try {
      if (decision === "approve") await api.approve(action.id, reason.trim(), token);
      else await api.reject(action.id, reason.trim(), token);
      setMessage("Decision recorded.");
      onChanged();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "The server rejected this decision.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="remediation-card">
      <div className="remediation-icon"><ShieldAlert size={17} /></div>
      <div className="remediation-main">
        <div className="remediation-title-row"><strong>{action.action_type.replaceAll("_", " ")}</strong><StateBadge state={action.status} /></div>
        <p>{action.target}</p>
        <div className="remediation-meta"><span className={"risk-tag risk-" + action.risk_level.toLowerCase().replace("_risk", "")}>{action.risk_level.replace("_RISK", "")} RISK</span><span>{action.rationale || action.approval?.reason || "No rationale provided"}</span></div>
        {action.approval?.expires_at && awaiting && <div className="approval-expiry"><Clock3 size={13} /> Request expires {formatDate(action.approval.expires_at)}</div>}
        {action.result && <pre className="result-block">{JSON.stringify(action.result, null, 2)}</pre>}
        {message && <div role="status" className="inline-feedback">{message}</div>}
        {awaiting && <div className="approval-actions"><button className="button button-primary button-sm" disabled={busy} onClick={() => void decide("approve")}><Check size={14} />{busy ? "Submitting…" : "Approve"}</button><button className="button button-danger button-sm" disabled={busy} onClick={() => void decide("reject")}><X size={14} />Reject</button><span className="authorization-note">Server verifies access</span></div>}
      </div>
    </article>
  );
}
