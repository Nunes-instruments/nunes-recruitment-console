"use client";

import {
  CheckCircle2,
  Clock3,
  MailCheck,
  Search,
  ShieldCheck,
} from "lucide-react";

export type SentResponseRow = {
  id: number;
  recipient_email: string;
  candidate_name?: string | null;
  candidate_phone?: string | null;
  job_title?: string | null;
  subject?: string | null;
  body?: string | null;
  smtp_transport?: string | null;
  message_id?: string | null;
  sent_at: string;
  correction_required?: number;
  correction_status?: string | null;
  correction_subject?: string | null;
  correction_body?: string | null;
  correction_sent_at?: string | null;
};

export type SentResponseSummary = {
  total: number;
  uniqueRecipients: number;
  corrections: number;
  latest: string | null;
};

type SentResponsesPageProps = {
  rows: SentResponseRow[];
  summary: SentResponseSummary;
  search: string;
  onSearchChange: (value: string) => void;
};

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

export function SentResponsesPage({
  rows,
  summary,
  search,
  onSearchChange,
}: SentResponsesPageProps) {
  return (
    <section className="sentResponsesWorkspace" id="sent-responses">
      <div className="sentResponseSummaryGrid">
        <article className="sentSummaryCard primary">
          <div className="sentSummaryIcon">
            <MailCheck />
          </div>

          <div>
            <span>Total delivered</span>
            <strong>{summary.total}</strong>
            <small>Permanent acknowledgement records</small>
          </div>
        </article>

        <article className="sentSummaryCard">
          <div className="sentSummaryIcon">
            <ShieldCheck />
          </div>

          <div>
            <span>Unique recipients</span>
            <strong>{summary.uniqueRecipients}</strong>
            <small>Duplicate-protected email addresses</small>
          </div>
        </article>

        <article className="sentSummaryCard">
          <div className="sentSummaryIcon">
            <CheckCircle2 />
          </div>

          <div>
            <span>Role corrections</span>
            <strong>{summary.corrections}</strong>
            <small>One-time clarification messages sent</small>
          </div>
        </article>

        <article className="sentSummaryCard">
          <div className="sentSummaryIcon">
            <Clock3 />
          </div>

          <div>
            <span>Latest delivery</span>

            <strong className="sentSummaryDate">
              {summary.latest
                ? formatDate(summary.latest)
                : "No delivery yet"}
            </strong>

            <small>Most recent successful acknowledgement</small>
          </div>
        </article>
      </div>

      <section className="sentHistoryCard">
        <div className="sentHistoryHeader">
          <div className="sentHistoryTitleGroup">
            <p className="sectionLabel">Communication History</p>

            <div className="sentHistoryTitleRow">
              <h2>Sent Responses</h2>

              <span className="sentTotalPill">
                {summary.total} delivered
              </span>
            </div>

            <p className="tableSubhead">
              Permanent delivery history. Acknowledged email addresses remain
              protected from duplicate sends.
            </p>
          </div>

          <label className="sentSearchBox">
            <Search />

            <input
              value={search}
              onChange={(event) => onSearchChange(event.target.value)}
              placeholder="Search candidate, email or role"
              aria-label="Search sent responses"
            />

            <span>{rows.length}</span>
          </label>
        </div>

        <div className="sentHistoryScroll modernSentTableWrap">
          <table className="modernSentTable">
            <colgroup>
              <col className="sentColCandidate" />
              <col className="sentColDelivery" />
              <col className="sentColRole" />
              <col className="sentColMessage" />
              <col className="sentColTime" />
            </colgroup>

            <thead>
              <tr>
                <th>Candidate</th>
                <th>Delivery</th>
                <th>Applied role</th>
                <th>Message</th>
                <th>Sent</th>
              </tr>
            </thead>

            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={5}>
                    <div className="emptyState sentEmptyState">
                      <div className="sentEmptyIcon">
                        <MailCheck />
                      </div>

                      <strong>
                        {search.trim()
                          ? "No matching sent responses"
                          : "No sent responses yet"}
                      </strong>

                      <span>
                        {search.trim()
                          ? "Try a different candidate, email address or role."
                          : "Successful acknowledgements will appear here automatically."}
                      </span>
                    </div>
                  </td>
                </tr>
              ) : (
                rows.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <div className="sentCandidateCell">
                        <div className="sentAvatar">
                          {(row.candidate_name || "C")
                            .slice(0, 1)
                            .toUpperCase()}
                        </div>

                        <div className="sentCandidateMeta">
                          <strong>
                            {row.candidate_name || "Candidate"}
                          </strong>

                          <span>
                            {row.candidate_phone || "No phone recorded"}
                          </span>
                        </div>
                      </div>
                    </td>

                    <td>
                      <div className="sentDeliveryCell">
                        <strong title={row.recipient_email}>
                          {row.recipient_email}
                        </strong>

                        <div className="sentDeliveryBadges">
                          <span className="deliveryBadge delivered">
                            <CheckCircle2 />
                            Delivered
                          </span>

                          {row.correction_status === "SENT" && (
                            <span className="deliveryBadge corrected">
                              Role corrected
                            </span>
                          )}

                          {row.correction_required === 1 &&
                            row.correction_status &&
                            row.correction_status !== "SENT" && (
                              <span className="deliveryBadge pending">
                                {row.correction_status === "WAITING_FOR_ROLE"
                                  ? "Correction waiting"
                                  : "Correction pending"}
                              </span>
                            )}
                        </div>
                      </div>
                    </td>

                    <td>
                      <div className="sentRoleCell">
                        <span>
                          {row.job_title || "Role not recorded"}
                        </span>
                      </div>
                    </td>

                    <td>
                      <details className="modernResponseDetails">
                        <summary>
                          <div className="responseSummaryText">
                            <strong>
                              {row.subject ||
                                "Historical acknowledgement"}
                            </strong>

                            <span>View delivered message</span>
                          </div>

                          <span className="responseChevron">⌄</span>
                        </summary>

                        <div className="modernResponseSnapshot">
                          {row.body ? (
                            <pre>{row.body}</pre>
                          ) : (
                            <p>
                              This acknowledgement was sent by an earlier
                              version before exact message snapshots were
                              stored. Recipient and delivery history remain
                              preserved.
                            </p>
                          )}

                          <div className="responseMetaRow">
                            {row.smtp_transport && (
                              <small>
                                Transport: {row.smtp_transport}
                              </small>
                            )}

                            {row.message_id && (
                              <small>Message recorded</small>
                            )}
                          </div>

                          {row.correction_sent_at && (
                            <div className="modernCorrectionSnapshot">
                              <div>
                                <strong>One-time role correction</strong>

                                <span>
                                  {row.correction_subject ||
                                    "Correction sent"}
                                </span>
                              </div>

                              {row.correction_body && (
                                <pre>{row.correction_body}</pre>
                              )}

                              <small>
                                Sent {formatDate(row.correction_sent_at)}
                              </small>
                            </div>
                          )}
                        </div>
                      </details>
                    </td>

                    <td>
                      <div className="sentTimeCell">
                        <strong>{formatDate(row.sent_at)}</strong>
                        <span>Successful</span>
                      </div>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </section>
    </section>
  );
}
