import { useCallback, useEffect, useState } from "react";
import { ArrowUpRight, RefreshCw, ShieldCheck, Workflow } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../data/client";
import type { Incident, Remediation } from "../data/types";
import { useAuth } from "../ui/auth";
import { RemediationCard } from "../ui/RemediationCard";
import { EmptyState, ErrorState, Loading, PageHeader } from "../ui/shared";

export function RemediationPage() {
  const { token } = useAuth();
  const [items, setItems] = useState<Array<{ incident: Incident; action: Remediation }>>([]);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(true);
  const load = useCallback(async () => {
    setBusy(true);
    try {
      const incidents = (await api.incidents(token, "?limit=100")).items;
      const pages = await Promise.all(incidents.map(async (incident) => {
        const page = await api.incidentRemediations(incident.id, token);
        return page.items.map((action) => ({ incident, action }));
      }));
      setItems(pages.flat());
      setError(undefined);
    } catch (reason) { setError(reason); }
    finally { setBusy(false); }
  }, [token]);
  useEffect(() => { void load(); }, [load]);
  const pending = items.filter(({ action }) => action.status.toLowerCase() === "approval_requested");
  const completed = items.filter(({ action }) => ["completed", "succeeded"].includes(action.status.toLowerCase()));
  const other = items.filter(({ action }) => !["approval_requested", "completed", "succeeded"].includes(action.status.toLowerCase()));
  return <>
    <PageHeader eyebrow="RESPONSE / CONTROLLED ACTIONS" title="Remediation center" subtitle="Review recommendations and record human approval through the OpsMind API." action={<button className="button button-quiet" onClick={() => void load()}><RefreshCw size={15} />Refresh</button>} />
    <section className="remediation-banner"><div className="remediation-banner-icon"><ShieldCheck size={22} /></div><div><strong>Every action is server-authorized</strong><p>The interface submits decisions. The remediation service checks your identity, permissions, approval expiry, policy, and audit trail.</p></div><div className="remediation-banner-count"><b>{pending.length}</b><small>awaiting review</small></div></section>
    {error ? <ErrorState error={error} onRetry={() => void load()} /> : busy ? <div className="panel"><Loading label="Loading remediation requests" /></div> : !items.length ? <div className="panel"><EmptyState icon={<Workflow size={20} />} title="No remediation actions" text="Recommendations will appear here when incident investigations propose a safe action." /></div> : <div className="remediation-columns">
      <section className="panel remediation-lane"><div className="panel-heading"><div><div className="eyebrow">HUMAN REVIEW</div><h2>Awaiting approval <span className="count-pill">{pending.length}</span></h2></div><span className="lane-indicator lane-amber" /></div>
        {pending.length ? pending.map(({ incident, action }) => <div className="action-with-incident" key={action.id}><Link to={"/incidents/" + incident.id} className="action-incident-link"><span>{incident.title}</span><ArrowUpRight size={13} /></Link><RemediationCard action={action} onChanged={() => void load()} /></div>) : <EmptyState title="Review queue is clear" text="No pending approval requests." />}
      </section>
      <div className="remediation-side-lanes"><section className="panel remediation-lane"><div className="panel-heading"><div><div className="eyebrow">COMPLETED</div><h2>Executed actions <span className="count-pill">{completed.length}</span></h2></div><span className="lane-indicator lane-green" /></div>{completed.length ? completed.map(({ incident, action }) => <div className="action-with-incident" key={action.id}><Link to={"/incidents/" + incident.id} className="action-incident-link">{incident.title}<ArrowUpRight size={13} /></Link><RemediationCard action={action} onChanged={() => void load()} /></div>) : <div className="lane-empty">No completed remediation yet.</div>}</section>
        <section className="panel remediation-lane"><div className="panel-heading"><div><div className="eyebrow">OTHER STATUS</div><h2>Rejected, expired or running <span className="count-pill">{other.length}</span></h2></div></div>{other.length ? other.map(({ incident, action }) => <div className="action-with-incident" key={action.id}><Link to={"/incidents/" + incident.id} className="action-incident-link">{incident.title}<ArrowUpRight size={13} /></Link><RemediationCard action={action} onChanged={() => void load()} /></div>) : <div className="lane-empty">No other actions to show.</div>}</section></div>
    </div>}
  </>;
}
