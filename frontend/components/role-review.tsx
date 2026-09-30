"use client";

import {
  BriefcaseBusiness,
  Check,
  CheckCircle2,
  ChevronDown,
  Crown,
  Eye,
  Clock3,
  FileText,
  Mail,
  MoreVertical,
  Phone,
  Search,
  Send,
  ShieldCheck,
  Sparkles,
  Trophy,
  UserRoundCheck,
  Users,
  X,
  XCircle,
  MailCheck,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { Button } from "@/components/ui/button";

type Role = {
  job_title: string;
  job_description?: string | null;
  description_source?: string | null;
  description_status?: string | null;
  indeed_job_url?: string | null;
  lifecycle_status?: string | null;
  applicant_count: number;
  active_applicant_count?: number;
  analyzed_count: number;
  waiting_count: number;
  auto_shortlisted_count: number;
  removed_count?: number;
};

type Notification = {
  message_type: string;
  channel: string;
  status: string;
  error?: string | null;
  sent_at?: string | null;
};

type ReviewApplicant = {
  id: number;
  candidate_name?: string | null;
  candidate_email?: string | null;
  candidate_phone?: string | null;
  job_title?: string | null;
  resume_path?: string | null;
  resume_text_cache?: string | null;
  profile_url?: string | null;
  send_status?: string | null;
  extraction_status?: string | null;
  first_seen_at?: string | null;
  analysis_status?: string | null;
  match_score?: number | null;
  confidence_score?: number | null;
  rank_position?: number | null;
  match_classification?: string | null;
  hr_flow?: {
    hr_status?: string | null;
    hr_approved_at?: string | null;
    interview_date?: string | null;
  };
  notifications?: Notification[];
  score_breakdown?: {
    top_matches?: string[];
    top_missing?: string[];
    knockout_missing?: string[];
  };
  summary?: {
    ai_reason?: string;
    confidence_score?: number;
    candidate_profile?: {
      total_experience_years_stated?: number | null;
      skills?: string[];
      education?: string[];
      job_titles?: string[];
    };
  };
  evidence?: Array<{
    requirement: string;
    status?: string;
    must_have?: boolean;
    evidence?: string;
  }>;
};

type RolePayload = {
  role: Role & {
    lifecycle?: {
      lifecycle_status?: string;
    };
  };
  applicants: ReviewApplicant[];
  active_applicants?: ReviewApplicant[];
  ranking: {
    ranked: number;
    waiting: number;
    auto_shortlisted: number;
    removed?: number;
  };
};

const api = async (path: string, options?: RequestInit) => {
  const response = await fetch(`/backend${path}`, {
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
    ...options,
  });

  const raw = await response.text();
  let data: any = {};

  if (raw) {
    try {
      data = JSON.parse(raw);
    } catch {
      throw new Error(`Backend returned HTTP ${response.status}`);
    }
  }

  if (!response.ok) {
    throw new Error(
      data?.message || data?.detail || `Request failed (${response.status})`
    );
  }

  return data;
};

const roleStatus = (role?: Role | null) =>
  String(role?.lifecycle_status || "UNKNOWN").toUpperCase();

const initials = (name?: string | null) => {
  const parts = String(name || "Candidate")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  return `${parts[0]?.[0] || "C"}${parts[1]?.[0] || ""}`.toUpperCase();
};

const interviewNotification = (row: ReviewApplicant) =>
  (row.notifications || []).find(
    (item) => item.message_type === "INTERVIEW_EMAIL"
  );

export function RoleReview() {
  const [roles, setRoles] = useState<Role[]>([]);
  const [selectedRole, setSelectedRole] = useState("");
  const [payload, setPayload] = useState<RolePayload | null>(null);

  const [roleSearch, setRoleSearch] = useState("");
  const [candidateSearch, setCandidateSearch] = useState("");
  const [sortMode, setSortMode] = useState<"score" | "name">("score");

  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  const [customCount, setCustomCount] = useState(20);
  const [presetMode, setPresetMode] = useState<"10" | "20" | "30" | "all" | "custom" | null>("20");

  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [actionLoadingId, setActionLoadingId] = useState<number | null>(null);
  const [actionType, setActionType] = useState<"mail" | "interview" | null>(null);

  const [detailCandidate, setDetailCandidate] =
    useState<ReviewApplicant | null>(null);
  const [resumeCandidate, setResumeCandidate] =
    useState<ReviewApplicant | null>(null);

  const loadRoles = useCallback(async () => {
    const result = await api("/api/role-review/roles");
    const rows = (result.roles || []) as Role[];
    setRoles(rows);

    setSelectedRole((current) => {
      if (current && rows.some((r) => r.job_title === current)) return current;

      const firstOpen =
        rows.find((r) => roleStatus(r) === "OPEN") ||
        rows.find((r) => roleStatus(r) === "UNKNOWN") ||
        rows[0];

      return firstOpen?.job_title || "";
    });
  }, []);

  const loadRole = useCallback(async (jobTitle: string) => {
    if (!jobTitle) {
      setPayload(null);
      return;
    }

    const result = await api(
      `/api/role-review/role?job_title=${encodeURIComponent(jobTitle)}`
    );

    setPayload(result as RolePayload);
    setMessage("");
  }, []);

  useEffect(() => {
    loadRoles().catch((error) =>
      setMessage(error instanceof Error ? error.message : String(error))
    );

    const timer = window.setInterval(
      () => loadRoles().catch(() => undefined),
      5000
    );

    return () => window.clearInterval(timer);
  }, [loadRoles]);

  useEffect(() => {
    if (!selectedRole) return;

    loadRole(selectedRole).catch((error) =>
      setMessage(error instanceof Error ? error.message : String(error))
    );

    const timer = window.setInterval(
      () => loadRole(selectedRole).catch(() => undefined),
      10000
    );

    return () => window.clearInterval(timer);
  }, [selectedRole, loadRole]);

  const visibleRoles = useMemo(() => {
    const q = roleSearch.trim().toLowerCase();

    return roles
      .filter((r) => {
        const s = roleStatus(r);
        return s === "OPEN" || s === "UNKNOWN";
      })
      .filter((r) => !q || r.job_title.toLowerCase().includes(q));
  }, [roles, roleSearch]);

  const candidates = useMemo(() => {
    const base = [
      ...(payload?.active_applicants || payload?.applicants || []),
    ];

    const seen = new Set<number>();
    const unique = base.filter((row) => {
      if (seen.has(row.id)) return false;
      seen.add(row.id);
      return true;
    });

    const q = candidateSearch.trim().toLowerCase();

    const filtered = unique.filter((row) => {
      if (!q) return true;
      return [
        row.candidate_name,
        row.candidate_email,
        row.candidate_phone,
        row.job_title,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase()
        .includes(q);
    });

    filtered.sort((a, b) => {
      if (sortMode === "name") {
        return String(a.candidate_name || "").localeCompare(
          String(b.candidate_name || "")
        );
      }

      const scoreDiff = Number(b.match_score || 0) - Number(a.match_score || 0);
      if (scoreDiff !== 0) return scoreDiff;
      return Number(a.rank_position || 9999) - Number(b.rank_position || 9999);
    });

    return filtered;
  }, [
    payload?.active_applicants,
    payload?.applicants,
    candidateSearch,
    sortMode,
  ]);

  const role = payload?.role;
  const lifecycle = String(
    role?.lifecycle?.lifecycle_status ||
      role?.lifecycle_status ||
      "UNKNOWN"
  ).toUpperCase();

  const approvedCount = (payload?.applicants || []).filter(
    (row) => String(row.hr_flow?.hr_status || "").toUpperCase() === "APPROVED"
  ).length;

  const applyTop = (
    count: number,
    mode: "10" | "20" | "30" | "all" | "custom"
  ) => {
    // Clicking the currently active preset again clears the whole selection.
    if (presetMode === mode) {
      setSelectedIds(new Set());
      setPresetMode(null);
      return;
    }

    const safeCount = Math.max(
      1,
      Math.min(count, Math.max(1, candidates.length))
    );

    const ids = candidates.slice(0, safeCount).map((row) => row.id);
    setSelectedIds(new Set(ids));
    setCustomCount(safeCount);
    setPresetMode(mode);
  };

  useEffect(() => {
    if (!candidates.length) {
      setSelectedIds(new Set());
      setPresetMode(null);
      return;
    }

    // On a newly selected role, default to Top 20 exactly once.
    const ids = candidates.slice(0, Math.min(20, candidates.length)).map((r) => r.id);
    setSelectedIds(new Set(ids));
    setCustomCount(Math.min(20, candidates.length));
    setPresetMode("20");
  }, [selectedRole]);

  // Keep existing selections valid as live candidate data changes.
  useEffect(() => {
    setSelectedIds((current) => {
      const valid = new Set(
        [...current].filter((id) => candidates.some((row) => row.id === id))
      );
      return valid;
    });
  }, [candidates.length]);

  const toggleCandidate = (id: number) => {
    setPresetMode(null);
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAllVisible = () => {
    const allSelected =
      candidates.length > 0 &&
      candidates.every((row) => selectedIds.has(row.id));

    if (allSelected) {
      setSelectedIds(new Set());
      setPresetMode(null);
      return;
    }

    setSelectedIds(new Set(candidates.map((row) => row.id)));
    setPresetMode("all");
    setCustomCount(candidates.length || 1);
  };

  const approveSelected = useCallback(async () => {
    const selected = candidates.filter((row) => selectedIds.has(row.id));
    if (!selected.length) return;

    setBusy(true);
    setMessage("");

    try {
      for (const row of selected) {
        const approved =
          String(row.hr_flow?.hr_status || "").toUpperCase() === "APPROVED";

        if (approved) continue;

        await api(`/api/recruitment/approve/${row.id}`, {
          method: "POST",
          body: JSON.stringify({}),
        });
      }

      setMessage(
        `${selected.length} selected candidate(s) processed for interview approval.`
      );

      if (selectedRole) await loadRole(selectedRole);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }, [candidates, selectedIds, selectedRole, loadRole]);

  const sendInitialMail = useCallback(
    async (candidateId: number) => {
      setActionLoadingId(candidateId);
      setActionType("mail");
      setMessage("");
      try {
        const res = await api(`/api/send/${candidateId}`, { method: "POST" });
        setMessage(res?.message || "Initial thank-you acknowledgement email sent successfully.");
        if (selectedRole) await loadRole(selectedRole);
      } catch (err: any) {
        setMessage(err instanceof Error ? err.message : String(err));
      } finally {
        setActionLoadingId(null);
        setActionType(null);
      }
    },
    [selectedRole, loadRole]
  );

  const approveSingleCandidate = useCallback(
    async (candidateId: number) => {
      setActionLoadingId(candidateId);
      setActionType("interview");
      setMessage("");
      try {
        const res = await api(`/api/recruitment/approve/${candidateId}`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        setMessage(res?.message || "HR approval saved and interview invitation sent.");
        if (selectedRole) await loadRole(selectedRole);
      } catch (err: any) {
        setMessage(err instanceof Error ? err.message : String(err));
      } finally {
        setActionLoadingId(null);
        setActionType(null);
      }
    },
    [selectedRole, loadRole]
  );

  return (
    <section className="rrxPage">
      {message && (
        <div className="rrxNotice">
          <CheckCircle2 />
          <span>{message}</span>
        </div>
      )}

      <div className="rrxGrid">
        <aside className="rrxSidebar">
          <button className="rrxCurrentRole" type="button">
            <BriefcaseBusiness />
            <div>
              <strong>{selectedRole || "Select role"}</strong>
              <span>
                {role?.active_applicant_count ?? role?.applicant_count ?? 0} Applicants
              </span>
            </div>
            <ChevronDown />
          </button>

          <label className="rrxRoleSearch">
            <Search />
            <input
              value={roleSearch}
              onChange={(e) => setRoleSearch(e.target.value)}
              placeholder="Search role..."
            />
          </label>

          <div className="rrxRoleList">
            {visibleRoles.map((item) => {
              const active = item.job_title === selectedRole;
              const count =
                item.active_applicant_count ?? item.applicant_count ?? 0;

              return (
                <button
                  type="button"
                  key={item.job_title}
                  className={`rrxRoleItem ${active ? "active" : ""}`}
                  onClick={() => setSelectedRole(item.job_title)}
                >
                  <BriefcaseBusiness />
                  <div>
                    <strong>{item.job_title}</strong>
                  </div>
                  <span>{count} applicants</span>
                  {active && <CheckCircle2 />}
                </button>
              );
            })}
          </div>
        </aside>

        <main className="rrxMain">
          <section className="rrxRoleCard">
            <div className="rrxRoleHeading">
              <div className="rrxRoleIcon">
                <BriefcaseBusiness />
              </div>

              <div>
                <h2>{selectedRole || "Select a role"}</h2>
                <div className="rrxOpenLine">
                  <span className="rrxOpenBadge">
                    <i />
                    {lifecycle === "OPEN" ? "Open" : lifecycle}
                  </span>
                  <small>
                    {lifecycle === "OPEN"
                      ? "Role is active and receiving applications"
                      : "Role status from Indeed"}
                  </small>
                </div>
              </div>
            </div>

            {role?.indeed_job_url && (
              <a
                className="rrxViewRole"
                href={role.indeed_job_url}
                target="_blank"
                rel="noreferrer"
              >
                View Role Details
              </a>
            )}

            <div className="rrxMetrics">
              <article>
                <Users />
                <div>
                  <strong>{role?.applicant_count || candidates.length || 0}</strong>
                  <span>Total Applicants</span>
                </div>
              </article>

              <article>
                <Trophy />
                <div>
                  <strong>{payload?.ranking?.ranked || 0}</strong>
                  <span>Ranking Complete</span>
                </div>
              </article>

              <article>
                <Clock3 />
                <div>
                  <strong>{payload?.ranking?.waiting || 0}</strong>
                  <span>Waiting</span>
                </div>
              </article>

              <article>
                <UserRoundCheck />
                <div>
                  <strong>{approvedCount}</strong>
                  <span>HR Approved</span>
                </div>
              </article>

              <article>
                <XCircle />
                <div>
                  <strong>{payload?.ranking?.removed || 0}</strong>
                  <span>Removed</span>
                </div>
              </article>
            </div>
          </section>

          <section className="rrxApprovalCard">
            <div className="rrxApprovalTitle">
              <div>
                <Trophy />
              </div>
              <strong>
                Choose how many top-ranked candidates should attend the interview
              </strong>
            </div>

            <div className="rrxApprovalRow">
              <div className="rrxPresets">
                {[10, 20, 30].map((count) => {
                  const mode = String(count) as "10" | "20" | "30";
                  const active = presetMode === mode;

                  return (
                    <button
                      type="button"
                      key={count}
                      className={active ? "active" : ""}
                      onClick={() => applyTop(count, mode)}
                      title={active ? "Click again to clear selection" : `Select Top ${count}`}
                    >
                      {active && <Check />}
                      Top {count}
                    </button>
                  );
                })}

                <button
                  type="button"
                  className={presetMode === "all" ? "active" : ""}
                  onClick={() => applyTop(candidates.length || 1, "all")}
                  title={presetMode === "all" ? "Click again to clear selection" : "Select all candidates"}
                >
                  {presetMode === "all" && <Check />}
                  All
                </button>

                <span className="rrxDivider" />

                <label className="rrxCustom">
                  <span>Custom</span>
                  <input
                    type="number"
                    min={1}
                    max={Math.max(1, candidates.length)}
                    value={customCount}
                    onChange={(e) => {
                      const value = Math.max(
                        1,
                        Math.min(
                          Number(e.target.value || 1),
                          Math.max(1, candidates.length)
                        )
                      );
                      setCustomCount(value);
                      setPresetMode("custom");
                      setSelectedIds(
                        new Set(candidates.slice(0, value).map((row) => row.id))
                      );
                    }}
                  />
                </label>
              </div>

              <div className="rrxSelectedAction">
                <div className="rrxSelectedCount">
                  <Users />
                  <span>
                    <strong>{selectedIds.size}</strong> candidates selected
                    <small>for interview message</small>
                  </span>
                </div>

                <Button
                  type="button"
                  size="lg"
                  disabled={!selectedIds.size || busy || lifecycle === "CLOSED"}
                  onClick={approveSelected}
                >
                  <Send />
                  {busy
                    ? "Processing..."
                    : `Send Interview Message to ${selectedIds.size} Selected`}
                </Button>
              </div>
            </div>
          </section>

          <section className="rrxCandidatesCard">
            <div className="rrxCandidatesHeader">
              <div>
                <h3>Candidates ({candidates.length})</h3>
                <p>
                  Selected rows are included in the approval batch. Click a candidate
                  to see the RAG explanation.
                </p>
              </div>

              <div className="rrxCandidateTools">
                <label className="rrxCandidateSearch">
                  <Search />
                  <input
                    value={candidateSearch}
                    onChange={(e) => setCandidateSearch(e.target.value)}
                    placeholder="Search name, email or phone..."
                  />
                </label>

                <select
                  value={sortMode}
                  onChange={(e) => setSortMode(e.target.value as "score" | "name")}
                >
                  <option value="score">Sort by: Match score</option>
                  <option value="name">Sort by: Name</option>
                </select>

                <button className="rrxMore" type="button" aria-label="More options">
                  <MoreVertical />
                </button>
              </div>
            </div>

            <div className="rrxTableWrap">
              <table className="rrxTable">
                <thead>
                  <tr>
                    <th className="rrxCheckCol">
                      <button
                        type="button"
                        className={`rrxCheck ${
                          candidates.length > 0 &&
                          candidates.every((row) => selectedIds.has(row.id))
                            ? "checked"
                            : ""
                        }`}
                        onClick={toggleAllVisible}
                      >
                        {candidates.length > 0 &&
                          candidates.every((row) => selectedIds.has(row.id)) && (
                            <Check />
                          )}
                      </button>
                    </th>
                    <th className="rrxRankCol">#</th>
                    <th>Candidate</th>
                    <th>AI Match</th>
                    <th>Acknowledgement</th>
                    <th>HR Decision</th>
                    <th>Interview Status</th>
                    <th>Resume Preview</th>
                    <th>Quick Actions</th>
                  </tr>
                </thead>

                <tbody>
                  {candidates.map((row, index) => {
                    const selected = selectedIds.has(row.id);
                    const approved =
                      String(row.hr_flow?.hr_status || "").toUpperCase() ===
                      "APPROVED";
                    const interview = interviewNotification(row);

                    return (
                      <tr key={row.id} className={selected ? "selected" : ""}>
                        <td className="rrxCheckCol">
                          <button
                            type="button"
                            className={`rrxCheck ${selected ? "checked" : ""}`}
                            onClick={() => toggleCandidate(row.id)}
                          >
                            {selected && <Check />}
                          </button>
                        </td>

                        <td className="rrxRankCol">
                          {(() => {
                            const rank = Number(row.rank_position || index + 1);
                            if (rank <= 3) {
                              const tone =
                                rank === 1 ? "gold" : rank === 2 ? "silver" : "bronze";
                              return (
                                <span className={`rrxTopRank ${tone}`} title={`Rank #${rank}`}>
                                  <Crown />
                                  <b>{rank}</b>
                                </span>
                              );
                            }

                            return <strong>{rank}</strong>;
                          })()}
                        </td>

                        <td>
                          <button
                            type="button"
                            className="rrxCandidateIdentity"
                            onClick={() => setDetailCandidate(row)}
                          >
                            <span className={`rrxAvatar a${row.id % 6}`}>
                              {initials(row.candidate_name)}
                            </span>

                            <span>
                              <strong>{row.candidate_name || "Candidate"}</strong>
                              <small>{row.candidate_email || "Email unavailable"}</small>
                              <small>
                                <Phone />
                                {row.candidate_phone || "Phone unavailable"}
                              </small>
                            </span>
                          </button>
                        </td>

                        <td>
                          <button
                            type="button"
                            className="rrxScore"
                            onClick={() => setDetailCandidate(row)}
                          >
                            {row.analysis_status === "READY"
                              ? `${Number(row.match_score || 0).toFixed(0)}%`
                              : "—"}
                          </button>
                        </td>

                        <td>
                          <div className="rrxStatus">
                            <Mail />
                            <span>
                              <strong>
                                {row.send_status === "SENT"
                                  ? "Received"
                                  : row.send_status || "Pending"}
                              </strong>
                              <small>
                                {row.send_status === "SENT"
                                  ? "Acknowledged"
                                  : "Waiting"}
                              </small>
                            </span>
                          </div>
                        </td>

                        <td>
                          <div className="rrxStatus">
                            <ShieldCheck />
                            <span>
                              <strong>
                                {approved
                                  ? "Approved"
                                  : row.hr_flow?.hr_status || "Pending"}
                              </strong>
                              <small>{approved ? "HR approved" : "HR review"}</small>
                            </span>
                          </div>
                        </td>

                        <td>
                          <span
                            className={`rrxInterview ${
                              interview?.status === "SENT"
                                ? "sent"
                                : approved
                                  ? "queued"
                                  : "notSent"
                            }`}
                          >
                            {interview?.status === "SENT"
                              ? "Sent"
                              : approved
                                ? "Queued"
                                : "Not Sent"}
                          </span>
                        </td>

                        <td>
                          <button
                            type="button"
                            className="rrxResume"
                            disabled={!row.resume_path && !row.resume_text_cache}
                            onClick={() => setResumeCandidate(row)}
                            title={
                              row.resume_path
                                ? "Preview downloaded resume"
                                : row.resume_text_cache
                                  ? "Preview resume text captured from Indeed"
                                  : "Resume has not been captured yet"
                            }
                          >
                            <FileText />
                            <span>
                              <strong>
                                {row.resume_path
                                  ? "Resume file"
                                  : row.resume_text_cache
                                    ? "Resume text"
                                    : "Not captured"}
                              </strong>
                              <small>
                                {row.resume_path || row.resume_text_cache
                                  ? "Click to preview"
                                  : "Waiting for Indeed resume"}
                              </small>
                            </span>
                            <Eye />
                          </button>
                        </td>

                        <td>
                          <div className="rrxRowActions">
                            {row.send_status === "SENT" ? (
                              <span className="rrxBadgeSent" title="Initial thank-you acknowledgement email already sent">
                                <MailCheck /> Sent
                              </span>
                            ) : row.candidate_email ? (
                              <button
                                type="button"
                                className="rrxActionBtn mail"
                                disabled={actionLoadingId === row.id || busy}
                                onClick={() => sendInitialMail(row.id)}
                                title="Send initial thank-you email"
                              >
                                <Mail />
                                {actionLoadingId === row.id && actionType === "mail"
                                  ? "Sending..."
                                  : "Send Mail"}
                              </button>
                            ) : (
                              <span className="rrxBadgeMuted">No Email</span>
                            )}

                            {approved ? (
                              <span className="rrxBadgeApproved" title={`Interview on ${row.hr_flow?.interview_date || "next business day"}`}>
                                <CheckCircle2 /> {row.hr_flow?.interview_date || "Approved"}
                              </span>
                            ) : (
                              <button
                                type="button"
                                className="rrxActionBtn approve"
                                disabled={actionLoadingId === row.id || busy || lifecycle === "CLOSED"}
                                onClick={() => approveSingleCandidate(row.id)}
                                title="Approve candidate for interview tomorrow & dispatch invitation"
                              >
                                <Check />
                                {actionLoadingId === row.id && actionType === "interview"
                                  ? "Approving..."
                                  : "Approve Interview"}
                              </button>
                            )}
                          </div>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        </main>
      </div>

      {detailCandidate && (
        <div className="rrxBackdrop">
          <section className="rrxDetailModal">
            <header>
              <div>
                <span className="rrxModalAvatar">
                  {initials(detailCandidate.candidate_name)}
                </span>
                <div>
                  <h2>{detailCandidate.candidate_name || "Candidate"}</h2>
                  <p>{detailCandidate.job_title || selectedRole}</p>
                </div>
              </div>

              <button
                type="button"
                className="rrxClose"
                onClick={() => setDetailCandidate(null)}
              >
                <X />
              </button>
            </header>

            <div className="rrxDetailBody">
              <section className="rrxWhy">
                <div className="rrxWhyTitle">
                  <Sparkles />
                  <div>
                    <strong>Why RAG AI ranked this candidate</strong>
                    <span>
                      Based on the selected role and retrieved resume evidence only.
                    </span>
                  </div>
                </div>

                <div className="rrxWhyMetrics">
                  <div>
                    <span>AI Match</span>
                    <strong>
                      {Number(detailCandidate.match_score || 0).toFixed(1)}%
                    </strong>
                  </div>
                  <div>
                    <span>Confidence</span>
                    <strong>
                      {Number(
                        detailCandidate.confidence_score ??
                          detailCandidate.summary?.confidence_score ??
                          0
                      ).toFixed(0)}
                      %
                    </strong>
                  </div>
                  <div>
                    <span>Rank</span>
                    <strong>#{detailCandidate.rank_position || "—"}</strong>
                  </div>
                </div>

                <p>
                  {detailCandidate.summary?.ai_reason ||
                    "Detailed RAG explanation is not available yet."}
                </p>
              </section>

              <div className="rrxEvidenceGrid">
                <section>
                  <h3>
                    <CheckCircle2 />
                    Strongest evidence
                  </h3>
                  {(detailCandidate.score_breakdown?.top_matches || []).length ? (
                    <ul>
                      {(detailCandidate.score_breakdown?.top_matches || [])
                        .slice(0, 6)
                        .map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                    </ul>
                  ) : (
                    <p>No strong evidence extracted yet.</p>
                  )}
                </section>

                <section>
                  <h3>
                    <XCircle />
                    Evidence not found
                  </h3>
                  {(detailCandidate.score_breakdown?.top_missing || []).length ? (
                    <ul>
                      {(detailCandidate.score_breakdown?.top_missing || [])
                        .slice(0, 6)
                        .map((item) => (
                          <li key={item}>{item}</li>
                        ))}
                    </ul>
                  ) : (
                    <p>No major missing evidence found.</p>
                  )}
                </section>
              </div>

              <section className="rrxFacts">
                <h3>Resume facts used by RAG</h3>
                <div>
                  <article>
                    <strong>Experience</strong>
                    <span>
                      {detailCandidate.summary?.candidate_profile
                        ?.total_experience_years_stated != null
                        ? `${detailCandidate.summary.candidate_profile.total_experience_years_stated} years stated`
                        : "Not clearly stated"}
                    </span>
                  </article>
                  <article>
                    <strong>Skills</strong>
                    <span>
                      {(detailCandidate.summary?.candidate_profile?.skills || [])
                        .slice(0, 10)
                        .join(", ") || "No structured skills extracted"}
                    </span>
                  </article>
                  <article>
                    <strong>Education</strong>
                    <span>
                      {(detailCandidate.summary?.candidate_profile?.education || [])
                        .slice(0, 5)
                        .join(" · ") || "Education not found"}
                    </span>
                  </article>
                </div>
              </section>
            </div>

            <footer>
              {detailCandidate.send_status !== "SENT" && detailCandidate.candidate_email && (
                <Button
                  type="button"
                  variant="outline"
                  disabled={actionLoadingId === detailCandidate.id || busy}
                  onClick={async () => {
                    await sendInitialMail(detailCandidate.id);
                    setDetailCandidate(null);
                  }}
                >
                  <Mail />
                  {actionLoadingId === detailCandidate.id && actionType === "mail"
                    ? "Sending..."
                    : "Send Thank-You Mail"}
                </Button>
              )}

              {String(detailCandidate.hr_flow?.hr_status || "").toUpperCase() !== "APPROVED" && (
                <Button
                  type="button"
                  disabled={actionLoadingId === detailCandidate.id || busy}
                  onClick={async () => {
                    await approveSingleCandidate(detailCandidate.id);
                    setDetailCandidate(null);
                  }}
                >
                  <Check />
                  {actionLoadingId === detailCandidate.id && actionType === "interview"
                    ? "Scheduling..."
                    : "Approve Interview (Tomorrow)"}
                </Button>
              )}

              <Button
                type="button"
                variant="outline"
                disabled={!detailCandidate.resume_path && !detailCandidate.resume_text_cache}
                onClick={() => {
                  setResumeCandidate(detailCandidate);
                  setDetailCandidate(null);
                }}
              >
                <Eye />
                Preview Resume
              </Button>
            </footer>
          </section>
        </div>
      )}

      {resumeCandidate && (
        <div className="rrxResumeModal">
          <header>
            <div>
              <FileText />
              <div>
                <strong>
                  {resumeCandidate.candidate_name || "Candidate"} — Resume
                </strong>
                <span>{resumeCandidate.job_title || selectedRole}</span>
              </div>
            </div>

            <div>
              {resumeCandidate.resume_path && (
                <a
                  href={`/backend/resume/${resumeCandidate.id}`}
                  target="_blank"
                  rel="noreferrer"
                >
                  Open in new tab
                </a>
              )}

              <button
                type="button"
                className="rrxClose"
                onClick={() => setResumeCandidate(null)}
              >
                <X />
              </button>
            </div>
          </header>

          {resumeCandidate.resume_path ? (
            <iframe
              title={`${resumeCandidate.candidate_name || "Candidate"} resume`}
              src={`/backend/resume/${resumeCandidate.id}`}
            />
          ) : resumeCandidate.resume_text_cache ? (
            <div className="rrxResumeText">
              <div className="rrxResumeTextLabel">
                Resume text captured from Indeed
              </div>
              <pre>{resumeCandidate.resume_text_cache}</pre>
            </div>
          ) : (
            <div className="rrxResumeMissing">
              <FileText />
              <strong>Resume has not been captured yet</strong>
              <span>
                This candidate record currently has no downloaded resume file or cached resume text.
              </span>
            </div>
          )}
        </div>
      )}

      <style jsx>{`
        .rrxPage {
          width: 100%;
          min-height: calc(100vh - 72px);
          padding: 16px 20px 22px;
          background: #f7f9fc;
          color: #0f1f38;
        }

        .rrxNotice {
          display: flex;
          align-items: center;
          gap: 8px;
          margin-bottom: 10px;
          padding: 9px 12px;
          border: 1px solid #cbe9da;
          border-radius: 9px;
          background: #f2fbf6;
          color: #176e4e;
          font-size: 12px;
          font-weight: 700;
        }

        .rrxNotice svg {
          width: 16px;
          height: 16px;
        }

        .rrxGrid {
          display: grid;
          grid-template-columns: 420px minmax(0, 1fr);
          gap: 14px;
          align-items: start;
        }

        .rrxSidebar,
        .rrxRoleCard,
        .rrxApprovalCard,
        .rrxCandidatesCard {
          border: 1px solid #dce4ef;
          border-radius: 14px;
          background: #fff;
          box-shadow: 0 4px 18px rgba(38, 63, 94, 0.035);
        }

        .rrxSidebar {
          position: sticky;
          top: 80px;
          overflow: hidden;
          padding: 10px;
        }

        .rrxCurrentRole {
          width: 100%;
          min-height: 42px;
          display: grid;
          grid-template-columns: 20px minmax(0, 1fr) 16px;
          align-items: center;
          gap: 8px;
          padding: 8px 10px;
          border: 1.5px solid #5d8cff;
          border-radius: 9px;
          background: #fff;
          color: #1458d8;
          cursor: pointer;
          text-align: left;
        }

        .rrxCurrentRole svg {
          width: 16px;
          height: 16px;
        }

        .rrxCurrentRole strong,
        .rrxCurrentRole span {
          display: block;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .rrxCurrentRole strong {
          font-size: 12px;
        }

        .rrxCurrentRole span {
          margin-top: 2px;
          color: #6f7f97;
          font-size: 9px;
        }

        .rrxRoleSearch {
          height: 42px;
          display: flex;
          align-items: center;
          gap: 7px;
          margin-top: 9px;
          padding: 0 10px;
          border: 1.5px solid #cbd8e9;
          border-radius: 9px;
          background: #ffffff;
          box-shadow: 0 1px 2px rgba(30, 58, 95, 0.04);
        }

        .rrxRoleSearch:focus-within {
          border-color: #5f8fff;
          box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.08);
        }

        .rrxRoleSearch svg {
          width: 14px;
          height: 14px;
          color: #8291a8;
        }

        .rrxRoleSearch input {
          width: 100%;
          border: 0;
          outline: 0;
          background: transparent;
          font: inherit;
          font-size: 12px;
          color: #1f2f49;
        }

        .rrxRoleSearch input::placeholder {
          color: #7d8ca3;
          opacity: 1;
        }

        .rrxRoleList {
          max-height: calc(100vh - 210px);
          overflow-y: auto;
          margin-top: 6px;
        }

        .rrxRoleItem {
          width: 100%;
          min-height: 45px;
          display: grid;
          grid-template-columns: 20px minmax(0, 1fr) auto 17px;
          align-items: center;
          gap: 7px;
          padding: 0 9px;
          border: 0;
          border-bottom: 1px solid #eef2f6;
          background: transparent;
          cursor: pointer;
          color: #243550;
          text-align: left;
        }

        .rrxRoleItem:hover {
          background: #f8fbff;
        }

        .rrxRoleItem.active {
          margin: 2px 0;
          border: 1px solid #d9e6ff;
          border-radius: 8px;
          background: #eef5ff;
        }

        .rrxRoleItem svg {
          width: 14px;
          height: 14px;
          color: #446388;
        }

        .rrxRoleItem strong {
          display: block;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
          font-size: 11px;
        }

        .rrxRoleItem span {
          white-space: nowrap;
          color: #718198;
          font-size: 9px;
        }

        .rrxMain {
          min-width: 0;
          display: grid;
          gap: 12px;
        }

        .rrxRoleCard {
          position: relative;
          padding: 13px;
        }

        .rrxRoleHeading {
          display: flex;
          align-items: center;
          gap: 11px;
          padding-right: 150px;
        }

        .rrxRoleIcon {
          width: 43px;
          height: 43px;
          display: grid;
          place-items: center;
          border-radius: 10px;
          background: #eef4ff;
          color: #2465ea;
        }

        .rrxRoleIcon svg {
          width: 23px;
          height: 23px;
        }

        .rrxRoleHeading h2 {
          margin: 0;
          font-size: 18px;
          letter-spacing: -0.2px;
        }

        .rrxOpenLine {
          display: flex;
          align-items: center;
          gap: 9px;
          margin-top: 5px;
        }

        .rrxOpenBadge {
          display: inline-flex;
          align-items: center;
          gap: 5px;
          padding: 4px 9px;
          border-radius: 999px;
          background: #e5f8ed;
          color: #11754d;
          font-size: 10px;
          font-weight: 800;
          text-transform: capitalize;
        }

        .rrxOpenBadge i {
          width: 6px;
          height: 6px;
          border-radius: 999px;
          background: currentColor;
        }

        .rrxOpenLine small {
          color: #74849c;
          font-size: 9.5px;
        }

        .rrxViewRole {
          position: absolute;
          top: 13px;
          right: 13px;
          height: 36px;
          display: inline-flex;
          align-items: center;
          padding: 0 12px;
          border: 1px solid #dbe4ef;
          border-radius: 8px;
          color: #145cd8;
          text-decoration: none;
          font-size: 10px;
          font-weight: 750;
        }

        .rrxMetrics {
          display: grid;
          grid-template-columns: repeat(5, minmax(0, 1fr));
          gap: 8px;
          margin-top: 12px;
        }

        .rrxMetrics article {
          min-height: 56px;
          display: grid;
          grid-template-columns: 24px minmax(0, 1fr);
          align-items: center;
          gap: 8px;
          padding: 8px 10px;
          border: 1px solid #e4e9f1;
          border-radius: 9px;
          background: #fbfcfe;
        }

        .rrxMetrics svg {
          width: 18px;
          height: 18px;
          color: #2465ea;
        }

        .rrxMetrics strong,
        .rrxMetrics span {
          display: block;
        }

        .rrxMetrics strong {
          font-size: 17px;
          line-height: 1;
        }

        .rrxMetrics span {
          margin-top: 3px;
          color: #687991;
          font-size: 9px;
        }

        .rrxApprovalCard {
          padding: 12px;
        }

        .rrxApprovalTitle {
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .rrxApprovalTitle > div {
          width: 29px;
          height: 29px;
          display: grid;
          place-items: center;
          border-radius: 8px;
          background: #e9f8ef;
          color: #16855c;
        }

        .rrxApprovalTitle svg {
          width: 16px;
          height: 16px;
        }

        .rrxApprovalTitle strong {
          font-size: 11.5px;
        }

        .rrxApprovalRow {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          margin-top: 8px;
        }

        .rrxPresets {
          display: flex;
          align-items: center;
          gap: 6px;
          flex-wrap: wrap;
        }

        .rrxPresets > button {
          height: 35px;
          min-width: 66px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 4px;
          padding: 0 11px;
          border: 1px solid #e0e6ef;
          border-radius: 8px;
          background: #f8fafc;
          color: #33435d;
          cursor: pointer;
          font-size: 10.5px;
          font-weight: 750;
        }

        .rrxPresets > button.active {
          color: #fff;
          background: #2466f3;
          border-color: #2466f3;
        }

        .rrxPresets svg {
          width: 12px;
          height: 12px;
        }

        .rrxDivider {
          width: 1px;
          height: 22px;
          margin: 0 4px;
          background: #dfe6ef;
        }

        .rrxCustom {
          display: flex;
          align-items: center;
          gap: 6px;
        }

        .rrxCustom span {
          font-size: 10px;
          font-weight: 750;
        }

        .rrxCustom input {
          width: 62px;
          height: 35px;
          padding: 0 8px;
          border: 1px solid #dfe6ef;
          border-radius: 8px;
          outline: none;
          font: inherit;
          font-size: 10.5px;
        }

        .rrxSelectedAction {
          display: flex;
          align-items: center;
          gap: 9px;
          padding: 5px 6px 5px 10px;
          border-radius: 9px;
          background: #eafaf1;
        }

        .rrxSelectedCount {
          min-width: 155px;
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .rrxSelectedCount > svg {
          width: 18px;
          height: 18px;
          color: #16885f;
        }

        .rrxSelectedCount span {
          display: block;
          color: #34455f;
          font-size: 10px;
        }

        .rrxSelectedCount span strong {
          margin-right: 3px;
          color: #173155;
          font-size: 16px;
        }

        .rrxSelectedCount small {
          display: block;
          margin-top: 1px;
          color: #71829a;
          font-size: 8.5px;
        }

        .rrxCandidatesCard {
          overflow: hidden;
        }

        .rrxCandidatesHeader {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 11px 13px;
          border-bottom: 1px solid #e8edf4;
        }

        .rrxCandidatesHeader h3 {
          margin: 0;
          font-size: 14px;
        }

        .rrxCandidatesHeader p {
          margin: 3px 0 0;
          color: #71819a;
          font-size: 9px;
        }

        .rrxCandidateTools {
          display: flex;
          align-items: center;
          gap: 7px;
        }

        .rrxCandidateSearch {
          width: 360px;
          min-width: 360px;
          height: 40px;
          display: flex;
          align-items: center;
          gap: 7px;
          padding: 0 9px;
          border: 1.5px solid #cbd8e9;
          border-radius: 9px;
          background: #ffffff;
          box-shadow: 0 1px 2px rgba(30, 58, 95, 0.04);
        }

        .rrxCandidateSearch svg {
          width: 14px;
          height: 14px;
          color: #8291a8;
        }

        .rrxCandidateSearch input {
          width: 100%;
          border: 0;
          outline: none;
          background: transparent;
          color: #1f2f49;
          font: inherit;
          font-size: 11.5px;
        }

        .rrxCandidateSearch input::placeholder {
          color: #7b8aa1;
          opacity: 1;
        }

        .rrxCandidateSearch:focus-within {
          border-color: #5f8fff;
          box-shadow: 0 0 0 3px rgba(37, 99, 235, 0.08);
        }

        .rrxCandidateTools select {
          height: 37px;
          padding: 0 10px;
          border: 1px solid #dfe6ef;
          border-radius: 8px;
          background: #fff;
          color: #33435d;
          font-size: 10px;
        }

        .rrxMore {
          width: 36px;
          height: 36px;
          display: grid;
          place-items: center;
          padding: 0;
          border: 1px solid #dfe6ef;
          border-radius: 8px;
          background: #fff;
          color: #44546e;
        }

        .rrxMore svg {
          width: 16px;
          height: 16px;
        }

        .rrxTableWrap {
          height: calc(100vh - 380px);
          min-height: 390px;
          overflow: auto;
        }

        .rrxTable {
          width: 100%;
          min-width: 1080px;
          border-collapse: separate;
          border-spacing: 0;
        }

        .rrxTable thead th {
          position: sticky;
          top: 0;
          z-index: 5;
          height: 36px;
          padding: 0 8px;
          border-bottom: 1px solid #dde5ef;
          background: #f8fafc;
          color: #667790;
          text-align: left;
          font-size: 8.7px;
          font-weight: 800;
        }

        .rrxTable tbody tr {
          background: #fff;
        }

        .rrxTable tbody tr.selected {
          background: #f7fbff;
        }

        .rrxTable tbody tr:hover {
          background: #fbfdff;
        }

        .rrxTable td {
          padding: 6px 8px;
          border-bottom: 1px solid #edf1f5;
          vertical-align: middle;
          font-size: 9.5px;
        }

        .rrxCheckCol {
          width: 34px;
          text-align: center !important;
        }

        .rrxRankCol {
          width: 34px;
          text-align: center !important;
        }

        .rrxCheck {
          width: 21px;
          height: 21px;
          display: grid;
          place-items: center;
          margin: 0 auto;
          padding: 0;
          border: 1.5px solid #aebbd0;
          border-radius: 5px;
          background: #fff;
          color: #fff;
          cursor: pointer;
        }

        .rrxCheck:hover {
          border-color: #5e8df4;
          background: #f4f8ff;
        }

        .rrxCheck.checked {
          border-color: #2466f3;
          background: #2466f3;
          box-shadow: 0 2px 6px rgba(37, 99, 235, 0.20);
        }

        .rrxCheck.checked:hover {
          background: #1d58dc;
        }

        .rrxCheck svg {
          width: 13px;
          height: 13px;
          stroke-width: 3;
        }


        .rrxTopRank {
          width: 30px;
          height: 30px;
          display: inline-flex;
          align-items: center;
          justify-content: center;
          gap: 2px;
          margin: 0 auto;
          border-radius: 9px;
          font-size: 9px;
          font-weight: 900;
        }

        .rrxTopRank svg {
          width: 13px;
          height: 13px;
          fill: currentColor;
        }

        .rrxTopRank.gold {
          color: #9a6500;
          border: 1px solid #f1d270;
          background: linear-gradient(180deg, #fff9d9 0%, #ffed9b 100%);
        }

        .rrxTopRank.silver {
          color: #607083;
          border: 1px solid #d0d7e0;
          background: linear-gradient(180deg, #f9fbfd 0%, #e6ebf0 100%);
        }

        .rrxTopRank.bronze {
          color: #8b4f27;
          border: 1px solid #e5b795;
          background: linear-gradient(180deg, #fff4ea 0%, #f4d1b4 100%);
        }

        .rrxCandidateIdentity {
          width: 100%;
          display: flex;
          align-items: center;
          gap: 8px;
          padding: 0;
          border: 0;
          background: transparent;
          cursor: pointer;
          text-align: left;
        }

        .rrxAvatar {
          width: 33px;
          height: 33px;
          flex: 0 0 33px;
          display: grid;
          place-items: center;
          border-radius: 999px;
          color: #245bc5;
          background: #e8f0ff;
          font-size: 10px;
          font-weight: 850;
        }

        .rrxAvatar.a1 { color: #b63048; background: #ffe8ed; }
        .rrxAvatar.a2 { color: #6c43b8; background: #f0eaff; }
        .rrxAvatar.a3 { color: #19734f; background: #e5f7ee; }
        .rrxAvatar.a4 { color: #b14050; background: #ffebee; }
        .rrxAvatar.a5 { color: #1768a5; background: #e5f3ff; }

        .rrxCandidateIdentity > span:last-child {
          min-width: 0;
        }

        .rrxCandidateIdentity strong,
        .rrxCandidateIdentity small {
          display: block;
          overflow: hidden;
          text-overflow: ellipsis;
          white-space: nowrap;
        }

        .rrxCandidateIdentity strong {
          color: #182945;
          font-size: 10.5px;
        }

        .rrxCandidateIdentity small {
          max-width: 220px;
          margin-top: 2px;
          color: #75859d;
          font-size: 8.3px;
        }

        .rrxCandidateIdentity small:last-child {
          display: flex;
          align-items: center;
          gap: 4px;
        }

        .rrxCandidateIdentity small svg {
          width: 9px;
          height: 9px;
        }

        .rrxScore {
          min-width: 46px;
          padding: 5px 8px;
          border: 0;
          border-radius: 999px;
          background: #def7e9;
          color: #0c724d;
          cursor: pointer;
          font-size: 10px;
          font-weight: 850;
        }

        .rrxStatus {
          display: flex;
          align-items: center;
          gap: 6px;
        }

        .rrxStatus > svg {
          width: 13px;
          height: 13px;
          color: #2563eb;
        }

        .rrxStatus strong,
        .rrxStatus small {
          display: block;
          white-space: nowrap;
        }

        .rrxStatus strong {
          color: #263957;
          font-size: 8.8px;
        }

        .rrxStatus small {
          margin-top: 1px;
          color: #8290a4;
          font-size: 7.7px;
        }

        .rrxInterview {
          display: inline-flex;
          align-items: center;
          justify-content: center;
          min-width: 61px;
          padding: 5px 8px;
          border-radius: 999px;
          font-size: 8.2px;
          font-weight: 800;
        }

        .rrxInterview.notSent {
          color: #9a5d0f;
          background: #fff0d2;
        }

        .rrxInterview.queued {
          color: #275bbf;
          background: #eaf1ff;
        }

        .rrxInterview.sent {
          color: #11714b;
          background: #e5f7ef;
        }

        .rrxRowActions {
          display: flex;
          align-items: center;
          gap: 6px;
          flex-wrap: wrap;
        }

        .rrxActionBtn {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          height: 26px;
          padding: 0 9px;
          border-radius: 6px;
          font-size: 8.8px;
          font-weight: 700;
          cursor: pointer;
          transition: all 0.15s ease;
          border: 1px solid transparent;
          white-space: nowrap;
        }

        .rrxActionBtn svg {
          width: 12px;
          height: 12px;
        }

        .rrxActionBtn.mail {
          background: #eff6ff;
          color: #2563eb;
          border-color: #bfdbfe;
        }

        .rrxActionBtn.mail:hover:not(:disabled) {
          background: #dbeafe;
          border-color: #93c5fd;
        }

        .rrxActionBtn.approve {
          background: #f0fdf4;
          color: #16a34a;
          border-color: #bbf7d0;
        }

        .rrxActionBtn.approve:hover:not(:disabled) {
          background: #dcfce7;
          border-color: #86efac;
        }

        .rrxActionBtn:disabled {
          opacity: 0.55;
          cursor: not-allowed;
        }

        .rrxBadgeSent {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          padding: 3px 7px;
          border-radius: 999px;
          background: #ecfdf5;
          color: #059669;
          border: 1px solid #a7f3d0;
          font-size: 8px;
          font-weight: 750;
          white-space: nowrap;
        }

        .rrxBadgeApproved {
          display: inline-flex;
          align-items: center;
          gap: 4px;
          padding: 3px 7px;
          border-radius: 999px;
          background: #eff6ff;
          color: #1d4ed8;
          border: 1px solid #bfdbfe;
          font-size: 8px;
          font-weight: 750;
          white-space: nowrap;
        }

        .rrxBadgeMuted {
          display: inline-flex;
          align-items: center;
          padding: 3px 6px;
          border-radius: 999px;
          background: #f3f4f6;
          color: #9ca3af;
          font-size: 8px;
          white-space: nowrap;
        }

        .rrxResume {
          min-width: 160px;
          display: grid;
          grid-template-columns: 24px minmax(0, 1fr) 18px;
          align-items: center;
          gap: 6px;
          padding: 4px 6px;
          border: 1px solid #e0e7f0;
          border-radius: 7px;
          background: #fff;
          cursor: pointer;
          text-align: left;
        }

        .rrxResume:disabled {
          opacity: 0.5;
          cursor: default;
        }

        .rrxResume > svg:first-child {
          width: 21px;
          height: 24px;
          padding: 4px;
          border: 1px solid #d8e1ec;
          border-radius: 3px;
          color: #6b7d97;
          background: #f8fafc;
        }

        .rrxResume > svg:last-child {
          width: 14px;
          height: 14px;
          color: #2563eb;
        }

        .rrxResume strong,
        .rrxResume small {
          display: block;
        }

        .rrxResume strong {
          color: #33445f;
          font-size: 8.5px;
        }

        .rrxResume small {
          margin-top: 1px;
          color: #8794a7;
          font-size: 7.5px;
        }

        .rrxBackdrop {
          position: fixed;
          inset: 0;
          z-index: 1000;
          display: grid;
          place-items: center;
          padding: 24px;
          background: rgba(12, 23, 40, 0.48);
          backdrop-filter: blur(4px);
        }

        .rrxDetailModal {
          width: min(900px, 94vw);
          max-height: 90vh;
          display: flex;
          flex-direction: column;
          overflow: hidden;
          border: 1px solid #dce4ef;
          border-radius: 15px;
          background: #fff;
          box-shadow: 0 24px 70px rgba(12, 23, 40, 0.22);
        }

        .rrxDetailModal > header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 14px 16px;
          border-bottom: 1px solid #e7edf4;
        }

        .rrxDetailModal > header > div {
          display: flex;
          align-items: center;
          gap: 9px;
        }

        .rrxModalAvatar {
          width: 40px;
          height: 40px;
          display: grid;
          place-items: center;
          border-radius: 999px;
          color: #2456bd;
          background: #e9f1ff;
          font-size: 11px;
          font-weight: 850;
        }

        .rrxDetailModal h2 {
          margin: 0;
          font-size: 17px;
        }

        .rrxDetailModal header p {
          margin: 3px 0 0;
          color: #728199;
          font-size: 9px;
        }

        .rrxClose {
          width: 33px;
          height: 33px;
          display: grid;
          place-items: center;
          padding: 0;
          border: 1px solid #dce4ee;
          border-radius: 8px;
          background: #fff;
          cursor: pointer;
        }

        .rrxClose svg {
          width: 16px;
          height: 16px;
        }

        .rrxDetailBody {
          overflow-y: auto;
          padding: 15px 16px;
        }

        .rrxWhy {
          padding: 13px;
          border: 1px solid #dce7fb;
          border-radius: 11px;
          background: #f7faff;
        }

        .rrxWhyTitle {
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .rrxWhyTitle > svg {
          width: 19px;
          height: 19px;
          color: #2563eb;
        }

        .rrxWhyTitle strong,
        .rrxWhyTitle span {
          display: block;
        }

        .rrxWhyTitle strong {
          font-size: 12px;
        }

        .rrxWhyTitle span {
          margin-top: 2px;
          color: #71819a;
          font-size: 8.8px;
        }

        .rrxWhyMetrics {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 8px;
          margin-top: 11px;
        }

        .rrxWhyMetrics > div {
          padding: 9px;
          border: 1px solid #e2e8f2;
          border-radius: 8px;
          background: #fff;
        }

        .rrxWhyMetrics span,
        .rrxWhyMetrics strong {
          display: block;
        }

        .rrxWhyMetrics span {
          color: #7a899f;
          font-size: 8.5px;
        }

        .rrxWhyMetrics strong {
          margin-top: 3px;
          font-size: 17px;
        }

        .rrxWhy > p {
          margin: 11px 0 0;
          color: #42536d;
          font-size: 10.5px;
          line-height: 1.5;
        }

        .rrxEvidenceGrid {
          display: grid;
          grid-template-columns: 1fr 1fr;
          gap: 11px;
          margin-top: 11px;
        }

        .rrxEvidenceGrid > section,
        .rrxFacts {
          padding: 12px;
          border: 1px solid #e2e8f0;
          border-radius: 10px;
          background: #fff;
        }

        .rrxEvidenceGrid h3,
        .rrxFacts h3 {
          display: flex;
          align-items: center;
          gap: 6px;
          margin: 0 0 8px;
          font-size: 11px;
        }

        .rrxEvidenceGrid h3 svg {
          width: 14px;
          height: 14px;
        }

        .rrxEvidenceGrid ul {
          margin: 0;
          padding-left: 18px;
          color: #40546e;
          font-size: 9.5px;
          line-height: 1.5;
        }

        .rrxEvidenceGrid p {
          margin: 0;
          color: #78869a;
          font-size: 9.5px;
        }

        .rrxFacts {
          margin-top: 11px;
        }

        .rrxFacts > div {
          display: grid;
          grid-template-columns: repeat(3, minmax(0, 1fr));
          gap: 8px;
        }

        .rrxFacts article {
          padding: 9px;
          border-radius: 8px;
          background: #f8fafc;
        }

        .rrxFacts strong,
        .rrxFacts span {
          display: block;
        }

        .rrxFacts strong {
          color: #263957;
          font-size: 9.5px;
        }

        .rrxFacts span {
          margin-top: 4px;
          color: #64758d;
          font-size: 9px;
          line-height: 1.4;
        }

        .rrxDetailModal > footer {
          display: flex;
          justify-content: flex-end;
          padding: 11px 16px;
          border-top: 1px solid #e7edf4;
          background: #fbfcfe;
        }

        .rrxResumeModal {
          position: fixed;
          inset: 14px;
          z-index: 1100;
          display: flex;
          flex-direction: column;
          overflow: hidden;
          border: 1px solid #dbe3ed;
          border-radius: 13px;
          background: #fff;
          box-shadow: 0 24px 80px rgba(9, 20, 37, 0.32);
        }

        .rrxResumeModal > header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 10px 13px;
          border-bottom: 1px solid #e5ebf2;
        }

        .rrxResumeModal > header > div {
          display: flex;
          align-items: center;
          gap: 8px;
        }

        .rrxResumeModal header strong,
        .rrxResumeModal header span {
          display: block;
        }

        .rrxResumeModal header strong {
          font-size: 11px;
        }

        .rrxResumeModal header span {
          margin-top: 2px;
          color: #79879d;
          font-size: 8.5px;
        }

        .rrxResumeModal header a {
          color: #2563eb;
          text-decoration: none;
          font-size: 9.5px;
          font-weight: 750;
        }

        .rrxResumeModal iframe {
          flex: 1;
          width: 100%;
          border: 0;
          background: #eef2f7;
        }


        .rrxResumeText {
          flex: 1;
          min-height: 0;
          overflow: auto;
          padding: 22px 26px;
          background: #f2f5f9;
        }

        .rrxResumeTextLabel {
          width: fit-content;
          margin: 0 auto 12px;
          padding: 6px 10px;
          border: 1px solid #d9e3ef;
          border-radius: 999px;
          background: #ffffff;
          color: #51637d;
          font-size: 10px;
          font-weight: 750;
        }

        .rrxResumeText pre {
          width: min(900px, 94%);
          min-height: 100%;
          margin: 0 auto;
          padding: 30px 34px;
          border: 1px solid #dfe5ed;
          border-radius: 8px;
          background: #ffffff;
          color: #253750;
          box-shadow: 0 4px 18px rgba(24, 43, 70, 0.08);
          font-family: Arial, sans-serif;
          font-size: 12px;
          line-height: 1.65;
          white-space: pre-wrap;
          word-break: break-word;
        }

        .rrxResumeMissing {
          flex: 1;
          display: grid;
          place-items: center;
          align-content: center;
          gap: 8px;
          padding: 30px;
          background: #f4f6f9;
          color: #7b899e;
          text-align: center;
        }

        .rrxResumeMissing svg {
          width: 38px;
          height: 38px;
          color: #9aa7b9;
        }

        .rrxResumeMissing strong {
          color: #40516a;
          font-size: 15px;
        }

        .rrxResumeMissing span {
          max-width: 520px;
          font-size: 11px;
          line-height: 1.5;
        }

        @media (max-width: 1250px) {
          .rrxGrid {
            grid-template-columns: 300px minmax(0, 1fr);
          }

          .rrxApprovalRow {
            align-items: stretch;
            flex-direction: column;
          }

          .rrxSelectedAction {
            justify-content: space-between;
          }

          .rrxMetrics {
            grid-template-columns: repeat(3, minmax(0, 1fr));
          }
        }

        @media (max-width: 900px) {
          .rrxPage {
            padding: 12px;
          }

          .rrxGrid {
            grid-template-columns: 1fr;
          }

          .rrxSidebar {
            position: static;
          }

          .rrxRoleList {
            max-height: 230px;
          }

          .rrxCandidatesHeader {
            align-items: stretch;
            flex-direction: column;
          }

          .rrxCandidateTools {
            align-items: stretch;
            flex-direction: column;
          }

          .rrxCandidateSearch {
            width: 100%;
            min-width: 0;
          }

          .rrxEvidenceGrid,
          .rrxFacts > div {
            grid-template-columns: 1fr;
          }
        }
      `}</style>
    </section>
  );
}
