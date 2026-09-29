

import { useMemo, useState } from "react";
import {
  Activity,
  Bot,
  CheckCircle2,
  Clock3,
  Database,
  Github,
  Mail,
  MailCheck,
  MonitorCog,
  RefreshCw,
  Save,
  Send,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Users,
  Wifi,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";

type SettingsPageProps = {
  settings: any;
  data: any;
  busy: string;
  password: string;
  hrReportPassword: string;
  viewError?: string;
  onPasswordChange: (value: string) => void;
  onHrReportPasswordChange: (value: string) => void;
  onUpdateSetting: (key: any, value: any) => void;
  onSave: () => void | Promise<void>;
  onRunAction: (
    key: string,
    path: string,
    successFallback: string,
    body?: unknown,
  ) => void | Promise<void>;
};

type TabKey = "general" | "connections" | "automation" | "reports" | "system";

const tabs: Array<{ key: TabKey; label: string; icon: any }> = [
  { key: "general", label: "General", icon: Settings2 },
  { key: "connections", label: "Connections", icon: Wifi },
  { key: "automation", label: "Automation", icon: SlidersHorizontal },
  { key: "reports", label: "Email & Reports", icon: Mail },
  { key: "system", label: "System", icon: MonitorCog },
];

const formatDate = (value?: string | null) => {
  if (!value) return "—";
  try {
    return new Intl.DateTimeFormat("en-IN", {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(new Date(value));
  } catch {
    return value;
  }
};

const mailErrorSummary = (value?: string | null) => {
  const text = (value || "").trim();
  if (!text) return "";
  const lower = text.toLowerCase();

  if (
    lower.includes("app password") ||
    lower.includes("authentication") ||
    lower.includes("535") ||
    lower.includes("534")
  ) {
    return "Gmail App Password rejected";
  }

  if (
    lower.includes("network") ||
    lower.includes("connect") ||
    lower.includes("timed out") ||
    lower.includes("timeout")
  ) {
    return "Gmail connection failed";
  }

  return text.length > 90 ? `${text.slice(0, 87)}…` : text;
};

function StatusBadge({
  ok,
  good = "Connected",
  bad = "Needs attention",
}: {
  ok: boolean;
  good?: string;
  bad?: string;
}) {
  return (
    <span className={`nset-status ${ok ? "ok" : "warn"}`}>
      <i />
      {ok ? good : bad}
    </span>
  );
}

function ToggleRow({
  title,
  description,
  checked,
  onChange,
}: {
  title: string;
  description: string;
  checked: boolean;
  onChange: (next: boolean) => void;
}) {
  return (
    <div className="nset-toggle-row">
      <div>
        <strong>{title}</strong>
        <span>{description}</span>
      </div>
      <button
        type="button"
        className={`nset-switch ${checked ? "on" : ""}`}
        onClick={() => onChange(!checked)}
        aria-pressed={checked}
      >
        <span />
      </button>
    </div>
  );
}

export function SettingsPage({
  settings,
  data,
  busy,
  password,
  hrReportPassword,
  viewError = "",
  onPasswordChange,
  onHrReportPasswordChange,
  onUpdateSetting,
  onSave,
  onRunAction,
}: SettingsPageProps) {
  const [activeTab, setActiveTab] = useState<TabKey>("general");

  const backendErrors = data?.backend_health?.errors || [];
  const databaseOk = Boolean(data?.backend_health?.database?.ok);
  const chromeOk = Boolean(data?.chrome?.ok);
  const indeedOk = Boolean(data?.live_detection?.healthy || data?.core_recruitment?.indeed_live);
  const candidateGmailOk = Boolean(data?.email_verified);
  const hrGmailOk = Boolean(data?.recruitment_pipeline?.report_gmail_verified);

  const githubRepo =
    settings?.persistence?.github_repository ||
    "Nunes-instruments/indeed_auomation";

  const rankingWeights = settings?.ranking_weights || {};

  const systemHealth = useMemo(() => {
    const checks = [databaseOk, chromeOk, backendErrors.length === 0];
    return checks.filter(Boolean).length;
  }, [databaseOk, chromeOk, backendErrors.length]);

  return (
    <article className="nset-shell" id="settings">
      <style>{`
        .nset-shell {
          --nset-border: #dce5ef;
          --nset-soft: #f6f9fc;
          --nset-text: #172033;
          --nset-muted: #667085;
          --nset-blue: #2563eb;
          --nset-green: #0f9f6e;
          --nset-amber: #b7791f;
          background: #fff;
          border: 1px solid var(--nset-border);
          border-radius: 22px;
          overflow: hidden;
          box-shadow: 0 14px 40px rgba(15, 23, 42, 0.07);
        }
        .nset-head {
          display:flex;
          align-items:center;
          justify-content:space-between;
          gap:20px;
          padding:24px 26px 18px;
          border-bottom:1px solid var(--nset-border);
          background:linear-gradient(180deg,#ffffff 0%,#fbfdff 100%);
        }
        .nset-head h2 { margin:4px 0 5px; font-size:26px; color:var(--nset-text); }
        .nset-head p { margin:0; color:var(--nset-muted); font-size:14px; }
        .nset-eyebrow {
          font-size:12px!important;
          font-weight:800;
          letter-spacing:.12em;
          text-transform:uppercase;
          color:var(--nset-blue)!important;
        }
        .nset-save { min-width:145px; }
        .nset-tabs {
          display:flex;
          gap:8px;
          padding:14px 18px;
          overflow:auto;
          border-bottom:1px solid var(--nset-border);
          background:#fff;
        }
        .nset-tab {
          border:1px solid transparent;
          background:transparent;
          color:#526071;
          border-radius:12px;
          padding:10px 14px;
          display:flex;
          align-items:center;
          gap:8px;
          font-weight:700;
          white-space:nowrap;
          cursor:pointer;
        }
        .nset-tab svg { width:17px; height:17px; }
        .nset-tab:hover { background:var(--nset-soft); }
        .nset-tab.active {
          color:var(--nset-blue);
          background:#eef4ff;
          border-color:#cfe0ff;
        }
        .nset-content { padding:22px; background:#f8fafc; }
        .nset-warning {
          display:flex;
          align-items:flex-start;
          gap:10px;
          margin-bottom:16px;
          padding:12px 14px;
          border:1px solid #f4d7a1;
          background:#fff9ed;
          border-radius:12px;
          color:#7a4d00;
          font-size:13px;
        }
        .nset-warning svg { width:18px; min-width:18px; margin-top:1px; }
        .nset-grid {
          display:grid;
          grid-template-columns:repeat(2,minmax(0,1fr));
          gap:16px;
        }
        .nset-card {
          background:#fff;
          border:1px solid var(--nset-border);
          border-radius:16px;
          padding:18px;
        }
        .nset-card.full { grid-column:1/-1; }
        .nset-card-head {
          display:flex;
          align-items:flex-start;
          justify-content:space-between;
          gap:12px;
          margin-bottom:16px;
        }
        .nset-card-title {
          display:flex;
          align-items:flex-start;
          gap:11px;
        }
        .nset-icon {
          width:38px;
          height:38px;
          border-radius:11px;
          display:grid;
          place-items:center;
          background:#eef4ff;
          color:var(--nset-blue);
          flex:none;
        }
        .nset-icon.green { background:#ecfdf5; color:#0f9f6e; }
        .nset-icon.amber { background:#fff7e8; color:#b7791f; }
        .nset-icon.gray { background:#f2f4f7; color:#475467; }
        .nset-icon svg { width:19px; height:19px; }
        .nset-card h3 { margin:0; font-size:17px; color:var(--nset-text); }
        .nset-card-head p {
          margin:4px 0 0;
          color:var(--nset-muted);
          font-size:13px;
          line-height:1.45;
        }
        .nset-form {
          display:grid;
          grid-template-columns:repeat(2,minmax(0,1fr));
          gap:14px;
        }
        .nset-field { display:flex; flex-direction:column; gap:7px; }
        .nset-field.full { grid-column:1/-1; }
        .nset-field > span {
          color:#344054;
          font-size:13px;
          font-weight:750;
        }
        .nset-field input,
        .nset-field select {
          width:100%;
          min-height:44px;
          border:1px solid #cfd8e3;
          background:#fff;
          color:#172033;
          border-radius:10px;
          padding:10px 12px;
          outline:none;
          font-size:14px;
        }
        .nset-field input:focus,
        .nset-field select:focus {
          border-color:#8eb4ff;
          box-shadow:0 0 0 3px rgba(37,99,235,.10);
        }
        .nset-field input[readonly] { background:#f7f9fc; color:#667085; }
        .nset-field small { color:#7b8797; line-height:1.4; }
        .nset-field small.good { color:#087a56; font-weight:650; }
        .nset-connections {
          display:grid;
          grid-template-columns:repeat(3,minmax(0,1fr));
          gap:14px;
        }
        .nset-connection {
          border:1px solid var(--nset-border);
          border-radius:14px;
          background:#fff;
          padding:16px;
          min-height:145px;
          display:flex;
          flex-direction:column;
          justify-content:space-between;
        }
        .nset-connection-top {
          display:flex;
          align-items:flex-start;
          justify-content:space-between;
          gap:10px;
        }
        .nset-connection h4 { margin:0 0 4px; font-size:15px; color:var(--nset-text); }
        .nset-connection p { margin:0; color:var(--nset-muted); font-size:12px; line-height:1.45; }
        .nset-status {
          display:inline-flex;
          align-items:center;
          gap:6px;
          border-radius:999px;
          padding:5px 9px;
          font-size:11px;
          font-weight:800;
          white-space:nowrap;
          background:#f2f4f7;
          color:#667085;
        }
        .nset-status i { width:7px; height:7px; border-radius:50%; background:currentColor; }
        .nset-status.ok { background:#ecfdf5; color:#087a56; }
        .nset-status.warn { background:#fff7e8; color:#9a6700; }
        .nset-meta {
          margin-top:12px;
          padding-top:12px;
          border-top:1px solid #edf1f5;
          font-size:12px;
          color:#667085;
          overflow-wrap:anywhere;
        }
        .nset-toggle-list {
          display:flex;
          flex-direction:column;
          border:1px solid var(--nset-border);
          border-radius:14px;
          overflow:hidden;
          background:#fff;
        }
        .nset-toggle-row {
          display:flex;
          align-items:center;
          justify-content:space-between;
          gap:16px;
          padding:15px 16px;
          border-bottom:1px solid #edf1f5;
        }
        .nset-toggle-row:last-child { border-bottom:none; }
        .nset-toggle-row strong { display:block; font-size:14px; color:var(--nset-text); }
        .nset-toggle-row span { display:block; margin-top:3px; font-size:12px; color:var(--nset-muted); }
        .nset-switch {
          width:48px;
          height:27px;
          border:none;
          border-radius:999px;
          background:#cbd5e1;
          padding:3px;
          cursor:pointer;
          transition:.2s;
          flex:none;
        }
        .nset-switch span {
          display:block;
          width:21px;
          height:21px;
          border-radius:50%;
          background:#fff;
          box-shadow:0 1px 4px rgba(15,23,42,.25);
          transform:translateX(0);
          transition:.2s;
          margin:0;
        }
        .nset-switch.on { background:#2563eb; }
        .nset-switch.on span { transform:translateX(21px); }
        .nset-kpi {
          display:grid;
          grid-template-columns:repeat(4,minmax(0,1fr));
          gap:12px;
          margin-top:14px;
        }
        .nset-kpi > div {
          border:1px solid var(--nset-border);
          border-radius:12px;
          background:#fff;
          padding:14px;
        }
        .nset-kpi span { display:block; font-size:12px; color:var(--nset-muted); }
        .nset-kpi strong { display:block; margin:4px 0; font-size:18px; color:var(--nset-text); }
        .nset-actions { display:flex; gap:10px; flex-wrap:wrap; margin-top:15px; }
        .nset-weight-grid {
          display:grid;
          grid-template-columns:repeat(3,minmax(0,1fr));
          gap:12px;
        }
        .nset-footer-save {
          position:sticky;
          bottom:12px;
          margin-top:18px;
          display:flex;
          justify-content:flex-end;
          pointer-events:none;
        }
        .nset-footer-save > * { pointer-events:auto; box-shadow:0 8px 24px rgba(37,99,235,.18); }
        @media (max-width: 1000px) {
          .nset-connections { grid-template-columns:repeat(2,minmax(0,1fr)); }
          .nset-kpi { grid-template-columns:repeat(2,minmax(0,1fr)); }
          .nset-weight-grid { grid-template-columns:repeat(2,minmax(0,1fr)); }
        }
        @media (max-width: 720px) {
          .nset-head { align-items:flex-start; flex-direction:column; }
          .nset-grid,.nset-form,.nset-connections,.nset-weight-grid { grid-template-columns:1fr; }
          .nset-card.full,.nset-field.full { grid-column:auto; }
          .nset-content { padding:14px; }
          .nset-kpi { grid-template-columns:1fr 1fr; }
        }
      `}</style>

      <div className="nset-head">
        <div>
          <p className="nset-eyebrow">Configuration</p>
          <h2>Settings</h2>
          <p>Manage accounts, integrations, automation and system preferences.</p>
        </div>
        <Button
          className="nset-save"
          size="sm"
          disabled={busy === "settings"}
          onClick={onSave}
        >
          <Save />
          {busy === "settings" ? "Saving…" : "Save changes"}
        </Button>
      </div>

      <div className="nset-tabs" role="tablist">
        {tabs.map((tab) => {
          const Icon = tab.icon;
          return (
            <button
              key={tab.key}
              type="button"
              className={`nset-tab ${activeTab === tab.key ? "active" : ""}`}
              onClick={() => setActiveTab(tab.key)}
            >
              <Icon />
              {tab.label}
            </button>
          );
        })}
      </div>

      <div className="nset-content">
        {viewError ? (
          <div className="nset-warning">
            <Activity />
            <div>
              <strong>Connection temporarily interrupted.</strong>{" "}
              {viewError} The app will continue retrying automatically.
            </div>
          </div>
        ) : null}

        {activeTab === "general" && (
          <div className="nset-grid">
            <section className="nset-card full">
              <div className="nset-card-head">
                <div className="nset-card-title">
                  <div className="nset-icon"><Settings2 /></div>
                  <div>
                    <h3>Company & Account</h3>
                    <p>Core identity and the Google account used by the recruitment workspace.</p>
                  </div>
                </div>
              </div>

              <div className="nset-form">
                <label className="nset-field">
                  <span>Company name</span>
                  <input
                    value={settings?.company_name || ""}
                    onChange={(e) => onUpdateSetting("company_name", e.target.value)}
                  />
                </label>

                <label className="nset-field">
                  <span>Candidate sender Gmail</span>
                  <input
                    type="email"
                    value={settings?.company_email || ""}
                    readOnly
                    title="Fixed sender account"
                  />
                  <small>This sender is fixed by the current recruitment mail configuration.</small>
                </label>

                <label className="nset-field">
                  <span>Indeed / Google login account</span>
                  <input
                    type="email"
                    value={settings?.indeed_google_account_email || ""}
                    onChange={(e) =>
                      onUpdateSetting("indeed_google_account_email", e.target.value)
                    }
                    placeholder="nuneslead@gmail.com"
                  />
                  <small>Only the account email is saved. Google password is not stored here.</small>
                </label>

                <label className="nset-field">
                  <span>Candidate Gmail App Password</span>
                  <input
                    type="password"
                    value={password}
                    placeholder={
                      settings?.smtp_app_password_set
                        ? "Configured • leave blank to keep"
                        : "Enter Gmail App Password"
                    }
                    onChange={(e) => onPasswordChange(e.target.value)}
                  />
                  <small className={candidateGmailOk ? "good" : ""}>
                    {candidateGmailOk
                      ? `Gmail verified${data?.email_transport ? ` • ${data.email_transport}` : ""}`
                      : data?.email_configured
                        ? mailErrorSummary(data?.email_last_error) || "Saved but not verified"
                        : "Use the 16-character Google App Password."}
                  </small>
                </label>
              </div>
            </section>
          </div>
        )}

        {activeTab === "connections" && (
          <div className="nset-grid">
            <section className="nset-card full">
              <div className="nset-card-head">
                <div className="nset-card-title">
                  <div className="nset-icon green"><Wifi /></div>
                  <div>
                    <h3>Connected Services</h3>
                    <p>Live state from the existing backend. No connection status is fabricated.</p>
                  </div>
                </div>
              </div>

              <div className="nset-connections">
                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div>
                      <h4>Candidate Gmail</h4>
                      <p>{settings?.company_email || "nuneslead@gmail.com"}</p>
                    </div>
                    <StatusBadge ok={candidateGmailOk} good="Verified" bad="Check Gmail" />
                  </div>
                  <div className="nset-meta">
                    {settings?.persistence?.candidate_gmail_saved ? "Credentials saved on this PC" : "Setup required"}
                  </div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div>
                      <h4>HR Gmail</h4>
                      <p>{settings?.hr_report_sender_email || "nunescbe@gmail.com"}</p>
                    </div>
                    <StatusBadge ok={hrGmailOk} good="Verified" bad="Check Gmail" />
                  </div>
                  <div className="nset-meta">
                    {settings?.persistence?.hr_gmail_saved ? "Credentials saved on this PC" : "Setup required"}
                  </div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div>
                      <h4>Indeed</h4>
                      <p>{settings?.indeed_google_account_email || "Google employer account"}</p>
                    </div>
                    <StatusBadge ok={indeedOk || chromeOk} good="Live" bad="Reconnect" />
                  </div>
                  <div className="nset-meta">
                    {chromeOk ? "Chrome session available" : "Chrome session not confirmed"}
                  </div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div>
                      <h4>GitHub</h4>
                      <p>Application update source</p>
                    </div>
                    <StatusBadge
                      ok={Boolean(settings?.persistence?.github_repository)}
                      good="Saved"
                      bad="Not confirmed"
                    />
                  </div>
                  <div className="nset-meta">{githubRepo}</div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div>
                      <h4>AI / Ranking</h4>
                      <p>{settings?.ranking_embedding_model || "Embedding model"}</p>
                    </div>
                    <StatusBadge
                      ok={!settings?.ranking_embedding_status?.error}
                      good="Ready"
                      bad="Fallback"
                    />
                  </div>
                  <div className="nset-meta">
                    {settings?.ranking_embedding_status?.backend || "Local ranking backend"}
                  </div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div>
                      <h4>Database</h4>
                      <p>Recruitment data store</p>
                    </div>
                    <StatusBadge ok={databaseOk} good="Healthy" bad="Check database" />
                  </div>
                  <div className="nset-meta">
                    {data?.backend_health?.database?.applications ?? 0} applications stored
                  </div>
                </div>
              </div>
            </section>
          </div>
        )}

        {activeTab === "automation" && (
          <div className="nset-grid">
            <section className="nset-card">
              <div className="nset-card-head">
                <div className="nset-card-title">
                  <div className="nset-icon"><SlidersHorizontal /></div>
                  <div>
                    <h3>Recruitment Automation</h3>
                    <p>Control the existing monitoring and acknowledgement workflow.</p>
                  </div>
                </div>
              </div>

              <div className="nset-toggle-list">
                <ToggleRow
                  title="24/7 automation"
                  description="Master switch for the recruitment automation service."
                  checked={Boolean(settings?.automation_enabled)}
                  onChange={(next) => onUpdateSetting("automation_enabled", next)}
                />
                <ToggleRow
                  title="Indeed monitoring"
                  description="Continuously monitor the configured Indeed candidate source."
                  checked={Boolean(settings?.monitoring_enabled)}
                  onChange={(next) => onUpdateSetting("monitoring_enabled", next)}
                />
                <ToggleRow
                  title="Automatic scan"
                  description="Scan for new applicants without manual action."
                  checked={Boolean(settings?.auto_scan)}
                  onChange={(next) => onUpdateSetting("auto_scan", next)}
                />
                <ToggleRow
                  title="Automatic acknowledgement email"
                  description="Send the existing acknowledgement after verification."
                  checked={Boolean(settings?.auto_send)}
                  onChange={(next) => onUpdateSetting("auto_send", next)}
                />
                <ToggleRow
                  title="Candidate-page email fallback"
                  description="Allow the existing fallback when resume email is unavailable."
                  checked={Boolean(settings?.allow_candidate_page_email_fallback)}
                  onChange={(next) =>
                    onUpdateSetting("allow_candidate_page_email_fallback", next)
                  }
                />
              </div>
            </section>

            <section className="nset-card">
              <div className="nset-card-head">
                <div className="nset-card-title">
                  <div className="nset-icon amber"><Bot /></div>
                  <div>
                    <h3>Ranking Settings</h3>
                    <p>Keep the current ranking engine and adjust only its existing thresholds.</p>
                  </div>
                </div>
                <StatusBadge
                  ok={!settings?.ranking_embedding_status?.error}
                  good="Ready"
                  bad="Fallback active"
                />
              </div>

              <div className="nset-form">
                <label className="nset-field full">
                  <span>Embedding model</span>
                  <input value={settings?.ranking_embedding_model || ""} readOnly />
                </label>
                <label className="nset-field">
                  <span>Strong match from</span>
                  <input
                    type="number"
                    min="0"
                    max="100"
                    value={settings?.ranking_strong_threshold ?? 85}
                    onChange={(e) =>
                      onUpdateSetting("ranking_strong_threshold", Number(e.target.value || 0))
                    }
                  />
                </label>
                <label className="nset-field">
                  <span>Good match from</span>
                  <input
                    type="number"
                    min="0"
                    max="100"
                    value={settings?.ranking_good_threshold ?? 70}
                    onChange={(e) =>
                      onUpdateSetting("ranking_good_threshold", Number(e.target.value || 0))
                    }
                  />
                </label>
                <label className="nset-field">
                  <span>Moderate match from</span>
                  <input
                    type="number"
                    min="0"
                    max="100"
                    value={settings?.ranking_moderate_threshold ?? 50}
                    onChange={(e) =>
                      onUpdateSetting("ranking_moderate_threshold", Number(e.target.value || 0))
                    }
                  />
                </label>
              </div>

              <div className="nset-actions">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={busy === "rag-reanalyze"}
                  onClick={() =>
                    onRunAction(
                      "rag-reanalyze",
                      "/api/ranking/reanalyze",
                      "Ranking reanalysis started.",
                    )
                  }
                >
                  <RefreshCw />
                  {busy === "rag-reanalyze" ? "Starting…" : "Reanalyze applicants"}
                </Button>
              </div>
            </section>

            <section className="nset-card full">
              <div className="nset-card-head">
                <div className="nset-card-title">
                  <div className="nset-icon gray"><ShieldCheck /></div>
                  <div>
                    <h3>Ranking Weights</h3>
                    <p>Existing scoring weights. Keep the total aligned with your intended scoring model.</p>
                  </div>
                </div>
              </div>

              <div className="nset-weight-grid">
                {[
                  ["required_skills", "Required skills", 35],
                  ["relevant_experience", "Relevant experience", 25],
                  ["responsibilities", "Responsibilities", 20],
                  ["domain_experience", "Domain / industry", 10],
                  ["education_certifications", "Education / certifications", 5],
                  ["preferred_skills", "Preferred skills", 5],
                ].map(([key, label, fallback]) => (
                  <label className="nset-field" key={String(key)}>
                    <span>{label} weight (%)</span>
                    <input
                      type="number"
                      min="0"
                      max="100"
                      value={rankingWeights?.[key as string] ?? fallback}
                      onChange={(e) =>
                        onUpdateSetting("ranking_weights", {
                          ...rankingWeights,
                          [key as string]: Number(e.target.value || 0),
                        })
                      }
                    />
                  </label>
                ))}
              </div>
            </section>
          </div>
        )}

        {activeTab === "reports" && (
          <div className="nset-grid">
            <section className="nset-card full">
              <div className="nset-card-head">
                <div className="nset-card-title">
                  <div className="nset-icon green"><Mail /></div>
                  <div>
                    <h3>Email & Daily Recruitment Report</h3>
                    <p>Candidate mail and the consolidated HR report continue using the existing backend endpoints.</p>
                  </div>
                </div>
                <StatusBadge ok={hrGmailOk} good="HR Gmail verified" bad="HR Gmail needs attention" />
              </div>

              <div className="nset-form">
                <label className="nset-field">
                  <span>HR report sender</span>
                  <input
                    type="email"
                    value={settings?.hr_report_sender_email || "nunescbe@gmail.com"}
                    readOnly
                  />
                </label>

                <label className="nset-field">
                  <span>Daily report recipient</span>
                  <input
                    type="email"
                    value={settings?.hr_report_recipient || ""}
                    onChange={(e) => onUpdateSetting("hr_report_recipient", e.target.value)}
                  />
                </label>

                <label className="nset-field">
                  <span>HR Gmail App Password</span>
                  <input
                    type="password"
                    value={hrReportPassword}
                    placeholder={
                      settings?.hr_report_smtp_app_password_set
                        ? "Configured • leave blank to keep"
                        : "Enter HR Gmail App Password"
                    }
                    onChange={(e) => onHrReportPasswordChange(e.target.value)}
                  />
                  <small className={hrGmailOk ? "good" : ""}>
                    {hrGmailOk
                      ? "HR ranking Gmail verified"
                      : data?.recruitment_pipeline?.report_gmail_error ||
                        "Required for the daily consolidated report."}
                  </small>
                </label>

                <label className="nset-field">
                  <span>End-of-day report time</span>
                  <input
                    type="time"
                    value={settings?.daily_report_time || "19:00"}
                    onChange={(e) => onUpdateSetting("daily_report_time", e.target.value)}
                  />
                  <small>Uses this Windows PC local time.</small>
                </label>

                <label className="nset-field full">
                  <span>Automatic end-of-day report</span>
                  <select
                    value={settings?.daily_consolidated_report_enabled ? "on" : "off"}
                    onChange={(e) =>
                      onUpdateSetting(
                        "daily_consolidated_report_enabled",
                        e.target.value === "on",
                      )
                    }
                  >
                    <option value="on">ON – send one consolidated recruitment report every day</option>
                    <option value="off">OFF</option>
                  </select>
                </label>
              </div>

              <div className="nset-actions">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={
                    busy === "hr-mail-test" ||
                    !settings?.hr_report_smtp_app_password_set
                  }
                  onClick={() =>
                    onRunAction(
                      "hr-mail-test",
                      "/api/recruitment/hr-mail/test",
                      "HR ranking Gmail verified.",
                    )
                  }
                >
                  <MailCheck />
                  {busy === "hr-mail-test" ? "Testing…" : "Test HR Gmail"}
                </Button>

                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  disabled={
                    busy === "daily-report-now" ||
                    !settings?.hr_report_smtp_app_password_set
                  }
                  onClick={() =>
                    onRunAction(
                      "daily-report-now",
                      "/api/recruitment/daily-report/send-now",
                      "Today's consolidated recruitment report was queued.",
                    )
                  }
                >
                  <Send />
                  {busy === "daily-report-now" ? "Queuing…" : "Send report now"}
                </Button>
              </div>

              <div className="nset-kpi">
                <div>
                  <span>Open roles</span>
                  <strong>{data?.recruitment_pipeline?.roles?.open || 0}</strong>
                  <span>{data?.recruitment_pipeline?.roles?.paused || 0} paused</span>
                </div>
                <div>
                  <span>Notifications</span>
                  <strong>{data?.recruitment_pipeline?.notifications?.queued || 0}</strong>
                  <span>{data?.recruitment_pipeline?.notifications?.sent || 0} sent</span>
                </div>
                <div>
                  <span>HR approved</span>
                  <strong>{data?.recruitment_pipeline?.hr_approved || 0}</strong>
                  <span>Interview workflow</span>
                </div>
                <div>
                  <span>Last report sent</span>
                  <strong style={{ fontSize: 14 }}>
                    {formatDate(data?.recruitment_pipeline?.daily_report?.last_sent_at)}
                  </strong>
                  <span>{settings?.daily_report_time || "19:00"} schedule</span>
                </div>
              </div>
            </section>
          </div>
        )}

        {activeTab === "system" && (
          <div className="nset-grid">
            <section className="nset-card full">
              <div className="nset-card-head">
                <div className="nset-card-title">
                  <div className="nset-icon gray"><MonitorCog /></div>
                  <div>
                    <h3>System Health</h3>
                    <p>Read-only operational information already supplied by the backend.</p>
                  </div>
                </div>
                <StatusBadge
                  ok={systemHealth === 3}
                  good="Healthy"
                  bad="Review status"
                />
              </div>

              <div className="nset-connections">
                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div><h4>Application</h4><p>Installed recruitment console</p></div>
                    <StatusBadge ok={true} good={data?.version || "Running"} />
                  </div>
                  <div className="nset-meta">Version {data?.version || "—"}</div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div><h4>Database</h4><p>Local recruitment database</p></div>
                    <StatusBadge ok={databaseOk} good="Healthy" bad="Attention" />
                  </div>
                  <div className="nset-meta">
                    {data?.backend_health?.database?.applications ?? 0} applications •{" "}
                    {data?.backend_health?.database?.sent_responses ?? 0} sent responses
                  </div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div><h4>Chrome / Indeed</h4><p>Browser automation session</p></div>
                    <StatusBadge ok={chromeOk} good="Connected" bad="Recovering" />
                  </div>
                  <div className="nset-meta">
                    {data?.live_detection?.last_success_at
                      ? `Last success ${formatDate(data.live_detection.last_success_at)}`
                      : "Waiting for successful detection"}
                  </div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div><h4>Ranking</h4><p>Candidate analysis service</p></div>
                    <StatusBadge
                      ok={!data?.role_ranking_error}
                      good="Running"
                      bad="Attention"
                    />
                  </div>
                  <div className="nset-meta">
                    {data?.role_ranking_status?.ranked_candidates || 0} ranked •{" "}
                    {data?.role_ranking_status?.waiting_candidates || 0} waiting
                  </div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div><h4>Saved Settings</h4><p>Persistent local configuration</p></div>
                    <StatusBadge
                      ok={Boolean(data?.backend_health?.settings_saved_at)}
                      good="Saved"
                      bad="Not confirmed"
                    />
                  </div>
                  <div className="nset-meta">
                    {formatDate(data?.backend_health?.settings_saved_at)}
                  </div>
                </div>

                <div className="nset-connection">
                  <div className="nset-connection-top">
                    <div><h4>Backend warnings</h4><p>Current subsystem warnings</p></div>
                    <StatusBadge
                      ok={backendErrors.length === 0}
                      good="None"
                      bad={`${backendErrors.length} warning(s)`}
                    />
                  </div>
                  <div className="nset-meta">
                    {backendErrors.length
                      ? String(backendErrors[0])
                      : "No backend warnings reported"}
                  </div>
                </div>
              </div>
            </section>
          </div>
        )}

        <div className="nset-footer-save">
          <Button
            size="sm"
            disabled={busy === "settings"}
            onClick={onSave}
          >
            <Save />
            {busy === "settings" ? "Saving…" : "Save changes"}
          </Button>
        </div>
      </div>
    </article>
  );
}