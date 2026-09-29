"use client";

import { useMemo, useState } from "react";
import {
  Activity,
  AlertCircle,
  CheckCircle2,
  Clock3,
  Info,
  Search,
  XCircle,
} from "lucide-react";
import { Input } from "@/components/ui/input";

type LogRow = {
  id: number;
  level: string;
  message: string;
  created_at: string;
};

type Props = {
  logs: LogRow[];
  lastScanAt?: string | null;
  viewError?: string;
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

const levelName = (value?: string) =>
  String(value || "INFO").toUpperCase();

export function ActivityPage({
  logs,
  lastScanAt,
  viewError = "",
}: Props) {
  const [search, setSearch] = useState("");
  const [filter, setFilter] =
    useState<"ALL" | "ERROR" | "OTHER">("ALL");

  const rows = useMemo(() => {
    const q = search.trim().toLowerCase();

    return (logs || []).filter((row) => {
      const level = levelName(row.level);

      if (filter === "ERROR" && level !== "ERROR") {
        return false;
      }

      if (filter === "OTHER" && level === "ERROR") {
        return false;
      }

      if (!q) return true;

      return `${row.message} ${row.level}`
        .toLowerCase()
        .includes(q);
    });
  }, [logs, search, filter]);

  const errors = (logs || []).filter(
    (row) => levelName(row.level) === "ERROR",
  ).length;

  const warnings = (logs || []).filter((row) =>
    ["WARNING", "WARN"].includes(levelName(row.level)),
  ).length;

  return (
    <section className="activityNewPage">
      <style>{`
        .activityNewPage {
          background:#fff;
          border:1px solid #dce5ef;
          border-radius:18px;
          overflow:hidden;
          box-shadow:0 10px 30px rgba(15,23,42,.05);
        }

        .activityNewHeader {
          display:flex;
          align-items:center;
          justify-content:space-between;
          gap:16px;
          padding:18px 20px;
          border-bottom:1px solid #e4eaf1;
        }

        .activityNewTitle {
          display:flex;
          align-items:center;
          gap:11px;
        }

        .activityNewIcon {
          width:40px;
          height:40px;
          display:grid;
          place-items:center;
          border-radius:11px;
          background:#eef4ff;
          color:#2563eb;
        }

        .activityNewTitle h2 {
          margin:0;
          font-size:20px;
        }

        .activityNewTitle p {
          margin:3px 0 0;
          color:#718096;
          font-size:12px;
        }

        .activityLastCheck {
          display:flex;
          align-items:center;
          gap:7px;
          color:#718096;
          font-size:12px;
        }

        .activitySummary {
          display:grid;
          grid-template-columns:repeat(4,1fr);
          gap:10px;
          padding:14px 18px;
          background:#f8fafc;
          border-bottom:1px solid #e4eaf1;
        }

        .activityStat {
          background:#fff;
          border:1px solid #e2e8f0;
          border-radius:11px;
          padding:11px 13px;
        }

        .activityStat span {
          display:block;
          color:#718096;
          font-size:11px;
        }

        .activityStat strong {
          display:block;
          margin-top:2px;
          font-size:18px;
        }

        .activityTools {
          display:flex;
          justify-content:space-between;
          align-items:center;
          gap:12px;
          padding:12px 18px;
          border-bottom:1px solid #e4eaf1;
        }

        .activitySearch {
          position:relative;
          width:min(460px,100%);
        }

        .activitySearch svg {
          position:absolute;
          left:12px;
          top:50%;
          transform:translateY(-50%);
          width:16px;
          color:#718096;
        }

        .activitySearch input {
          padding-left:38px;
        }

        .activityFilters {
          display:flex;
          gap:7px;
        }

        .activityFilters button {
          border:1px solid #d6dee9;
          background:#fff;
          border-radius:999px;
          padding:7px 11px;
          font-weight:700;
          font-size:11px;
          cursor:pointer;
        }

        .activityFilters button.active {
          background:#eef4ff;
          color:#2563eb;
          border-color:#bcd2ff;
        }

        .activityCompactList {
          max-height:520px;
          overflow:auto;
        }

        .activityCompactRow {
          display:grid;
          grid-template-columns:30px 1fr auto;
          gap:11px;
          align-items:center;
          padding:10px 16px;
          border-bottom:1px solid #edf1f5;
          min-height:58px;
        }

        .activityCompactRow:hover {
          background:#fbfdff;
        }

        .activityRowIcon {
          width:28px;
          height:28px;
          border-radius:8px;
          display:grid;
          place-items:center;
          background:#eef4ff;
          color:#2563eb;
        }

        .activityRowIcon svg {
          width:14px;
        }

        .activityCompactRow.error .activityRowIcon {
          background:#fff0f0;
          color:#d64545;
        }

        .activityCompactRow.warning .activityRowIcon {
          background:#fff7e8;
          color:#b7791f;
        }

        .activityRowText strong {
          display:block;
          font-size:13px;
          color:#344054;
          line-height:1.35;
        }

        .activityRowText small {
          display:block;
          margin-top:3px;
          color:#98a2b3;
          font-size:11px;
        }

        .activityLevel {
          border-radius:999px;
          padding:5px 8px;
          font-size:10px;
          font-weight:800;
          background:#f2f4f7;
          color:#667085;
        }

        .activityCompactRow.error .activityLevel {
          background:#fff0f0;
          color:#c93e3e;
        }

        .activityWarning {
          margin:12px 18px 0;
          padding:10px 12px;
          background:#fff7e8;
          border:1px solid #efd59f;
          border-radius:10px;
          color:#8a6100;
          font-size:12px;
        }

        .activityEmpty {
          padding:50px;
          text-align:center;
          color:#718096;
        }

        @media(max-width:800px) {
          .activitySummary {
            grid-template-columns:repeat(2,1fr);
          }

          .activityTools {
            align-items:stretch;
            flex-direction:column;
          }

          .activitySearch {
            width:100%;
          }
        }
      `}</style>

      <div className="activityNewHeader">
        <div className="activityNewTitle">
          <div className="activityNewIcon">
            <Activity />
          </div>

          <div>
            <h2>Recent Events</h2>
            <p>
              Live monitoring, ranking and recruitment delivery
              activity.
            </p>
          </div>
        </div>

        <div className="activityLastCheck">
          <Clock3 />
          Last check: {formatDate(lastScanAt)}
        </div>
      </div>

      {viewError ? (
        <div className="activityWarning">
          Activity refresh issue: {viewError}
        </div>
      ) : null}

      <div className="activitySummary">
        <div className="activityStat">
          <span>Total loaded</span>
          <strong>{logs.length}</strong>
        </div>

        <div className="activityStat">
          <span>Errors</span>
          <strong>{errors}</strong>
        </div>

        <div className="activityStat">
          <span>Warnings</span>
          <strong>{warnings}</strong>
        </div>

        <div className="activityStat">
          <span>Other events</span>
          <strong>
            {Math.max(0, logs.length - errors - warnings)}
          </strong>
        </div>
      </div>

      <div className="activityTools">
        <div className="activitySearch">
          <Search />

          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search activity..."
          />
        </div>

        <div className="activityFilters">
          <button
            type="button"
            className={filter === "ALL" ? "active" : ""}
            onClick={() => setFilter("ALL")}
          >
            All
          </button>

          <button
            type="button"
            className={filter === "ERROR" ? "active" : ""}
            onClick={() => setFilter("ERROR")}
          >
            Errors
          </button>

          <button
            type="button"
            className={filter === "OTHER" ? "active" : ""}
            onClick={() => setFilter("OTHER")}
          >
            Other
          </button>
        </div>
      </div>

      <div className="activityCompactList">
        {rows.length === 0 ? (
          <div className="activityEmpty">
            No matching activity.
          </div>
        ) : (
          rows.map((row) => {
            const level = levelName(row.level);

            const tone =
              level === "ERROR"
                ? "error"
                : ["WARNING", "WARN"].includes(level)
                  ? "warning"
                  : "";

            return (
              <div
                className={`activityCompactRow ${tone}`}
                key={row.id}
              >
                <div className="activityRowIcon">
                  {level === "ERROR" ? (
                    <XCircle />
                  ) : ["WARNING", "WARN"].includes(level) ? (
                    <AlertCircle />
                  ) : level === "SUCCESS" ? (
                    <CheckCircle2 />
                  ) : (
                    <Info />
                  )}
                </div>

                <div className="activityRowText">
                  <strong>{row.message}</strong>
                  <small>{formatDate(row.created_at)}</small>
                </div>

                <span className="activityLevel">
                  {level}
                </span>
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}