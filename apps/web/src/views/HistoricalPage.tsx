import { useEffect, useState } from "react";
import { Archive, ArrowUpRight, CalendarClock, RefreshCw, Search } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../data/client";
import type { Incident } from "../data/types";
import { useAuth } from "../ui/auth";
import { IncidentTable } from "../ui/IncidentTable";
import { EmptyState, ErrorState, Loading, PageHeader } from "../ui/shared";

export function HistoricalPage() {
  const { token } = useAuth();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [query, setQuery] = useState("");
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(true);
  const load = async () => {
    setBusy(true);
    try {
      const [resolved, closed] = await Promise.all([
        api.incidents(token, "?state=RESOLVED&limit=100"),
        api.incidents(token, "?state=CLOSED&limit=100"),
      ]);
      const unique = new Map([...resolved.items, ...closed.items].map((item) => [item.id, item]));
      setIncidents(Array.from(unique.values()).sort((a, b) => b.updated_at.localeCompare(a.updated_at)));
      setError(undefined);
    } catch (reason) { setError(reason); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, [token]);
  const filtered = incidents.filter((item) => (item.title + " " + (item.description ?? "")).toLowerCase().includes(query.toLowerCase()));
  return <>
    <PageHeader eyebrow="RESPONSE / MEMORY" title="Historical incidents" subtitle="Review resolved work and revisit the evidence and remediation attached to each incident." action={<button className="button button-quiet" onClick={() => void load()}><RefreshCw size={15} />Refresh</button>} />
    <section className="history-banner"><div className="history-icon"><Archive size={20} /></div><div><strong>Incident memory</strong><p>Resolved and closed incidents are retained for review. Open a record to inspect its timeline, hypotheses, evidence, and remediation history.</p></div><div className="history-counter"><b>{busy || error ? "—" : incidents.length}</b><small>records</small></div></section>
    <section className="panel history-panel"><div className="panel-heading"><div><div className="eyebrow">RESOLVED WORK</div><h2>Incident archive <span className="count-pill">{filtered.length}</span></h2></div><label className="search-box"><Search size={15} /><input aria-label="Search historical incidents" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search incident title or description" /></label></div>
      {error ? <ErrorState error={error} onRetry={() => void load()} /> : busy ? <Loading label="Loading incident history" /> : incidents.length ? <IncidentTable incidents={filtered} /> : <EmptyState icon={<CalendarClock size={19} />} title="No resolved incidents" text="Incidents moved to Resolved or Closed will appear in this archive." to="/incidents" linkLabel="Browse active incidents" />}
    </section>
    <div className="history-note"><CalendarClock size={15} /><span>Historical context is shown for reference. It is not current incident evidence.</span><Link to="/incidents">Go to current incidents<ArrowUpRight size={13} /></Link></div>
  </>;
}
