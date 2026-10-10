import { useEffect, useState } from "react";
import { Activity, AlertTriangle, ArrowUpRight, Boxes, Cpu, Database, RefreshCw, Server, Waves } from "lucide-react";
import { api } from "../data/client";
import type { ApiHealth, PrometheusResult, ServiceNode } from "../data/types";
import { useAuth } from "../ui/auth";
import { EmptyState, Loading, PageHeader, StateBadge } from "../ui/shared";

interface Target { service: string; instance: string; up: boolean; }

export function HealthPage() {
  const { token } = useAuth();
  const [health, setHealth] = useState<ApiHealth>();
  const [targets, setTargets] = useState<Target[]>([]);
  const [services, setServices] = useState<ServiceNode[]>([]);
  const [errors, setErrors] = useState<string[]>([]);
  const [busy, setBusy] = useState(true);
  const load = async () => {
    setBusy(true);
    const [apiHealth, prometheus, servicePage] = await Promise.allSettled([
      api.health(), api.prometheus("up"), api.services(token),
    ]);
    const nextErrors: string[] = [];
    if (apiHealth.status === "fulfilled") setHealth(apiHealth.value);
    else nextErrors.push("OpsMind API readiness endpoint is unavailable.");
    if (prometheus.status === "fulfilled" && prometheus.value.status === "success") {
      const result = (prometheus.value as PrometheusResult).data?.result ?? [];
      setTargets(result.map((item) => ({
        service: item.metric.job ?? item.metric.service ?? item.metric.instance ?? "Prometheus target",
        instance: item.metric.instance ?? "—",
        up: Number(item.value[1]) === 1,
      })));
    } else nextErrors.push("Prometheus target health could not be loaded.");
    if (servicePage.status === "fulfilled") setServices(servicePage.value.items);
    else nextErrors.push("Service registry could not be loaded.");
    setErrors(nextErrors);
    setBusy(false);
  };
  useEffect(() => { void load(); }, [token]);
  const down = targets.filter((target) => !target.up);
  const operational = !!health && health.status === "ready" && down.length === 0;
  return <>
    <PageHeader eyebrow="PLATFORM / OBSERVABILITY" title="System health" subtitle="Readiness and target health from OpsMind and Prometheus." action={<button className="button button-quiet" onClick={() => void load()}><RefreshCw size={15} />Refresh</button>} />
    <section className={"health-summary" + (operational ? " summary-ok" : down.length || errors.length ? " summary-warn" : "")}>
      <div className="health-summary-icon">{errors.length || down.length ? <AlertTriangle size={20} /> : <Activity size={20} />}</div>
      <div><strong>{busy ? "Checking systems…" : operational ? "Monitoring sources are reachable" : "Health status is incomplete"}</strong><p>{busy ? "Contacting readiness and metrics endpoints." : targets.length + " Prometheus targets · " + services.length + " registered services"}</p></div>
      <div className="health-last-check"><small>LAST CHECK</small><b>{busy ? "—" : new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</b></div>
    </section>
    {errors.length > 0 && <div className="health-errors">{errors.map((item) => <div key={item}><AlertTriangle size={14} />{item}</div>)}</div>}
    <div className="health-metrics-grid">
      <HealthMetric icon={<Activity size={16} />} label="OpsMind API" status={health?.status ?? "unknown"} value={health?.service ?? "Readiness endpoint"} detail="GET /health/ready" />
      <HealthMetric icon={<Waves size={16} />} label="Prometheus" status={targets.length ? "connected" : "unknown"} value={targets.length ? targets.length + " targets" : "No target data"} detail="up metric query" />
      <HealthMetric icon={<Boxes size={16} />} label="Service registry" status={services.length ? "registered" : "unknown"} value={services.length + " services"} detail="Tenant service directory" />
      <HealthMetric icon={<Database size={16} />} label="Target failures" status={down.length ? "degraded" : targets.length ? "healthy" : "unknown"} value={String(down.length)} detail="Targets reporting up = 0" />
    </div>
    <div className="health-content-grid">
      <section className="panel health-panel"><div className="panel-heading"><div><div className="eyebrow">SCRAPED TARGETS</div><h2>Prometheus health <span className="count-pill">{targets.length}</span></h2></div><span className="panel-icon"><Server size={16} /></span></div>
        {busy ? <Loading label="Querying Prometheus" /> : targets.length ? <div className="health-target-list">{targets.map((target) => <div className="health-target" key={target.service + target.instance}><div className={"target-icon" + (target.up ? " target-up" : " target-down")}><Cpu size={16} /></div><div className="target-copy"><strong>{target.service}</strong><small>{target.instance}</small></div><StateBadge state={target.up ? "UP" : "DOWN"} /></div>)}</div> : <EmptyState icon={<Server size={18} />} title="No Prometheus targets" text="Prometheus is reachable, but its up query returned no target series." />}
      </section>
      <section className="panel health-panel"><div className="panel-heading"><div><div className="eyebrow">REGISTERED SERVICES</div><h2>Service inventory <span className="count-pill">{services.length}</span></h2></div><a className="mini-icon-link" href="/topology" aria-label="Open topology"><ArrowUpRight size={15} /></a></div>
        {busy ? <Loading label="Loading service inventory" /> : services.length ? <div className="health-target-list">{services.map((service) => <div className="health-target" key={service.id}><div className="target-icon target-neutral"><Boxes size={16} /></div><div className="target-copy"><strong>{service.name}</strong><small>{service.environment}{service.owner_team ? " · " + service.owner_team : ""}</small></div><span className="registry-state">Registered</span></div>)}</div> : <EmptyState icon={<Boxes size={18} />} title="No services registered" text="Register a service to include it in the topology inventory." />}
      </section>
    </div>
    <div className="health-footnote"><AlertTriangle size={14} /><span>The service registry does not publish health probes. Service availability here reflects Prometheus targets when configured; unreported status is left unknown.</span></div>
  </>;
}

function HealthMetric({ icon, label, status, value, detail }: { icon: React.ReactNode; label: string; status: string; value: string; detail: string }) {
  return <article className="health-metric"><div className="health-metric-top"><span>{icon}</span><StateBadge state={status} /></div><small>{label}</small><strong>{value}</strong><p>{detail}</p></article>;
}
