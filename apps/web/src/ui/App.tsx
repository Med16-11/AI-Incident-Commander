import { lazy, Suspense, useState } from "react";
import { NavLink, Route, Routes, useLocation } from "react-router-dom";
import {
  Activity, BellRing, Boxes, ChevronDown, CircleHelp, Command, GitBranch,
  History, KeyRound, LayoutDashboard, LifeBuoy, Menu, PanelLeftClose,
  ShieldCheck, X,
} from "lucide-react";
import { useAuth } from "./auth";
import { api } from "../data/client";
const DashboardPage = lazy(() => import("../views/DashboardPage").then((module) => ({ default: module.DashboardPage })));
const IncidentDetailsPage = lazy(() => import("../views/IncidentDetailsPage").then((module) => ({ default: module.IncidentDetailsPage })));
const TopologyPage = lazy(() => import("../views/TopologyPage").then((module) => ({ default: module.TopologyPage })));
const TimelinePage = lazy(() => import("../views/TimelinePage").then((module) => ({ default: module.TimelinePage })));
const RemediationPage = lazy(() => import("../views/RemediationPage").then((module) => ({ default: module.RemediationPage })));
const HistoricalPage = lazy(() => import("../views/HistoricalPage").then((module) => ({ default: module.HistoricalPage })));
const HealthPage = lazy(() => import("../views/HealthPage").then((module) => ({ default: module.HealthPage })));

const navigation = [
  { label: "Overview", to: "/", icon: LayoutDashboard, section: "COMMAND" },
  { label: "Incidents", to: "/incidents", icon: BellRing, section: "COMMAND" },
  { label: "Service topology", to: "/topology", icon: GitBranch, section: "OBSERVABILITY" },
  { label: "Investigation timeline", to: "/timeline", icon: Activity, section: "OBSERVABILITY" },
  { label: "Remediation center", to: "/remediations", icon: ShieldCheck, section: "RESPONSE" },
  { label: "Historical incidents", to: "/history", icon: History, section: "RESPONSE" },
  { label: "System health", to: "/health", icon: Boxes, section: "PLATFORM" },
];

const titles: Record<string, string> = {
  "/": "Incident dashboard", "/incidents": "Incident dashboard", "/topology": "Service topology",
  "/timeline": "Investigation timeline", "/remediations": "Remediation center",
  "/history": "Historical incidents", "/health": "System health",
};

export function App() {
  const [mobileOpen, setMobileOpen] = useState(false);
  const [tokenOpen, setTokenOpen] = useState(false);
  const [tokenDraft, setTokenDraft] = useState("");
  const [tokenError, setTokenError] = useState("");
  const [tokenConnecting, setTokenConnecting] = useState(false);
  const { token, setToken } = useAuth();
  const location = useLocation();
  const title = location.pathname.startsWith("/incidents/") ? "Incident details" : titles[location.pathname] ?? "OpsMind";

  const aside = (
    <aside className="sidebar">
      <div className="brand"><div className="brand-mark"><Command size={19} /></div><span>opsmind<span className="brand-dot">.</span></span><button className="icon-button sidebar-collapse" aria-label="Close navigation" onClick={() => setMobileOpen(false)}><PanelLeftClose size={16} /></button></div>
      <div className="workspace-switch"><div className="workspace-avatar">O</div><div className="workspace-copy"><strong>OpsMind workspace</strong><small>Tenant-scoped view</small></div><ChevronDown size={15} /></div>
      <div className="nav-scroll">
        {(["COMMAND", "OBSERVABILITY", "RESPONSE", "PLATFORM"] as const).map((section) => (
          <div className="nav-section" key={section}>
            <div className="nav-label">{section}</div>
            {navigation.filter((item) => item.section === section).map(({ label, to, icon: Icon }) => (
              <NavLink key={to} to={to} end={to === "/"} className={({ isActive }) => `nav-link${isActive ? " active" : ""}`} onClick={() => setMobileOpen(false)}>
                <Icon size={17} strokeWidth={1.8} /><span>{label}</span>
              </NavLink>
            ))}
          </div>
        ))}
      </div>
      <div className="sidebar-bottom"><div className="sidebar-help"><div className="help-icon"><CircleHelp size={15} /></div><div><strong>Need a hand?</strong><small>Read the runbook</small></div><LifeBuoy size={14} /></div>
        <button className="profile-row" onClick={() => { setTokenDraft(token ?? ""); setTokenError(""); setTokenOpen(true); }}><div className="profile-avatar">U</div><div className="profile-copy"><strong>My session</strong><small>{token ? "API credential verified" : "API credential not set"}</small></div><ChevronDown size={15} /></button>
      </div>
    </aside>
  );

  return (
    <div className="app-shell">
      {mobileOpen && <button aria-label="Close menu overlay" className="mobile-scrim" onClick={() => setMobileOpen(false)} />}
      {aside}
      <main className="main-shell">
        <header className="topbar">
          <button className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setMobileOpen(true)}><Menu size={19} /></button>
          <div className="breadcrumbs"><span>OpsMind</span><b>/</b><strong>{title}</strong></div>
          <div className="topbar-right"><span className="live-indicator"><i />{token ? "API session verified" : "Session not connected"}</span><span className="top-divider" /><button className="token-button" onClick={() => { setTokenDraft(token ?? ""); setTokenError(""); setTokenOpen(true); }}><KeyRound size={15} />{token ? "Session verified" : "Connect session"}</button><div className="top-avatar">U</div></div>
        </header>
        <div className="content-area">
          <Suspense fallback={<div className="panel"><div className="loading">Loading workspace…</div></div>}><Routes>
            <Route path="/" element={<DashboardPage />} />
            <Route path="/incidents" element={<DashboardPage />} />
            <Route path="/incidents/:incidentId" element={<IncidentDetailsPage />} />
            <Route path="/topology" element={<TopologyPage />} />
            <Route path="/timeline" element={<TimelinePage />} />
            <Route path="/remediations" element={<RemediationPage />} />
            <Route path="/history" element={<HistoricalPage />} />
            <Route path="/health" element={<HealthPage />} />
          </Routes></Suspense>
        </div>
        <footer className="app-footer"><span>OpsMind Command Center <b>v0.1.0</b></span><span>Tenant-scoped data <b>·</b> Authorization enforced by API <b>·</b> {new Date().getFullYear()}</span></footer>
      </main>
      {tokenOpen && <div className="modal-backdrop" onClick={() => setTokenOpen(false)}><section className="modal-card" onClick={(event) => event.stopPropagation()}>
        <div className="modal-top"><div className="modal-symbol"><KeyRound size={19} /></div><button className="icon-button" aria-label="Close" onClick={() => setTokenOpen(false)}><X size={18} /></button></div>
        <div className="eyebrow">SESSION CREDENTIAL</div><h2>Connect to your OpsMind API</h2><p>Paste the local demo token configured in your .env file. The API validates it and enforces tenant permissions server-side.</p>
        <label className="field-label" htmlFor="session-token">Bearer token</label><input id="session-token" className="text-input" type="password" autoComplete="off" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} placeholder="Paste your API token" />
        {tokenError && <p className="session-error" role="alert">{tokenError}</p>}
        <div className="modal-actions"><button className="button button-quiet" onClick={() => { setToken(""); setTokenError(""); setTokenOpen(false); }}>Disconnect</button><button className="button button-primary" disabled={tokenConnecting || !tokenDraft.trim()} onClick={() => {
          const candidate = tokenDraft.trim();
          setTokenConnecting(true);
          setTokenError("");
          void api.incidents(candidate, "?limit=1").then(() => {
            setToken(candidate);
            setTokenOpen(false);
          }).catch((error: unknown) => {
            const message = error instanceof Error ? error.message : "The API could not validate this credential.";
            setTokenError(message === "Authentication is not configured"
              ? "The API has no local demo credential configured. Set OPSMIND_LOCAL_DEMO_AUTH_ENABLED=true and OPSMIND_LOCAL_DEMO_TOKEN in the repo-root .env, then recreate the api and remediation-engine containers."
              : `The API did not accept this credential: ${message}`);
          }).finally(() => setTokenConnecting(false));
        }}>{tokenConnecting ? "Verifying…" : "Verify and connect"}</button></div>
      </section></div>}
    </div>
  );
}
