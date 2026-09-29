"use client";

import { LoaderCircle, Save } from "lucide-react";
import { Button } from "@/components/ui/button";

type Props = {
  settings: any;
  busy: string;
  onUpdateSetting: (key: any, value: any) => void;
  onSave: () => void | Promise<void>;
};

export function RecruitmentMessagesPage({
  settings,
  busy,
  onUpdateSetting,
  onSave,
}: Props) {
  return (
    <article className="messagePage">
      <style>{`
        .messagePage {
          background:#fff;
          border:1px solid #dce5ef;
          border-radius:18px;
          overflow:hidden;
          box-shadow:0 10px 30px rgba(15,23,42,.05);
        }

        .messagePageHeader {
          display:flex;
          align-items:center;
          justify-content:space-between;
          gap:16px;
          padding:18px 20px;
          border-bottom:1px solid #e4eaf1;
        }

        .messagePageHeader h2 {
          margin:0;
          font-size:21px;
          color:#172033;
        }

        .messagePageHeader p {
          margin:4px 0 0;
          color:#718096;
          font-size:12px;
        }

        .messageCards {
          display:grid;
          grid-template-columns:1fr 1fr;
          gap:14px;
          padding:18px;
        }

        .messageCard {
          border:1px solid #dce5ef;
          border-radius:15px;
          background:#fff;
          padding:16px;
        }

        .messageCardHeader {
          display:flex;
          align-items:flex-start;
          gap:11px;
          margin-bottom:14px;
        }

        .messageNumber {
          width:38px;
          height:38px;
          border-radius:10px;
          display:grid;
          place-items:center;
          background:#eef4ff;
          color:#2563eb;
          font-weight:800;
          flex:none;
        }

        .messageCard:nth-child(2) .messageNumber {
          background:#f3edff;
          color:#7047eb;
        }

        .messageCardHeader strong {
          display:block;
          color:#172033;
          font-size:16px;
        }

        .messageCardHeader small {
          display:block;
          color:#7b8797;
          margin-top:3px;
          line-height:1.4;
        }

        .messageEditorField {
          display:flex;
          flex-direction:column;
          gap:7px;
          margin-top:12px;
        }

        .messageEditorField span {
          font-size:13px;
          font-weight:750;
          color:#344054;
        }

        .messageEditorField input,
        .messageEditorField textarea {
          width:100%;
          border:1px solid #cfd8e3;
          border-radius:10px;
          padding:10px 12px;
          outline:none;
          font-size:13px;
          color:#172033;
          background:#fff;
        }

        .messageEditorField input {
          min-height:42px;
        }

        .messageEditorField textarea {
          min-height:180px;
          resize:vertical;
          line-height:1.5;
        }

        .messageEditorField input:focus,
        .messageEditorField textarea:focus {
          border-color:#8eb4ff;
          box-shadow:0 0 0 3px rgba(37,99,235,.10);
        }

        .messageRules {
          display:grid;
          grid-template-columns:1fr 1fr;
          gap:12px;
          padding:0 18px 18px;
        }

        .messageRule {
          background:#f8fafc;
          border:1px solid #e1e7ef;
          border-radius:11px;
          padding:11px 13px;
        }

        .messageRule strong {
          display:block;
          font-size:12px;
          color:#344054;
          margin-bottom:4px;
        }

        .messageRule span {
          color:#718096;
          font-size:11px;
          line-height:1.5;
        }

        @media(max-width:900px) {
          .messageCards,
          .messageRules {
            grid-template-columns:1fr;
          }
        }
      `}</style>

      <div className="messagePageHeader">
        <div>
          <h2>Recruitment Messages</h2>
          <p>
            Stage 1 sends the acknowledgement after verification.
            Stage 2 sends the interview invitation only after HR approval.
          </p>
        </div>

        <Button
          size="sm"
          disabled={busy === "settings"}
          onClick={onSave}
        >
          {busy === "settings" ? (
            <LoaderCircle className="buttonSpinner" />
          ) : (
            <Save />
          )}

          {busy === "settings"
            ? "Saving…"
            : "Save all messages"}
        </Button>
      </div>

      <div className="messageCards">
        <section className="messageCard">
          <div className="messageCardHeader">
            <span className="messageNumber">1</span>

            <div>
              <strong>Application acknowledgement</strong>
              <small>
                Automatic email after exact candidate + role + verified email
              </small>
            </div>
          </div>

          <label className="messageEditorField">
            <span>Email subject</span>

            <input
              value={settings.subject_template || ""}
              onChange={(e) =>
                onUpdateSetting(
                  "subject_template",
                  e.target.value,
                )
              }
            />
          </label>

          <label className="messageEditorField">
            <span>Email message</span>

            <textarea
              value={settings.body_template || ""}
              onChange={(e) =>
                onUpdateSetting(
                  "body_template",
                  e.target.value,
                )
              }
            />
          </label>
        </section>

        <section className="messageCard">
          <div className="messageCardHeader">
            <span className="messageNumber">2</span>

            <div>
              <strong>HR interview invitation</strong>
              <small>
                Editable email released only after HR approves the candidate
              </small>
            </div>
          </div>

          <label className="messageEditorField">
            <span>Email subject</span>

            <input
              value={settings.interview_subject_template || ""}
              onChange={(e) =>
                onUpdateSetting(
                  "interview_subject_template",
                  e.target.value,
                )
              }
            />
          </label>

          <label className="messageEditorField">
            <span>Email message</span>

            <textarea
              value={settings.interview_body_template || ""}
              onChange={(e) =>
                onUpdateSetting(
                  "interview_body_template",
                  e.target.value,
                )
              }
            />
          </label>
        </section>
      </div>

      <div className="messageRules">
        <div className="messageRule">
          <strong>Available fields</strong>

          <span>
            {"{candidate_name}"} · {"{job_title}"} ·
            {" {company_name}"} · {"{interview_date}"}
          </span>
        </div>

        <div className="messageRule">
          <strong>Interview scheduling</strong>

          <span>
            Approved Mon–Wed → next day ·
            Approved Thu–Fri → Monday
          </span>
        </div>
      </div>
    </article>
  );
}