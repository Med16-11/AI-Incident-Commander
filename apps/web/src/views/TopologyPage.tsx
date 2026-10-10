import { useEffect, useMemo, useState } from "react";
import { ArrowDownRight, ArrowUpRight, Boxes, Database, GitBranch, RefreshCw, Server, Shield, Workflow } from "lucide-react";
import { api } from "../data/client";
import type { ServiceDependencies, ServiceNode } from "../data/types";
import { useAuth } from "../ui/auth";
import { EmptyState, ErrorState, Loading, PageHeader } from "../ui/shared";

interface GraphNode { service: ServiceNode; x: number; y: number; }

export function TopologyPage() {
  const { token } = useAuth();
  const [services, setServices] = useState<ServiceNode[]>([]);
  const [dependencies, setDependencies] = useState<ServiceDependencies[]>([]);
  const [selected, setSelected] = useState<string>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(true);

  const load = async () => {
    setBusy(true);
    setError(undefined);
    try {
      const page = await api.services(token);
      const graph = await Promise.all(page.items.map((service) => api.dependencies(service.id, token)));
      setServices(page.items);
      setDependencies(graph);
      setSelected((current) => current && page.items.some((service) => service.id === current) ? current : page.items[0]?.id);
    } catch (reason) { setError(reason); }
    finally { setBusy(false); }
  };
  useEffect(() => { void load(); }, [token]);

  const graph = useMemo(() => graphLayout(services, dependencies), [services, dependencies]);
  const current = services.find((item) => item.id === selected);
  const detail = dependencies.find((item) => item.service.id === selected);
  const width = Math.max(760, ...graph.nodes.map((node) => node.x + 200));
  const height = Math.max(410, ...graph.nodes.map((node) => node.y + 116));

  return <>
    <PageHeader eyebrow="OBSERVABILITY / DEPENDENCIES" title="Service topology" subtitle="Explore runtime dependencies and understand the impact radius of a service." action={<button className="button button-quiet" onClick={() => void load()}><RefreshCw size={15} />Refresh graph</button>} />
    <section className="topology-summary">
      <div className="topology-stat"><div className="topology-stat-icon"><Boxes size={17} /></div><div><small>REGISTERED SERVICES</small><strong>{busy ? "—" : services.length}</strong></div></div>
      <div className="topology-stat"><div className="topology-stat-icon topology-stat-cyan"><Workflow size={17} /></div><div><small>DEPENDENCY EDGES</small><strong>{busy ? "—" : graph.edges.length}</strong></div></div>
      <div className="topology-stat"><div className="topology-stat-icon topology-stat-violet"><Shield size={17} /></div><div><small>GRAPH SOURCE</small><strong>Service registry</strong></div></div>
      <div className="topology-stat topology-help"><div><strong>Dependency direction</strong><small>An arrow points from a service to the dependency it calls.</small></div><div className="direction-sample"><span /> <ArrowDownRight size={14} /></div></div>
    </section>
    {error ? <ErrorState error={error} onRetry={() => void load()} /> : busy ? <div className="panel topology-loading"><Loading label="Mapping service dependencies" /></div> : services.length === 0 ? <div className="panel"><EmptyState icon={<GitBranch size={20} />} title="No topology registered" text="Create services and dependency edges through the OpsMind service API." /></div> : <>
      <div className="topology-layout">
        <section className="panel graph-panel">
          <div className="panel-heading"><div><div className="eyebrow">DEPENDENCY MAP</div><h2>Production service graph</h2></div><div className="graph-legend"><span><i className="legend-api" />Service</span><span><i className="legend-db" />Data store</span></div></div>
          <div className="graph-scroll"><svg className="topology-svg" width={width} height={height} role="img" aria-label="Service dependency graph">
            <defs><marker id="topology-arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="#4e6681" /></marker></defs>
            {graph.edges.map((edge) => { const from = graph.nodes.find((node) => node.service.id === edge.from); const to = graph.nodes.find((node) => node.service.id === edge.to); if (!from || !to) return null; const x1 = from.x + 172; const y1 = from.y + 38; const x2 = to.x; const y2 = to.y + 38; const bend = Math.max(45, (x2 - x1) / 2); return <path key={edge.from + edge.to} className="topology-edge" d={"M " + x1 + " " + y1 + " C " + (x1 + bend) + " " + y1 + ", " + (x2 - bend) + " " + y2 + ", " + x2 + " " + y2} markerEnd="url(#topology-arrow)" />; })}
            {graph.nodes.map(({ service, x, y }) => { const storage = /postgres|redis|database|db|cache/i.test(service.name); const Icon = storage ? Database : Server; return <g key={service.id} className={"topology-node" + (selected === service.id ? " selected" : "")} onClick={() => setSelected(service.id)} tabIndex={0} role="button" aria-label={"Select service " + service.name} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") setSelected(service.id); }}>
              <rect x={x} y={y} width="172" height="78" rx="12" />
              <rect x={x + 12} y={y + 15} width="34" height="34" rx="9" className={storage ? "node-icon-store" : "node-icon-service"} />
              <foreignObject x={x + 21} y={y + 23} width="18" height="18"><div className="node-icon-foreign"><Icon size={16} /></div></foreignObject>
              <text x={x + 56} y={y + 32} className="node-title">{service.name}</text>
              <text x={x + 56} y={y + 51} className="node-subtitle">{service.environment}{service.owner_team ? " · " + service.owner_team : ""}</text>
              <circle cx={x + 154} cy={y + 18} r="3.5" className="node-health-unknown" />
            </g>; })}
            {!graph.edges.length && <text x="34" y="375" className="node-subtitle">No dependency edges have been registered yet.</text>}
          </svg></div>
          <div className="graph-caption"><span><i className="health-unknown" />Health status is not supplied by the topology API</span><span>{services.length} services · {graph.edges.length} edges</span></div>
        </section>
        <aside className="panel topology-detail">
          <div className="panel-heading"><div><div className="eyebrow">SERVICE PROFILE</div><h2>{current?.name ?? "Select a node"}</h2></div><div className="detail-service-icon"><Server size={17} /></div></div>
          {current && detail && <><div className="service-profile-env"><span className="env-pip" />{current.environment}<span className="profile-divider" />{current.owner_team || "No owner team"}</div>
            <div className="topology-detail-section"><small>UPSTREAM IMPACT</small><div className="impact-number">{detail.upstream_services.length}<span> services depend on this path</span></div><div className="impact-chips">{detail.upstream_services.slice(0, 5).map((item) => <span key={item.id}>{item.name}</span>)}{!detail.upstream_services.length && <span className="muted-chip">No upstream callers</span>}</div></div>
            <div className="topology-detail-section"><small>DOWNSTREAM DEPENDENCIES</small><div className="impact-number">{detail.direct_dependencies.length}<span> direct dependencies</span></div><div className="impact-chips">{detail.direct_dependencies.map((item) => <span key={item.id}>{item.name}</span>)}{!detail.direct_dependencies.length && <span className="muted-chip">No dependencies</span>}</div></div>
            <div className="topology-depth-row"><div><small>DEPENDENCY DEPTH</small><strong>{detail.downstream_depth} hops</strong></div><div><small>CRITICAL PATH</small><strong>{detail.critical_path.length} services</strong></div></div>
            <div className="profile-note"><Shield size={15} /><span>Service health checks are not exposed by the topology response. Live health is shown in System Health when Prometheus has targets.</span></div>
          </>}
          {!current && <p className="muted">Select a service node to inspect its dependency and impact details.</p>}
        </aside>
      </div>
      <section className="panel service-directory"><div className="panel-heading"><div><div className="eyebrow">REGISTRY</div><h2>Service directory</h2></div><span className="count-pill">{services.length}</span></div><div className="directory-grid">{services.map((service) => <button key={service.id} className={"directory-service" + (selected === service.id ? " selected" : "")} onClick={() => setSelected(service.id)}><span className="directory-icon"><Server size={15} /></span><span><strong>{service.name}</strong><small>{service.environment} · {service.owner_team || "Unowned"}</small></span><ArrowUpRight size={14} /></button>)}</div></section>
    </>}
  </>;
}

function graphLayout(services: ServiceNode[], dependencies: ServiceDependencies[]) {
  const edges = dependencies.flatMap((item) => item.direct_dependencies.map((target) => ({ from: item.service.id, to: target.id })));
  const indegree = new Map(services.map((service) => [service.id, 0]));
  const adjacency = new Map(services.map((service) => [service.id, [] as string[]]));
  edges.forEach(({ from, to }) => { adjacency.get(from)?.push(to); indegree.set(to, (indegree.get(to) ?? 0) + 1); });
  const levels = new Map(services.map((service) => [service.id, 0]));
  const queue = services.filter((service) => indegree.get(service.id) === 0).map((service) => service.id);
  for (let index = 0; index < queue.length; index += 1) {
    const source = queue[index];
    for (const target of adjacency.get(source) ?? []) {
      levels.set(target, Math.max(levels.get(target) ?? 0, (levels.get(source) ?? 0) + 1));
      indegree.set(target, (indegree.get(target) ?? 1) - 1);
      if (indegree.get(target) === 0) queue.push(target);
    }
  }
  const groups = new Map<number, ServiceNode[]>();
  services.forEach((service) => { const level = levels.get(service.id) ?? 0; groups.set(level, [...(groups.get(level) ?? []), service]); });
  const nodes: GraphNode[] = [];
  for (const [level, group] of groups) group.forEach((service, index) => nodes.push({ service, x: 44 + level * 235, y: 45 + index * 128 }));
  return { nodes, edges };
}
