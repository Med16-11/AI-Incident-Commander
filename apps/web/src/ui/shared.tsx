import { AlertTriangle, ArrowUpRight, LoaderCircle, Radio } from "lucide-react";
import { Link } from "react-router-dom";
import { ApiError } from "../data/client";

export function PageHeader({ eyebrow, title, subtitle, action }: {
  eyebrow: string;
  title: string;
  subtitle: string;
  action?: React.ReactNode;
}) {
  return (
    <header className="page-header">
      <div><div className="eyebrow">{eyebrow}</div><h1>{title}</h1><p>{subtitle}</p></div>
      {action && <div className="page-header-action">{action}</div>}
    </header>
  );
}

export function Loading({ label = "Loading workspace" }: { label?: string }) {
  return <div className="loading"><LoaderCircle size={18} className="spin" />{label}</div>;
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : "Something went wrong while loading data.";
  const status = error instanceof ApiError ? error.status : undefined;
  return (
    <div className="error-state">
      <div className="error-icon"><AlertTriangle size={18} /></div>
      <div><strong>{status === 401 ? "Authentication required" : status === 403 ? "Access denied by API" : "Unable to load this view"}</strong><p>{message}</p>
        {error instanceof ApiError && error.correlationId && <small>Request ID · {error.correlationId}</small>}
      </div>
      {onRetry && <button className="button button-quiet" onClick={onRetry}>Try again</button>}
    </div>
  );
}

export function StateBadge({ state }: { state: string }) {
  return <span className={`state-badge state-${state.toLowerCase()}`}>{state.replaceAll("_", " ")}</span>;
}

export function Severity({ severity }: { severity: string }) {
  return <span className={`severity severity-${severity.toLowerCase()}`}><i />{severity}</span>;
}

export function EmptyState({ icon = <Radio size={20} />, title, text, to, linkLabel }: {
  icon?: React.ReactNode;
  title: string;
  text: string;
  to?: string;
  linkLabel?: string;
}) {
  return <div className="empty-state"><div className="empty-icon">{icon}</div><h3>{title}</h3><p>{text}</p>{to && linkLabel && <Link to={to} className="text-link">{linkLabel}<ArrowUpRight size={14} /></Link>}</div>;
}

export function formatDate(value?: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

export function relativeTime(value: string) {
  const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function ConfidenceBar({ value }: { value: number }) {
  const percentage = Math.round(Math.max(0, Math.min(1, value)) * 100);
  return <div className="confidence"><div className="confidence-track"><span style={{ width: `${percentage}%` }} /></div><b>{percentage}%</b></div>;
}
