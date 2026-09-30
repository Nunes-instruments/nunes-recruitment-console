from __future__ import annotations

import hashlib
import json
import re
import smtplib
import ssl
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from config import load_settings
from database import conn, get_by_id, get_by_source_key, get_state, set_state, log, now


HR_REPORT_SENDER = "nunescbe@gmail.com"
ACTIVE_ROLE_STATES = {"OPEN", "PAUSED", "FLAGGED"}
INACTIVE_ROLE_STATES = {"CLOSED"}
TERMINAL_CANDIDATE_STATES = {
    "hired", "selected", "not selected", "rejected", "withdrawn", "archived"
}

DEFAULT_INTERVIEW_SUBJECT_TEMPLATE = "Interview invitation – {job_title} – {interview_date}"
DEFAULT_INTERVIEW_BODY_TEMPLATE = (
    "Dear {candidate_name},\n\n"
    "Our HR team has reviewed your application for the {job_title} position at {company_name} "
    "and selected you for the interview stage.\n\n"
    "Your interview is scheduled for {interview_date}. Our HR team will contact you with the "
    "time and venue/meeting details.\n\n"
    "Regards,\n{company_name}"
)


def _clean_role(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:200]
    # Strip location suffixes appended by Indeed (e.g. " • Rathinapuri, Coimbatore, Tamil Nadu")
    text = re.sub(r"\s+[•·|—–-]\s*(?:Rathinapuri|Coimbatore|Tamil Nadu|Mayiladuthurai|India).*$", "", text, flags=re.I)
    text = re.sub(r"\s*,\s*(?:Rathinapuri|Coimbatore|Tamil Nadu|Mayiladuthurai|India).*$", "", text, flags=re.I)
    # Normalize unicode dash variants and corrupted question mark
    text = text.replace(" ? ", " – ").replace("  ", " – ").replace(" - ", " – ")
    return text.strip(" |•·–—-")


def _norm_status(value):
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower()
    if not text:
        return "UNKNOWN"
    if any(x in text for x in ("closed", "expired", "filled")):
        return "CLOSED"
    if "flagged" in text:
        return "FLAGGED"
    if any(x in text for x in ("paused", "pause", "inactive")):
        return "PAUSED"
    if any(x in text for x in ("open", "active", "live", "published")):
        return "OPEN"
    return "UNKNOWN"


def _safe_json(value, default):
    try:
        parsed = json.loads(value or json.dumps(default))
        return parsed
    except Exception:
        return default


def init_recruitment_pipeline_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS recruitment_role_state (
                job_title TEXT PRIMARY KEY COLLATE NOCASE,
                lifecycle_status TEXT NOT NULL DEFAULT 'UNKNOWN',
                indeed_status TEXT,
                indeed_job_url TEXT,
                candidate_total_hint INTEGER,
                candidate_new_hint INTEGER,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                closed_at TEXT,
                ranking_hash TEXT,
                report_status TEXT NOT NULL DEFAULT 'IDLE',
                report_sent_at TEXT,
                report_error TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recruitment_candidate_flow (
                application_id INTEGER PRIMARY KEY,
                hr_status TEXT NOT NULL DEFAULT 'PENDING',
                hr_approved_at TEXT,
                interview_date TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS recruitment_notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_key TEXT NOT NULL UNIQUE,
                application_id INTEGER,
                job_title TEXT,
                channel TEXT NOT NULL,
                message_type TEXT NOT NULL,
                recipient TEXT,
                sender TEXT,
                subject TEXT,
                body TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED',
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                sent_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_recruitment_notifications_status
            ON recruitment_notifications(status, channel, message_type);

            CREATE INDEX IF NOT EXISTS idx_recruitment_notifications_app
            ON recruitment_notifications(application_id, message_type);

            CREATE INDEX IF NOT EXISTS idx_recruitment_role_state_status
            ON recruitment_role_state(lifecycle_status, updated_at);
            """
        )

        role_cols = {
            r["name"]
            for r in c.execute(
                "PRAGMA table_info(recruitment_role_state)"
            ).fetchall()
        }
        if "candidate_total_hint" not in role_cols:
            c.execute(
                "ALTER TABLE recruitment_role_state "
                "ADD COLUMN candidate_total_hint INTEGER"
            )
        if "candidate_new_hint" not in role_cols:
            c.execute(
                "ALTER TABLE recruitment_role_state "
                "ADD COLUMN candidate_new_hint INTEGER"
            )

        # A process interruption after a network send is deliberately not retried.
        c.execute(
            """
            UPDATE recruitment_notifications
            SET status='SEND_UNCERTAIN',
                error=CASE
                    WHEN trim(COALESCE(error,''))='' THEN
                        'Previous process ended while delivery was in progress. Automatic resend is blocked.'
                    ELSE error
                END,
                updated_at=?
            WHERE status='SENDING'
            """,
            (now(),),
        )

        # V11.11.13 is EMAIL ONLY. Legacy WhatsApp notifications from older
        # versions are permanently retired so they can never block or retry.
        c.execute(
            """
            UPDATE recruitment_notifications
            SET status='CHANNEL_REMOVED',
                error='WhatsApp delivery was removed in V11.11.13; email-only workflow is active.',
                updated_at=?
            WHERE channel='WHATSAPP'
              AND status NOT IN ('SENT','SEND_UNCERTAIN','CHANNEL_REMOVED')
            """,
            (now(),),
        )

    if not get_state("recruitment_pipeline_activation_at"):
        set_state("recruitment_pipeline_activation_at", now())


def _pause_role_outreach(job_title, reason):
    """Stop queued candidate-facing outreach only when an Indeed role is closed."""
    title = _clean_role(job_title)
    if not title:
        return 0

    with conn() as c:
        cur = c.execute(
            """
            UPDATE recruitment_notifications
            SET status='PAUSED', error=?, updated_at=?
            WHERE lower(job_title)=lower(?)
              AND message_type='INTERVIEW_EMAIL'
              AND status IN ('QUEUED','SEND_FAILED','WAITING_LOGIN','WAITING_CONFIG')
            """,
            (str(reason or 'Role is paused/closed.')[:2000], now(), title),
        )
        return max(0, int(cur.rowcount or 0))


def ensure_role_state(job_title, lifecycle_status="UNKNOWN", indeed_status=None, job_url=None, candidate_total_hint=None, candidate_new_hint=None):
    init_recruitment_pipeline_db()
    title = _clean_role(job_title)
    if not title or title.lower() in {"the position", "position", "job", "unknown"}:
        return None

    ts = now()
    normalized = _norm_status(lifecycle_status or indeed_status)

    with conn() as c:
        existing = c.execute(
            "SELECT * FROM recruitment_role_state WHERE lower(job_title)=lower(?)",
            (title,),
        ).fetchone()

        old_status = (existing["lifecycle_status"] if existing else "UNKNOWN") or "UNKNOWN"
        new_status = normalized
        closed_at = existing["closed_at"] if existing else None

        if new_status == "CLOSED" and old_status != "CLOSED":
            closed_at = ts
        elif new_status in ACTIVE_ROLE_STATES:
            closed_at = None

        c.execute(
            """
            INSERT INTO recruitment_role_state(
                job_title, lifecycle_status, indeed_status, indeed_job_url,
                candidate_total_hint, candidate_new_hint,
                first_seen_at, last_seen_at, closed_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_title) DO UPDATE SET
                lifecycle_status=excluded.lifecycle_status,
                indeed_status=COALESCE(excluded.indeed_status, recruitment_role_state.indeed_status),
                indeed_job_url=COALESCE(excluded.indeed_job_url, recruitment_role_state.indeed_job_url),
                candidate_total_hint=COALESCE(excluded.candidate_total_hint, recruitment_role_state.candidate_total_hint),
                candidate_new_hint=COALESCE(excluded.candidate_new_hint, recruitment_role_state.candidate_new_hint),
                last_seen_at=excluded.last_seen_at,
                closed_at=excluded.closed_at,
                updated_at=excluded.updated_at
            """,
            (
                title,
                new_status,
                str(indeed_status or "")[:120] or None,
                str(job_url or "")[:1200] or None,
                int(candidate_total_hint) if candidate_total_hint is not None else None,
                int(candidate_new_hint) if candidate_new_hint is not None else None,
                existing["first_seen_at"] if existing else ts,
                ts,
                closed_at,
                ts,
            ),
        )

    if old_status != new_status:
        log("INFO", f"ROLE LIFECYCLE: {title} -> {new_status}.")
        set_state("recruitment_role_lifecycle_changed_at", ts)

        if new_status in INACTIVE_ROLE_STATES:
            paused = _pause_role_outreach(
                title,
                f"Role changed to {new_status} on Indeed; candidate outreach is stopped.",
            )
            if paused:
                log(
                    "INFO",
                    f"ROLE LIFECYCLE: paused {paused} queued candidate outreach item(s) for {title}.",
                )

    return get_role_state(title)


def sync_discovered_jobs(rows):
    """
    Synchronize the user's current Indeed Jobs screen.

    Current roles = Open, Paused, Flagged, Unknown.
    Ignored roles = Closed/Expired/Filled.

    Duplicate postings can share the same title. For the title-level ranking
    workspace, OPEN wins over FLAGGED, then PAUSED, then UNKNOWN, then CLOSED.

    When Indeed reports a complete current-results snapshot (for example
    "14 results" and we parsed all 14), previously-known roles that disappeared
    from that non-closed view are safely marked CLOSED.
    """
    changed = []
    rows = [
        dict(row)
        for row in (rows or [])
        if isinstance(row, dict)
    ]

    priority = {
        "OPEN": 50,
        "FLAGGED": 40,
        "PAUSED": 30,
        "UNKNOWN": 20,
        "CLOSED": 0,
    }

    grouped = {}
    total_hint = 0

    for row in rows:
        title = _clean_role(
            row.get("job_title")
            or row.get("title")
        )
        if not title:
            continue

        raw_status = (
            row.get("job_status")
            or row.get("status")
            or "UNKNOWN"
        )
        normalized = _norm_status(raw_status)

        try:
            total_hint = max(
                total_hint,
                int(row.get("snapshot_total_hint") or 0),
            )
        except Exception:
            pass

        current = grouped.get(title)
        try:
            row_total = int(row.get("candidate_total") or 0)
        except Exception:
            row_total = 0
        try:
            row_new = int(row.get("candidate_new") or 0)
        except Exception:
            row_new = 0

        candidate = {
            "title": title,
            "normalized": normalized,
            "raw_status": raw_status,
            "job_url": row.get("job_url") or row.get("url"),
            "candidate_total_hint": row_total,
            "candidate_new_hint": row_new,
        }

        if current is None:
            grouped[title] = candidate
        else:
            # The same role title may have more than one Indeed posting (for
            # example one Open posting and one Paused posting). Ranking is role-
            # based, so combine their All/New candidate counts while retaining
            # the most-active lifecycle status and its primary job URL.
            current["candidate_total_hint"] = int(
                current.get("candidate_total_hint") or 0
            ) + row_total
            current["candidate_new_hint"] = int(
                current.get("candidate_new_hint") or 0
            ) + row_new

            if priority.get(normalized, 10) > priority.get(current["normalized"], 10):
                current["normalized"] = normalized
                current["raw_status"] = raw_status
                current["job_url"] = row.get("job_url") or row.get("url")

    seen_titles = set()

    for title, selected in grouped.items():
        seen_titles.add(title.lower())
        before = get_role_state(title)

        after = ensure_role_state(
            title,
            lifecycle_status=selected["normalized"],
            indeed_status=selected["raw_status"],
            job_url=selected["job_url"],
            candidate_total_hint=selected.get("candidate_total_hint"),
            candidate_new_hint=selected.get("candidate_new_hint"),
        )

        if (
            after
            and (
                not before
                or before.get("lifecycle_status")
                != after.get("lifecycle_status")
            )
        ):
            changed.append(title)

    # Only infer closure when we are confident the entire current filtered
    # result set was parsed. This avoids accidentally closing roles after a
    # partial/failed DOM scan.
    complete_snapshot = (
        total_hint > 0
        and total_hint <= 25
        and len(rows) >= total_hint
    )

    if complete_snapshot:
        with conn() as c:
            previous_rows = c.execute(
                """
                SELECT job_title, lifecycle_status, indeed_job_url
                FROM recruitment_role_state
                WHERE upper(trim(COALESCE(lifecycle_status,'UNKNOWN'))) <> 'CLOSED'
                """
            ).fetchall()

        for previous in previous_rows:
            old_title = _clean_role(previous["job_title"])
            if not old_title:
                continue
            if old_title.lower() in seen_titles:
                continue

            before = str(
                previous["lifecycle_status"]
                or "UNKNOWN"
            ).upper()

            ensure_role_state(
                old_title,
                lifecycle_status="CLOSED",
                indeed_status="Closed/removed from current Indeed Jobs view",
                job_url=previous["indeed_job_url"],
            )

            if before != "CLOSED":
                changed.append(old_title)

    try:
        canonicalize_existing_application_roles()
    except Exception as exc:
        log("WARN", f"ROLE CANONICALIZATION waiting: {exc}")

    return list(dict.fromkeys(changed))


def get_role_state(job_title):
    init_recruitment_pipeline_db()
    title = _clean_role(job_title)
    if not title:
        return None
    with conn() as c:
        row = c.execute(
            "SELECT * FROM recruitment_role_state WHERE lower(job_title)=lower(?) LIMIT 1",
            (title,),
        ).fetchone()
        return dict(row) if row else None


def _role_key(value):
    text = _clean_role(value).lower()
    text = re.sub(r"[^a-z0-9+#]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def canonical_active_role_title(job_title):
    """Map candidate-page role text to a current Indeed role title.

    Indeed candidate rows sometimes append a location to the job title. This
    caused duplicate dashboard roles such as "Purchase Executive • Rathinapuri".
    The Jobs table is the authority; candidate rows are canonicalized against it.
    """
    title = _clean_role(job_title)
    low = title.lower().strip()
    if not title or low in {
        "the position", "position", "job", "the job", "unknown",
        "day", "days", "today", "yesterday", "month", "months",
        "year", "years", "ago", "all", "new", "matches", "with", "in a", "role"
    }:
        return ""

    # Map the three core company recruitment roles directly and reliably
    if "purchase" in low and "executive" in low:
        return "Purchase Executive"
    if "marketing" in low and ("lead" in low or "coordination" in low or "executive" in low):
        return "Marketing & Lead Coordination Executive"
    if "driver" in low or ("electrician" in low and "technical" in low):
        return "Driver cum Electrician – Technical Support Assistant"

    init_recruitment_pipeline_db()
    with conn() as c:
        rows = c.execute(
            """
            SELECT job_title, lifecycle_status
            FROM recruitment_role_state
            WHERE upper(trim(COALESCE(lifecycle_status,'UNKNOWN')))
                  IN ('OPEN','PAUSED','FLAGGED')
            ORDER BY length(job_title) DESC
            """
        ).fetchall()

    key = _role_key(title)
    best = None
    best_score = 0.0
    for row in rows:
        current = _clean_role(row["job_title"])
        current_key = _role_key(current)
        if not current_key:
            continue
        if key == current_key:
            return current

        score = 0.0
        # Strong match for the common "Role • Location" / "Role - Location" form.
        if key.startswith(current_key + " "):
            score = 0.96
        elif current_key.startswith(key + " ") and len(key) >= 12:
            score = 0.90
        else:
            a = set(key.split())
            b = set(current_key.split())
            if a and b:
                score = len(a & b) / max(1, len(a | b))

        if score > best_score:
            best_score = score
            best = current

    return best if best and best_score >= 0.78 else title


def canonicalize_existing_application_roles():
    """Repair old candidate rows using current Jobs-table role titles."""
    init_recruitment_pipeline_db()
    changed = 0
    with conn() as c:
        apps = c.execute(
            "SELECT id, job_title FROM applications WHERE trim(COALESCE(job_title,''))<>''"
        ).fetchall()

        for app in apps:
            old = _clean_role(app["job_title"])
            canonical = canonical_active_role_title(old)
            if not canonical or canonical.lower() == old.lower():
                continue
            c.execute(
                """
                UPDATE applications
                SET job_title=?,
                    updated_at=?,
                    extraction_status=CASE
                        WHEN extraction_status='NEEDS_REVIEW_ROLE'
                             AND candidate_email IS NOT NULL AND trim(candidate_email)<>''
                        THEN 'VERIFIED_EMAIL_AND_ROLE'
                        ELSE extraction_status
                    END,
                    decision_reason=CASE
                        WHEN extraction_status='NEEDS_REVIEW_ROLE'
                             AND candidate_email IS NOT NULL AND trim(candidate_email)<>''
                        THEN 'Ready: application, candidate email and applied role verified'
                        ELSE decision_reason
                    END
                WHERE id=?
                """,
                (canonical, now(), app["id"]),
            )
            # Keep review rows aligned when the table already exists.
            try:
                c.execute(
                    "UPDATE candidate_reviews SET job_title=?, updated_at=? WHERE application_id=?",
                    (canonical, now(), app["id"]),
                )
            except Exception:
                pass
            changed += 1

    if changed:
        log("INFO", f"ROLE CANONICALIZATION: repaired {changed} candidate role title(s).")
    return changed


def role_accepts_new_applications(job_title):
    canonical = canonical_active_role_title(job_title)
    if not canonical:
        return False
    # The 3 core company recruitment roles are always active
    if canonical in {
        "Purchase Executive",
        "Driver cum Electrician – Technical Support Assistant",
        "Marketing & Lead Coordination Executive",
    }:
        return True
    state = get_role_state(canonical)
    if not state:
        return False
    return (state.get("lifecycle_status") or "UNKNOWN").upper() in ACTIVE_ROLE_STATES


def role_accepts_ranking(job_title):
    return role_accepts_new_applications(job_title)


def _notification(unique_key):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM recruitment_notifications WHERE unique_key=? LIMIT 1",
            (unique_key,),
        ).fetchone()
        return dict(row) if row else None




def queue_notification(
    unique_key,
    *,
    channel,
    message_type,
    body,
    recipient=None,
    sender=None,
    subject=None,
    application_id=None,
    job_title=None,
    initial_status="QUEUED",
    error=None,
):
    init_recruitment_pipeline_db()
    ts = now()
    with conn() as c:
        c.execute(
            """
            INSERT INTO recruitment_notifications(
                unique_key, application_id, job_title, channel, message_type,
                recipient, sender, subject, body, status, attempts, error,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(unique_key) DO UPDATE SET
                recipient=CASE
                    WHEN recruitment_notifications.status IN ('SENT','SEND_UNCERTAIN')
                    THEN recruitment_notifications.recipient
                    ELSE excluded.recipient
                END,
                sender=CASE
                    WHEN recruitment_notifications.status IN ('SENT','SEND_UNCERTAIN')
                    THEN recruitment_notifications.sender
                    ELSE excluded.sender
                END,
                subject=CASE
                    WHEN recruitment_notifications.status IN ('SENT','SEND_UNCERTAIN')
                    THEN recruitment_notifications.subject
                    ELSE excluded.subject
                END,
                body=CASE
                    WHEN recruitment_notifications.status IN ('SENT','SEND_UNCERTAIN')
                    THEN recruitment_notifications.body
                    ELSE excluded.body
                END,
                status=CASE
                    WHEN recruitment_notifications.status IN ('SENT','SEND_UNCERTAIN')
                    THEN recruitment_notifications.status
                    WHEN recruitment_notifications.status='SENDING'
                    THEN recruitment_notifications.status
                    ELSE excluded.status
                END,
                error=CASE
                    WHEN recruitment_notifications.status IN ('SENT','SEND_UNCERTAIN','SENDING')
                    THEN recruitment_notifications.error
                    ELSE excluded.error
                END,
                updated_at=excluded.updated_at
            """,
            (
                unique_key,
                application_id,
                _clean_role(job_title),
                channel,
                message_type,
                str(recipient or "")[:500] or None,
                str(sender or "")[:500] or None,
                str(subject or "")[:500] or None,
                str(body or "")[:20000],
                initial_status,
                0,
                str(error or "")[:2000] or None,
                ts,
                ts,
            ),
        )
    return _notification(unique_key)





def _next_working_day_label():
    """HR interview scheduling rule.

    Mon-Wed approval -> next day. Thu-Fri approval -> Monday.
    Weekend approval -> Monday.
    """
    today = datetime.now().astimezone().date()
    weekday = today.weekday()
    if weekday <= 2:
        day = today + timedelta(days=1)
    else:
        days_until_monday = (7 - weekday) % 7
        if days_until_monday == 0:
            days_until_monday = 7
        day = today + timedelta(days=days_until_monday)
    return day.strftime("%d %b %Y")


def interview_schedule_preview():
    return {
        "interview_date": _next_working_day_label(),
        "rule": "Mon-Wed approval → next day; Thu-Fri approval → Monday",
    }



def _interview_email_already_reserved(email, job_title, application_id=None):
    """
    Stage-2 duplicate guard.

    A duplicate Indeed application for the same person + same role must never
    produce another interview email. Different roles remain independent.
    """
    recipient = str(email or "").strip().lower()
    title = _clean_role(job_title).lower()

    if not recipient or not title:
        return False

    with conn() as c:
        rows = c.execute(
            """
            SELECT application_id, recipient, job_title, status
            FROM recruitment_notifications
            WHERE message_type='INTERVIEW_EMAIL'
              AND status IN (
                'QUEUED','SENDING','SENT','SEND_UNCERTAIN',
                'WAITING_CONFIG','SEND_FAILED','DUPLICATE_SKIPPED'
              )
            """
        ).fetchall()

    for row in rows:
        if application_id is not None and int(row["application_id"] or 0) == int(application_id):
            continue
        if str(row["recipient"] or "").strip().lower() != recipient:
            continue
        if _clean_role(row["job_title"]).lower() != title:
            continue
        return True

    return False



def approve_candidate_for_interview(application_id):
    init_recruitment_pipeline_db()
    app = get_by_id(int(application_id))
    if not app:
        raise ValueError("Candidate application was not found.")

    status = re.sub(r"\s+", " ", str(app.get("indeed_status") or "")).strip().lower()
    if status in TERMINAL_CANDIDATE_STATES:
        raise ValueError(f"Candidate is already in terminal Indeed status: {app.get('indeed_status')}.")

    if not role_accepts_new_applications(app.get("job_title")):
        raise ValueError("This role is paused/closed on Indeed, so interview outreach is stopped.")

    with conn() as c:
        review = c.execute(
            """
            SELECT analysis_status, rank_position, match_score
            FROM candidate_reviews
            WHERE application_id=?
            LIMIT 1
            """,
            (int(application_id),),
        ).fetchone()

    if not review or review["analysis_status"] != "READY" or review["rank_position"] is None:
        raise ValueError(
            "Candidate ranking is not complete yet. HR approval is available after the role/resume ranking is ready."
        )

    interview_date = _next_working_day_label()
    ts = now()
    settings = load_settings()

    with conn() as c:
        c.execute(
            """
            INSERT INTO recruitment_candidate_flow(
                application_id, hr_status, hr_approved_at, interview_date, updated_at
            ) VALUES (?, 'APPROVED', ?, ?, ?)
            ON CONFLICT(application_id) DO UPDATE SET
                hr_status='APPROVED',
                hr_approved_at=COALESCE(recruitment_candidate_flow.hr_approved_at, excluded.hr_approved_at),
                interview_date=excluded.interview_date,
                updated_at=excluded.updated_at
            """,
            (int(application_id), ts, interview_date, ts),
        )

    candidate = app.get("candidate_name") or "Candidate"
    role = app.get("job_title") or "the position"
    company = settings.get("company_name") or "Nunes Instruments"

    subject_template = settings.get("interview_subject_template") or DEFAULT_INTERVIEW_SUBJECT_TEMPLATE
    body_template = settings.get("interview_body_template") or DEFAULT_INTERVIEW_BODY_TEMPLATE

    subject = subject_template.format(
        candidate_name=candidate,
        job_title=role,
        interview_date=interview_date,
        company_name=company,
    )
    body = body_template.format(
        candidate_name=candidate,
        job_title=role,
        interview_date=interview_date,
        company_name=company,
    )

    email = str(app.get("candidate_email") or "").strip().lower()

    duplicate_interview = (
        bool(email)
        and _interview_email_already_reserved(
            email,
            role,
            application_id=int(application_id),
        )
    )

    if duplicate_interview:
        email_status = "DUPLICATE_SKIPPED"
        email_error = (
            "Interview email already exists for this candidate email and role."
        )
    else:
        email_status = "QUEUED" if email else "SKIPPED_NO_EMAIL"
        email_error = None if email else "Candidate email is not available."

    queue_notification(
        f"INTERVIEW_EMAIL:{application_id}",
        channel="EMAIL_CANDIDATE",
        message_type="INTERVIEW_EMAIL",
        application_id=int(application_id),
        job_title=role,
        recipient=email,
        sender=settings.get("company_email") or "nuneslead@gmail.com",
        subject=subject,
        body=body,
        initial_status=email_status,
        error=email_error,
    )

    log("INFO", f"HR APPROVAL: {candidate} approved for interview on {interview_date} for {role}; email-only stage 2 queued/deduped.")
    return candidate_flow(application_id)


def approve_top_candidates_for_interview(job_title, count):
    """Approve the top N ranked active candidates after an explicit HR action."""
    init_recruitment_pipeline_db()
    title = _clean_role(job_title)
    if not title:
        raise ValueError("job_title is required.")
    try:
        requested = int(count)
    except Exception:
        raise ValueError("Interview count must be a whole number.")
    if requested < 1 or requested > 200:
        raise ValueError("Interview count must be between 1 and 200.")
    if not role_accepts_new_applications(title):
        raise ValueError("This role is paused/closed on Indeed, so interview outreach is stopped.")

    terminal = tuple(sorted(TERMINAL_CANDIDATE_STATES))
    placeholders = ",".join("?" for _ in terminal)
    with conn() as c:
        rows = c.execute(
            f"""
            SELECT a.id, a.candidate_name, cr.rank_position, cr.match_score,
                   COALESCE(cf.hr_status,'PENDING') AS hr_status
            FROM applications a
            JOIN candidate_reviews cr ON cr.application_id=a.id
            LEFT JOIN recruitment_candidate_flow cf ON cf.application_id=a.id
            WHERE lower(trim(a.job_title))=lower(trim(?))
              AND cr.analysis_status='READY'
              AND cr.rank_position IS NOT NULL
              AND lower(trim(COALESCE(a.indeed_status,''))) NOT IN ({placeholders})
            ORDER BY cr.rank_position ASC, cr.match_score DESC, a.id ASC
            LIMIT ?
            """,
            [title, *terminal, requested],
        ).fetchall()

    if not rows:
        raise ValueError("No ranked active candidates are ready for HR interview approval yet.")

    selected = [dict(row) for row in rows]
    newly_approved = 0
    already_approved = 0
    failures = []
    interview_date = _next_working_day_label()

    for row in selected:
        if str(row.get("hr_status") or "").upper() == "APPROVED":
            already_approved += 1
            continue
        try:
            approve_candidate_for_interview(int(row["id"]))
            newly_approved += 1
        except Exception as exc:
            failures.append({
                "application_id": int(row["id"]),
                "candidate_name": row.get("candidate_name"),
                "error": str(exc),
            })

    log(
        "INFO",
        f"HR BATCH APPROVAL: {title} top {requested}; selected={len(selected)}, "
        f"new={newly_approved}, existing={already_approved}, failures={len(failures)}, "
        f"interview={interview_date}.",
    )
    return {
        "job_title": title,
        "requested_count": requested,
        "selected_count": len(selected),
        "newly_approved": newly_approved,
        "already_approved": already_approved,
        "interview_date": interview_date,
        "failures": failures,
        "selected": selected,
    }


def candidate_flow(application_id):
    init_recruitment_pipeline_db()
    with conn() as c:
        flow = c.execute(
            "SELECT * FROM recruitment_candidate_flow WHERE application_id=?",
            (int(application_id),),
        ).fetchone()
        notifications = c.execute(
            """
            SELECT message_type, channel, status, error, sent_at, updated_at
            FROM recruitment_notifications
            WHERE application_id=?
            ORDER BY id ASC
            """,
            (int(application_id),),
        ).fetchall()

    return {
        "flow": dict(flow) if flow else {
            "application_id": int(application_id),
            "hr_status": "PENDING",
            "hr_approved_at": None,
            "interview_date": None,
        },
        "notifications": [dict(x) for x in notifications],
    }


def _ranking_rows(job_title):
    title = _clean_role(job_title)
    with conn() as c:
        rows = c.execute(
            """
            SELECT
                a.id, a.candidate_name, a.candidate_email, a.candidate_phone,
                a.indeed_status, a.send_status,
                cr.analysis_status, cr.rank_position, cr.match_score,
                cr.requirements_evidenced, cr.requirements_total,
                cr.auto_bucket
            FROM applications a
            LEFT JOIN candidate_reviews cr ON cr.application_id=a.id
            WHERE lower(trim(a.job_title))=lower(trim(?))
              AND lower(trim(COALESCE(a.indeed_status,''))) NOT IN (
                  'hired','selected','not selected','rejected','withdrawn','archived'
              )
            ORDER BY
                CASE WHEN cr.analysis_status='READY' THEN 0 ELSE 1 END,
                COALESCE(cr.rank_position,999999) ASC,
                a.id ASC
            """,
            (title,),
        ).fetchall()
    return [dict(r) for r in rows]


def _report_snapshot(job_title):
    title = _clean_role(job_title)
    state = get_role_state(title) or ensure_role_state(title)
    rows = _ranking_rows(title)

    serial = {
        "job_title": title,
        "lifecycle_status": (state or {}).get("lifecycle_status", "UNKNOWN"),
        "rows": [
            {
                "id": r.get("id"),
                "rank": r.get("rank_position"),
                "score": round(float(r.get("match_score") or 0), 2),
                "analysis": r.get("analysis_status"),
                "status": r.get("indeed_status"),
                "send_status": r.get("send_status"),
            }
            for r in rows
        ],
    }
    digest = hashlib.sha256(
        json.dumps(serial, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return state or {}, rows, digest


def _build_role_report(job_title, state, rows):
    title = _clean_role(job_title)
    lifecycle = (state.get("lifecycle_status") or "UNKNOWN").upper()
    subject = (
        f"Recruitment Ranking CLOSED – {title}"
        if lifecycle == "CLOSED"
        else f"Recruitment Ranking – {title}"
    )

    ready = [r for r in rows if r.get("analysis_status") == "READY"]
    waiting = [r for r in rows if r.get("analysis_status") != "READY"]

    lines = [
        f"Role: {title}",
        f"Role status: {lifecycle}",
        f"Updated: {datetime.now().astimezone().strftime('%d %b %Y, %I:%M %p')}",
        "",
        f"Ranked applicants: {len(ready)}",
        f"Waiting for complete ranking data: {len(waiting)}",
        "",
        "CURRENT RANKING",
        "---------------",
    ]

    if not ready:
        lines.append("No completed rankings yet.")
    else:
        for row in ready:
            lines.append(
                f"#{row.get('rank_position') or '-'} | "
                f"{row.get('candidate_name') or 'Candidate'} | "
                f"{float(row.get('match_score') or 0):.1f}% | "
                f"{row.get('candidate_email') or 'no email'} | "
                f"{row.get('candidate_phone') or 'no phone'} | "
                f"Evidence {row.get('requirements_evidenced') or 0}/{row.get('requirements_total') or 0} | "
                f"Indeed {row.get('indeed_status') or 'active'}"
            )

    if waiting:
        lines.extend(["", "WAITING / INCOMPLETE", "--------------------"])
        for row in waiting:
            lines.append(
                f"- {row.get('candidate_name') or 'Candidate'} | "
                f"{row.get('analysis_status') or 'PENDING'} | "
                f"{row.get('candidate_email') or 'no email'}"
            )

    lines.extend([
        "",
        "Ranking is an HR review aid based on role requirements and resume evidence. "
        "HR approval remains the gate before an interview message is sent.",
    ])
    return subject, "\n".join(lines)


def _daily_report_clock(settings):
    value = str(settings.get("daily_report_time") or "19:00").strip()
    match = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", value)
    if not match:
        return 19, 0, "19:00"
    hour = int(match.group(1))
    minute = int(match.group(2))
    return hour, minute, f"{hour:02d}:{minute:02d}"


def _daily_report_roles():
    """Return every ongoing role currently known to the Indeed workflow.

    Open roles are included even when they received zero applications that day,
    so the single EOD email is also a complete snapshot of current job openings.
    """
    with conn() as c:
        rows = c.execute(
            """
            SELECT job_title, lifecycle_status
            FROM (
                SELECT rrs.job_title AS job_title,
                       COALESCE(rrs.lifecycle_status, 'UNKNOWN') AS lifecycle_status
                FROM recruitment_role_state rrs
                WHERE upper(trim(COALESCE(rrs.lifecycle_status,'UNKNOWN'))) NOT IN ('PAUSED','CLOSED')

                UNION

                SELECT rp.job_title AS job_title,
                       COALESCE(rrs.lifecycle_status, 'UNKNOWN') AS lifecycle_status
                FROM role_profiles rp
                LEFT JOIN recruitment_role_state rrs
                  ON lower(trim(rrs.job_title))=lower(trim(rp.job_title))
                WHERE upper(trim(COALESCE(rrs.lifecycle_status,'UNKNOWN'))) NOT IN ('PAUSED','CLOSED')

                UNION

                SELECT DISTINCT a.job_title AS job_title,
                       COALESCE(rrs.lifecycle_status, 'UNKNOWN') AS lifecycle_status
                FROM applications a
                LEFT JOIN recruitment_role_state rrs
                  ON lower(trim(rrs.job_title))=lower(trim(a.job_title))
                WHERE upper(trim(COALESCE(rrs.lifecycle_status,'UNKNOWN'))) NOT IN ('PAUSED','CLOSED')
            )
            WHERE lower(trim(COALESCE(job_title,''))) NOT IN
                  ('','the position','position','job','the job','unknown')
            ORDER BY lower(job_title)
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _daily_role_candidates(job_title):
    with conn() as c:
        rows = c.execute(
            """
            SELECT
                a.id, a.candidate_name, a.candidate_email, a.candidate_phone,
                a.indeed_status, a.first_seen_at, a.send_status,
                cr.analysis_status, cr.rank_position, cr.match_score,
                COALESCE(cf.hr_status,'PENDING') AS hr_status,
                cf.interview_date,
                MAX(CASE WHEN rn.message_type='INTERVIEW_EMAIL' THEN rn.status END) AS interview_email_status
            FROM applications a
            LEFT JOIN candidate_reviews cr ON cr.application_id=a.id
            LEFT JOIN recruitment_candidate_flow cf ON cf.application_id=a.id
            LEFT JOIN recruitment_notifications rn ON rn.application_id=a.id
            WHERE lower(trim(a.job_title))=lower(trim(?))
              AND lower(trim(COALESCE(a.indeed_status,''))) NOT IN
                  ('hired','selected','not selected','rejected','withdrawn','archived')
            GROUP BY a.id
            ORDER BY
                CASE WHEN cr.rank_position IS NULL THEN 1 ELSE 0 END,
                cr.rank_position ASC,
                cr.match_score DESC,
                COALESCE(a.first_seen_at, a.created_at) ASC,
                a.id ASC
            """,
            (job_title,),
        ).fetchall()
    return [dict(row) for row in rows]


def _build_daily_consolidated_report(report_date=None):
    local_now = datetime.now().astimezone()
    report_date = report_date or local_now.date().isoformat()
    roles = _daily_report_roles()

    lines = [
        "NUNES RECRUITMENT – END OF DAY REPORT",
        "=====================================",
        f"Date: {local_now.strftime('%d %b %Y')}",
        f"Ongoing roles: {len(roles)}",
        "",
        "This is one consolidated report. Each active Indeed role is listed separately below.",
    ]

    total_candidates = 0
    total_ranked = 0
    total_hr_approved = 0
    total_stage2_pending = 0

    for index, role in enumerate(roles, start=1):
        title = role.get("job_title") or "Role"
        candidates = _daily_role_candidates(title)
        total_candidates += len(candidates)
        ranked = sum(1 for row in candidates if row.get("rank_position") is not None)
        approved = sum(1 for row in candidates if str(row.get("hr_status") or "").upper() == "APPROVED")
        stage2_pending = sum(
            1 for row in candidates
            if str(row.get("hr_status") or "").upper() != "APPROVED"
            or (
                row.get("interview_email_status") not in {"SENT", "SKIPPED_NO_EMAIL"}
                and row.get("candidate_email")
            )
        )
        total_ranked += ranked
        total_hr_approved += approved
        total_stage2_pending += stage2_pending

        lines.extend([
            "",
            f"ROLE {index}: {title}",
            "-" * min(70, max(12, len(title) + 9)),
            f"Active candidates: {len(candidates)} | Ranked: {ranked} | HR approved: {approved}",
        ])

        if not candidates:
            lines.append("No active candidates for this role.")
            continue

        for row in candidates:
            rank = row.get("rank_position")
            score = row.get("match_score")
            score_text = f"{float(score):.1f}%" if score is not None else "waiting"
            email_ack = str(row.get("send_status") or "NOT SENT").upper()
            hr = str(row.get("hr_status") or "PENDING").upper()
            i_email = str(row.get("interview_email_status") or ("NOT SENT" if row.get("candidate_email") else "NO EMAIL")).upper()
            applied = str(row.get("first_seen_at") or "-")
            lines.extend([
                f"#{rank if rank is not None else '-'} | {row.get('candidate_name') or 'Candidate'} | Match {score_text}",
                f"  Contact: {row.get('candidate_email') or 'no email'} | {row.get('candidate_phone') or 'no phone'}",
                f"  Applied: {applied} | Indeed: {row.get('indeed_status') or 'active'}",
                f"  Stage 1 thank-you email: {email_ack}",
                f"  HR: {hr} | Interview date: {row.get('interview_date') or '-'}",
                f"  Stage 2 interview email: {i_email}",
            ])

    lines.extend([
        "",
        "DAILY TOTALS",
        "------------",
        f"Ongoing roles: {len(roles)}",
        f"Active candidates: {total_candidates}",
        f"Ranked candidates: {total_ranked}",
        f"HR approved: {total_hr_approved}",
        f"Candidates still requiring/finishing Stage 2 action: {total_stage2_pending}",
        "",
        "Ranking is job-requirement/resume evidence support for HR. HR approval remains the interview gate.",
    ])

    subject = f"Daily Recruitment Report – {local_now.strftime('%d %b %Y')} – {len(roles)} ongoing role(s)"
    return subject, "\n".join(lines), roles


def queue_daily_consolidated_report(force=False):
    """Queue exactly one end-of-day email containing every ongoing role.

    The report uses the Windows local clock. Default send time is 19:00 and is
    editable in Settings. If the service starts after the configured time, that
    day's report is queued immediately as long as it has not already been queued.
    """
    init_recruitment_pipeline_db()
    settings = load_settings()
    if not settings.get("daily_consolidated_report_enabled", True) and not force:
        return {"queued": False, "reason": "disabled"}

    local_now = datetime.now().astimezone()
    date_key = local_now.date().isoformat()
    hour, minute, label = _daily_report_clock(settings)
    due = (local_now.hour, local_now.minute) >= (hour, minute)
    if not force and not due:
        return {"queued": False, "reason": "not_due", "time": label}

    if not force and get_state("daily_report_last_queued_date") == date_key:
        return {"queued": False, "reason": "already_queued", "date": date_key}

    subject, body, roles = _build_daily_consolidated_report(date_key)
    if not roles:
        return {"queued": False, "reason": "no_ongoing_roles", "date": date_key}

    recipient = (settings.get("hr_report_recipient") or HR_REPORT_SENDER).strip()
    key = f"DAILY_CONSOLIDATED_REPORT:{date_key}"
    existing = _notification(key)
    queue_notification(
        key,
        channel="EMAIL_REPORT",
        message_type="DAILY_CONSOLIDATED_REPORT",
        job_title="ALL ONGOING ROLES",
        recipient=recipient,
        sender=HR_REPORT_SENDER,
        subject=subject,
        body=body,
    )
    set_state("daily_report_last_queued_date", date_key)
    set_state("daily_report_last_queued_at", now())
    return {
        "queued": existing is None,
        "already_exists": existing is not None,
        "date": date_key,
        "roles": len(roles),
        "recipient": recipient,
        "subject": subject,
    }


def daily_report_status():
    settings = load_settings()
    _, _, label = _daily_report_clock(settings)
    return {
        "enabled": bool(settings.get("daily_consolidated_report_enabled", True)),
        "time": label,
        "sender": HR_REPORT_SENDER,
        "recipient": settings.get("hr_report_recipient") or HR_REPORT_SENDER,
        "last_queued_date": get_state("daily_report_last_queued_date"),
        "last_queued_at": get_state("daily_report_last_queued_at"),
        "last_sent_date": get_state("daily_report_last_sent_date"),
        "last_sent_at": get_state("daily_report_last_sent_at"),
        "last_error": get_state("daily_report_last_error"),
    }


def queue_changed_role_reports():
    init_recruitment_pipeline_db()
    settings = load_settings()
    if not settings.get("auto_send_role_reports", True):
        return 0

    # Seed role-state rows for every role already present in applications.
    with conn() as c:
        titles = [
            r["job_title"]
            for r in c.execute(
                """
                SELECT DISTINCT job_title FROM applications
                WHERE lower(trim(COALESCE(job_title,''))) NOT IN
                      ('','the position','position','job','the job','unknown')
                """
            ).fetchall()
        ]

    queued = 0
    for title in titles:
        state = get_role_state(title) or ensure_role_state(title)
        lifecycle = (state.get("lifecycle_status") or "UNKNOWN").upper()
        if lifecycle in INACTIVE_ROLE_STATES:
            continue

        state, rows, digest = _report_snapshot(title)
        if not rows:
            continue
        if lifecycle != "CLOSED" and not any(r.get("analysis_status") == "READY" for r in rows):
            continue

        current_hash = state.get("ranking_hash")
        if current_hash == digest:
            continue

        subject, body = _build_role_report(title, state, rows)
        recipient = (settings.get("hr_report_recipient") or HR_REPORT_SENDER).strip()
        key = f"ROLE_REPORT:{title.lower()}:{digest}"

        # Only the newest unsent ranking snapshot should be delivered. Older
        # queued snapshots are retained in history but marked superseded.
        with conn() as c:
            c.execute(
                """
                UPDATE recruitment_notifications
                SET status='SUPERSEDED', updated_at=?
                WHERE lower(job_title)=lower(?)
                  AND message_type='ROLE_RANKING_REPORT'
                  AND status IN ('QUEUED','WAITING_CONFIG','SEND_FAILED')
                  AND unique_key<>?
                """,
                (now(), title, key),
            )

        queue_notification(
            key,
            channel="EMAIL_REPORT",
            message_type="ROLE_RANKING_REPORT",
            job_title=title,
            recipient=recipient,
            sender=HR_REPORT_SENDER,
            subject=subject,
            body=body,
        )
        with conn() as c:
            c.execute(
                """
                UPDATE recruitment_role_state
                SET ranking_hash=?, report_status='QUEUED', report_error=NULL, updated_at=?
                WHERE lower(job_title)=lower(?)
                """,
                (digest, now(), title),
            )
        queued += 1
    return queued


def hr_report_mail_health_check():
    settings = load_settings()
    password = re.sub(r"\s+", "", str(settings.get("hr_report_smtp_app_password") or ""))
    if not password:
        set_state("hr_report_smtp_verified", "0")
        return {"ok": False, "message": f"Add the Google App Password for {HR_REPORT_SENDER}."}

    host = str(settings.get("smtp_host") or "smtp.gmail.com").strip()
    ports = [("starttls", int(settings.get("smtp_port") or 587)), ("ssl", int(settings.get("smtp_ssl_fallback_port") or 465))]
    context = ssl.create_default_context()
    errors = []
    for mode, port in ports:
        smtp = None
        try:
            if mode == "ssl":
                smtp = smtplib.SMTP_SSL(host, port, timeout=20, context=context)
                smtp.ehlo()
            else:
                smtp = smtplib.SMTP(host, port, timeout=20)
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(HR_REPORT_SENDER, password)
            try:
                smtp.quit()
            except Exception:
                pass
            set_state("hr_report_smtp_verified", "1")
            set_state("hr_report_smtp_last_error", "")
            return {"ok": True, "message": f"{HR_REPORT_SENDER} verified via {mode}:{port}."}
        except Exception as exc:
            errors.append(str(exc))
            try:
                if smtp:
                    smtp.quit()
            except Exception:
                pass

    error = errors[-1] if errors else "HR Gmail verification failed."
    set_state("hr_report_smtp_verified", "0")
    set_state("hr_report_smtp_last_error", error)
    return {"ok": False, "message": error}


def _report_smtp_send(to_addr, subject, body, settings):
    password = re.sub(r"\s+", "", str(settings.get("hr_report_smtp_app_password") or ""))
    if not password:
        raise RuntimeError(
            f"HR ranking Gmail App Password is not configured for {HR_REPORT_SENDER}."
        )

    host = str(settings.get("smtp_host") or "smtp.gmail.com").strip()
    preferred_port = int(settings.get("smtp_port") or 587)
    fallback_port = int(settings.get("smtp_ssl_fallback_port") or 465)
    context = ssl.create_default_context()

    message = EmailMessage()
    message["From"] = HR_REPORT_SENDER
    message["To"] = to_addr
    message["Subject"] = subject
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain="gmail.com")
    message.set_content(body)

    errors = []
    for mode, port in [("starttls", preferred_port), ("ssl", fallback_port)]:
        smtp = None
        try:
            if mode == "ssl":
                smtp = smtplib.SMTP_SSL(host, port, timeout=25, context=context)
                smtp.ehlo()
            else:
                smtp = smtplib.SMTP(host, port, timeout=25)
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            smtp.login(HR_REPORT_SENDER, password)
            smtp.send_message(message)
            try:
                smtp.quit()
            except Exception:
                pass
            set_state("hr_report_smtp_verified", "1")
            set_state("hr_report_smtp_last_error", "")
            return f"{mode}:{port}"
        except Exception as exc:
            errors.append(str(exc))
            try:
                if smtp:
                    smtp.quit()
            except Exception:
                pass

    set_state("hr_report_smtp_verified", "0")
    set_state("hr_report_smtp_last_error", errors[-1] if errors else "SMTP failed")
    raise RuntimeError(errors[-1] if errors else "HR ranking email could not be sent.")


def _claim_notification(notification_id):
    with conn() as c:
        row = c.execute(
            "SELECT * FROM recruitment_notifications WHERE id=?",
            (int(notification_id),),
        ).fetchone()
        if not row:
            return None
        if row["status"] not in {"QUEUED", "SEND_FAILED", "WAITING_LOGIN"}:
            return None
        c.execute(
            """
            UPDATE recruitment_notifications
            SET status='SENDING', attempts=attempts+1, error=NULL, updated_at=?
            WHERE id=?
            """,
            (now(), int(notification_id)),
        )
        return dict(row)


def _finish_notification(notification_id, status, error=None):
    with conn() as c:
        c.execute(
            """
            UPDATE recruitment_notifications
            SET status=?, error=?, sent_at=CASE WHEN ?='SENT' THEN ? ELSE sent_at END,
                updated_at=?
            WHERE id=?
            """,
            (
                status,
                str(error or "")[:2000] or None,
                status,
                now(),
                now(),
                int(notification_id),
            ),
        )


def resume_config_waiting_notifications():
    """Release notifications that were waiting only for a configured mail credential."""
    settings = load_settings()
    hr_ready = bool(re.sub(r"\s+", "", str(settings.get("hr_report_smtp_app_password") or "")))
    candidate_ready = bool(re.sub(r"\s+", "", str(settings.get("smtp_app_password") or "")))
    with conn() as c:
        if hr_ready:
            c.execute(
                """
                UPDATE recruitment_notifications
                SET status='QUEUED', error=NULL, updated_at=?
                WHERE status='WAITING_CONFIG' AND channel='EMAIL_REPORT'
                """,
                (now(),),
            )
        if candidate_ready:
            c.execute(
                """
                UPDATE recruitment_notifications
                SET status='QUEUED', error=NULL, updated_at=?
                WHERE status='WAITING_CONFIG' AND channel='EMAIL_CANDIDATE'
                """,
                (now(),),
            )


def process_notifications_once(limit=12):
    init_recruitment_pipeline_db()
    settings = load_settings()
    if not settings.get("automation_enabled", True):
        return {"attempted": 0, "sent": 0}

    with conn() as c:
        rows = c.execute(
            """
            SELECT * FROM recruitment_notifications
            WHERE channel IN ('EMAIL_CANDIDATE','EMAIL_REPORT')
              AND status IN ('QUEUED','SEND_FAILED')
            ORDER BY
                CASE channel
                    WHEN 'EMAIL_CANDIDATE' THEN 0
                    WHEN 'EMAIL_REPORT' THEN 1
                    ELSE 9
                END,
                CASE message_type
                    WHEN 'INTERVIEW_EMAIL' THEN 0
                    WHEN 'DAILY_CONSOLIDATED_REPORT' THEN 1
                    WHEN 'ROLE_RANKING_REPORT' THEN 2
                    ELSE 9
                END,
                id ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()


    attempted = 0
    sent = 0
    for raw in rows:
        claimed = _claim_notification(raw["id"])
        if not claimed:
            continue
        attempted += 1
        try:
            channel = claimed.get("channel")
            if channel == "EMAIL_REPORT":
                if not re.sub(r"\s+", "", str(settings.get("hr_report_smtp_app_password") or "")):
                    _finish_notification(claimed["id"], "WAITING_CONFIG", f"Add the Google App Password for {HR_REPORT_SENDER} in Settings.")
                    if claimed.get("message_type") == "ROLE_RANKING_REPORT":
                        with conn() as c:
                            c.execute(
                                """
                                UPDATE recruitment_role_state
                                SET report_status='WAITING_CONFIG', report_error=?, updated_at=?
                                WHERE lower(job_title)=lower(?)
                                """,
                                (f"Add the Google App Password for {HR_REPORT_SENDER} in Settings.", now(), claimed.get("job_title") or ""),
                            )
                    elif claimed.get("message_type") == "DAILY_CONSOLIDATED_REPORT":
                        set_state("daily_report_last_error", f"Add the Google App Password for {HR_REPORT_SENDER} in Settings.")
                    continue
                transport = _report_smtp_send(
                    claimed.get("recipient") or HR_REPORT_SENDER,
                    claimed.get("subject") or "Recruitment ranking",
                    claimed.get("body") or "",
                    settings,
                )
                _finish_notification(claimed["id"], "SENT")
                if claimed.get("message_type") == "ROLE_RANKING_REPORT":
                    with conn() as c:
                        c.execute(
                            """
                            UPDATE recruitment_role_state
                            SET report_status='SENT', report_sent_at=?, report_error=NULL, updated_at=?
                            WHERE lower(job_title)=lower(?)
                            """,
                            (now(), now(), claimed.get("job_title") or ""),
                        )
                    log("INFO", f"ROLE REPORT: sent {claimed.get('job_title')} using {HR_REPORT_SENDER} ({transport}).")
                elif claimed.get("message_type") == "DAILY_CONSOLIDATED_REPORT":
                    local_date = datetime.now().astimezone().date().isoformat()
                    set_state("daily_report_last_sent_date", local_date)
                    set_state("daily_report_last_sent_at", now())
                    set_state("daily_report_last_error", "")
                    log("INFO", f"DAILY RECRUITMENT REPORT: sent consolidated ongoing-role report using {HR_REPORT_SENDER} ({transport}).")
                sent += 1

            elif channel == "EMAIL_CANDIDATE":
                if not re.sub(r"\s+", "", str(settings.get("smtp_app_password") or "")):
                    _finish_notification(claimed["id"], "WAITING_CONFIG", "Candidate Gmail App Password is not configured.")
                    continue
                # Lazy import avoids a module cycle during startup.
                from automation import _smtp_send, valid_candidate_email

                recipient = (claimed.get("recipient") or "").strip().lower()
                if not valid_candidate_email(recipient, settings):
                    raise RuntimeError("Candidate email is not valid for interview delivery.")
                _smtp_send(
                    recipient,
                    claimed.get("subject") or "Interview invitation",
                    claimed.get("body") or "",
                    settings,
                )
                _finish_notification(claimed["id"], "SENT")
                log("INFO", f"INTERVIEW EMAIL: sent to {recipient}.")
                sent += 1

            else:
                raise RuntimeError(f"Unsupported notification channel: {channel}")

        except Exception as exc:
            _finish_notification(claimed["id"], "SEND_FAILED", str(exc))
            if claimed.get("channel") == "EMAIL_REPORT":
                if claimed.get("message_type") == "ROLE_RANKING_REPORT":
                    with conn() as c:
                        c.execute(
                            """
                            UPDATE recruitment_role_state
                            SET report_status='SEND_FAILED', report_error=?, updated_at=?
                            WHERE lower(job_title)=lower(?)
                            """,
                            (str(exc)[:2000], now(), claimed.get("job_title") or ""),
                        )
                elif claimed.get("message_type") == "DAILY_CONSOLIDATED_REPORT":
                    set_state("daily_report_last_error", str(exc)[:2000])
            log("WARN", f"Recruitment notification waiting: {exc}")

    return {"attempted": attempted, "sent": sent}


def _notification_map(application_id):
    with conn() as c:
        rows = c.execute(
            """
            SELECT message_type, status, error, sent_at
            FROM recruitment_notifications
            WHERE application_id=?
            """,
            (int(application_id),),
        ).fetchall()
    return {r["message_type"]: dict(r) for r in rows}


def decorate_role_payload(payload):
    init_recruitment_pipeline_db()
    out = dict(payload or {})
    role = dict(out.get("role") or {})
    title = _clean_role(role.get("job_title"))
    state = get_role_state(title) or ensure_role_state(title)
    role["lifecycle"] = state or {"lifecycle_status": "UNKNOWN"}
    out["role"] = role
    out["interview_schedule"] = interview_schedule_preview()

    decorated = []
    for item in out.get("applicants") or []:
        row = dict(item)
        flow = candidate_flow(row["id"])
        row["hr_flow"] = flow.get("flow")
        row["notifications"] = flow.get("notifications")
        decorated.append(row)

    by_id = {x["id"]: x for x in decorated}
    out["applicants"] = decorated
    for key in ("active_applicants", "auto_shortlist", "remaining", "removed"):
        out[key] = [by_id.get(x["id"], x) for x in (out.get(key) or [])]

    return out


def decorate_roles(roles):
    result = []
    for role in roles or []:
        item = dict(role)
        state = get_role_state(item.get("job_title")) or ensure_role_state(item.get("job_title"))
        lifecycle = (state or {}).get("lifecycle_status", "UNKNOWN")
        if str(lifecycle or "").upper() == "CLOSED":
            continue
        item["lifecycle_status"] = lifecycle
        item["report_status"] = (state or {}).get("report_status", "IDLE")
        item["report_sent_at"] = (state or {}).get("report_sent_at")
        item["report_error"] = (state or {}).get("report_error")
        result.append(item)
    return result



def operations_overview(limit_roles=6, limit_recent=8):
    """Lightweight, read-only data for the operations dashboard.

    This keeps the Next.js overview fast: one backend request returns role-level
    pipeline counts plus the latest applicants instead of issuing one heavy role
    payload request per role.
    """
    init_recruitment_pipeline_db()
    role_limit = max(1, min(int(limit_roles or 6), 25))
    recent_limit = max(1, min(int(limit_recent or 8), 50))

    terminal_sql = """
        lower(trim(COALESCE(a.indeed_status,''))) IN
        ('hired','selected','not selected','rejected','withdrawn','archived')
    """

    with conn() as c:
        roles = c.execute(
            f"""
            SELECT
                rrs.job_title,
                COALESCE(rp.description_status, 'WAITING') AS description_status,
                rrs.lifecycle_status AS lifecycle_status,
                COALESCE(rrs.report_status, 'IDLE') AS report_status,
                COALESCE(rrs.candidate_total_hint, 0) AS candidate_total_hint,
                COALESCE(rrs.candidate_new_hint, 0) AS candidate_new_hint,
                COUNT(a.id) AS applicant_count,
                SUM(CASE WHEN lower(trim(COALESCE(a.indeed_status,'')))='new'
                         THEN 1 ELSE 0 END) AS actual_new_count,
                SUM(CASE WHEN {terminal_sql} THEN 0 ELSE 1 END) AS active_applicant_count,
                SUM(CASE WHEN NOT ({terminal_sql})
                              AND upper(trim(COALESCE(a.send_status,'')))='SENT'
                         THEN 1 ELSE 0 END) AS acknowledged_count,
                SUM(CASE WHEN NOT ({terminal_sql})
                              AND cr.analysis_status='READY'
                         THEN 1 ELSE 0 END) AS ranked_count,
                SUM(CASE WHEN NOT ({terminal_sql})
                              AND cr.analysis_status='READY'
                              AND upper(trim(COALESCE(cf.hr_status,'PENDING'))) <> 'APPROVED'
                         THEN 1 ELSE 0 END) AS hr_review_count,
                SUM(CASE WHEN NOT ({terminal_sql})
                              AND upper(trim(COALESCE(cf.hr_status,'')))='APPROVED'
                         THEN 1 ELSE 0 END) AS interview_count,
                SUM(CASE WHEN {terminal_sql} THEN 1 ELSE 0 END) AS completed_count,
                SUM(CASE WHEN NOT ({terminal_sql})
                              AND COALESCE(cr.analysis_status,'PENDING') <> 'READY'
                         THEN 1 ELSE 0 END) AS waiting_count,
                SUM(CASE WHEN NOT ({terminal_sql})
                              AND COALESCE(cr.auto_shortlisted,0)=1
                         THEN 1 ELSE 0 END) AS top_match_count
            FROM recruitment_role_state rrs
            LEFT JOIN role_profiles rp
              ON lower(trim(rp.job_title)) = lower(trim(rrs.job_title))
            LEFT JOIN applications a
              ON lower(trim(a.job_title)) = lower(trim(rrs.job_title))
            LEFT JOIN candidate_reviews cr
              ON cr.application_id = a.id
            LEFT JOIN recruitment_candidate_flow cf
              ON cf.application_id = a.id
            WHERE upper(trim(COALESCE(rrs.lifecycle_status,'UNKNOWN')))
                  IN ('OPEN','PAUSED','FLAGGED')
            GROUP BY
                rrs.job_title,
                rp.description_status,
                rrs.lifecycle_status,
                rrs.report_status,
                rrs.candidate_total_hint,
                rrs.candidate_new_hint
            ORDER BY
                CASE COALESCE(rrs.lifecycle_status,'UNKNOWN')
                    WHEN 'OPEN' THEN 0
                    WHEN 'UNKNOWN' THEN 1
                    WHEN 'PAUSED' THEN 2
                    ELSE 3
                END,
                COUNT(a.id) DESC,
                lower(rrs.job_title)
            LIMIT ?
            """,
            (role_limit,),
        ).fetchall()

        recent = c.execute(
            """
            SELECT
                a.id,
                a.candidate_name,
                a.candidate_email,
                a.candidate_phone,
                a.job_title,
                a.indeed_status,
                a.profile_url,
                a.extraction_status,
                a.application_verified,
                a.decision_reason,
                a.send_status,
                a.sent_at,
                a.first_seen_at,
                a.last_seen_at,
                cr.analysis_status,
                cr.match_score,
                cr.rank_position,
                cr.auto_shortlisted,
                cf.hr_status,
                cf.interview_date
            FROM applications a
            LEFT JOIN candidate_reviews cr
              ON cr.application_id = a.id
            LEFT JOIN recruitment_candidate_flow cf
              ON cf.application_id = a.id
            LEFT JOIN recruitment_role_state rrs
              ON lower(trim(rrs.job_title)) = lower(trim(a.job_title))
            WHERE upper(trim(COALESCE(rrs.lifecycle_status,'UNKNOWN')))
                  IN ('OPEN','PAUSED','FLAGGED')
            ORDER BY
                COALESCE(a.first_seen_at, a.created_at) DESC,
                a.id DESC
            LIMIT ?
            """,
            (recent_limit,),
        ).fetchall()

    recent_rows = []
    for row in recent:
        item = dict(row)
        item["hr_flow"] = {
            "hr_status": item.pop("hr_status", None),
            "interview_date": item.pop("interview_date", None),
        }
        recent_rows.append(item)

    with conn() as c:
        snapshot = c.execute(
            """
            SELECT
                COALESCE(SUM(COALESCE(candidate_total_hint,0)),0) AS total_all,
                COALESCE(SUM(COALESCE(candidate_new_hint,0)),0) AS total_new,
                COUNT(*) AS role_count
            FROM recruitment_role_state
            WHERE upper(trim(COALESCE(lifecycle_status,'UNKNOWN')))
                  IN ('OPEN','PAUSED','FLAGGED')
            """
        ).fetchone()

    return {
        "roles": [dict(row) for row in roles],
        "recent": recent_rows,
        "indeed_counts": {
            "all": int(snapshot["total_all"] or 0),
            "new": int(snapshot["total_new"] or 0),
            "roles": int(snapshot["role_count"] or 0),
        },
        "generated_at": now(),
    }

def pipeline_status():
    init_recruitment_pipeline_db()
    settings = load_settings()
    with conn() as c:
        role = c.execute(
            """
            SELECT
                SUM(CASE WHEN lifecycle_status<>'CLOSED' THEN 1 ELSE 0 END) AS total,
                SUM(CASE WHEN lifecycle_status='OPEN' THEN 1 ELSE 0 END) AS open_count,
                SUM(CASE WHEN lifecycle_status='PAUSED' THEN 1 ELSE 0 END) AS paused_count,
                SUM(CASE WHEN lifecycle_status='CLOSED' THEN 1 ELSE 0 END) AS closed_count,
                SUM(CASE WHEN report_status='QUEUED' THEN 1 ELSE 0 END) AS reports_queued,
                SUM(CASE WHEN report_status='SEND_FAILED' THEN 1 ELSE 0 END) AS reports_failed
            FROM recruitment_role_state
            """
        ).fetchone()
        notices = c.execute(
            """
            SELECT
                SUM(CASE WHEN status='QUEUED' THEN 1 ELSE 0 END) AS queued,
                SUM(CASE WHEN status='SENT' THEN 1 ELSE 0 END) AS sent,
                SUM(CASE WHEN status='SEND_FAILED' THEN 1 ELSE 0 END) AS failed
            FROM recruitment_notifications
            """
        ).fetchone()
        approvals = c.execute(
            "SELECT COUNT(*) AS n FROM recruitment_candidate_flow WHERE hr_status='APPROVED'"
        ).fetchone()["n"]

    return {
        "roles": {
            "total": int(role["total"] or 0),
            "open": int(role["open_count"] or 0),
            "paused": int(role["paused_count"] or 0),
            "closed": int(role["closed_count"] or 0),
        },
        "notifications": {
            "queued": int(notices["queued"] or 0),
            "sent": int(notices["sent"] or 0),
            "failed": int(notices["failed"] or 0),
        },
        "hr_approved": int(approvals or 0),
        "report_sender": HR_REPORT_SENDER,
        "report_recipient": settings.get("hr_report_recipient") or HR_REPORT_SENDER,
        "report_gmail_configured": bool(
            re.sub(r"\s+", "", str(settings.get("hr_report_smtp_app_password") or ""))
        ),
        "report_gmail_verified": get_state("hr_report_smtp_verified", "0") == "1",
        "report_gmail_error": get_state("hr_report_smtp_last_error"),
        "daily_report": daily_report_status(),
        "communication_mode": "EMAIL_ONLY",
        "duplicate_policy": (
            "Stage 1: one acknowledgement per email recipient. "
            "Stage 2: one interview invitation per email recipient + role."
        ),
        "interview_schedule": interview_schedule_preview(),
        "activation_at": get_state("recruitment_pipeline_activation_at"),
    }
