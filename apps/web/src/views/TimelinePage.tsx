import { useCallback, useEffect, useState } from "react";
import { Activity, ArrowUpRight, RefreshCw, Waves } from "lucide-react";
import { Link } from "react-router-dom";
import { api, openIncidentStream } from "../data/client";
import type { Incident, TimelineEntry } from "../data/types";
import { useAuth } from "../ui/auth";
import { EmptyState, ErrorState, formatDate, Loading, PageHeader, Severity, StateBadge } from "../ui/shared";

export function TimelinePage() {
  const { token } = useAuth();
  const [incidents, setIncidents] = useState<Incident[]>([]);
  const [selected, setSelected] = useState("");
  const [entries, setEntries] = useState<TimelineEntry[]>([]);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState<unknown>();
  const [live, setLive] = useState(false);

  const loadIncidents = async () => {
    try {
      const page = await api.incidents(token);
      setIncidents(page.items);
      setSelected((current) => current || page.items[0]?.id || "");
    } catch (reason) { setError(reason); }
  };
  const loadTimeline = useCallback(async () => {
    if (!selected) { setEntries([]); setBusy(false); return; }
    setBusy(true);
    try { setEntries((await api.timeline(selected, token)).items); setError(undefined); }
    catch (reason) { setError(reason); }
    finally { setBusy(false); }
  }, [selected, token]);

  useEffect(() => { void loadIncidents(); }, [token]);
  useEffect(() => { void loadTimeline(); }, [loadTimeline]);
  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    let retry: number | undefined;
    const connect = async () => {
      try { await openIncidentStream(selected, token, controller.signal, () => void loadTimeline()); }
      catch {
        if (!controller.signal.aborted) {
          setLive(false);
          void loadTimeline();
          retry = window.setTimeout(connect, 5000);
        }
      }
    };
    setLive(true);
    void connect();
    return () => { controller.abort(); if (retry) window.clearTimeout(retry); };
  }, [selected, token, loadTimeline]);

  const incident = incidents.find((item) => item.id === selected);
  return <>
    <PageHeader eyebrow="OBSERVABILITY / EVENT STREAM" title="Investigation timeline" subtitle="Follow incident activity and evidence as it is recorded." action={<span className={"live-feed-badge" + (live ? " is-live" : "")}><i />{live ? "LIVE STREAM" : "CONNECTING"}</span>} />
    <div className="timeline-workspace">
      <aside className="panel timeline-sidebar"><div className="panel-heading"><div><div className="eyebrow">INCIDENTS</div><h2>Choose an incident</h2></div><button className="mini-icon-link" aria-label="Refresh timeline" onClick={() => void loadIncidents()}><RefreshCw size={14} /></button></div>
        {incidents.length ? <div className="timeline-incident-list">{incidents.map((item) => <button key={item.id} className={"timeline-incident" + (selected === item.id ? " active" : "")} onClick={() => setSelected(item.id)}><span className="timeline-incident-top"><Severity severity={item.severity} /><StateBadge state={item.state} /></span><strong>{item.title}</strong><small>{item.id.slice(0, 8).toUpperCase()} · {formatDate(item.created_at)}</small></button>)}</div> : error ? <ErrorState error={error} /> : <Loading label="Loading incidents" />}
      </aside>
      <section className="panel timeline-stream-panel">
        {!incident ? <EmptyState icon={<Activity size={20} />} title="Select an incident" text="Choose an incident to inspect its chronological activity." /> : <>
          <div className="panel-heading"><div><div className="eyebrow">INC-{incident.id.slice(0, 6).toUpperCase()} / ACTIVITY</div><h2>{incident.title}</h2></div><Link to={"/incidents/" + incident.id} className="text-link">Open incident<ArrowUpRight size={14} /></Link></div>
          {busy ? <Loading label="Loading event timeline" /> : error ? <ErrorState error={error} onRetry={() => void loadTimeline()} /> : entries.length ? <div className="timeline-list timeline-stream-list">{entries.slice().sort((a, b) => a.sequence - b.sequence).map((entry) => <article className="timeline-entry" key={entry.id}><div className="timeline-rail"><span className="timeline-point" /><i /></div><div className="timeline-entry-body"><div className="timeline-entry-head"><strong>{entry.summary}</strong><time>{formatDate(entry.occurred_at)}</time></div><div className="timeline-entry-meta"><span>{entry.event_type.replaceAll("_", " ")}</span><span>Sequence {entry.sequence}</span>{entry.actor_id && <span>Human action</span>}</div>{entry.evidence_id && <div className="timeline-evidence-ref"><Waves size={13} /> Evidence reference <code>{entry.evidence_id.slice(0, 12)}</code></div>}</div></article>)}</div> : <EmptyState icon={<Waves size={20} />} title="No activity recorded" text="New timeline entries will stream here as the incident changes." />}
        </>}
      </section>
    </div>
  </>;
}
