import { ArrowUpRight, ChevronRight, Clock3 } from "lucide-react";
import { Link } from "react-router-dom";
import type { Incident } from "../data/types";
import { formatDate, relativeTime, Severity, StateBadge } from "./shared";

export function IncidentTable({ incidents, compact = false }: { incidents: Incident[]; compact?: boolean }) {
  return (
    <div className="table-wrap">
      <table className="data-table incident-table">
        <thead><tr><th>Incident</th><th>Severity</th><th>Status</th><th>Detected</th><th>Updated</th><th /></tr></thead>
        <tbody>{incidents.map((incident) => (
          <tr key={incident.id}>
            <td><Link className="incident-name" to={"/incidents/" + incident.id}><span className="incident-key">INC-{incident.id.slice(0, 6).toUpperCase()}</span><strong>{incident.title}</strong><small>{incident.description || "Service ID · " + (incident.service_id?.slice(0, 8) ?? "Not assigned")}</small></Link></td>
            <td><Severity severity={incident.severity} /></td><td><StateBadge state={incident.state} /></td>
            <td><span className="table-date"><Clock3 size={13} />{compact ? relativeTime(incident.created_at) : formatDate(incident.created_at)}</span></td>
            <td><span className="muted">{relativeTime(incident.updated_at)}</span></td>
            <td><Link aria-label={"Open " + incident.title} className="row-open" to={"/incidents/" + incident.id}><ArrowUpRight size={15} /><ChevronRight size={14} /></Link></td>
          </tr>
        ))}</tbody>
      </table>
      {incidents.length === 0 && <div className="table-empty">No incidents match this view.</div>}
    </div>
  );
}
