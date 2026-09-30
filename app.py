from __future__ import annotations

import os
import json
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory
from werkzeug.exceptions import HTTPException

from config import load_settings, save_settings
from persistent_profile import sync_persistent_profile, persistence_status
from database import (
    conn as db_conn,
    init_db,
    list_applications,
    list_logs,
    list_sent_responses,
    stats,
    live_detection_snapshot,
    database_health,
    get_by_id,
    log,
    mark_backlog_completed,
    reset_backlog_once,
    pending_initial_new_count,
    all_candidates_backfill_completed,
    mark_all_candidates_backfill_completed,
    reset_all_candidates_backfill,
    pending_seen_unprocessed_count,
    mark_seen_processed,
    get_state,
    set_state,
    now,
    list_ready_to_send,
)
from automation import (
    process_indeed_results,
    send_thank_you,
    send_all_ready,
    smtp_health_check,
    repair_backlog_candidates,
)
from chrome_cdp import (
    chrome_status,
    scan_existing_chrome,
    devtools_active_port_path,
    open_indeed_in_existing_chrome,
    detect_candidates_page,
    ensure_candidates_page,
    IndeedCandidatePermissionError,
    IndeedChallengeError,
    new_candidates_queue_url,
    all_candidates_queue_url,
    discover_employer_job_descriptions,
    cleanup_recruitment_indeed_tabs,
    cdp_session_recovery_snapshot,
    open_remote_debugging_settings,
    launch_recruitment_chrome,
    recruitment_chrome_profile_initialized,
    recruitment_chrome_running,
    launch_recruitment_login_chrome,
    stop_recruitment_chrome,
    reset_shared_chrome,
    current_indeed_jobs_snapshot,
)

from review_engine import (
    init_role_review_db,
    list_roles,
    save_role_description,
    role_review_payload,
    analyze_role,
    analyze_pending_reviews,
    sync_job_descriptions_from_scan,
    ingest_discovered_job_descriptions,
    analyze_scan_results,
    ranking_status,
    save_role_knockout_requirements,
)
from local_rag import CATEGORY_WEIGHTS, EMBEDDING_MODEL, embedding_status
from recruitment_pipeline import (
    init_recruitment_pipeline_db,
    sync_discovered_jobs,
    queue_changed_role_reports,
    queue_daily_consolidated_report,
    process_notifications_once,
    resume_config_waiting_notifications,
    hr_report_mail_health_check,
    decorate_role_payload,
    decorate_roles,
    approve_candidate_for_interview,
    approve_top_candidates_for_interview,
    interview_schedule_preview,
    pipeline_status,
    operations_overview,
)

BASE_DIR = Path(__file__).resolve().parent
API_PORT = 5286
APP_VERSION = "V11.11.24"
APP_FIRST_MAIL_ONLY_PATCH = "V11.11.24-FIRST-MAIL-ONLY"

app = Flask(__name__)
app.secret_key = "nunes-recruitment-console-v10"

scan_lock = threading.Lock()
stop_event = threading.Event()

# Wake signals make Start Automation and newly-verified applicants react
# immediately instead of waiting for the next one-second polling boundary.
scan_wake_event = threading.Event()
outbox_wake_event = threading.Event()
role_review_wake_event = threading.Event()
pipeline_wake_event = threading.Event()

AUTO_CONNECT_RETRY_SECONDS = 4
AUTO_READY_RETRY_SECONDS = 2
AUTO_OPEN_RETRY_SECONDS = 120

# Internal live-operation cadence. These are implementation details, not user
# settings. Candidate scans and mail delivery run independently.
LIVE_CANDIDATE_IDLE_SECONDS = 1
LIVE_OUTBOX_IDLE_SECONDS = 1
MAIL_REVERIFY_SECONDS = 60
FULL_RECONCILE_SECONDS = 180
ROLE_REVIEW_IDLE_SECONDS = 3
ROLE_DESCRIPTION_DISCOVERY_SECONDS = 30

auto_connection_lock = threading.RLock()
auto_connection_state = {
    "phase": "starting",
    "message": "Starting automatic browser connection",
    "last_error": None,
    "last_connected_at": None,
    "last_candidates_at": None,
    "next_connect_attempt_at": 0.0,
    "next_indeed_open_at": 0.0,
}

_runtime_schema_lock = threading.RLock()
_runtime_schema_ready = False
_runtime_schema_error = None


def ensure_runtime_schema():
    """Idempotent persistent-data migration/health gate."""
    global _runtime_schema_ready, _runtime_schema_error

    if _runtime_schema_ready:
        return True

    with _runtime_schema_lock:
        if _runtime_schema_ready:
            return True

        try:
            init_db()
            init_role_review_db()
            init_recruitment_pipeline_db()
            try:
                repair_backlog_candidates()
            except Exception as repair_exc:
                log("WARN", f"Backlog candidate repair deferred: {repair_exc}")
            _runtime_schema_ready = True
            _runtime_schema_error = None
            return True
        except Exception as exc:
            _runtime_schema_error = str(exc)
            return False


def runtime_schema_status():
    return {
        "ready": bool(_runtime_schema_ready),
        "error": _runtime_schema_error,
    }


@app.before_request
def _ensure_schema_before_request():
    # Normally ready before app.run(). This retry makes upgrade recovery robust
    # if a first migration attempt was interrupted.
    if not _runtime_schema_ready:
        ensure_runtime_schema()




def critical_runtime_self_test():
    """Local production self-test. No live Indeed/Gmail traffic is generated."""
    checks = {}
    errors = []

    def check(name, fn):
        try:
            value = fn()
            checks[name] = {"ok": True, "value": value}
            return value
        except Exception as exc:
            checks[name] = {"ok": False, "error": str(exc)}
            errors.append(f"{name}: {exc}")
            return None

    check("clock", lambda: datetime.now(timezone.utc).isoformat())
    check("database_schema", lambda: ensure_runtime_schema())
    check("settings", lambda: public_settings().get("company_email"))
    check("database_stats", lambda: stats().get("total", 0))
    check("live_state", lambda: live_detection_snapshot().get("status"))
    check("role_ranking_schema", lambda: ranking_status().get("total_roles", 0))
    check("recruitment_pipeline", lambda: pipeline_status().get("roles", {}).get("total", 0))

    # State round-trip catches missing imports/state regressions before a user
    # clicks Start Automation.
    marker = datetime.now(timezone.utc).isoformat()
    check(
        "state_round_trip",
        lambda: (
            set_state("runtime_self_test_at", marker),
            get_state("runtime_self_test_at"),
        )[1],
    )

    return {
        "ok": not errors,
        "version": APP_VERSION,
        "checks": checks,
        "errors": errors,
    }


def _automation_connection_kick():
    """Chrome/Indeed connection work is never allowed to break Start API."""
    try:
        automatic_connection_tick(force_connect=True)
    except Exception as exc:
        try:
            log("WARN", f"Automation connection recovery: {exc}")
        except Exception:
            pass


def _clear_stale_candidates_binding_if_needed(error_text, failure_count):
    """After repeated target/session failures force a clean Candidates rebind."""
    text = str(error_text or "").lower()
    recoverable = any(token in text for token in (
        "session with given id not found",
        "-32001",
        "target closed",
        "no target",
        "candidate page failed",
        "page.navigate failed",
        "not the employer candidates page",
        "javascript error in indeed page",
    ))

    if not recoverable or int(failure_count or 0) < 2:
        return False

    try:
        settings = load_settings()
        if settings.get("indeed_candidates_url"):
            settings["indeed_candidates_url"] = ""
            save_settings(settings)
        set_state("live_monitor_scan_mode", "rebinding_candidates")
        set_state("live_monitor_last_found_links", "0")
        return True
    except Exception:
        return False

def _set_auto_connection_state(**changes):
    with auto_connection_lock:
        auto_connection_state.update(changes)


def auto_connection_snapshot():
    with auto_connection_lock:
        out = dict(auto_connection_state)

    now_mono = time.monotonic()
    retry_at = float(out.get("next_connect_attempt_at") or 0.0)
    out["retry_in_seconds"] = max(0, int(retry_at - now_mono))
    return out


def find_chrome():
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
    ]
    for path in candidates:
        if path and Path(path).exists():
            return path
    return None


def open_chrome_url(url):
    # Do not launch a bare Chrome process because PCs with multiple Chrome
    # profiles may show "Who's using Chrome?". Live automation uses CDP.
    # This helper is retained only for compatibility and uses the Windows
    # default browser routing instead.
    try:
        if os.name == "nt":
            os.startfile(url)
            return True
    except Exception:
        pass
    return False


def sender_configured():
    s = load_settings()
    return bool(
        (s.get("company_email") or "").strip()
        and (s.get("smtp_app_password") or "").replace(" ", "").strip()
    )


def enforce_always_on_mode():
    """V11.11.2 runs recruitment continuously while the local service is open."""
    s = load_settings()
    changed = False
    for key in ("automation_enabled", "monitoring_enabled", "auto_scan", "auto_send"):
        if not s.get(key, False):
            s[key] = True
            changed = True
    if changed:
        save_settings(s)
    return s


def monitoring_enabled_by_user(settings):
    return bool(
        settings.get("automation_enabled", True)
        and settings.get("monitoring_enabled", True)
        and settings.get("auto_scan", True)
    )


def monitoring_ready(settings):
    return bool(
        monitoring_enabled_by_user(settings)
        and (settings.get("indeed_candidates_url") or "").strip()
    )


def bind_candidates_page_if_available(reset_catchup=False):
    """
    Attach the real Candidates page to this installation as soon as it becomes
    available. The user does not need to press a separate Detect/Activate step.
    """
    detected = ensure_candidates_page()

    s = load_settings()
    previous = (s.get("indeed_candidates_url") or "").strip()
    detected_url = all_candidates_queue_url(
        (detected.get("url") or "").strip()
    )

    if not detected_url:
        raise RuntimeError("Candidates page URL was not available.")

    changed = previous != detected_url

    s["indeed_candidates_url"] = detected_url
    # Do not change automation_enabled here. The user-controlled ON/OFF switch
    # is authoritative.
    s["monitoring_enabled"] = True
    s["auto_scan"] = True
    s["auto_send"] = True
    s["process_current_candidates_once"] = True
    s["process_all_active_candidates_once"] = True
    s["initial_catchup_new_only"] = False
    save_settings(s)

    if reset_catchup or (changed and not previous):
        reset_backlog_once()
        reset_all_candidates_backfill()

    return detected



def desired_indeed_url(settings=None):
    """Open Jobs first so role discovery works even without candidate permission."""
    return (
        "https://employers.indeed.com/jobs?"
        "status=open%2Cpaused&claimed=false&createdOnIndeed=true&tab=0&"
        "sortDirection=DESC&sortField=datePostedOnIndeed"
    )


def automatic_connection_tick(force_connect=False):
    """
    One iteration of the hands-free connection manager.

    - reconnects to the persistent Recruitment Chrome automatically
    - reuses the same approved CDP session
    - opens the saved/direct Indeed Candidates URL when no Indeed tab exists
    - waits for a one-time Indeed sign-in when required
    - activates Candidates/monitoring automatically after sign-in

    The Recruitment Chrome profile exposes a local DevTools endpoint directly. Failed connection attempts are throttled so
    Chrome is not spammed with repeated permission dialogs.
    """
    now_mono = time.monotonic()
    s = load_settings()

    if not s.get("automation_enabled", True):
        _set_auto_connection_state(
            phase="paused",
            message="Automation is turned off",
            last_error=None,
        )
        return auto_connection_snapshot()

    if not s.get("auto_connect_chrome", True):
        _set_auto_connection_state(
            phase="disabled",
            message="Automatic browser connection is disabled",
        )
        return auto_connection_snapshot()

    # ONE-TIME SAFE LOGIN MODE:
    # Google can reject OAuth sign-in while Chrome is under a debugging
    # connection. During this explicit mode the same Recruitment Chrome profile
    # is opened WITHOUT CDP. The console waits until the user closes it, then
    # automatically resumes live CDP using the authenticated session.
    safe_login_mode = (
        get_state(
            "indeed_safe_login_mode",
            "0",
        )
        == "1"
    )

    if safe_login_mode:
        try:
            started_epoch = float(
                get_state(
                    "indeed_safe_login_started_epoch",
                    "0",
                )
                or 0
            )
        except Exception:
            started_epoch = 0.0

        running = recruitment_chrome_running()
        grace = (
            time.time() - started_epoch
            < 10
        )

        if running or grace:
            _set_auto_connection_state(
                phase="safe_sign_in",
                message=(
                    "Safe Indeed sign-in is open for the configured Google account. Complete Google/Indeed login, "
                    "then close Recruitment Chrome completely. Live recruitment "
                    "will resume automatically."
                ),
                last_error=None,
                next_connect_attempt_at=now_mono + 2,
            )
            return auto_connection_snapshot()

        # Safe login window was closed. Resume the same saved profile in
        # debugging/live mode.
        set_state(
            "indeed_safe_login_mode",
            "0",
        )
        set_state(
            "indeed_safe_login_completed_at",
            datetime.now(timezone.utc).isoformat(),
        )
        set_state(
            "recruitment_chrome_bootstrapped",
            "1",
        )
        try:
            reset_shared_chrome(
                "Safe Indeed sign-in completed"
            )
        except Exception:
            pass

        _set_auto_connection_state(
            phase="reconnecting",
            message=(
                "Safe Indeed sign-in window closed. Reopening Recruitment Chrome "
                "for live monitoring."
            ),
            last_error=None,
            next_connect_attempt_at=0.0,
        )

    current = chrome_status()

    if not current.get("ok"):
        snapshot = auto_connection_snapshot()
        retry_at = float(snapshot.get("next_connect_attempt_at") or 0.0)

        if not force_connect and now_mono < retry_at:
            return snapshot

        _set_auto_connection_state(
            phase="connecting",
            message="Connecting to Recruitment Chrome",
            last_error=None,
        )

        connected = chrome_status(connect_if_needed=True)
        if not connected.get("ok"):
            err = str(connected.get("error") or "Chrome connection is not ready")

            should_launch = (
                force_connect
                or (
                    recruitment_chrome_profile_initialized()
                    and get_state("recruitment_chrome_bootstrapped", "0") == "1"
                )
            )

            launch_result = None
            if should_launch:
                try:
                    launch_result = launch_recruitment_chrome(
                        desired_indeed_url(s),
                        wait_seconds=8 if force_connect else 3,
                    )
                    reset_shared_chrome(
                        "Recruitment Chrome launched/reused"
                    )
                except Exception as launch_error:
                    launch_result = {
                        "ok": False,
                        "message": str(launch_error),
                    }

            # If the launch created a live DevTools endpoint, attach now.
            if launch_result and launch_result.get("ok"):
                connected = chrome_status(connect_if_needed=True)

            if not connected.get("ok"):
                launch_message = str(
                    (launch_result or {}).get("message")
                    or ""
                ).strip()

                _set_auto_connection_state(
                    phase=(
                        "waiting_for_recruitment_chrome"
                        if should_launch
                        else "reconnecting"
                    ),
                    message=(
                        launch_message
                        or "Press Open Recruitment Chrome once to start the persistent Indeed browser."
                    ),
                    last_error=err,
                    next_connect_attempt_at=now_mono + AUTO_CONNECT_RETRY_SECONDS,
                )
                return auto_connection_snapshot()

        current = connected
        _set_auto_connection_state(
            phase="connected",
            message="Recruitment Chrome connected",
            last_error=None,
            last_connected_at=time.time(),
            next_connect_attempt_at=now_mono + AUTO_READY_RETRY_SECONDS,
        )
        log("INFO", "AUTO CONNECTION: Recruitment Chrome connected.")
        try:
            cleanup_recruitment_indeed_tabs()
        except Exception:
            pass
        role_review_wake_event.set()

    # We have an approved Chrome connection. Role discovery is independent
    # from candidate permission, so keep Jobs/ranking role discovery awake.
    role_review_wake_event.set()

    # If Indeed already told us this account lacks Manage candidates access,
    # avoid retrying every 1-2 seconds. Re-check periodically or immediately
    # after a manual Connect/Safe Login action.
    snapshot = auto_connection_snapshot()
    if (
        snapshot.get("phase") == "permission_required"
        and not force_connect
        and now_mono < float(snapshot.get("next_connect_attempt_at") or 0.0)
    ):
        return snapshot

    try:
        detected = bind_candidates_page_if_available(reset_catchup=False)
        set_state("indeed_candidate_permission_status", "OK")
        set_state("indeed_candidate_permission", "")
        set_state("indeed_candidate_permission_error", "")
        _set_auto_connection_state(
            phase="ready",
            message="Indeed Candidates is active",
            last_error=None,
            last_candidates_at=time.time(),
            next_connect_attempt_at=now_mono + AUTO_READY_RETRY_SECONDS,
        )
        scan_wake_event.set()
        role_review_wake_event.set()
        return auto_connection_snapshot()
    except IndeedCandidatePermissionError as e:
        err = str(e)
        permission = getattr(e, "permission", "Hosted_Candidate") or "Hosted_Candidate"
        set_state("indeed_candidate_permission_status", "MISSING")
        set_state("indeed_candidate_permission", permission)
        set_state("indeed_candidate_permission_error", err[:2000])
        set_state("live_monitor_scan_mode", "permission_required")
        set_state("live_monitor_last_error", err[:1200])
        # This is an account entitlement state, not a runtime crash. Do not let
        # the dashboard accumulate 100+ fake errors.
        set_state("live_monitor_consecutive_failures", "0")
        _set_auto_connection_state(
            phase="permission_required",
            message=(
                "Indeed account is connected, but Manage candidates access is missing. "
                f"Required permission: {permission}. Current roles remain live from Manage Jobs. Candidate access will be rechecked later or when Retry is pressed."
            ),
            last_error=err,
            next_connect_attempt_at=now_mono + 900,
        )
        role_review_wake_event.set()
        return auto_connection_snapshot()
    except Exception as e:
        err = str(e)

    current = chrome_status()
    if current.get("ok"):
        auth_confirmed = (
            get_state("indeed_auth_status", "")
            == "SIGNED_IN"
        )

        if (
            not current.get("indeed_found")
            and s.get("auto_open_indeed", True)
            and not auth_confirmed
        ):
            snapshot = auto_connection_snapshot()
            next_open = float(snapshot.get("next_indeed_open_at") or 0.0)

            if now_mono >= next_open:
                try:
                    open_indeed_in_existing_chrome(
                        desired_indeed_url(s)
                    )
                    _set_auto_connection_state(
                        phase="opening_indeed",
                        message="Opening the single Indeed Jobs tab",
                        last_error=None,
                        next_indeed_open_at=now_mono + 120,
                    )
                    log(
                        "INFO",
                        "AUTO CONNECTION: Reused/opened the single Indeed Jobs tab.",
                    )
                    return auto_connection_snapshot()
                except Exception as open_error:
                    err = str(open_error)

        # If an Indeed page exists but is a login page, do not keep opening tabs.
        _set_auto_connection_state(
            phase="waiting_for_sign_in",
            message=(
                "Waiting for Indeed sign-in; Candidates will open automatically afterwards"
            ),
            last_error=err,
            next_connect_attempt_at=now_mono + AUTO_READY_RETRY_SECONDS,
        )
        return auto_connection_snapshot()

    _set_auto_connection_state(
        phase="reconnecting",
        message="Recruitment Chrome connection was lost; reconnecting automatically",
        last_error=err,
        next_connect_attempt_at=now_mono + 10,
    )
    return auto_connection_snapshot()


def auto_connection_loop():
    stop_event.wait(1.5)

    while not stop_event.is_set():
        try:
            automatic_connection_tick()
        except Exception as e:
            _set_auto_connection_state(
                phase="error",
                message="Automatic browser connection encountered an error",
                last_error=str(e),
                next_connect_attempt_at=time.monotonic() + AUTO_CONNECT_RETRY_SECONDS,
            )
            log("WARN", "AUTO CONNECTION: " + str(e))

        stop_event.wait(AUTO_READY_RETRY_SECONDS)


def execute_monitor_check(fast_only=False):
    with scan_lock:
        scan_started = datetime.now(timezone.utc).isoformat()
        set_state("live_monitor_heartbeat_at", scan_started)
        set_state(
            "live_monitor_scan_mode",
            "fast_new_watch" if fast_only else "full_reconcile",
        )

        data = scan_existing_chrome(fast_only=fast_only)
        result = process_indeed_results(data.get("results", []))

        # Mail delivery has priority over ranking.
        if result.get("ready", 0) or list_ready_to_send(limit=1):
            outbox_wake_event.set()

        try:
            sync_job_descriptions_from_scan(data.get("results", []))
        except Exception as exc:
            log("WARN", f"Role description sync waiting: {exc}")

        if data.get("results"):
            role_review_wake_event.set()

        # Any attempted applicant now has an application record and is considered
        # processed for the one-time New queue. NEEDS_REVIEW rows are retried later
        # by the normal retry rule.
        for item in data.get("results", []):
            source_key = item.get("source_key")
            if source_key:
                mark_seen_processed(source_key)

        # Candidate monitoring never sends mail directly. The live outbox owns
        # delivery. V11.11.24 backfills the complete All-applicants population
        # while keeping live New candidates first.
        pending_initial = pending_initial_new_count()
        pending_all = pending_seen_unprocessed_count()

        if (
            not fast_only
            and data.get("collection_complete")
            and pending_initial == 0
        ):
            mark_backlog_completed()

        if (
            not fast_only
            and data.get("all_backfill_pending")
            and data.get("collection_complete")
            and pending_all == 0
        ):
            mark_all_candidates_backfill_completed()

        success_at = datetime.now(timezone.utc).isoformat()
        set_state("live_monitor_heartbeat_at", success_at)
        set_state("live_monitor_last_success_at", success_at)
        set_state("live_monitor_last_error", "")
        set_state("live_monitor_consecutive_failures", "0")
        set_state(
            "live_monitor_last_found_links",
            str(data.get("found_links", 0)),
        )

        new_rows = [
            row
            for row in data.get("results", [])
            if row.get("new_applicant")
        ]

        if new_rows:
            newest = new_rows[0]
            set_state(
                "live_monitor_last_new_candidate",
                newest.get("candidate_name") or "Candidate",
            )
            set_state(
                "live_monitor_last_new_candidate_at",
                success_at,
            )
            set_state(
                "live_monitor_last_new_role",
                newest.get("job_title") or "",
            )

        return {
            **data,
            "processed": result.get("processed", 0),
            "verified": result.get("ready", 0),
            "sent_now": 0,
            "pending_initial_new": pending_initial,
            "pending_all_backfill": pending_all,
            "all_backfill_completed": all_candidates_backfill_completed(),
        }


def background_loop():
    """
    Live Today watcher with automatic Candidates-page recovery.

    Automation ON no longer depends on a previously-saved Candidates URL.
    If the binding is missing/stale, the worker reconnects Chrome, binds the
    current Indeed Candidates page, then performs a fresh scan.
    """
    scan_wake_event.wait(timeout=0.35)
    scan_wake_event.clear()

    did_initial_full = False
    next_full_reconcile = 0.0
    set_state("live_monitor_started_at", datetime.now(timezone.utc).isoformat())

    while not stop_event.is_set():
        heartbeat_at = datetime.now(timezone.utc).isoformat()
        set_state("live_monitor_heartbeat_at", heartbeat_at)

        settings = load_settings()
        user_enabled = monitoring_enabled_by_user(settings)
        set_state("live_monitor_enabled", "1" if user_enabled else "0")

        if user_enabled:
            try:
                if get_state("indeed_candidate_permission_status", "") == "MISSING":
                    set_state("live_monitor_scan_mode", "permission_required")
                    set_state("live_monitor_consecutive_failures", "0")
                    set_state(
                        "live_monitor_last_error",
                        get_state(
                            "indeed_candidate_permission_error",
                            "Indeed Manage candidates permission is required.",
                        ),
                    )
                    # Roles still update through role_review_loop. Candidate
                    # access is re-checked by automatic_connection_loop.
                    scan_wake_event.wait(timeout=5.0)
                    scan_wake_event.clear()
                    continue

                if not monitoring_ready(settings):
                    set_state("live_monitor_scan_mode", "connecting_candidates")

                    try:
                        automatic_connection_tick(force_connect=False)
                    except Exception:
                        pass

                    settings = load_settings()

                    if not monitoring_ready(settings):
                        try:
                            bind_candidates_page_if_available(
                                reset_catchup=False
                            )
                        except Exception as bind_error:
                            set_state(
                                "live_monitor_last_error",
                                (
                                    "Waiting for live Indeed Candidates page: "
                                    + str(bind_error)
                                )[:1200],
                            )

                    settings = load_settings()

                if monitoring_ready(settings):
                    now_mono = time.monotonic()

                    do_full = (
                        not did_initial_full
                        or now_mono >= next_full_reconcile
                    )

                    result = execute_monitor_check(
                        fast_only=not do_full
                    )

                    if do_full:
                        did_initial_full = True
                        backfill_pending = int(
                            result.get("pending_all_backfill") or 0
                        ) > 0
                        next_full_reconcile = (
                            time.monotonic()
                            + (25 if backfill_pending else FULL_RECONCILE_SECONDS)
                        )

                    if (
                        result.get("new_candidates", 0)
                        or result.get("processed", 0)
                    ):
                        log(
                            "INFO",
                            (
                                "LIVE NEW APPLICANT CHECK: "
                                if result.get("fast_only")
                                else "FULL CANDIDATE RECONCILE: "
                            )
                            + f"visible={result.get('found_links',0)}, "
                            + f"new={result.get('new_candidates',0)}, "
                            + f"processed={result.get('processed',0)}"
                        )
                else:
                    set_state(
                        "live_monitor_scan_mode",
                        "waiting_for_candidates",
                    )

            except IndeedChallengeError as challenge_err:
                set_state("live_monitor_scan_mode", "challenge_waiting")
                set_state(
                    "live_monitor_last_error",
                    "Indeed verification/security challenge presented. Please complete it in Recruitment Chrome to continue.",
                )
                set_state("live_monitor_consecutive_failures", "0")
                log("INFO", f"Indeed challenge detected: {challenge_err}. Waiting safely for page resolution...")
                scan_wake_event.wait(timeout=10.0)
                scan_wake_event.clear()

            except Exception as e:
                previous_failures = int(
                    get_state(
                        "live_monitor_consecutive_failures",
                        "0",
                    )
                    or 0
                )

                set_state(
                    "live_monitor_consecutive_failures",
                    str(previous_failures + 1),
                )
                set_state(
                    "live_monitor_last_error",
                    str(e)[:1200],
                )
                set_state(
                    "live_monitor_scan_mode",
                    "recovering",
                )

                _clear_stale_candidates_binding_if_needed(
                    e,
                    previous_failures + 1,
                )

                try:
                    automatic_connection_tick(force_connect=True)
                except Exception:
                    pass

                log(
                    "WARN",
                    "Live candidate monitoring recovering: " + str(e),
                )
        else:
            set_state("live_monitor_scan_mode", "off")

        scan_wake_event.wait(timeout=LIVE_CANDIDATE_IDLE_SECONDS)
        scan_wake_event.clear()


def live_outbox_loop():
    """
    Independent mail reconciliation worker.

    Every verified candidate whose acknowledgement is not SENT remains in the
    outbox. The worker continuously retries pending mail while Gmail is healthy.

    If Gmail becomes unavailable/rejects authentication, it periodically
    re-verifies the saved App Password and resumes the outbox automatically
    when service recovers.
    """
    outbox_wake_event.wait(timeout=0.35)
    outbox_wake_event.clear()

    next_health_check = 0.0

    while not stop_event.is_set():
        settings = load_settings()

        if (
            settings.get("automation_enabled", True)
            and settings.get("auto_send", True)
            and sender_configured()
        ):
            verified = get_state("smtp_verified", "0") == "1"

            if not verified and time.monotonic() >= next_health_check:
                check = smtp_health_check(settings)
                verified = bool(check.get("ok"))
                next_health_check = (
                    time.monotonic() + MAIL_REVERIFY_SECONDS
                )

                if verified:
                    log(
                        "INFO",
                        "LIVE OUTBOX: Gmail authentication recovered."
                    )

            if verified:
                try:
                    result = send_all_ready()

                    if (
                        result.get("attempted", 0)
                        or result.get("sent", 0)
                    ):
                        log(
                            "INFO",
                            "LIVE OUTBOX: "
                            f"attempted={result.get('attempted',0)}, "
                            f"sent={result.get('sent',0)}, "
                            f"remaining={result.get('remaining',0)}"
                        )

                except Exception as e:
                    log("WARN", f"Live outbox reconciliation failed: {e}")

        outbox_wake_event.wait(timeout=LIVE_OUTBOX_IDLE_SECONDS)
        outbox_wake_event.clear()



def role_review_loop():
    """
    Core role/ranking worker.

    Current Indeed roles are discovered independently of email delivery.
    A role appears immediately from the Jobs listing; ranking waits only when
    its detailed JD is not yet available.
    """
    role_review_wake_event.wait(timeout=0.35)
    role_review_wake_event.clear()
    next_description_discovery = time.monotonic() + 1.0

    while not stop_event.is_set():
        try:
            settings = load_settings()

            if settings.get("automation_enabled", True):
                analyzed = analyze_pending_reviews(limit=80)
                if analyzed:
                    log(
                        "INFO",
                        f"AUTO ROLE RANKING: processed {analyzed} waiting/new applicant(s).",
                    )

                now_mono = time.monotonic()

                if now_mono >= next_description_discovery:
                    set_state("role_discovery_status", "SCANNING")
                    set_state(
                        "role_discovery_heartbeat_at",
                        datetime.now(timezone.utc).isoformat(),
                    )

                    acquired = scan_lock.acquire(blocking=False)

                    if acquired:
                        try:
                            # Parse the complete current Indeed Jobs page.
                            # The old max_jobs=1 behavior updated only one role per
                            # cycle and left dashboard totals/role counts stale.
                            discovered = discover_employer_job_descriptions(
                                max_jobs=25
                            )
                            candidate_queues = next(
                                (
                                    row.get("candidate_queues")
                                    for row in discovered
                                    if row.get("candidate_queues")
                                ),
                                [],
                            )
                            set_state(
                                "indeed_job_candidate_queues",
                                json.dumps(candidate_queues),
                            )
                            lifecycle_changes = sync_discovered_jobs(discovered)
                            updated_roles = ingest_discovered_job_descriptions(
                                discovered
                            )

                            set_state(
                                "role_discovery_last_success_at",
                                datetime.now(timezone.utc).isoformat(),
                            )
                            set_state("role_discovery_last_error", "")
                            set_state(
                                "role_discovery_roles_found",
                                str(len(discovered)),
                            )
                            set_state(
                                "role_discovery_status",
                                "LIVE" if discovered else "WAITING_FOR_INDEED",
                            )

                            if discovered:
                                set_state(
                                    "indeed_auth_status",
                                    "SIGNED_IN",
                                )
                                set_state(
                                    "indeed_auth_confirmed_at",
                                    datetime.now(timezone.utc).isoformat(),
                                )
                                role_preview = " | ".join(
                                    (
                                        str(x.get("job_title") or "")
                                        + " ["
                                        + str(x.get("job_status") or "UNKNOWN")
                                        + "]"
                                    )
                                    for x in discovered[:8]
                                )
                                log(
                                    "INFO",
                                    "INDEED JOBS DISCOVERY: "
                                    f"parsed={len(discovered)}; "
                                    f"sample={role_preview}",
                                )

                            if lifecycle_changes:
                                log(
                                    "INFO",
                                    "AUTO ROLE LIFECYCLE: synced "
                                    + ", ".join(lifecycle_changes[:25]),
                                )

                            if updated_roles:
                                log(
                                    "INFO",
                                    "AUTO ROLE DESCRIPTIONS: synced "
                                    + ", ".join(updated_roles[:25]),
                                )
                                analyze_pending_reviews(limit=120)

                            if discovered or lifecycle_changes or updated_roles:
                                pipeline_wake_event.set()

                            next_description_discovery = (
                                time.monotonic()
                                + (
                                    ROLE_DESCRIPTION_DISCOVERY_SECONDS
                                    if discovered
                                    else 8
                                )
                            )

                        except Exception as exc:
                            set_state("role_discovery_status", "RECOVERING")
                            set_state(
                                "role_discovery_last_error",
                                str(exc)[:2000],
                            )
                            log(
                                "WARN",
                                "Automatic Indeed role discovery waiting: "
                                + str(exc),
                            )
                            next_description_discovery = (
                                time.monotonic()
                                + ROLE_DESCRIPTION_DISCOVERY_SECONDS
                            )
                        finally:
                            scan_lock.release()
                    else:
                        next_description_discovery = time.monotonic() + 1.5

        except Exception as exc:
            set_state("role_discovery_status", "RECOVERING")
            set_state("role_discovery_last_error", str(exc)[:2000])
            log("WARN", f"Automatic role ranking waiting: {exc}")

        role_review_wake_event.wait(timeout=ROLE_REVIEW_IDLE_SECONDS)
        role_review_wake_event.clear()
def recruitment_pipeline_loop():
    """Email-only delivery worker for HR interviews and the daily HR report."""
    pipeline_wake_event.wait(timeout=0.8)
    pipeline_wake_event.clear()
    next_daily_report_check = 0.0

    while not stop_event.is_set():
        try:
            settings = load_settings()
            if settings.get("automation_enabled", True):
                now_mono = time.monotonic()
                if now_mono >= next_daily_report_check:
                    daily = queue_daily_consolidated_report(force=False)
                    if daily.get("queued"):
                        log(
                            "INFO",
                            f"DAILY RECRUITMENT REPORT: queued one consolidated report for "
                            f"{daily.get('roles',0)} ongoing role(s).",
                        )
                    next_daily_report_check = now_mono + 30

                result = process_notifications_once(limit=16)
                if result.get("sent"):
                    log("INFO", f"RECRUITMENT PIPELINE: delivered {result.get('sent')} notification(s).")
        except Exception as exc:
            log("WARN", f"Recruitment pipeline waiting: {exc}")

        pipeline_wake_event.wait(timeout=1)
        pipeline_wake_event.clear()



def core_recruitment_status():
    pipe = pipeline_status()
    auto_state = auto_connection_snapshot()
    live = live_detection_snapshot()
    indeed_jobs = exact_indeed_jobs_snapshot()
    phase = str(auto_state.get("phase") or "").lower()

    return {
        "indeed_total_applicants": _safe_int(
            indeed_jobs.get("total_applicants")
        ),
        "indeed_current_new": _safe_int(
            indeed_jobs.get("total_new")
        ),
        "indeed_role_count": _safe_int(
            indeed_jobs.get("role_count")
        ),
        "indeed_counts_captured_at": indeed_jobs.get("captured_at"),
        "service_live": True,
        "communication_mode": "EMAIL_ONLY",
        "chrome_connected": phase in {
            "connected",
            "ready",
            "candidates_ready",
            "opening_indeed",
            "waiting_for_sign_in",
        },
        "indeed_live": bool(live.get("healthy")),
        "role_discovery_status": str(
            get_state("role_discovery_status", "STARTING")
            or "STARTING"
        ).upper(),
        "roles_found": int(
            get_state("role_discovery_roles_found", "0")
            or 0
        ),
        "jobs_rows_parsed": int(
            get_state("indeed_jobs_discovery_rows", "0")
            or 0
        ),
        "jobs_total_hint": int(
            get_state("indeed_jobs_discovery_total_hint", "0")
            or 0
        ),
        "jobs_target_url": get_state(
            "indeed_jobs_discovery_target_url"
        ),
        "jobs_used_existing_tab": (
            get_state(
                "indeed_jobs_discovery_used_existing_tab",
                "0",
            )
            == "1"
        ),
        "jobs_discovery_sample": get_state(
            "indeed_jobs_discovery_sample"
        ),
        "jobs_discovery_error": get_state(
            "indeed_jobs_discovery_last_error"
        ),
        "candidate_permission_status": get_state(
            "indeed_candidate_permission_status",
            "UNKNOWN",
        ),
        "candidate_permission": get_state(
            "indeed_candidate_permission",
            "",
        ),
        "candidate_permission_error": get_state(
            "indeed_candidate_permission_error",
            "",
        ),
        "indeed_auth_status": get_state(
            "indeed_auth_status",
            "UNKNOWN",
        ),
        "indeed_auth_confirmed_at": get_state(
            "indeed_auth_confirmed_at",
        ),
        "duplicate_tabs_closed": int(
            get_state(
                "indeed_duplicate_tabs_closed",
                "0",
            )
            or 0
        ),
        "ranking_running": True,
        "message": (
            "Core recruitment is live: Indeed roles, applicants, ranking and "
            "email delivery run continuously."
        ),
    }



def public_settings():
    s = load_settings()
    return {
        "company_name": s.get("company_name", ""),
        "company_email": "nuneslead@gmail.com",
        "smtp_app_password_set": bool(
            (s.get("smtp_app_password") or "").strip()
        ),
        "indeed_start_url": s.get("indeed_start_url", ""),
        "indeed_candidates_url": s.get("indeed_candidates_url", ""),
        "automation_enabled": bool(s.get("automation_enabled", True)),
        "monitoring_enabled": bool(s.get("monitoring_enabled", True)),
        "auto_scan": bool(s.get("auto_scan", True)),
        "auto_send": bool(s.get("auto_send", True)),
        "allow_candidate_page_email_fallback": bool(
            s.get("allow_candidate_page_email_fallback", False)
        ),
        "local_ai_enabled": bool(s.get("local_ai_enabled", False)),
        "ollama_url": s.get("ollama_url", ""),
        "ollama_model": s.get("ollama_model", ""),
        "ranking_weights": s.get("ranking_weights") or dict(CATEGORY_WEIGHTS),
        "ranking_strong_threshold": float(s.get("ranking_strong_threshold", 85.0)),
        "ranking_good_threshold": float(s.get("ranking_good_threshold", 70.0)),
        "ranking_moderate_threshold": float(s.get("ranking_moderate_threshold", 50.0)),
        "ranking_embedding_model": str(s.get("ranking_embedding_model") or EMBEDDING_MODEL),
        "ranking_embedding_status": embedding_status(),
        "subject_template": s.get("subject_template", ""),
        "body_template": s.get("body_template", ""),
        "interview_subject_template": s.get("interview_subject_template", ""),
        "interview_body_template": s.get("interview_body_template", ""),
        "hr_report_sender_email": "nunescbe@gmail.com",
        "hr_report_recipient": s.get("hr_report_recipient", "nunescbe@gmail.com"),
        "hr_report_smtp_app_password_set": bool(
            (s.get("hr_report_smtp_app_password") or "").strip()
        ),
        "auto_send_role_reports": False,
        "daily_consolidated_report_enabled": bool(s.get("daily_consolidated_report_enabled", True)),
        "daily_report_time": s.get("daily_report_time", "19:00"),
        "persistence": persistence_status(
            settings=s,
        ),
    }


def cached_dashboard_chrome_status():
    """
    Fast, non-blocking browser status for UI requests.

    The background connection manager owns Chrome/CDP work. Dashboard HTTP
    requests must never attach/evaluate an Indeed page because that can keep the
    browser on the loading screen for several seconds.
    """
    state = auto_connection_snapshot()
    phase = (state.get("phase") or "").lower()
    connected = phase in {"ready", "candidates_ready", "connected"}
    settings = load_settings()
    candidate_url = (settings.get("indeed_candidates_url") or "").strip()

    return {
        "ok": connected,
        "chrome_connected": connected,
        "indeed_found": bool(connected and candidate_url),
        "url": candidate_url if connected else "",
        "title": "Indeed Candidates" if connected and candidate_url else "",
        "message": state.get("message") or (
            "CHROME CONNECTED" if connected else "CHROME CONNECTING"
        ),
        "error": state.get("last_error"),
        "cached": True,
    }



def _safe_int(value, default=0):
    try:
        return max(0, int(value or 0))
    except Exception:
        return int(default or 0)


def _normalized_role_title(value):
    s = str(value or "")
    for char in ["\u2013", "\u2014", "\ufffd", "\u00e2\u20ac\u2013"]:
        s = s.replace(char, "-")
    return " ".join(s.split()).strip().casefold()


def exact_indeed_jobs_snapshot():
    """
    Read the authoritative role/count snapshot produced by chrome_cdp.py.

    Source of truth:
      candidate_total -> Indeed Jobs page "All"
      candidate_new   -> Indeed Jobs page "New"

    Dashboard/API polling only reads saved state; it never touches Chrome here.
    """
    try:
        snapshot = current_indeed_jobs_snapshot()
        if isinstance(snapshot, dict):
            roles = snapshot.get("roles")
            if not isinstance(roles, list):
                roles = []

            total = _safe_int(snapshot.get("total_applicants"))
            current_new = _safe_int(snapshot.get("total_new"))

            # Defensive recalculation when an older snapshot omitted totals.
            if roles and total <= 0:
                total = sum(
                    _safe_int(
                        role.get("candidate_total")
                        or role.get("candidate_total_hint")
                    )
                    for role in roles
                    if isinstance(role, dict)
                )

            if roles and current_new <= 0:
                current_new = sum(
                    _safe_int(
                        role.get("candidate_new")
                        or role.get("candidate_new_hint")
                    )
                    for role in roles
                    if isinstance(role, dict)
                )

            return {
                **snapshot,
                "roles": roles,
                "role_count": _safe_int(
                    snapshot.get("role_count"),
                    len(roles),
                ) or len(roles),
                "total_applicants": total,
                "total_new": current_new,
                "source": "indeed_jobs_all_new_exact",
            }
    except Exception:
        pass

    # Backward-compatible state fallback.
    try:
        raw = get_state("indeed_jobs_role_snapshot", "{}") or "{}"
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    roles = payload.get("roles")
    if not isinstance(roles, list):
        roles = []

    return {
        "captured_at": (
            payload.get("captured_at")
            or get_state("indeed_jobs_role_snapshot_at", "")
        ),
        "role_count": _safe_int(
            payload.get("role_count")
            or get_state("indeed_jobs_role_count", "0"),
            len(roles),
        ) or len(roles),
        "total_applicants": _safe_int(
            payload.get("total_applicants")
            or get_state("indeed_jobs_total_applicants", "0")
        ),
        "total_new": _safe_int(
            payload.get("total_new")
            or get_state("indeed_jobs_total_new", "0")
        ),
        "roles": roles,
        "source": "indeed_jobs_all_new_exact",
    }


def _merge_role_counts_from_indeed(role_rows, snapshot):
    """
    Preserve ranking/pipeline fields while replacing applicant-count hints with
    the exact Jobs-page All/New values.

    If Indeed has duplicate postings with the same title, counts are aggregated
    for title-based legacy role rows. The exact posting-level rows remain
    available in payload["indeed_jobs"]["roles"] with role_key/job_url.
    """
    rows = [
        dict(row)
        for row in (role_rows or [])
        if isinstance(row, dict)
    ]

    snapshot_roles = [
        dict(role)
        for role in (snapshot.get("roles") or [])
        if isinstance(role, dict)
    ]

    by_title = {}
    for role in snapshot_roles:
        key = _normalized_role_title(role.get("job_title"))
        if not key:
            continue

        bucket = by_title.setdefault(
            key,
            {
                "candidate_total": 0,
                "candidate_new": 0,
                "postings": 0,
                "statuses": [],
                "role_keys": [],
                "job_urls": [],
            },
        )

        bucket["candidate_total"] += _safe_int(
            role.get("candidate_total")
            or role.get("candidate_total_hint")
        )
        bucket["candidate_new"] += _safe_int(
            role.get("candidate_new")
            or role.get("candidate_new_hint")
        )
        bucket["postings"] += 1

        status = str(role.get("job_status") or "").strip()
        if status and status not in bucket["statuses"]:
            bucket["statuses"].append(status)

        role_key = str(role.get("role_key") or "").strip()
        if role_key:
            bucket["role_keys"].append(role_key)

        job_url = str(role.get("job_url") or "").strip()
        if job_url:
            bucket["job_urls"].append(job_url)

    for row in rows:
        key = _normalized_role_title(row.get("job_title"))
        exact = by_title.get(key)
        if not exact:
            continue

        total = exact["candidate_total"]
        current_new = exact["candidate_new"]

        row["candidate_total_hint"] = total
        row["candidate_new_hint"] = current_new
        row["candidate_total"] = total
        row["candidate_new"] = current_new
        row["applicant_count"] = total
        row["active_applicant_count"] = total
        row["indeed_posting_count"] = exact["postings"]
        row["indeed_job_statuses"] = exact["statuses"]
        row["indeed_role_keys"] = exact["role_keys"]
        row["indeed_job_urls"] = exact["job_urls"]
        row["count_source"] = "indeed_jobs_all_new_exact"

    return rows


def _apply_exact_indeed_overview(overview, snapshot):
    """
    Add/override the common overview count fields without removing any existing
    recruitment-pipeline data.
    """
    out = dict(overview or {})
    total = _safe_int(snapshot.get("total_applicants"))
    current_new = _safe_int(snapshot.get("total_new"))

    # Stable explicit fields for new frontend builds.
    out["indeed_total_applicants"] = total
    out["indeed_current_new"] = current_new
    out["indeed_role_count"] = _safe_int(snapshot.get("role_count"))
    out["indeed_jobs"] = snapshot

    # Common top-level names used by older/newer overview implementations.
    out["total_applicants"] = total
    out["current_new"] = current_new
    out["new_applicants"] = current_new

    summary = out.get("summary")
    if isinstance(summary, dict):
        summary = dict(summary)
        summary["total_applicants"] = total
        summary["all_applicants"] = total
        summary["current_new"] = current_new
        summary["new"] = current_new
        summary["new_applicants"] = current_new
        summary["count_source"] = "indeed_jobs_all_new_exact"
        out["summary"] = summary

    totals = out.get("totals")
    if isinstance(totals, dict):
        totals = dict(totals)
        totals["total_applicants"] = total
        totals["all_applicants"] = total
        totals["current_new"] = current_new
        totals["new"] = current_new
        out["totals"] = totals

    if isinstance(out.get("roles"), list):
        out["roles"] = _merge_role_counts_from_indeed(
            out.get("roles"),
            snapshot,
        )

    return out


def dashboard_payload(lite=False):
    """
    Resilient overview payload.

    One broken subsystem must not turn the whole dashboard into HTTP 500 or
    make saved candidates/settings appear to be missing.
    """
    errors = []

    def safe(label, fn, default):
        try:
            return fn()
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            try:
                log("ERROR", f"Dashboard {label} failed: {exc}")
            except Exception:
                pass
            return default

    settings = safe(
        "settings",
        public_settings,
        {
            "company_name": "Nunes",
            "company_email": "nuneslead@gmail.com",
            "smtp_app_password_set": False,
            "indeed_start_url": "",
            "indeed_candidates_url": "",
            "automation_enabled": False,
            "monitoring_enabled": True,
            "auto_scan": True,
            "auto_send": True,
            "allow_candidate_page_email_fallback": True,
            "local_ai_enabled": False,
            "ollama_url": "",
            "ollama_model": "",
            "subject_template": "Thank you for applying – {job_title}",
            "body_template": "",
            "hr_report_sender_email": "nunescbe@gmail.com",
            "hr_report_recipient": "nunescbe@gmail.com",
            "hr_report_smtp_app_password_set": False,
            "auto_send_role_reports": False,
            "daily_consolidated_report_enabled": True,
            "daily_report_time": "19:00",
            },
    )

    empty_stats = {
        "total": 0,
        "ready": 0,
        "sent": 0,
        "review": 0,
        "skipped": 0,
        "application_verified": 0,
        "last_scan_at": None,
        "last_new_count": "0",
        "last_visible_count": "0",
        "backlog_completed": False,
        "seen_count": 0,
        "pending_initial_new": 0,
    }
    st = safe("database statistics", stats, empty_stats)
    st = dict(st or empty_stats)

    indeed_jobs = safe(
        "Indeed Jobs exact snapshot",
        exact_indeed_jobs_snapshot,
        {
            "captured_at": None,
            "role_count": 0,
            "total_applicants": 0,
            "total_new": 0,
            "roles": [],
            "source": "indeed_jobs_all_new_exact",
        },
    )

    # IMPORTANT:
    # stats.total is the number of locally processed/stored applications.
    # The Overview "All Applicants" card must instead reflect Indeed's Jobs-page
    # All counts across current Open/Paused/Flagged roles.
    if _safe_int(indeed_jobs.get("role_count")) > 0:
        st["local_processed_total"] = _safe_int(st.get("total"))
        st["total"] = _safe_int(indeed_jobs.get("total_applicants"))
        st["all_applicants"] = _safe_int(
            indeed_jobs.get("total_applicants")
        )
        st["current_new"] = _safe_int(indeed_jobs.get("total_new"))
        st["indeed_total_applicants"] = st["all_applicants"]
        st["indeed_current_new"] = st["current_new"]
        st["count_source"] = "indeed_jobs_all_new_exact"

    chrome = safe("browser status", cached_dashboard_chrome_status, {
        "ok": False,
        "chrome_connected": False,
        "indeed_found": False,
        "message": "Browser status is starting",
        "cached": True,
    })
    auto_state = safe("automatic connection", auto_connection_snapshot, {
        "phase": "starting",
        "message": "Starting automatic browser connection",
    })
    live = safe("live detection", live_detection_snapshot, {
        "healthy": False,
        "status": "STARTING",
        "consecutive_failures": 0,
        "last_found_links": 0,
        "new_last_scan": 0,
        "detected_today": 0,
        "verified_today": 0,
        "review_today": 0,
        "sent_today": 0,
        "skipped_today": 0,
        "seen_today": 0,
        "last_seen_today_applicant": None,
    })

    role_rows = []
    role_status = {}
    role_error = None
    if not lite:
        try:
            role_status = ranking_status()
            role_rows = _merge_role_counts_from_indeed(
                list_roles(),
                indeed_jobs,
            )
        except Exception as exc:
            role_error = str(exc)
            errors.append(f"role ranking: {exc}")

    detected = None
    if chrome.get("indeed_found"):
        detected = {
            "url": settings.get("indeed_candidates_url", ""),
            "title": "Indeed Candidates",
        }

    configured = safe("email configuration", sender_configured, False)
    smtp_verified = safe(
        "email verification state",
        lambda: get_state("smtp_verified", "0") == "1",
        False,
    )

    payload = {
        "ok": len(errors) == 0,
        "version": APP_VERSION,
        "settings": settings,
        "stats": st,
        "indeed_jobs": indeed_jobs,
        "chrome": chrome,
        "detected_candidates_page": detected,
        "detected_candidates_error": None,
        "email_configured": configured,
        "email_verified": bool(configured and smtp_verified),
        "email_last_error": safe(
            "email error state", lambda: get_state("smtp_last_error"), None
        ),
        "email_transport": safe(
            "email transport state", lambda: get_state("smtp_transport"), None
        ),
        "automation_enabled": bool(settings.get("automation_enabled", True)),
        "live_detection": live,
        "auto_connection": auto_state,
        "applicants": [],
        "sent_responses": [],
        "logs": [],
        "role_review_roles": role_rows,
        "role_ranking_status": role_status,
        "role_ranking_error": role_error,
        "recruitment_pipeline": safe("recruitment pipeline", pipeline_status, {}),
        "core_recruitment": safe(
            "core recruitment",
            core_recruitment_status,
            {
                "service_live": True,
                "communication_mode": "EMAIL_ONLY",
                "indeed_live": False,
                "roles_found": 0,
                "ranking_running": True,
            },
        ),
        "backend_health": {
            "schema": runtime_schema_status(),
            "database": safe("database health", database_health, {"ok": False}),
            "settings_saved_at": safe(
                "settings saved state", lambda: get_state("settings_saved_at"), None
            ),
            "errors": errors,
        },
    }

    if not lite:
        payload["applicants"] = safe(
            "applicant list", lambda: list_applications(500), []
        )
        payload["sent_responses"] = safe(
            "sent history", lambda: list_sent_responses(1000), []
        )
        payload["logs"] = safe("activity list", lambda: list_logs(100), [])

    return payload


@app.get("/")
def root():
    return jsonify({
        "ok": True,
        "name": "Nunes Recruitment Console API",
        "version": APP_VERSION,
        "port": API_PORT,
    })


@app.get("/version")
def version():
    return jsonify({
        "version": APP_VERSION,
        "safe_indeed_login": True,
        "expected_google_account": "nuneslead@gmail.com",
        "google_password_stored_by_app": False,
        "communication_mode": "EMAIL_ONLY",
        "count_patch": "INDEED_ALL_NEW_EXACT_V1",
        "port": API_PORT,
        "ui_port": 5285,
    })


@app.get("/api/role-ranking-status")
def api_role_ranking_status():
    try:
        return jsonify({"role_ranking_status": ranking_status()})
    except Exception as exc:
        return jsonify({
            "role_ranking_status": {},
            "message": str(exc),
        }), 200


@app.get("/api/applicants")
def api_applicants():
    try:
        limit = min(max(int(request.args.get("limit", 500)), 1), 1500)
    except Exception:
        limit = 500
    return jsonify({"applicants": list_applications(limit)})


@app.get("/api/sent-responses")
def api_sent_responses():
    try:
        limit = min(max(int(request.args.get("limit", 1000)), 1), 2500)
    except Exception:
        limit = 1000
    return jsonify({"sent_responses": list_sent_responses(limit)})


@app.get("/api/activity")
def api_activity():
    try:
        limit = min(max(int(request.args.get("limit", 100)), 1), 500)
    except Exception:
        limit = 100
    return jsonify({"logs": list_logs(limit)})


@app.get("/api/dashboard")
def api_dashboard():
    try:
        lite = str(request.args.get("lite", "0")).lower() in {"1", "true", "yes"}
        return jsonify(dashboard_payload(lite=lite))
    except Exception as exc:
        try:
            log("ERROR", f"Dashboard payload failed: {exc}")
        except Exception:
            pass
        # Keep the frontend usable while a schema/component repairs itself.
        return jsonify({
            "ok": False,
            "message": "Dashboard backend is recovering.",
            "detail": str(exc),
            "version": APP_VERSION,
            "settings": public_settings(),
            "stats": {"total": 0, "ready": 0, "sent": 0, "review": 0, "skipped": 0},
            "backend_health": {"schema": runtime_schema_status(), "errors": [str(exc)]},
        }), 200



@app.post("/api/indeed/safe-login")
def api_indeed_safe_login():
    set_state("indeed_candidate_permission_status", "")
    set_state("indeed_candidate_permission_error", "")
    """
    One-time Google/Indeed login without debugging flags.

    This intentionally stops ONLY the NUNES Recruitment Chrome profile, then
    opens the same persistent profile without CDP. Normal Chrome is untouched.
    """
    try:
        reset_shared_chrome(
            "Safe Indeed sign-in requested"
        )
    except Exception:
        pass

    try:
        settings = load_settings()
        expected_google_email = str(
            settings.get("indeed_google_account_email")
            or settings.get("company_email")
            or "nuneslead@gmail.com"
        ).strip().lower()

        result = launch_recruitment_login_chrome(
            "https://employers.indeed.com/",
            expected_google_email=expected_google_email,
        )
    except Exception as exc:
        result = {
            "ok": False,
            "message": str(exc),
        }

    if not result.get("ok"):
        set_state(
            "indeed_safe_login_mode",
            "0",
        )
        return jsonify({
            **result,
            "ok": False,
        }), 500

    _set_auto_connection_state(
        phase="safe_sign_in",
        message=result.get("message"),
        last_error=None,
        next_connect_attempt_at=time.monotonic() + 2,
    )

    return jsonify({
        **result,
        "ok": True,
        "auto_connection": auto_connection_snapshot(),
    })


@app.post("/api/connect")
def api_connect():
    set_state("indeed_candidate_permission_status", "")
    set_state("indeed_candidate_permission_error", "")
    # A manual Connect/Allow request is a strong recovery action:
    # discard stale browser websocket/session state and rediscover Chrome.
    try:
        reset_shared_chrome("Manual Chrome reconnect requested")
    except Exception:
        pass

    state = automatic_connection_tick(force_connect=True)

    if state.get("phase") == "ready":
        return jsonify({
            "ok": True,
            "message": "Chrome and Indeed Candidates are connected.",
            "auto_connection": state,
        })

    return jsonify({
        "ok": state.get("phase") not in {"error", "permission_required"},
        "message": state.get("message") or "Automatic connection is running.",
        "detail": state.get("last_error"),
        "auto_connection": state,
    }), (409 if state.get("phase") == "permission_required" else 200)







@app.post("/api/indeed/retry-candidate-access")
def api_retry_candidate_access():
    set_state(
        "indeed_candidate_permission_status",
        "",
    )
    set_state(
        "indeed_candidate_permission_error",
        "",
    )
    _set_auto_connection_state(
        phase="connected",
        message="Retrying Manage candidates access once",
        last_error=None,
        next_connect_attempt_at=0.0,
    )

    try:
        state = automatic_connection_tick(
            force_connect=True
        )
    except Exception as exc:
        state = auto_connection_snapshot()
        state["last_error"] = str(exc)

    return jsonify({
        "ok": True,
        "auto_connection": state,
        "permission_status": get_state(
            "indeed_candidate_permission_status",
            "",
        ),
    })


@app.get("/api/indeed/permission-status")
def api_indeed_permission_status():
    return jsonify({
        "ok": True,
        "status": get_state(
            "indeed_candidate_permission_status",
            "UNKNOWN",
        ),
        "permission": get_state(
            "indeed_candidate_permission",
            "",
        ),
        "error": get_state(
            "indeed_candidate_permission_error",
            "",
        ),
        "expected_google_account": (
            load_settings().get("indeed_google_account_email")
            or "nuneslead@gmail.com"
        ),
        "roles_can_continue": True,
        "candidate_processing_requires_permission": True,
    })


@app.get("/api/role-discovery/diagnostics")
def api_role_discovery_diagnostics():
    return jsonify({
        "ok": True,
        "status": get_state(
            "role_discovery_status",
            "STARTING",
        ),
        "roles_found": int(
            get_state(
                "role_discovery_roles_found",
                "0",
            )
            or 0
        ),
        "jobs_rows_parsed": int(
            get_state(
                "indeed_jobs_discovery_rows",
                "0",
            )
            or 0
        ),
        "jobs_total_hint": int(
            get_state(
                "indeed_jobs_discovery_total_hint",
                "0",
            )
            or 0
        ),
        "exact_total_applicants": _safe_int(
            exact_indeed_jobs_snapshot().get("total_applicants")
        ),
        "exact_current_new": _safe_int(
            exact_indeed_jobs_snapshot().get("total_new")
        ),
        "exact_role_count": _safe_int(
            exact_indeed_jobs_snapshot().get("role_count")
        ),
        "target_url": get_state(
            "indeed_jobs_discovery_target_url"
        ),
        "used_existing_jobs_tab": (
            get_state(
                "indeed_jobs_discovery_used_existing_tab",
                "0",
            )
            == "1"
        ),
        "sample": get_state(
            "indeed_jobs_discovery_sample"
        ),
        "last_error": get_state(
            "indeed_jobs_discovery_last_error"
        ),
        "last_at": get_state(
            "indeed_jobs_discovery_last_at"
        ),
        "current_roles": list_roles(),
        "indeed_auth_status": get_state(
            "indeed_auth_status",
            "UNKNOWN",
        ),
        "duplicate_tabs_closed": int(
            get_state(
                "indeed_duplicate_tabs_closed",
                "0",
            )
            or 0
        ),
        "tab_cleanup_last_at": get_state(
            "indeed_tab_cleanup_last_at"
        ),
    })


@app.get("/api/final-status")
def api_final_status():
    """
    One endpoint for installation/version/live-readiness diagnostics.
    """
    version_payload = {
        "expected_version": APP_VERSION,
        "api_version": APP_VERSION,
        "version_ok": APP_VERSION == "V11.11.24",
    }

    return jsonify({
        "ok": True,
        **version_payload,
        "schema": runtime_schema_status(),
        "chrome": cached_dashboard_chrome_status(),
        "auto_connection": auto_connection_snapshot(),
        "live_detection": live_detection_snapshot(),
        "roles": ranking_status(),
        "pipeline": pipeline_status(),
        "core_recruitment": core_recruitment_status(),
        "persistent": persistence_status(
            settings=load_settings(),
        ),
    })


@app.get("/api/chrome/diagnostics")
def api_chrome_diagnostics():
    current = chrome_status()
    auto = auto_connection_snapshot()
    return jsonify({
        "ok": bool(current.get("ok")),
        "version": APP_VERSION,
        "chrome": current,
        "auto_connection": auto,
        "devtools_active_port_default": str(devtools_active_port_path()),
        "last_working_port": get_state("chrome_last_working_devtools_port"),
        "last_working_source": get_state("chrome_last_working_devtools_source"),
        "help_opened_at": get_state("chrome_remote_debugging_help_opened_at"),
        "recruitment_chrome_profile_initialized": recruitment_chrome_profile_initialized(),
        "recruitment_chrome_bootstrapped": get_state("recruitment_chrome_bootstrapped", "0") == "1",
        "recruitment_chrome_last_launch_at": get_state("recruitment_chrome_last_launch_at"),
        "safe_login_mode": get_state("indeed_safe_login_mode", "0") == "1",
        "safe_login_started_at": get_state("indeed_safe_login_started_at"),
        "safe_login_completed_at": get_state("indeed_safe_login_completed_at"),
        "expected_google_account": (
            load_settings().get("indeed_google_account_email")
            or "nuneslead@gmail.com"
        ),
        "google_password_stored_by_app": False,
        "candidate_permission_status": get_state(
            "indeed_candidate_permission_status",
            "UNKNOWN",
        ),
        "candidate_permission": get_state(
            "indeed_candidate_permission",
            "",
        ),
        "candidate_permission_error": get_state(
            "indeed_candidate_permission_error",
            "",
        ),
    })


@app.post("/api/open-indeed")
def api_open_indeed():
    try:
        result = open_indeed_in_existing_chrome(
            load_settings().get("indeed_start_url")
        )
        return jsonify({
            "ok": True,
            "message": "Indeed opened in your existing Chrome.",
            "page": result,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "message": str(e),
        }), 500


@app.post("/api/activate")
def api_activate():
    try:
        detected = bind_candidates_page_if_available(reset_catchup=True)
        return jsonify({
            "ok": True,
            "message": "Candidates detected. Monitoring is active.",
            "page": detected,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "message": str(e),
        }), 409



@app.post("/api/automation/start")
def api_automation_start():
    s = load_settings()
    s["automation_enabled"] = True
    s["monitoring_enabled"] = True
    set_state("live_monitor_enabled", "1")
    set_state("live_monitor_last_error", "")
    set_state("live_monitor_consecutive_failures", "0")
    set_state("live_monitor_last_success_at", "")
    set_state("live_monitor_scan_mode", "starting")
    set_state(
        "live_monitor_started_at",
        datetime.now(timezone.utc).isoformat(),
    )
    set_state("live_monitor_last_found_links", "0")
    set_state("last_new_count", "0")
    s["auto_scan"] = True
    s["auto_send"] = True
    save_settings(s)

    # Immediate production wake-up. The background threads remain the owners
    # of scanning/sending, but they do not wait for their next timer tick.
    scan_wake_event.set()
    outbox_wake_event.set()
    role_review_wake_event.set()
    pipeline_wake_event.set()

    _set_auto_connection_state(
        phase="starting",
        message="Automation starting",
        last_error=None,
        next_connect_attempt_at=0.0,
        next_indeed_open_at=0.0,
    )

    # Return Start immediately. Chrome/Indeed can take time to render and must
    # never make the Start button return HTTP 500 or sit waiting.
    threading.Thread(
        target=_automation_connection_kick,
        daemon=True,
        name="automation-start-connection",
    ).start()

    return jsonify({
        "ok": True,
        "message": "Automation is ON.",
        "settings": public_settings(),
    })


@app.post("/api/automation/stop")
def api_automation_stop():
    # V11.11.2 is deliberately always-on while the local recruitment service
    # is running. STOP.bat remains the explicit full-service emergency stop.
    enforce_always_on_mode()
    scan_wake_event.set()
    outbox_wake_event.set()
    role_review_wake_event.set()
    pipeline_wake_event.set()
    return jsonify({
        "ok": False,
        "message": "24/7 Live mode is enabled. Use STOP.bat to stop the local recruitment service.",
        "settings": public_settings(),
    }), 409


@app.post("/api/check-now")
def api_check_now():
    if scan_lock.locked():
        return jsonify({
            "ok": False,
            "message": "A monitoring check is already running.",
        }), 409

    try:
        result = execute_monitor_check()
        return jsonify({
            "ok": True,
            "message": "Monitoring check completed.",
            "result": result,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "message": str(e),
        }), 409


@app.get("/api/settings")
def api_settings_get():
    try:
        cdp_diag = cdp_session_recovery_snapshot()
    except Exception as exc:
        cdp_diag = {"recoveries": 0, "last_recovery_at": None, "error": str(exc)}

    return jsonify({
        "ok": True,
        "settings": public_settings(),
        "email_configured": sender_configured(),
        "email_verified": get_state("smtp_verified", "0") == "1",
        "email_last_error": get_state("smtp_last_error"),
        "email_transport": get_state("smtp_transport"),
        "settings_saved_at": get_state("settings_saved_at"),
        "cdp_session_recovery": cdp_diag,
    })


def _verify_mail_after_settings_save():
    """Do SMTP/network work after Save has already returned to the UI."""
    try:
        current = load_settings()
        if not (current.get("smtp_app_password") or "").replace(" ", "").strip():
            set_state("smtp_verified", "0")
            return

        result = smtp_health_check(current)
        if result.get("ok"):
            try:
                send_all_ready()
            except Exception as exc:
                log("WARN", f"Pending mail retry after settings save: {exc}")
    except Exception as exc:
        try:
            log("WARN", f"Background Gmail verification after settings save: {exc}")
        except Exception:
            pass


@app.post("/api/settings")
def api_settings():
    payload = request.get_json(silent=True) or {}
    s = load_settings()

    allowed = [
        "company_email",
        "indeed_google_account_email",
        "company_name",
        "indeed_start_url",
        "monitoring_enabled",
        "auto_scan",
        "auto_send",
        "allow_candidate_page_email_fallback",
        "local_ai_enabled",
        "ollama_url",
        "ollama_model",
        "ranking_weights",
        "ranking_strong_threshold",
        "ranking_good_threshold",
        "ranking_moderate_threshold",
        "ranking_embedding_model",
        "subject_template",
        "body_template",
        "interview_subject_template",
        "interview_body_template",
        "hr_report_recipient",
        "daily_consolidated_report_enabled",
        "daily_report_time",
    ]

    for key in allowed:
        if key in payload:
            s[key] = payload[key]

    if "ranking_weights" in payload:
        proposed = payload.get("ranking_weights") or {}
        categories = set(CATEGORY_WEIGHTS)
        try:
            cleaned = {key: float(proposed[key]) for key in categories}
        except (KeyError, TypeError, ValueError):
            return jsonify({"ok": False, "message": "All six ranking weights must be numbers."}), 400
        if any(value < 0 or value > 100 for value in cleaned.values()) or sum(cleaned.values()) <= 0:
            return jsonify({"ok": False, "message": "Ranking weights must be between 0 and 100 and at least one must be nonzero."}), 400
        s["ranking_weights"] = cleaned

    thresholds = [
        float(s.get("ranking_moderate_threshold", 50)),
        float(s.get("ranking_good_threshold", 70)),
        float(s.get("ranking_strong_threshold", 85)),
    ]
    if not (0 <= thresholds[0] <= thresholds[1] <= thresholds[2] <= 100):
        return jsonify({"ok": False, "message": "Thresholds must satisfy 0 ≤ Moderate ≤ Good ≤ Strong ≤ 100."}), 400
    if str(s.get("ranking_embedding_model") or "") != EMBEDDING_MODEL:
        return jsonify({"ok": False, "message": f"This installation currently supports the local embedding model {EMBEDDING_MODEL}."}), 400

    # V11.11.2 always-on policy. These cannot be disabled from the browser UI.
    s["automation_enabled"] = True
    s["monitoring_enabled"] = True
    s["auto_scan"] = True
    s["auto_send"] = True

    if "smtp_app_password" in payload:
        pw = str(payload.get("smtp_app_password") or "").replace(" ", "").strip()
        if pw:
            s["smtp_app_password"] = pw

    if "hr_report_smtp_app_password" in payload:
        hr_pw = str(payload.get("hr_report_smtp_app_password") or "").replace(" ", "").strip()
        if hr_pw:
            s["hr_report_smtp_app_password"] = hr_pw

    saved = save_settings(s)
    sync_persistent_profile(settings=saved)
    set_state("settings_saved_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    try:
        resume_config_waiting_notifications()
        pipeline_wake_event.set()
    except Exception as exc:
        log("WARN", f"Recruitment pipeline settings refresh waiting: {exc}")

    # Saving is local and immediate. Gmail verification can involve DNS/network
    # and must never make the Save button look broken or lose the just-saved UI.
    threading.Thread(
        target=_verify_mail_after_settings_save,
        daemon=True,
        name="settings-mail-verify",
    ).start()

    return jsonify({
        "ok": True,
        "message": (
            "Settings saved. Gmail verification is running in the background."
            if sender_configured()
            else "Settings saved. Add the Gmail App Password to enable sending."
        ),
        "settings": public_settings(),
        "email_configured": sender_configured(),
        "email_verified": get_state("smtp_verified", "0") == "1",
        "settings_saved_at": get_state("settings_saved_at"),
    })



@app.post("/api/mail/test")
def api_mail_test():
    result = smtp_health_check(load_settings())

    return jsonify({
        "ok": bool(result.get("ok")),
        "message": result.get("message"),
        "transport": result.get("transport"),
    }), (200 if result.get("ok") else 409)


@app.post("/api/mail/retry")
def api_mail_retry():
    check = smtp_health_check(load_settings())

    if not check.get("ok"):
        return jsonify({
            "ok": False,
            "message": check.get("message") or "Gmail authentication failed.",
            "mail_check": check,
        }), 409

    result = send_all_ready()

    return jsonify({
        "ok": True,
        "message": (
            f"Retry completed. {result.get('sent', 0)} message(s) sent "
            f"from {result.get('attempted', 0)} pending candidate(s)."
        ),
        "result": result,
    })


@app.post("/api/send/<int:app_id>")
def api_send(app_id):
    row = get_by_id(app_id)

    if not row:
        return jsonify({
            "ok": False,
            "message": "Applicant not found.",
        }), 404

    try:
        result = send_thank_you(row["source_key"])
        return jsonify({
            "ok": True,
            "message": f"Email result: {result['status']}.",
            "result": result,
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "message": str(e),
        }), 500


@app.get("/resume/<int:app_id>")
def resume(app_id):
    row = get_by_id(app_id)

    if not row or not row.get("resume_path"):
        return jsonify({
            "ok": False,
            "message": "No resume is available.",
        }), 404

    p = Path(row["resume_path"])

    if not p.exists():
        return jsonify({
            "ok": False,
            "message": "Resume file is missing.",
        }), 404

    return send_from_directory(
        p.parent,
        p.name,
        as_attachment=False,
    )



@app.get("/api/role-review/roles")
def api_role_review_roles():
    try:
        snapshot = exact_indeed_jobs_snapshot()
        roles = operations_overview(
            limit_roles=25,
            limit_recent=1,
        ).get("roles", [])
        roles = _merge_role_counts_from_indeed(
            roles,
            snapshot,
        )
        roles = [
            role
            for role in roles
            if not str(role.get("job_title") or "").strip().lower().startswith(
                ("sponsorship ended ", "upgrade to premium")
            )
        ]
        return jsonify({"ok": True, "roles": roles})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.get("/api/role-review/role")
def api_role_review_role():
    title = str(request.args.get("job_title") or "").strip()
    if not title:
        return jsonify({"ok": False, "message": "job_title is required."}), 400
    try:
        return jsonify({"ok": True, **decorate_role_payload(role_review_payload(title))})
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/role-review/description")
def api_role_review_description():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("job_title") or "").strip()
    description = str(payload.get("job_description") or "")
    try:
        role = save_role_description(title, description)
        result = analyze_role(title, force=True, limit=800)
        return jsonify({
            "ok": True,
            "message": "Job description saved and resume evidence refreshed.",
            "role": role,
            "analysis": result,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/role-review/analyze")
def api_role_review_analyze():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("job_title") or "").strip()
    if not title:
        return jsonify({"ok": False, "message": "job_title is required."}), 400
    try:
        result = analyze_role(title, force=True, limit=800)
        return jsonify({
            "ok": True,
            "message": f"Resume evidence refreshed for {result.get('analyzed', 0)} applicant(s).",
            "result": result,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 500


@app.post("/api/role-review/candidate/<int:app_id>/status")
def api_role_review_candidate_status(app_id):
    return jsonify({
        "ok": False,
        "message": (
            "Manual shortlist is disabled in V11.10. "
            "Role ranking and shortlist are automatic."
        ),
    }), 410


@app.get("/api/ranking/settings")
def api_ranking_settings():
    settings = load_settings()
    return jsonify({
        "ok": True,
        "weights": settings.get("ranking_weights") or dict(CATEGORY_WEIGHTS),
        "strong_threshold": float(settings.get("ranking_strong_threshold", 85)),
        "good_threshold": float(settings.get("ranking_good_threshold", 70)),
        "moderate_threshold": float(settings.get("ranking_moderate_threshold", 50)),
        "embedding_model": settings.get("ranking_embedding_model") or EMBEDDING_MODEL,
        "embedding": embedding_status(),
    })


@app.post("/api/role-review/knockouts")
def api_role_review_knockouts():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("job_title") or "").strip()
    requirements = payload.get("requirements") or []
    if not title or not isinstance(requirements, list):
        return jsonify({"ok": False, "message": "job_title and a requirements array are required."}), 400
    try:
        role = save_role_knockout_requirements(title, requirements)
        role_review_wake_event.set()
        return jsonify({
            "ok": True,
            "message": "Knockout rules saved. Candidates are still ranked and shown; missing rules are flagged, not rejected.",
            "role": role,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


def _reanalyze_roles_in_background(job_titles):
    try:
        for title in job_titles:
            result = analyze_role(title, force=True, limit=10000)
            log("INFO", f"RAG REANALYSIS: role={title}; analyzed={result.get('analyzed', 0)}; waiting={result.get('errors', 0)}.")
        set_state("ranking_reanalysis_state", "COMPLETE")
        set_state("ranking_reanalysis_completed_at", now())
    except Exception as exc:
        set_state("ranking_reanalysis_state", "FAILED")
        set_state("ranking_reanalysis_error", str(exc)[:1000])
        log("ERROR", f"RAG REANALYSIS failed: {exc}")
    finally:
        set_state("ranking_reanalysis_running", "0")


@app.post("/api/ranking/reanalyze")
def api_ranking_reanalyze():
    if get_state("ranking_reanalysis_running", "0") == "1":
        return jsonify({"ok": False, "message": "A ranking refresh is already running."}), 409
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("job_title") or "").strip()
    titles = [title] if title else [str(role.get("job_title") or "").strip() for role in list_roles()]
    titles = [value for value in titles if value]
    if not titles:
        return jsonify({"ok": False, "message": "No active roles are available to reanalyze."}), 409
    set_state("ranking_reanalysis_running", "1")
    set_state("ranking_reanalysis_state", "RUNNING")
    threading.Thread(
        target=_reanalyze_roles_in_background,
        args=(titles,),
        daemon=True,
        name="rag-full-reanalysis",
    ).start()
    return jsonify({"ok": True, "message": f"Local RAG reanalysis started for {len(titles)} role(s). Existing records and email history are preserved."})



@app.get("/api/persistence/status")
def api_persistence_status():
    return jsonify({
        "ok": True,
        "persistence": persistence_status(settings=load_settings()),
    })


@app.get("/api/recruitment/status")
def api_recruitment_status():
    return jsonify({"ok": True, "pipeline": pipeline_status()})


@app.get("/api/recruitment/overview")
def api_recruitment_overview():
    try:
        role_limit = min(max(int(request.args.get("roles", 6)), 1), 25)
    except Exception:
        role_limit = 6
    try:
        recent_limit = min(max(int(request.args.get("recent", 8)), 1), 50)
    except Exception:
        recent_limit = 8

    try:
        snapshot = exact_indeed_jobs_snapshot()
        overview = _apply_exact_indeed_overview(
            operations_overview(role_limit, recent_limit),
            snapshot,
        )
        return jsonify({
            "ok": True,
            **overview,
            "core_recruitment": core_recruitment_status(),
            "role_discovery": {
                "status": get_state("role_discovery_status", "STARTING"),
                "last_success_at": get_state("role_discovery_last_success_at"),
                "last_error": get_state("role_discovery_last_error"),
                "roles_found": int(
                    get_state("role_discovery_roles_found", "0")
                    or 0
                ),
                "exact_total_applicants": _safe_int(
                    snapshot.get("total_applicants")
                ),
                "exact_current_new": _safe_int(
                    snapshot.get("total_new")
                ),
                "exact_role_count": _safe_int(
                    snapshot.get("role_count")
                ),
                "counts_captured_at": snapshot.get("captured_at"),
            },
        })
    except Exception as exc:
        return jsonify({
            "ok": False,
            "roles": [],
            "recent": [],
            "message": str(exc),
        }), 200


@app.post("/api/recruitment/approve/<int:app_id>")
def api_recruitment_approve(app_id):
    try:
        result = approve_candidate_for_interview(app_id)
        dispatch_res = process_notifications_once(limit=5)
        pipeline_wake_event.set()
        date_str = result.get('flow', {}).get('interview_date') or interview_schedule_preview().get('interview_date')
        return jsonify({
            "ok": True,
            "message": f"HR approval saved. Interview invitation scheduled for {date_str} and notification sent.",
            **result,
            "dispatch": dispatch_res,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.post("/api/recruitment/approve-top")
def api_recruitment_approve_top():
    payload = request.get_json(silent=True) or {}
    try:
        result = approve_top_candidates_for_interview(
            payload.get("job_title"),
            payload.get("count"),
        )
        dispatch_res = process_notifications_once(limit=25)
        pipeline_wake_event.set()
        return jsonify({
            "ok": True,
            "message": (
                f"HR approved the top {result.get('selected_count', 0)} candidate(s). "
                f"Interview emails dispatched for {result.get('interview_date')}."
            ),
            **result,
            "dispatch": dispatch_res,
        })
    except Exception as exc:
        return jsonify({"ok": False, "message": str(exc)}), 400


@app.get("/api/recruitment/interview-schedule")
def api_recruitment_interview_schedule():
    return jsonify({"ok": True, **interview_schedule_preview()})


@app.post("/api/recruitment/hr-mail/test")
def api_hr_report_mail_test():
    result = hr_report_mail_health_check()
    if result.get("ok"):
        resume_config_waiting_notifications()
        pipeline_wake_event.set()
        return jsonify(result)
    return jsonify(result), 409


@app.post("/api/recruitment/daily-report/send-now")
def api_daily_report_send_now():
    result = queue_daily_consolidated_report(force=True)
    if result.get("queued") or result.get("already_exists"):
        pipeline_wake_event.set()
    return jsonify({"ok": True, "result": result})


@app.errorhandler(Exception)
def json_error_handler(exc):
    if isinstance(exc, HTTPException):
        return jsonify({
            "ok": False,
            "message": exc.description or exc.name,
            "status": exc.code,
        }), exc.code

    detail = str(exc)
    try:
        stack = traceback.format_exc(limit=12)
        log("ERROR", f"Unhandled backend error: {detail}\n{stack}")
    except Exception:
        pass
    return jsonify({
        "ok": False,
        "message": "Internal backend error",
        "detail": detail,
    }), 500



@app.get("/api/self-test")
def api_self_test():
    result = critical_runtime_self_test()
    return jsonify(result), (200 if result.get("ok") else 500)


@app.get("/api/data-status")
def api_data_status():
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "schema": runtime_schema_status(),
        "database": database_health(),
        "settings": {
            "company_name": public_settings().get("company_name"),
            "sender": public_settings().get("company_email"),
            "gmail_password_saved": public_settings().get("smtp_app_password_set"),
            "saved_at": get_state("settings_saved_at"),
        },
    })


@app.get("/health")
def health():
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "stats": stats(),
        "chrome": chrome_status(),
        "auto_connection": auto_connection_snapshot(),
        "devtools_active_port_file": str(devtools_active_port_path()),
    })


if __name__ == "__main__":
    enforce_always_on_mode()
    sync_persistent_profile(settings=load_settings())
    if not ensure_runtime_schema():
        print(
            "[ERROR] Persistent-data migration failed: "
            + str(_runtime_schema_error),
            flush=True,
        )
        raise RuntimeError(_runtime_schema_error or "Runtime schema initialization failed")

    startup_test = critical_runtime_self_test()
    if not startup_test.get("ok"):
        print(
            "[ERROR] Runtime self-test failed: "
            + "; ".join(startup_test.get("errors") or []),
            flush=True,
        )
        raise RuntimeError("Runtime self-test failed")

    log("INFO", "V11.11.24 all-candidates RAG runtime self-test passed.")

    threading.Thread(
        target=auto_connection_loop,
        daemon=True,
        name="chrome-auto-connection",
    ).start()

    threading.Thread(
        target=background_loop,
        daemon=True,
        name="indeed-monitor",
    ).start()

    threading.Thread(
        target=live_outbox_loop,
        daemon=True,
        name="mail-outbox",
    ).start()

    threading.Thread(
        target=role_review_loop,
        daemon=True,
        name="role-review-evidence",
    ).start()

    threading.Thread(
        target=recruitment_pipeline_loop,
        daemon=True,
        name="recruitment-pipeline",
    ).start()


    app.run(
        host="127.0.0.1",
        port=API_PORT,
        debug=False,
        threaded=True,
        use_reloader=False,
    )
