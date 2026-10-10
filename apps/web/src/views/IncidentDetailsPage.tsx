import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowLeft, ArrowUpRight, CheckCircle2, CircleDot, FileText, Gauge, GitBranch, Radio, RefreshCw, Sparkles, Waves } from "lucide-react";
import { Link, useParams } from "react-router-dom";
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, openIncidentStream } from "../data/client";
import type { Evidence, Hypothesis, Incident, Remediation, TimelineEntry } from "../data/types";
import { useAuth } from "../ui/auth";
import { RemediationCard } from "../ui/RemediationCard";
import { ConfidenceBar, EmptyState, ErrorState, formatDate, Loading, PageHeader, Severity, StateBadge } from "../ui/shared";

export function IncidentDetailsPage() {
  const { incidentId = "" } = useParams();
  const { token } = useAuth();
  const [incident, setIncident] = useState<Incident>();
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [evidence, setEvidence] = useState<Evidence[]>([]);
  const [hypotheses, setHypotheses] = useState<Hypothesis[]>([]);
  const [remediations, setRemediations] = useState<Remediation[]>([]);
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState(true);
  const [live, setLive] = useState(false);

  const load = useCallback(async () => {
    if (!incidentId) return;
    try {
      const [current, timelinePage, evidencePage, hypothesisPage, remediationPage] = await Promise.all([
        api.incident(incidentId, token), api.timeline(incidentId, token), api.evidence(incidentId, token),
        api.hypotheses(incidentId, token), api.incidentRemediations(incidentId, token),
      ]);
      setIncident(current);
      setTimeline(timelinePage.items);
      setEvidence(evidencePage.items);
      setHypotheses(hypothesisPage.items);
      setRemediations(remediationPage.items);
      setError(undefined);
    } catch (reason) { setError(reason); }
    finally { setBusy(false); }
  }, [incidentId, token]);

  useEffect(() => { setBusy(true); void load(); }, [load]);
  useEffect(() => {
    if (!incidentId) return;
    const controller = new AbortController();
    let retryTimer: number | undefined;
    const connect = async () => {
      try {
        await openIncidentStream(incidentId, token, controller.signal, () => void load());
        setLive(false);
      } catch {
        if (!controller.signal.aborted) {
          setLive(false);
          void load();
          retryTimer = window.setTimeout(connect, 5000);
        }
      }
    };
    setLive(true);
    void connect();
    return () => { controller.abort(); if (retryTimer) window.clearTimeout(retryTimer); };
  }, [incidentId, token, load]);

  const serviceNames = useMemo(() => Array.from(new Set(evidence.map((item) => item.service).filter(Boolean))), [evidence]);
  const logs = evidence.filter((item) => item.evidence_type.toLowerCase().includes("log"));
  const traces = evidence.filter((item) => item.evidence_type.toLowerCase().includes("trace"));
  const metrics = evidence.filter((item) => ["metric", "metric_snapshot", "anomaly"].some((kind) => item.evidence_type.toLowerCase().includes(kind)));
  const chartData = metrics.map((item) => {
    const raw = item.content.value ?? item.content.observed_value ?? item.content.metric_value;
    return { time: new Date(item.observed_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), value: typeof raw === "number" ? raw : Number(raw), metric: String(item.content.metric ?? item.evidence_type) };
  }).filter((point) => Number.isFinite(point.value));

  if (busy) return <Loading label="Opening incident workspace" />;
  if (error || !incident) return <><PageHeader eyebrow="INCIDENT / DETAILS" title="Incident details" subtitle="The incident record could not be loaded." /><ErrorState error={error ?? new Error("Incident not found")} onRetry={() => void load()} /></>;

  return <>
    <div className="back-row"><Link to="/incidents"><ArrowLeft size={15} />All incidents</Link><span className={"live-feed-badge" + (live ? " is-live" : "")}><i />{live ? "LIVE UPDATES" : "RECONNECTING"}</span><button className="icon-button" aria-label="Refresh incident" onClick={() => void load()}><RefreshCw size={15} /></button></div>
    <PageHeader eyebrow={"INCIDENT / INC-" + incident.id.slice(0, 6).toUpperCase()} title={incident.title} subtitle={incident.description || "No incident description has been added."} action={<><Severity severity={incident.severity} /><StateBadge state={incident.state} /></>} />
    <section className="incident-overview-grid">
      <div className="overview-tile"><span>DETECTED</span><strong>{formatDate(incident.created_at)}</strong><small>Last updated {formatDate(incident.updated_at)}</small></div>
      <div className="overview-tile"><span>AFFECTED SERVICES</span><strong>{serviceNames.length || (incident.service_id ? 1 : 0)} services</strong><small>{serviceNames.slice(0, 3).join(", ") || (incident.service_id ? "Service " + incident.service_id.slice(0, 8) : "Not assigned")}</small></div>
      <div className="overview-tile"><span>EVIDENCE ITEMS</span><strong>{evidence.length}</strong><small>Across logs, metrics, and traces</small></div>
      <div className="overview-tile"><span>AI CONFIDENCE</span><strong>{hypotheses.length ? Math.round(Math.max(...hypotheses.map((item) => item.confidence)) * 100) + "%" : "—"}</strong><small>{hypotheses.length} current hypotheses</small></div>
    </section>
    <div className="detail-layout">
      <div className="detail-main-column">
        <section className="panel detail-panel"><div className="panel-heading"><div><div className="eyebrow">INCIDENT SIGNALS</div><h2>Metrics <span className="count-pill">{metrics.length}</span></h2></div><span className="panel-icon panel-icon-cyan"><Gauge size={16} /></span></div>
          {chartData.length ? <div className="chart-box"><ResponsiveContainer width="100%" height={240}><LineChart data={chartData}><CartesianGrid strokeDasharray="3 3" stroke="#243043" /><XAxis dataKey="time" stroke="#8793a7" tickLine={false} axisLine={false} /><YAxis stroke="#8793a7" tickLine={false} axisLine={false} width={46} /><Tooltip contentStyle={{ background: "#101827", border: "1px solid #263347", borderRadius: 9, color: "#e8edf7" }} /><Line type="monotone" dataKey="value" stroke="#75a7ff" strokeWidth={2} dot={{ r: 3, fill: "#75a7ff" }} /></LineChart></ResponsiveContainer></div> : <EmptyState icon={<Gauge size={18} />} title="No metric evidence" text="Metric snapshots will appear when correlated evidence is available." />}
          {metrics.length > 0 && <EvidenceList items={metrics} />}
        </section>
        <section className="panel detail-panel"><div className="panel-heading"><div><div className="eyebrow">INVESTIGATION</div><h2>Timeline <span className="count-pill">{timeline.length}</span></h2></div><span className="panel-icon"><Waves size={16} /></span></div>
          <TimelineList entries={timeline} />
        </section>
        <div className="signals-grid">
          <EvidencePanel title="Logs" icon={<FileText size={15} />} items={logs} empty="No log evidence collected." />
          <EvidencePanel title="Traces" icon={<GitBranch size={15} />} items={traces} empty="No trace evidence collected." />
        </div>
      </div>
      <div className="detail-side-column">
        <section className="panel detail-panel hypothesis-panel"><div className="panel-heading"><div><div className="eyebrow">AI INVESTIGATION</div><h2>Hypotheses <span className="count-pill">{hypotheses.length}</span></h2></div><span className="panel-icon panel-icon-violet"><Sparkles size={16} /></span></div>
          {hypotheses.length === 0 ? <EmptyState icon={<Sparkles size={18} />} title="No hypotheses yet" text="The investigation engine has not published an analysis for this incident." /> : <div className="hypothesis-list">{hypotheses.map((item) => <article className="hypothesis-card" key={item.id}><div className="hypothesis-title"><strong>{item.summary}</strong><StateBadge state={item.status} /></div><ConfidenceBar value={item.confidence} /><p>{item.rationale || "No rationale provided."}</p><small>Updated {formatDate(item.updated_at)}</small></article>)}</div>}
        </section>
        <section className="panel detail-panel evidence-panel"><div className="panel-heading"><div><div className="eyebrow">CORRELATED DATA</div><h2>Supporting evidence</h2></div><span className="panel-icon panel-icon-green"><CheckCircle2 size={16} /></span></div><EvidenceList items={evidence} /></section>
        <section className="panel detail-panel"><div className="panel-heading"><div><div className="eyebrow">RESPONSE</div><h2>Recommended remediation <span className="count-pill">{remediations.length}</span></h2></div></div>
          {remediations.length ? remediations.map((action) => <RemediationCard key={action.id} action={action} onChanged={() => void load()} />) : <EmptyState icon={<CircleDot size={18} />} title="No remediation proposed" text="Recommendations will appear after an investigation proposes an action." />}
        </section>
      </div>
    </div>
  </>;
}

function TimelineList({ entries }: { entries: TimelineEntry[] }) {
  if (!entries.length) return <EmptyState icon={<Radio size={18} />} title="Timeline is empty" text="Incident activity and collected evidence will be recorded here." />;
  return <div className="timeline-list">{entries.slice().sort((a, b) => a.sequence - b.sequence).map((entry) => <article className="timeline-entry" key={entry.id}><div className="timeline-rail"><span className="timeline-point" /><i /></div><div className="timeline-entry-body"><div className="timeline-entry-head"><strong>{entry.summary}</strong><time>{formatDate(entry.occurred_at)}</time></div><div className="timeline-entry-meta"><span>{entry.event_type.replaceAll("_", " ")}</span>{entry.evidence_id && <span>Evidence linked</span>}</div></div></article>)}</div>;
}

function EvidenceList({ items }: { items: Evidence[] }) {
  if (!items.length) return <div className="empty-inline">No supporting evidence has been attached.</div>;
  return <div className="evidence-list">{items.map((item) => <article className="evidence-item" key={item.id}><span className={"evidence-kind kind-" + item.evidence_type.toLowerCase().replace(/[^a-z]/g, "").slice(0, 6)}>{item.evidence_type.replaceAll("_", " ")}</span><div><strong>{item.normalized_summary}</strong><small>{item.service} · {item.source} · {formatDate(item.observed_at)}</small></div><span className="evidence-confidence">{Math.round(item.confidence * 100)}%</span>{item.external_ref && <a href={item.external_ref} target="_blank" rel="noreferrer" aria-label="Open evidence source"><ArrowUpRight size={14} /></a>}</article>)}</div>;
}

function EvidencePanel({ title, icon, items, empty }: { title: string; icon: React.ReactNode; items: Evidence[]; empty: string }) {
  return <section className="panel detail-panel small-evidence-panel"><div className="panel-heading"><div><div className="eyebrow">CORRELATED DATA</div><h2>{icon}{title}<span className="count-pill">{items.length}</span></h2></div></div>{items.length ? <EvidenceList items={items} /> : <EmptyState icon={icon} title={"No " + title.toLowerCase() + " data"} text={empty} />}</section>;
}
