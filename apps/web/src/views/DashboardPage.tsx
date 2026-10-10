import { useEffect, useMemo, useState } from "react";
import { Activity, ArrowUpRight, BellRing, CircleCheck, Clock3, GitBranch, RefreshCw, Siren, Boxes } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../data/client";
import type { Incident, ServiceNode } from "../data/types";
import { useAuth } from "../ui/auth";
import { IncidentTable } from "../ui/IncidentTable";
import { EmptyState, ErrorState, Loading, PageHeader } from "../ui/shared";

type Filter = "all" | "active" | "critical" | "resolved";

export function DashboardPage() {
  const { token } = useAuth();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [services, setServices] = useState<ServiceNode[]>([]);
  const [error, setError] = useState<unknown>();
  const [serviceError, setServiceError] = useState<unknown>();
  const [busy, setBusy] = useState(true);
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");

  const load = async () => {
    setBusy(true);
    setError(undefined);
    try {
      const [incidentResult, serviceResult] = await Promise.allSettled([api.incidents(token), api.services(token)]);
      if (incidentResult.status === "rejected") throw incidentResult.reason;
      setIncidents(incidentResult.value.items);
      if (serviceResult.status === "fulfilled") {
        setServices(serviceResult.value.items);
        setServiceError(undefined);
      } else {
        setServices([]);
        setServiceError(serviceResult.reason);
      }
    } catch (reason) { setError(reason); }
    finally { setBusy(false); }
  };
  useEffect(() => {
    void load();
    const refresh = window.setInterval(() => void load(), 10_000);
    return () => window.clearInterval(refresh);
  }, [token]);

  const active = incidents.filter((item) => !["RESOLVED", "CLOSED"].includes(item.state));
  const critical = active.filter((item) => item.severity === "SEV1" || item.severity === "SEV2");
  const resolved = incidents.filter((item) => item.state === "RESOLVED" || item.state === "CLOSED");
  const displayed = useMemo(() => incidents.filter((incident) => {
    const matchesFilter = filter === "all" || (filter === "active" && !["RESOLVED", "CLOSED"].includes(incident.state)) || (filter === "critical" && ["SEV1", "SEV2"].includes(incident.severity)) || (filter === "resolved" && ["RESOLVED", "CLOSED"].includes(incident.state));
    return matchesFilter && (incident.title + " " + (incident.description ?? "")).toLowerCase().includes(search.toLowerCase());
  }), [incidents, filter, search]);

  return <>
    <PageHeader eyebrow="COMMAND CENTER / INCIDENTS" title="Incident dashboard" subtitle="A current view of your tenant's incident response activity." action={<button className="button button-quiet" onClick={() => void load()}><RefreshCw size={15} />Refresh</button>} />
    <section className="welcome-banner">
      <div className="welcome-copy"><div className="welcome-kicker"><span className="live-pulse" /> OPERATIONAL OVERVIEW</div><h2>Stay ahead of the incident.</h2><p>See active risk, follow investigations, and coordinate a safe response from one place.</p><div className="welcome-meta"><span><Clock3 size={14} /> Tenant incident data</span><span className="welcome-separator" /><span><Activity size={14} /> Incident feed</span></div></div>
      <div className="welcome-art" aria-hidden="true"><div className="orbit orbit-one" /><div className="orbit orbit-two" /><div className="orbit orbit-three" /><div className="orbit-core"><Siren size={28} /></div><span className="orbit-node node-a" /><span className="orbit-node node-b" /><span className="orbit-node node-c" /></div>
    </section>
    <section className="stat-grid">
      <StatCard label="Active incidents" value={busy ? "—" : String(active.length).padStart(2, "0")} detail="Across all monitored services" icon={<BellRing size={17} />} tone="blue" />
      <StatCard label="High severity" value={busy ? "—" : String(critical.length).padStart(2, "0")} detail="SEV-1 and SEV-2 incidents" icon={<Siren size={17} />} tone="red" />
      <StatCard label="Services tracked" value={busy ? "—" : String(services.length).padStart(2, "0")} detail="Registered in this tenant" icon={<GitBranch size={17} />} tone="violet" />
      <StatCard label="Resolved history" value={busy ? "—" : String(resolved.length).padStart(2, "0")} detail="Resolved or closed incidents" icon={<CircleCheck size={17} />} tone="green" />
    </section>
    <section className="dashboard-grid">
      <div className="panel incident-panel">
        <div className="panel-heading"><div><div className="eyebrow">INCIDENT QUEUE</div><h2>Latest incidents <span className="count-pill">{incidents.length}</span></h2></div><Link to="/incidents" className="text-link">View all<ArrowUpRight size={14} /></Link></div>
        {error ? <ErrorState error={error} onRetry={() => void load()} /> : busy ? <Loading label="Loading incidents" /> : incidents.length === 0 ? <EmptyState icon={<BellRing size={19} />} title="No incidents yet" text="When OpsMind detects an anomaly, incidents will appear here." /> : <>
          <div className="table-toolbar"><div className="filter-pills">{(["active", "all", "critical", "resolved"] as Filter[]).map((item) => <button key={item} onClick={() => setFilter(item)} className={"filter-pill" + (filter === item ? " selected" : "")}>{item === "critical" ? "High severity" : item[0].toUpperCase() + item.slice(1)}</button>)}</div><input className="search-input" aria-label="Filter incidents" placeholder="Filter incidents…" value={search} onChange={(event) => setSearch(event.target.value)} /></div>
          <IncidentTable incidents={displayed.slice(0, 6)} compact />
        </>}
      </div>
      <aside className="right-rail">
        <div className="panel posture-panel"><div className="panel-heading"><div><div className="eyebrow">RESPONSE POSTURE</div><h2>Open incident load</h2></div><span className="panel-icon"><Activity size={16} /></span></div>
          <div className="posture-score"><div className="score-ring"><span>{busy ? "—" : active.length}</span><small>open</small></div><div><strong>{active.length === 0 ? "No active incidents" : active.length > 2 ? "Multiple active incidents" : "Active response"}</strong><p>{busy ? "Loading incident state" : "Counts from tenant incident records"}</p></div></div>
          <div className="posture-legend"><div><i className="dot dot-red" /><span>Critical</span><b>{critical.length}</b></div><div><i className="dot dot-amber" /><span>Investigating</span><b>{active.filter((item) => ["TRIAGING", "INVESTIGATING", "IDENTIFIED", "MITIGATING"].includes(item.state)).length}</b></div><div><i className="dot dot-green" /><span>Resolved</span><b>{resolved.length}</b></div></div>
          <Link to="/health" className="panel-footer-link">View system health<ArrowUpRight size={14} /></Link>
        </div>
        <div className="panel services-panel"><div className="panel-heading"><div><div className="eyebrow">SERVICE INVENTORY</div><h2>Registered services</h2></div><Link to="/topology" className="mini-icon-link" aria-label="Open topology"><ArrowUpRight size={15} /></Link></div>
          {serviceError ? <ErrorState error={serviceError} /> : busy ? <Loading label="Loading services" /> : services.length === 0 ? <p className="muted">No services registered.</p> : <div className="service-mini-list">{services.slice(0, 5).map((service) => <div className="service-mini" key={service.id}><div className="service-mini-icon"><Boxes size={15} /></div><div><strong>{service.name}</strong><small>{service.environment}{service.owner_team ? " · " + service.owner_team : ""}</small></div><span className="health-unknown" title="Health is not exposed by the service registry">—</span></div>)}</div>}
          <Link to="/topology" className="panel-footer-link">Explore service topology<ArrowUpRight size={14} /></Link>
        </div>
      </aside>
    </section>
  </>;
}

function StatCard({ label, value, detail, icon, tone }: { label: string; value: string; detail: string; icon: React.ReactNode; tone: string }) {
  return <article className="stat-card"><div className={"stat-icon tone-" + tone}>{icon}</div><span className="stat-label">{label}</span><strong className="stat-value">{value}</strong><small>{detail}</small></article>;
}
