from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urldefrag, urlsplit, urlunsplit, parse_qsl, urlencode, quote, unquote

import websocket

from config import load_settings
from database import (
    get_by_source_key,
    backlog_completed,
    get_state,
    set_state,
    remember_seen_candidate,
    is_seen_candidate,
    seen_candidate,
    mark_seen_processed,
    pending_initial_new_count,
    all_candidates_backfill_completed,
    pending_seen_unprocessed_count,
    touch_visible_applications,
)

BASE_DIR = Path(__file__).resolve().parent
CANDIDATE_NAME_SAFETY_PATCH = "V11.11.24-CANDIDATE-NAME-SAFETY"
RETRY_CLOCK_PATCH = "V11.11.24-DETAIL-RETRY-CLOCK"
JOBS_QUEUE_RECOVERY_PATCH = "V11.11.24-JOBS-QUEUE-RECOVERY"
CANDIDATE_COLLECTOR_PATCH = "V11.11.24-COLLECTOR-V2"
RESUME_CAPTURE_PATCH = "V11.11.24-CANDIDATE-DETAIL-RESUME"

EMAILISH_INDEED = re.compile(r"indeed\.", re.I)

INDEED_CURRENT_JOBS_URL = (
    "https://employers.indeed.com/jobs?"
    "status=open%2Cpaused&claimed=false&createdOnIndeed=true&tab=0&"
    "sortDirection=DESC&sortField=datePostedOnIndeed"
)


def new_candidates_queue_url(url):
    value = (url or "").strip()
    if not value or "indeed." not in value.lower():
        return value

    try:
        parts = urlsplit(value)
        low_path = (parts.path or "").lower()
        if not any(x in low_path for x in ["candidate", "applicant", "application"]):
            return value

        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        query["statusName"] = "New"
        query["tab"] = "manage"

        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                parts.path,
                urlencode(query),
                parts.fragment,
            )
        )
    except Exception:
        return value


def all_candidates_queue_url(url):
    """Normalize an Indeed Manage Candidates URL to the unfiltered All view."""
    value = (url or "").strip()
    if not value or "indeed." not in value.lower():
        return value

    try:
        parts = urlsplit(value)
        low_path = (parts.path or "").lower()
        if not any(x in low_path for x in ["candidate", "applicant", "application"]):
            return value

        pairs = parse_qsl(parts.query, keep_blank_values=True)
        list_query = next(
            (v for k, v in pairs if k.lower() == "listquery"),
            "",
        )

        if list_query:
            decoded_list_query = unquote(list_query)
            try:
                padding = "=" * (-len(list_query) % 4)
                decoded_list_query = base64.b64decode(
                    list_query + padding
                ).decode("utf-8")
                decoded_list_query = unquote(decoded_list_query)
            except Exception:
                pass
            query = dict(parse_qsl(decoded_list_query, keep_blank_values=True))
        elif re.search(r"/view/?$", parts.path, re.I):
            # A candidate-detail URL cannot be used as the collection queue.
            query = {}
        else:
            query = {k: v for k, v in pairs if k.lower() != "statusname"}

        path = re.sub(r"/view/?$", "", parts.path, flags=re.I)
        if not path:
            path = "/candidates"
        query = {k: v for k, v in query.items() if k.lower() != "statusname"}
        query["tab"] = "manage"
        query["statusName"] = "All"

        return urlunsplit((
            parts.scheme, parts.netloc, path, urlencode(query), parts.fragment
        ))
    except Exception:
        return value


WAIT_CANDIDATES_READY_JS = r"""
(async () => {
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const started = Date.now();

  while (Date.now() - started < 9000) {
    const title = document.title || '';
    const body = document.body?.innerText || '';

    const isChallenge = (
      /just a moment/i.test(title)
      || /turnstile/i.test(body)
      || /checking if the site connection is secure/i.test(body)
      || Boolean(document.querySelector('#turnstile-wrapper, .cf-turnstile, iframe[src*="challenges.cloudflare.com"]'))
    );
    if (isChallenge) {
      return {ready: false, challenge: true, bodyPreview: body.slice(0, 5000)};
    }

    const hasApplicationRows = (
      /\bapplied\s+to\s*:/i.test(body)
      || Boolean(document.querySelector('[data-testid*="candidate" i], [data-testid*="applicant" i], [role="row"], tr[data-testid]'))
      || /\b(new|reviewing|contacting|interviewing|selected|rejected|hired)\b/i.test(body)
    );
    const hasEmptyState = /no\s+(?:applicants|candidates|applications)/i.test(body);
    const hasCandidateDetail = /resume|contact\s+information|candidate\s+email/i.test(body);
    const ready = hasApplicationRows || hasEmptyState || hasCandidateDetail;

    if (ready) {
      window.scrollTo(0, 0);

      for (const el of document.querySelectorAll(
        '[role="main"] [style*="overflow"],'
        + '[data-testid*="candidate" i] [style*="overflow"],'
        + '[class*="candidate" i] [style*="overflow"]'
      )) {
        try {
          if (el.scrollHeight > el.clientHeight + 120) {
            el.scrollTop = 0;
          }
        } catch (_) {}
      }

      return {ready: true, challenge: false, bodyPreview: body.slice(0, 5000)};
    }

    await sleep(250);
  }

  const finalTitle = document.title || '';
  const finalBody = document.body?.innerText || '';
  const isChallenge = (
    /just a moment/i.test(finalTitle)
    || /turnstile/i.test(finalBody)
    || /checking if the site connection is secure/i.test(finalBody)
    || Boolean(document.querySelector('#turnstile-wrapper, .cf-turnstile, iframe[src*="challenges.cloudflare.com"]'))
  );

  return {
    ready: false,
    challenge: isChallenge,
    bodyPreview: finalBody.slice(0, 5000),
  };
})()
"""


class ChromeConnectionError(RuntimeError):
    pass


class IndeedChallengeError(ChromeConnectionError):
    """Indeed is presenting a Cloudflare / Turnstile security challenge."""
    pass


class IndeedCandidatePermissionError(ChromeConnectionError):
    """Indeed Employer account is signed in but lacks candidate access."""

    def __init__(self, message, permission="Hosted_Candidate", url=""):
        super().__init__(message)
        self.permission = permission
        self.url = url



import shutil


_chrome_process_cache = {
    "at": 0.0,
    "rows": [],
}
_chrome_process_cache_lock = threading.RLock()


def _hidden_creationflags():
    if os.name != "nt":
        return 0
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def chrome_local_state_path() -> Path:
    return chrome_user_data_dir() / "Local State"


def chrome_profile_directory_from_local_state():
    """
    Return Chrome's last-used profile directory, e.g. Default or Profile 6.

    Passing --profile-directory explicitly prevents Chrome's multi-profile
    "Who's using Chrome?" picker from appearing when the console opens a
    helper/settings tab.
    """
    try:
        payload = json.loads(
            chrome_local_state_path().read_text(
                encoding="utf-8",
                errors="replace",
            )
        )
    except Exception:
        return ""

    profile = payload.get("profile") or {}

    for key in ("last_used", "last_active_profiles"):
        value = profile.get(key)

        if isinstance(value, str) and value.strip():
            candidate = value.strip()
        elif isinstance(value, list) and value:
            candidate = str(value[-1] or "").strip()
        else:
            candidate = ""

        if candidate:
            path = chrome_user_data_dir() / candidate
            if path.exists():
                return candidate

    return ""


def preferred_chrome_profile_directory():
    """
    Prefer a profile explicitly used by a running Chrome process.
    Fall back to Chrome Local State's last-used profile.
    """
    try:
        for command_line in _windows_chrome_command_lines(force=False):
            value = _extract_flag(command_line, "profile-directory")
            if value:
                path = chrome_user_data_dir() / value
                if path.exists():
                    return value
    except Exception:
        pass

    return chrome_profile_directory_from_local_state()


def chrome_user_data_dir() -> Path:
    """
    Chrome Stable default user-data root on Windows.
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise ChromeConnectionError(
            "LOCALAPPDATA is not available on this Windows account."
        )
    return Path(local) / "Google" / "Chrome" / "User Data"


def nunes_automation_chrome_dir() -> Path:
    """
    Persistent NUNES Recruitment Chrome profile.

    It uses a NON-DEFAULT Chrome user-data directory so Chrome can expose a
    DevTools endpoint without relying on the blank chrome://inspect helper page.
    The user signs into Indeed once; the session is then reused on later starts.
    """
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        raise ChromeConnectionError(
            "LOCALAPPDATA is not available on this Windows account."
        )
    return (
        Path(local)
        / "NunesRecruitmentConsole"
        / "ChromeAutomationProfile"
    )


def devtools_active_port_path() -> Path:
    # Kept for backward compatibility and diagnostics.
    return chrome_user_data_dir() / "DevToolsActivePort"


def _chrome_binary_candidates():
    candidates = [
        os.path.expandvars(
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"
        ),
        os.path.expandvars(
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
        ),
        os.path.expandvars(
            r"%LocalAppData%\Google\Chrome\Application\chrome.exe"
        ),
    ]

    return [
        Path(value)
        for value in candidates
        if value
        and "%" not in value
        and Path(value).exists()
    ]


def find_chrome_binary():
    for path in _chrome_binary_candidates():
        return path
    return None



def selected_or_preferred_chrome_profile():
    saved = str(
        get_state(
            "chrome_selected_profile_directory",
            "",
        )
        or ""
    ).strip()

    if saved:
        try:
            if (chrome_user_data_dir() / saved).exists():
                return saved
        except Exception:
            pass

    return preferred_chrome_profile_directory()


def open_url_in_existing_chrome_profile(url, *, purpose="browser tab"):
    """
    Open one URL in the user's already-used Chrome profile WITHOUT requiring
    CDP to be connected first.

    Chrome's single-instance routing opens the URL in the matching profile.
    Explicit --profile-directory avoids the multi-profile chooser.
    """
    chrome = find_chrome_binary()
    if not chrome:
        return {
            "ok": False,
            "opened": False,
            "message": "Google Chrome was not found on this PC.",
        }

    profile = selected_or_preferred_chrome_profile()
    if not profile:
        return {
            "ok": False,
            "opened": False,
            "profile_picker_avoided": True,
            "message": (
                "The console could not safely identify which Chrome profile "
                f"should receive the {purpose}. Open it manually in the Chrome "
                "profile where Indeed Employer is signed in."
            ),
        }

    try:
        subprocess.Popen(
            [
                str(chrome),
                f"--profile-directory={profile}",
                "--no-first-run",
                "--disable-default-apps",
                str(url),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_creationflags(),
            close_fds=True,
        )

        set_state(
            "chrome_selected_profile_directory",
            profile,
        )

        return {
            "ok": True,
            "opened": True,
            "profile_directory": profile,
            "url": str(url),
            "message": (
                f"{purpose.capitalize()} opened in Chrome profile {profile}."
            ),
        }

    except Exception as exc:
        return {
            "ok": False,
            "opened": False,
            "profile_directory": profile,
            "message": f"Could not open {purpose}: {exc}",
        }



def recruitment_chrome_profile_initialized():
    """
    True after Chrome has created the persistent NUNES recruitment profile.
    """
    try:
        root = nunes_automation_chrome_dir()
        return (
            (root / "Local State").exists()
            or (root / "Default").exists()
            or (root / "DevToolsActivePort").exists()
        )
    except Exception:
        return False


def _recruitment_chrome_running():
    try:
        wanted = str(nunes_automation_chrome_dir()).lower()
    except Exception:
        return False

    for command_line in _windows_chrome_command_lines(force=True):
        if wanted in str(command_line or "").lower():
            return True

    return False



def recruitment_chrome_running():
    return _recruitment_chrome_running()


def stop_recruitment_chrome():
    """
    Stop ONLY Chrome processes using the NUNES Recruitment Chrome profile.

    Normal user Chrome profiles are untouched.
    """
    if os.name != "nt":
        return {
            "ok": True,
            "stopped": 0,
            "message": "No Windows Recruitment Chrome process to stop.",
        }

    profile_root = str(nunes_automation_chrome_dir())
    powershell = (
        shutil.which("powershell.exe")
        or shutil.which("powershell")
        or shutil.which("pwsh.exe")
        or shutil.which("pwsh")
    )

    if not powershell:
        return {
            "ok": False,
            "stopped": 0,
            "message": "PowerShell is not available.",
        }

    safe = profile_root.replace("'", "''")
    script = (
        f"$needle='{safe}';"
        "$rows=Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" "
        "| Where-Object { $_.CommandLine -and $_.CommandLine.Contains($needle) };"
        "$count=0;"
        "foreach($row in $rows){"
        " try { Stop-Process -Id $row.ProcessId -Force -ErrorAction Stop; $count++ } catch {}"
        "};"
        "Write-Output $count"
    )

    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=10,
            creationflags=_hidden_creationflags(),
        )

        try:
            stopped = int(
                (result.stdout or "0").strip().splitlines()[-1]
            )
        except Exception:
            stopped = 0

        with _chrome_process_cache_lock:
            _chrome_process_cache["at"] = 0.0
            _chrome_process_cache["rows"] = []

        try:
            port_file = (
                nunes_automation_chrome_dir()
                / "DevToolsActivePort"
            )
            if port_file.exists():
                port_file.unlink()
        except Exception:
            pass

        return {
            "ok": result.returncode == 0,
            "stopped": stopped,
            "message": (
                "Recruitment Chrome stopped."
                if stopped
                else "Recruitment Chrome was not running."
            ),
        }

    except Exception as exc:
        return {
            "ok": False,
            "stopped": 0,
            "message": f"Could not stop Recruitment Chrome: {exc}",
        }


def launch_recruitment_login_chrome(url=None, expected_google_email=None):
    """
    Launch the SAME persistent Recruitment Chrome profile WITHOUT remote
    debugging or automation flags.

    This mode is specifically for the one-time Indeed/Google sign-in. Google
    can reject OAuth sign-in when a browser is actively controlled through a
    debugging/automation connection. After sign-in, the user closes this
    window; the normal console then relaunches the same profile with CDP and
    reuses the authenticated Indeed cookies/session.
    """
    chrome = find_chrome_binary()
    if not chrome:
        return {
            "ok": False,
            "opened": False,
            "message": "Google Chrome was not found on this PC.",
        }

    # Make sure the same profile is not currently locked by debug Chrome.
    stop_recruitment_chrome()
    time.sleep(0.8)

    profile_root = nunes_automation_chrome_dir()
    profile_root.mkdir(
        parents=True,
        exist_ok=True,
    )

    expected_google_email = str(
        expected_google_email
        or "nuneslead@gmail.com"
    ).strip().lower()

    target_url = str(
        url
        or "https://employers.indeed.com/"
    ).strip()

    if "indeed." not in target_url.lower():
        target_url = "https://employers.indeed.com/"

    args = [
        str(chrome),
        f"--user-data-dir={profile_root}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-default-apps",
        "--start-maximized",
        target_url,
    ]

    try:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_hidden_creationflags(),
            close_fds=True,
        )
    except Exception as exc:
        return {
            "ok": False,
            "opened": False,
            "message": f"Could not open safe Indeed login Chrome: {exc}",
        }

    now_ts = time.time()
    set_state(
        "indeed_safe_login_mode",
        "1",
    )
    set_state(
        "indeed_safe_login_started_epoch",
        str(now_ts),
    )
    set_state(
        "indeed_safe_login_started_at",
        datetime.now(timezone.utc).isoformat(),
    )
    set_state(
        "indeed_expected_google_account",
        expected_google_email,
    )

    return {
        "ok": True,
        "opened": True,
        "profile_directory": str(profile_root),
        "url": target_url,
        "expected_google_account": expected_google_email,
        "password_stored_by_app": False,
        "message": (
            "Safe Indeed login Chrome opened with debugging OFF. "
            f"Use Google account {expected_google_email}. "
            "Complete the Google/Indeed sign-in manually, confirm the Employer "
            "account opens, then CLOSE this Recruitment Chrome window completely. "
            "The console will resume live recruitment automatically and reuse "
            "the saved browser session."
        ),
    }



def launch_recruitment_chrome(url=None, *, wait_seconds=12):
    """
    Launch or reuse ONE persistent Recruitment Chrome session.

    V11.11.20 prevents repeated browser windows/tabs:
      - if the dedicated Recruitment Chrome profile is already running, do not
        start chrome.exe again;
      - wait for the same DevToolsActivePort instead;
      - open the Jobs page only on the first browser launch.
    """
    chrome = find_chrome_binary()
    if not chrome:
        return {
            "ok": False,
            "opened": False,
            "message": "Google Chrome was not found on this PC.",
        }

    profile_root = nunes_automation_chrome_dir()
    profile_root.mkdir(parents=True, exist_ok=True)

    target_url = str(
        url
        or INDEED_CURRENT_JOBS_URL
    ).strip()

    if "indeed." not in target_url.lower():
        target_url = INDEED_CURRENT_JOBS_URL

    already_running = _recruitment_chrome_running()

    if not already_running:
        args = [
            str(chrome),
            f"--user-data-dir={profile_root}",
            "--profile-directory=Default",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-default-apps",
            "--start-maximized",
            target_url,
        ]

        try:
            subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_hidden_creationflags(),
                close_fds=True,
            )
        except Exception as exc:
            return {
                "ok": False,
                "opened": False,
                "message": f"Could not start Recruitment Chrome: {exc}",
            }

        set_state(
            "recruitment_chrome_last_launch_at",
            datetime.now(timezone.utc).isoformat(),
        )

    set_state(
        "recruitment_chrome_bootstrapped",
        "1",
    )

    deadline = time.monotonic() + max(1, int(wait_seconds or 1))
    endpoint = None

    while time.monotonic() < deadline:
        parsed = _parse_devtools_file(
            profile_root / "DevToolsActivePort"
        )
        if parsed:
            probe = _probe_debug_port(
                parsed["port"],
                timeout=0.8,
            )
            if probe:
                endpoint = {
                    **parsed,
                    **probe,
                    "source": "nunes_recruitment_chrome",
                }
                _safe_state_set(
                    "chrome_last_working_devtools_port",
                    str(parsed["port"]),
                )
                _safe_state_set(
                    "chrome_last_working_devtools_source",
                    "nunes_recruitment_chrome",
                )
                break
        time.sleep(0.2)

    if endpoint:
        return {
            "ok": True,
            "opened": not already_running,
            "reused_existing": already_running,
            "endpoint_ready": True,
            "profile_directory": str(profile_root),
            "url": target_url,
            "devtools_port": endpoint.get("port"),
            "message": (
                "Existing Recruitment Chrome reused."
                if already_running
                else "Recruitment Chrome opened on Indeed Jobs."
            ),
        }

    return {
        "ok": True,
        "opened": not already_running,
        "reused_existing": already_running,
        "endpoint_ready": False,
        "profile_directory": str(profile_root),
        "url": target_url,
        "message": (
            "Recruitment Chrome is already open; waiting for its debugging endpoint."
            if already_running
            else "Recruitment Chrome opened; waiting for its debugging endpoint."
        ),
    }

def open_remote_debugging_settings():
    """
    Backward-compatible connection action.

    V11.11.14 no longer opens Chrome's internal remote-debugging helper page. That internal page was blank on some systems. Instead we launch/reuse the dedicated
    persistent Recruitment Chrome profile directly on Indeed Candidates.
    """
    return launch_recruitment_chrome(
        INDEED_CURRENT_JOBS_URL,
        wait_seconds=12,
    )


def _parse_devtools_file(path: Path):
    try:
        raw = path.read_text(
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return None

    lines = [
        value.strip()
        for value in raw.splitlines()
        if value.strip()
    ]

    if not lines:
        return None

    try:
        port = int(lines[0])
    except Exception:
        return None

    if not (1 <= port <= 65535):
        return None

    browser_path = lines[1] if len(lines) >= 2 else ""
    if browser_path and not browser_path.startswith("/"):
        browser_path = "/" + browser_path

    return {
        "port": port,
        "browser_path": browser_path,
        "source_file": str(path),
        "user_data_dir": str(path.parent),
    }


def _probe_debug_port(port, timeout=0.8):
    """
    Validate a DevTools port through Chrome's /json/version endpoint.

    This avoids trusting a stale DevToolsActivePort file left behind by a
    previous Chrome process.
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{int(port)}/json/version",
            timeout=timeout,
        ) as response:
            raw = response.read(256 * 1024)
        payload = json.loads(
            raw.decode("utf-8", errors="replace")
        )
    except Exception:
        return None

    ws_url = str(
        payload.get("webSocketDebuggerUrl")
        or ""
    ).strip()

    if not ws_url.startswith(("ws://", "wss://")):
        return None

    return {
        "port": int(port),
        "ws_url": ws_url,
        "browser": payload.get("Browser"),
        "protocol_version": payload.get(
            "Protocol-Version"
        ),
    }


def _windows_chrome_command_lines(force=False):
    """
    Read running Chrome command lines without flashing a PowerShell window.

    Results are cached briefly because Chrome endpoint recovery can run every
    few seconds. Re-running WMI/PowerShell on every retry is unnecessary and
    was the source of visible terminal flashes on some Windows PCs.
    """
    if os.name != "nt":
        return []

    now_mono = time.monotonic()

    with _chrome_process_cache_lock:
        cached_at = float(_chrome_process_cache.get("at") or 0.0)
        cached_rows = list(_chrome_process_cache.get("rows") or [])

        if not force and cached_rows and (now_mono - cached_at) < 12:
            return cached_rows

    powershell = (
        shutil.which("powershell.exe")
        or shutil.which("powershell")
        or shutil.which("pwsh.exe")
        or shutil.which("pwsh")
    )

    if not powershell:
        return cached_rows

    script = (
        "Get-CimInstance Win32_Process "
        "-Filter \"Name='chrome.exe'\" "
        "| Select-Object -ExpandProperty CommandLine"
    )

    try:
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=5,
            creationflags=_hidden_creationflags(),
        )
    except Exception:
        return cached_rows

    if result.returncode != 0:
        return cached_rows

    rows = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    ]

    with _chrome_process_cache_lock:
        _chrome_process_cache["at"] = now_mono
        _chrome_process_cache["rows"] = rows

    return rows


def _extract_flag(command_line, name):
    # Supports --flag=value and --flag "value".
    pattern = re.compile(
        rf'(?:^|\s)--{re.escape(name)}'
        rf'(?:=|\s+)'
        rf'(?:"([^"]+)"|([^\s"]+))',
        re.I,
    )
    match = pattern.search(
        str(command_line or "")
    )
    if not match:
        return ""
    return (
        match.group(1)
        or match.group(2)
        or ""
    ).strip()


def _devtools_file_candidates():
    candidates = []
    seen = set()

    def add(path):
        try:
            p = Path(path)
        except Exception:
            return
        key = str(p).lower()
        if key in seen:
            return
        seen.add(key)
        if p.exists() and p.is_file():
            candidates.append(p)

    # Normal Chrome Remote Debugging toggle.
    try:
        root = chrome_user_data_dir()
        add(root / "DevToolsActivePort")

        # Be tolerant of Chrome/profile layout differences.
        for pattern in (
            "Default/DevToolsActivePort",
            "Profile */DevToolsActivePort",
            "Guest Profile/DevToolsActivePort",
            "System Profile/DevToolsActivePort",
        ):
            try:
                for item in root.glob(pattern):
                    add(item)
            except Exception:
                pass
    except Exception:
        pass

    # Persistent NUNES fallback profile if it was ever used.
    try:
        add(
            nunes_automation_chrome_dir()
            / "DevToolsActivePort"
        )
    except Exception:
        pass

    # Discover custom user-data-dir from running Chrome processes.
    for command_line in _windows_chrome_command_lines():
        user_data = _extract_flag(
            command_line,
            "user-data-dir",
        )
        if user_data:
            add(
                Path(
                    os.path.expandvars(user_data)
                )
                / "DevToolsActivePort"
            )

    # Newest file first.
    candidates.sort(
        key=lambda p: (
            p.stat().st_mtime
            if p.exists()
            else 0
        ),
        reverse=True,
    )
    return candidates


def _safe_state_get(key, default=""):
    try:
        return get_state(key, default)
    except Exception:
        return default


def _safe_state_set(key, value):
    try:
        set_state(key, value)
    except Exception:
        pass


def discover_devtools_endpoint():
    """
    Find the CURRENT working Chrome DevTools endpoint.

    Discovery order:
      1. last known-good port
      2. every usable DevToolsActivePort file
      3. Chrome command-line --remote-debugging-port values
      4. common fixed debugging ports

    Every candidate is live-probed before use.
    """
    port_candidates = []
    metadata = {}

    def add_port(port, **info):
        try:
            value = int(port)
        except Exception:
            return
        if not (1 <= value <= 65535):
            return
        if value not in port_candidates:
            port_candidates.append(value)
            metadata[value] = dict(info)

    # Last known-good port makes normal restart reconnect very fast.
    add_port(
        _safe_state_get(
            "chrome_last_working_devtools_port",
            "",
        ),
        source="last_working_port",
    )

    for path in _devtools_file_candidates():
        parsed = _parse_devtools_file(path)
        if not parsed:
            continue

        add_port(
            parsed["port"],
            source="DevToolsActivePort",
            source_file=parsed.get(
                "source_file"
            ),
            user_data_dir=parsed.get(
                "user_data_dir"
            ),
            browser_path=parsed.get(
                "browser_path"
            ),
        )

    for command_line in _windows_chrome_command_lines():
        value = _extract_flag(
            command_line,
            "remote-debugging-port",
        )
        if value and value != "0":
            add_port(
                value,
                source="chrome_process",
            )

    for port in (9222, 9223, 9333, 9515):
        add_port(
            port,
            source="known_port",
        )

    errors = []

    for port in port_candidates:
        probe = _probe_debug_port(port)
        if not probe:
            errors.append(
                f"{port}: not responding"
            )
            continue

        info = {
            **metadata.get(port, {}),
            **probe,
        }

        _safe_state_set(
            "chrome_last_working_devtools_port",
            str(port),
        )
        _safe_state_set(
            "chrome_last_working_devtools_source",
            str(
                info.get("source")
                or ""
            ),
        )

        return info

    raise ChromeConnectionError(
        "Recruitment Chrome debugging is not active yet. "
        "Press Open Recruitment Chrome once. The console will launch a "
        "persistent Chrome profile directly on Indeed Candidates and reconnect "
        "automatically."
    )


def read_devtools_endpoint():
    """
    Backward-compatible public endpoint reader.

    V11.11.7 no longer trusts only one fixed DevToolsActivePort location.
    """
    discovered = discover_devtools_endpoint()

    return {
        "port": int(discovered["port"]),
        "ws_url": discovered["ws_url"],
        "path": str(
            discovered.get("source_file")
            or ""
        ),
        "user_data_dir": str(
            discovered.get("user_data_dir")
            or ""
        ),
        "source": discovered.get(
            "source"
        ),
        "browser": discovered.get(
            "browser"
        ),
    }


class CDPClient:
    """
    Minimal Chrome DevTools Protocol client attached to either an already
    approved Chrome WebSocket or the persistent NUNES Recruitment Chrome profile.

    No Playwright and no ephemeral test browser.
    """
    def __init__(self, timeout=15):
        ep = read_devtools_endpoint()
        self.endpoint = ep
        try:
            # suppress_origin avoids Chrome rejecting a synthetic WebSocket Origin.
            self.ws = websocket.create_connection(
                ep["ws_url"],
                timeout=timeout,
                suppress_origin=True,
            )
        except TypeError:
            # Compatibility fallback for older websocket-client.
            self.ws = websocket.create_connection(
                ep["ws_url"],
                timeout=timeout,
            )
        except Exception as e:
            raise ChromeConnectionError(
                "Chrome debugging endpoint was found but the WebSocket connection "
                "did not complete. The console will rediscover and retry automatically. "
                f"Details: {e}"
            )

        self._id = 0
        self._lock = threading.RLock()

        # Page-level CDP sessions can be invalidated by Indeed navigation or
        # Chrome target replacement while the browser-level DevTools WebSocket
        # remains healthy. Keep target/session aliases so we can re-attach
        # transparently instead of failing every candidate.
        self._session_targets = {}
        self._session_aliases = {}
        self._session_recovery_count = 0

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass

        self._session_targets.clear()
        self._session_aliases.clear()

    @staticmethod
    def _is_missing_session_error(error):
        text = str(error or "").lower()
        return (
            "-32001" in text
            or "session with given id not found" in text
            or "session with given id" in text and "not found" in text
            or "no session with given id" in text
            or "invalid session id" in text
        )

    def _resolve_session_id(self, session_id):
        if not session_id:
            return session_id

        current = session_id
        seen = set()

        while current in self._session_aliases and current not in seen:
            seen.add(current)
            current = self._session_aliases[current]

        return current

    def _remember_session(self, session_id, target_id):
        if session_id and target_id:
            self._session_targets[session_id] = target_id

    def _forget_session(self, session_id):
        if not session_id:
            return

        resolved = self._resolve_session_id(session_id)

        for key, value in list(self._session_aliases.items()):
            if key in {session_id, resolved} or value in {session_id, resolved}:
                self._session_aliases.pop(key, None)

        self._session_targets.pop(session_id, None)
        self._session_targets.pop(resolved, None)

    def _recover_page_session(self, original_session_id):
        """
        Re-attach a dead flattened CDP page session without opening a new
        browser-level DevTools WebSocket.

        This specifically repairs Chrome error -32001:
          "Session with given id not found"
        """
        resolved = self._resolve_session_id(original_session_id)
        target_id = (
            self._session_targets.get(resolved)
            or self._session_targets.get(original_session_id)
        )

        # Confirm the old target still exists. If Indeed replaced it, pick the
        # best currently-open Indeed page and continue in that target.
        try:
            targets = self.targets()
        except Exception:
            targets = []

        live_ids = {
            t.get("targetId")
            for t in targets
            if t.get("targetId")
        }

        if target_id not in live_ids:
            indeed_targets = [
                t
                for t in targets
                if "indeed." in (t.get("url") or "").lower()
            ]

            if not indeed_targets:
                return None

            saved = load_settings().get("indeed_candidates_url", "")
            indeed_targets.sort(
                key=lambda t: _target_score(t, saved),
                reverse=True,
            )
            target_id = indeed_targets[0].get("targetId")

        if not target_id:
            return None

        # Attach directly at browser level. command() has no page session here,
        # so recovery cannot recurse into itself.
        r = self.command(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            timeout=15,
            _allow_session_recovery=False,
        )

        new_sid = r.get("sessionId")
        if not new_sid:
            return None

        self._remember_session(new_sid, target_id)
        self._session_aliases[original_session_id] = new_sid

        if resolved and resolved != original_session_id:
            self._session_aliases[resolved] = new_sid

        # Enable useful page domains on the replacement session.
        for domain in ("Runtime.enable", "Page.enable"):
            try:
                self.command(
                    domain,
                    session_id=new_sid,
                    timeout=10,
                    _allow_session_recovery=False,
                )
            except Exception:
                pass

        self._session_recovery_count += 1

        try:
            set_state(
                "cdp_session_recoveries",
                str(self._session_recovery_count),
            )
            set_state(
                "cdp_last_session_recovery_at",
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception:
            pass

        return new_sid

    def command(
        self,
        method,
        params=None,
        session_id=None,
        timeout=30,
        _allow_session_recovery=True,
    ):
        with self._lock:
            original_session_id = session_id
            effective_session_id = self._resolve_session_id(session_id)

            self._id += 1
            msg_id = self._id

            payload = {
                "id": msg_id,
                "method": method,
                "params": params or {},
            }

            if effective_session_id:
                payload["sessionId"] = effective_session_id

            try:
                self.ws.settimeout(timeout)
                self.ws.send(json.dumps(payload))
            except Exception as e:
                raise ChromeConnectionError(
                    f"Chrome command send failed: {e}"
                )

            deadline = time.time() + timeout

            while time.time() < deadline:
                try:
                    raw = self.ws.recv()
                except Exception as e:
                    raise ChromeConnectionError(
                        f"Chrome command receive failed: {e}"
                    )

                if not raw:
                    continue

                try:
                    data = json.loads(raw)
                except Exception:
                    continue

                # Ignore asynchronous CDP events and replies for other commands.
                if data.get("id") != msg_id:
                    continue

                if "error" in data:
                    err = data["error"]

                    if (
                        original_session_id
                        and _allow_session_recovery
                        and self._is_missing_session_error(err)
                        and method not in {
                            "Target.attachToTarget",
                            "Target.detachFromTarget",
                        }
                    ):
                        recovered = self._recover_page_session(
                            original_session_id
                        )

                        if recovered:
                            remaining = max(
                                5,
                                int(deadline - time.time()),
                            )

                            return self.command(
                                method,
                                params,
                                session_id=original_session_id,
                                timeout=remaining,
                                _allow_session_recovery=False,
                            )

                    raise ChromeConnectionError(
                        f"Chrome CDP {method} failed: {err}"
                    )

                return data.get("result") or {}

            raise ChromeConnectionError(
                f"Chrome CDP command timed out: {method}"
            )

    def targets(self):
        result = self.command("Target.getTargets", timeout=15)
        targets = []
        for t in result.get("targetInfos", []):
            if t.get("type") != "page":
                continue
            targets.append({
                "targetId": t.get("targetId"),
                "url": t.get("url") or "",
                "title": t.get("title") or "",
                "attached": bool(t.get("attached")),
            })
        return targets

    def attach(self, target_id):
        r = self.command(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
            timeout=15,
        )
        sid = r.get("sessionId")
        if not sid:
            raise ChromeConnectionError("Chrome did not return a page session.")

        self._remember_session(sid, target_id)

        # Enable useful domains.
        try:
            self.command("Runtime.enable", session_id=sid, timeout=10)
        except Exception:
            pass
        try:
            self.command("Page.enable", session_id=sid, timeout=10)
        except Exception:
            pass
        return sid

    def detach(self, session_id):
        effective = self._resolve_session_id(session_id)

        try:
            self.command(
                "Target.detachFromTarget",
                {"sessionId": effective},
                timeout=10,
                _allow_session_recovery=False,
            )
        except Exception:
            pass
        finally:
            self._forget_session(session_id)

    def evaluate(
        self,
        session_id,
        expression,
        await_promise=True,
        timeout=60,
        user_gesture=False,
    ):
        result = self.command(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": bool(await_promise),
                "returnByValue": True,
                "userGesture": bool(user_gesture),
            },
            session_id=session_id,
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            desc = (
                result.get("exceptionDetails", {})
                .get("exception", {})
                .get("description")
            ) or str(result.get("exceptionDetails"))
            raise ChromeConnectionError(f"JavaScript error in Indeed page: {desc}")
        return (result.get("result") or {}).get("value")

    def navigate(self, session_id, url, timeout=45):
        if "indeed." not in (url or "").lower():
            raise ChromeConnectionError("V6 blocks automatic navigation outside Indeed.")
        self.command(
            "Page.navigate",
            {"url": url},
            session_id=session_id,
            timeout=15,
        )

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = self.evaluate(
                    session_id,
                    "document.readyState",
                    await_promise=False,
                    timeout=8,
                )
                if state in ("interactive", "complete"):
                    time.sleep(0.8)
                    return
            except Exception:
                pass
            time.sleep(0.25)

        raise ChromeConnectionError("Indeed page navigation timed out.")



# ---------------------------------------------------------------------
# V10.1: ONE PERSISTENT CHROME DEVTOOLS CONNECTION
# ---------------------------------------------------------------------
#
# Chrome's "Allow remote debugging?" permission is associated with a new
# external DevTools connection. V10 previously created a new WebSocket for
# every dashboard poll/status check, which caused the permission popup to
# appear repeatedly.
#
# V10.1 creates one browser-level CDP WebSocket after the user explicitly
# clicks "Connect Chrome" and reuses it for all subsequent work.
# Dashboard polling NEVER opens a new Chrome connection.
#
_shared_client = None
_shared_client_lock = threading.RLock()
_shared_connected_at = None
_shared_last_error = None


def _close_client_quietly(client):
    try:
        if client:
            client.close()
    except Exception:
        pass


def reset_shared_chrome(reason=None):
    global _shared_client, _shared_connected_at, _shared_last_error

    with _shared_client_lock:
        old = _shared_client
        _shared_client = None
        _shared_connected_at = None
        if reason:
            _shared_last_error = str(reason)

    _close_client_quietly(old)


def _ping_client(client):
    # Browser.getVersion is a cheap browser-level command and does not
    # attach to/open any page.
    client.command("Browser.getVersion", timeout=8)
    return True


def connect_shared_chrome(timeout=60):
    """
    Explicit user-approved connection.

    This is the ONLY normal path that creates a new Chrome WebSocket.
    It may trigger Chrome's one-time "Allow remote debugging?" prompt.
    """
    global _shared_client, _shared_connected_at, _shared_last_error

    with _shared_client_lock:
        if _shared_client is not None:
            try:
                _ping_client(_shared_client)
                return _shared_client
            except Exception as e:
                old = _shared_client
                _shared_client = None
                _shared_connected_at = None
                _shared_last_error = str(e)
                _close_client_quietly(old)

        client = CDPClient(timeout=timeout)

        try:
            _ping_client(client)
        except Exception:
            _close_client_quietly(client)
            raise

        _shared_client = client
        _shared_connected_at = time.time()
        _shared_last_error = None
        return client


def get_shared_chrome():
    """
    Return the shared Chrome connection, automatically reconnecting if
    the existing client dropped or has not been initialized yet.
    """
    with _shared_client_lock:
        client = _shared_client

    if client is None:
        try:
            return connect_shared_chrome(timeout=10)
        except Exception as e:
            raise ChromeConnectionError(
                "Chrome is not connected to this console session yet. "
                f"Automatic reconnect will restore it ({e})."
            )

    try:
        _ping_client(client)
        return client
    except Exception as e:
        reset_shared_chrome(e)
        try:
            return connect_shared_chrome(timeout=10)
        except Exception:
            raise ChromeConnectionError(
                "The existing Chrome connection was closed. "
                "Automatic reconnect will restore it."
            )


def shared_connection_snapshot():
    with _shared_client_lock:
        return {
            "connected": _shared_client is not None,
            "connected_at": _shared_connected_at,
            "last_error": _shared_last_error,
        }


def cdp_session_recovery_snapshot():
    with _shared_client_lock:
        client = _shared_client

    return {
        "recoveries": int(
            getattr(client, "_session_recovery_count", 0)
            if client is not None
            else (get_state("cdp_session_recoveries", "0") or 0)
        ),
        "last_recovery_at": get_state("cdp_last_session_recovery_at"),
    }


def _target_score(t, saved_url=""):
    url = (t.get("url") or "")
    low = url.lower()
    title = (t.get("title") or "").lower()
    if "indeed." not in low:
        return -1000

    score = 1
    saved = urldefrag(saved_url or "")[0]
    if saved and urldefrag(url)[0] == saved:
        score += 100
    if any(k in low for k in ["candidate", "applicant", "application"]):
        score += 20
    if any(k in title for k in ["candidate", "applicant", "jobs - indeed for employers"]):
        score += 10
    if "employer" in low or "employers" in low:
        score += 5
    return score


def choose_indeed_target(client: CDPClient, saved_url=""):
    targets = client.targets()
    scored = sorted(
        ((_target_score(t, saved_url), t) for t in targets),
        key=lambda x: x[0],
        reverse=True,
    )
    if not scored or scored[0][0] < 0:
        raise ChromeConnectionError(
            "Chrome is connected, but no Indeed tab is open yet."
        )
    return scored[0][1], targets


def find_indeed_target(client: CDPClient, saved_url=""):
    """Return (target_or_none, all_targets) without treating missing Indeed as a Chrome failure."""
    targets = client.targets()
    scored = sorted(
        ((_target_score(t, saved_url), t) for t in targets),
        key=lambda x: x[0],
        reverse=True,
    )
    if not scored or scored[0][0] < 0:
        return None, targets
    return scored[0][1], targets



def _indeed_target_kind(target):
    url = str((target or {}).get("url") or "")
    title = str((target or {}).get("title") or "")
    low_url = url.lower()
    low_title = title.lower()

    if "indeed." not in low_url:
        return "other"

    if (
        "choose-product" in low_url
        or "missingpermissions" in low_url
        or "hosted_candidate" in low_url
        or "you do not have permission" in low_title
    ):
        return "permission"

    if "employers.indeed.com/jobs" in low_url:
        return "jobs"

    if any(
        key in low_url
        for key in (
            "/candidate",
            "/applicant",
            "/application",
        )
    ):
        return "candidates"

    if (
        "secure.indeed.com/auth" in low_url
        or "/login" in low_url
        or "sign in" in low_title
    ):
        return "login"

    return "indeed_other"


def cleanup_recruitment_indeed_tabs(client=None):
    """
    Keep only the useful Indeed workspaces in the dedicated Recruitment Chrome.

    Permission/chooser tabs are closed after their state is captured.
    Duplicate Jobs/Candidates/login tabs are reduced to one each.
    """
    c = client or get_shared_chrome()
    targets = [
        t
        for t in c.targets()
        if str(t.get("type") or "page").lower() == "page"
    ]

    by_kind = {}
    for target in targets:
        kind = _indeed_target_kind(target)
        if kind == "other":
            continue
        by_kind.setdefault(kind, []).append(target)

    closed = []

    for target in by_kind.get("permission", []):
        tid = target.get("targetId")
        if not tid:
            continue
        try:
            c.command(
                "Target.closeTarget",
                {"targetId": tid},
                timeout=8,
            )
            closed.append(tid)
        except Exception:
            pass

    for kind in ("jobs", "candidates", "login"):
        rows = by_kind.get(kind, [])
        if len(rows) <= 1:
            continue

        def score(target):
            url = str(target.get("url") or "").lower()
            value = 0
            if kind == "jobs":
                if "status=open" in url:
                    value += 100
                if "paused" in url:
                    value += 20
                if "closed" in url or "expired" in url:
                    value -= 100
            if kind == "candidates":
                # NUNES policy: keep the unfiltered All-candidates workspace.
                if "statusname=all" in url:
                    value += 100
                if "statusname=new" in url:
                    value -= 50
                if "tab=manage" in url:
                    value += 20
            return value

        keep = sorted(
            rows,
            key=score,
            reverse=True,
        )[0].get("targetId")

        for target in rows:
            tid = target.get("targetId")
            if not tid or tid == keep:
                continue
            try:
                c.command(
                    "Target.closeTarget",
                    {"targetId": tid},
                    timeout=8,
                )
                closed.append(tid)
            except Exception:
                pass

    if closed:
        try:
            previous = int(
                get_state(
                    "indeed_duplicate_tabs_closed",
                    "0",
                )
                or 0
            )
        except Exception:
            previous = 0

        set_state(
            "indeed_duplicate_tabs_closed",
            str(previous + len(closed)),
        )
        set_state(
            "indeed_tab_cleanup_last_at",
            datetime.now(timezone.utc).isoformat(),
        )

    return {
        "ok": True,
        "closed": len(closed),
    }


def ensure_indeed_jobs_tab(client=None):
    """
    Return ONE persistent Indeed Jobs target.

    Reuse an existing Jobs tab first. If none exists, repurpose an existing
    Indeed error/other tab before creating a new tab. The resulting Jobs tab
    remains open for every future role scan.
    """
    c = client or get_shared_chrome()
    targets = [
        t
        for t in c.targets()
        if str(t.get("type") or "page").lower() == "page"
    ]

    jobs = [
        t
        for t in targets
        if _indeed_target_kind(t) == "jobs"
    ]

    if jobs:
        def score(target):
            url = str(target.get("url") or "").lower()
            value = 0
            if "status=open" in url:
                value += 100
            if "paused" in url:
                value += 30
            if "closed" in url or "expired" in url:
                value -= 200
            return value

        chosen = sorted(
            jobs,
            key=score,
            reverse=True,
        )[0]
        cleanup_recruitment_indeed_tabs(c)
        return {
            "target": chosen,
            "created": False,
            "repurposed": False,
        }

    reusable = [
        t
        for t in targets
        if _indeed_target_kind(t)
        in {"permission", "indeed_other"}
    ]

    if reusable:
        chosen = reusable[0]
        sid = None
        try:
            sid = c.attach(chosen["targetId"])
            c.navigate(
                sid,
                INDEED_CURRENT_JOBS_URL,
                timeout=45,
            )
        finally:
            if sid:
                try:
                    c.detach(sid)
                except Exception:
                    pass

        time.sleep(0.6)

        refreshed = next(
            (
                t
                for t in c.targets()
                if t.get("targetId")
                == chosen.get("targetId")
            ),
            chosen,
        )
        cleanup_recruitment_indeed_tabs(c)
        return {
            "target": refreshed,
            "created": False,
            "repurposed": True,
        }

    result = c.command(
        "Target.createTarget",
        {"url": INDEED_CURRENT_JOBS_URL},
        timeout=20,
    )
    target_id = result.get("targetId")

    if not target_id:
        raise ChromeConnectionError(
            "Chrome did not create the Indeed Jobs tab."
        )

    time.sleep(0.8)

    target = next(
        (
            t
            for t in c.targets()
            if t.get("targetId") == target_id
        ),
        {
            "targetId": target_id,
            "url": INDEED_CURRENT_JOBS_URL,
            "title": "Indeed Jobs",
        },
    )

    cleanup_recruitment_indeed_tabs(c)

    return {
        "target": target,
        "created": True,
        "repurposed": False,
    }


def open_indeed_in_existing_chrome(url=None):
    """
    Reuse the existing Recruitment Chrome tab instead of creating duplicates.
    """
    s = load_settings()
    target_url = (
        url
        or s.get("indeed_start_url")
        or INDEED_CURRENT_JOBS_URL
    ).strip()

    if "indeed." not in target_url.lower():
        target_url = INDEED_CURRENT_JOBS_URL

    c = get_shared_chrome()
    low_target = target_url.lower()

    if "employers.indeed.com/jobs" in low_target:
        result = ensure_indeed_jobs_tab(c)
        target = result["target"]
        return {
            "ok": True,
            "target_id": target.get("targetId"),
            "url": target.get("url") or target_url,
            "title": target.get("title") or "Indeed Jobs",
            "reused": not result.get("created"),
        }

    targets = [
        t
        for t in c.targets()
        if str(t.get("type") or "page").lower() == "page"
        and "indeed." in str(t.get("url") or "").lower()
    ]

    for target in targets:
        current_url = str(target.get("url") or "")
        if current_url.split("#")[0] == target_url.split("#")[0]:
            return {
                "ok": True,
                "target_id": target.get("targetId"),
                "url": current_url,
                "title": target.get("title") or "Indeed",
                "reused": True,
            }

    reusable = next(
        (
            t
            for t in targets
            if _indeed_target_kind(t)
            in {"permission", "indeed_other"}
        ),
        None,
    )

    if reusable:
        sid = None
        try:
            sid = c.attach(reusable["targetId"])
            c.navigate(
                sid,
                target_url,
                timeout=45,
            )
        finally:
            if sid:
                try:
                    c.detach(sid)
                except Exception:
                    pass

        cleanup_recruitment_indeed_tabs(c)

        return {
            "ok": True,
            "target_id": reusable.get("targetId"),
            "url": target_url,
            "title": "Indeed",
            "reused": True,
        }

    result = c.command(
        "Target.createTarget",
        {"url": target_url},
        timeout=20,
    )
    target_id = result.get("targetId")

    if not target_id:
        raise ChromeConnectionError(
            "Chrome did not create the Indeed tab."
        )

    cleanup_recruitment_indeed_tabs(c)

    return {
        "ok": True,
        "target_id": target_id,
        "url": target_url,
        "title": "Indeed",
        "reused": False,
    }

EMPLOYER_JOB_DISCOVERY_JS = r"""
(async () => {
  const MAX_JOBS = __MAX_JOBS__;
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const normalize = value => (value || '').replace(/\s+/g, ' ').trim();
  const STATUS_RE = /\b(Open|Paused|Flagged|Closed|Expired|Filled)\b/i;

  const started = Date.now();

  // Indeed's Jobs page is rendered dynamically. Wait until the table and at
  // least one status/title are visible.
  while (Date.now() - started < 12000) {
    const body = document.body?.innerText || '';
    const hasJobsSurface = (
      /\bjob title\b/i.test(body)
      && /\bjob status\b/i.test(body)
      && /\b(candidates|date posted)\b/i.test(body)
    );
    const hasAnyStatus = STATUS_RE.test(body);

    if (hasJobsSurface && hasAnyStatus) break;
    await sleep(300);
  }

  // Load virtual/lazy rows. The user's current Indeed page can contain more
  // rows than the initial viewport.
  const originalY = window.scrollY || 0;
  for (let i = 0; i < 7; i += 1) {
    window.scrollTo(0, document.body?.scrollHeight || 0);
    await sleep(220);
  }
  window.scrollTo(0, originalY);
  await sleep(180);

  const bodyText = normalize(document.body?.innerText || '');
  const totalMatch = bodyText.match(/\b(\d{1,4})\s+results?\b/i);
  const totalHint = totalMatch ? Number(totalMatch[1]) : 0;

  const exactStatus = value => {
    const text = normalize(value);
    const match = text.match(STATUS_RE);
    return match ? normalize(match[1]) : '';
  };

  const statusFromNode = node => {
    if (!node) return '';

    // The current Indeed UI renders Job status using a select/button-like
    // control. Prefer that exact control before scanning row text.
    const selectors = [
      'select option:checked',
      'select',
      '[data-testid*="status" i]',
      '[aria-label*="status" i]',
      'button',
      '[role="button"]'
    ];

    for (const selector of selectors) {
      let items = [];
      try {
        items = [...node.querySelectorAll(selector)];
      } catch (_) {}

      for (const item of items) {
        const text = normalize(
          item.innerText
          || item.textContent
          || item.getAttribute?.('aria-label')
          || item.value
          || ''
        );

        const status = exactStatus(text);
        if (
          status
          && new RegExp(`^${status}$`, 'i').test(text)
        ) {
          return status;
        }
      }
    }

    return exactStatus(node.innerText || node.textContent || '');
  };

  const rowForAnchor = anchor => {
    let node = anchor;

    // The 2026 Indeed Jobs screen is largely div-based, not a simple <tr>.
    // Walk upward until the smallest container has BOTH the row metrics and a
    // job-status value.
    for (let i = 0; i < 11; i += 1) {
      node = node?.parentElement;
      if (!node) break;

      const text = normalize(node.innerText || node.textContent || '');
      if (text.length < 8 || text.length > 3500) continue;

      const status = statusFromNode(node);
      const hasCandidateMetrics = (
        /\bAll\b/i.test(text)
        && /\bNew\b/i.test(text)
        && /\bMatches\b/i.test(text)
      );
      const hasDatePosted = (
        /\bdate posted\b/i.test(text)
        || /\b(?:day|days|month|months|year|years)\s+ago\b/i.test(text)
        || /\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b/.test(text)
      );

      if (
        status
        && (
          hasCandidateMetrics
          || hasDatePosted
          || node.matches?.('tr,[role="row"],article,li')
        )
      ) {
        return node;
      }
    }

    return anchor.closest?.('tr,[role="row"],article,li') || null;
  };

  const numberNear = (text, label) => {
    const value = normalize(text);
    const re = new RegExp(`(?:^|\\s)(\\d{1,6})\\s+${label}(?:\\s|$)`, 'i');
    const match = value.match(re);
    return match ? Number(match[1]) : null;
  };

  const extractMetrics = row => {
    const text = normalize(row?.innerText || row?.textContent || '');

    // Current row usually appears as:
    //   27 All   24 New   Matches
    // but DOM order can vary. Capture conservative numeric hints.
    let allCount = null;
    let newCount = null;

    const allMatch = text.match(/\b(\d{1,6})\s+All\b/i);
    if (allMatch) allCount = Number(allMatch[1]);

    const newMatch = text.match(/\b(\d{1,6})\s+New\b/i);
    if (newMatch) newCount = Number(newMatch[1]);

    return {
      candidate_total: Number.isFinite(allCount) ? allCount : null,
      candidate_new: Number.isFinite(newCount) ? newCount : null
    };
  };

  const isNavigationText = text =>
    /^(all|new|matches|jobs|tags|post a job|candidates|sponsor job|sponsor now|yes|no|open|paused|flagged|closed)$/i.test(
      normalize(text)
    );

  const candidates = [];

  for (const anchor of [...document.querySelectorAll('a[href]')]) {
    const href = String(anchor.href || '').trim();
    const title = normalize(anchor.innerText || anchor.textContent || '');

    if (!href || !title || title.length < 3 || title.length > 220) continue;
    if (isNavigationText(title)) continue;
    if (!/indeed\./i.test(href)) continue;

    const row = rowForAnchor(anchor);
    if (!row) continue;

    const rowText = normalize(row.innerText || row.textContent || '');
    const jobStatus = statusFromNode(row);

    // This is the key discriminator for the current Indeed Jobs table. A job
    // title row contains candidate metrics and a Job status control.
    const looksLikeCurrentJobRow = (
      Boolean(jobStatus)
      && (
        (
          /\bAll\b/i.test(rowText)
          && /\bNew\b/i.test(rowText)
          && /\bMatches\b/i.test(rowText)
        )
        || /\b(?:Premium|Sponsorship|Date posted)\b/i.test(rowText)
      )
    );

    // Keep a URL-shape fallback for older Indeed layouts.
    const lowHref = href.toLowerCase();
    const looksLikeLegacyJobLink =
      /(viewjob|jobdetail|jobkey|\/job\/|\/jobs\/|jobid=|jobkey=)/i.test(lowHref);

    if (!looksLikeCurrentJobRow && !looksLikeLegacyJobLink) continue;

    const metrics = extractMetrics(row);
        const allCandidatesAnchor = [...row.querySelectorAll('a[href]')].find(anchor => {
            const candidateUrl = String(anchor.href || '').toLowerCase();
            const label = normalize(
                anchor.innerText
                || anchor.getAttribute('aria-label')
                || anchor.parentElement?.innerText
                || ''
            );
            return (
                /(candidate|applicant|application)/i.test(candidateUrl)
                && /\ball\b/i.test(label)
            );
        });

    candidates.push({
      href,
      title,
      job_status: jobStatus || 'Open',
      candidate_total: metrics.candidate_total,
      candidate_new: metrics.candidate_new,
            candidate_all_url: allCandidatesAnchor?.href || '',
      snapshot_total_hint: totalHint
    });
  }

  // Dedupe by posting URL, NOT title. The user's account can legitimately
  // contain two postings with the same title but different statuses.
  const unique = [];
  const seen = new Set();

  for (const item of candidates) {
    const urlKey = String(item.href || '').split('#')[0];
    const key = `${urlKey}::${item.title}`;
    if (seen.has(key)) continue;
    seen.add(key);
    unique.push(item);
  }

  const htmlToText = raw => {
    try {
      const doc = new DOMParser().parseFromString(raw || '', 'text/html');
      return normalize(doc.body?.innerText || doc.body?.textContent || '');
    } catch (_) {
      return normalize(raw || '');
    }
  };

  const cleanDescription = value =>
    (value || '')
      .replace(/\r/g, '\n')
      .replace(/[ \t]+/g, ' ')
      .replace(/\n{3,}/g, '\n\n')
      .trim();

  const parseJobPage = (raw, url, fallbackTitle, fallbackStatus) => {
    const doc = new DOMParser().parseFromString(raw || '', 'text/html');
    let title = fallbackTitle || '';
    let description = '';

    try {
      for (const script of [...doc.querySelectorAll('script[type="application/ld+json"]')]) {
        try {
          const value = JSON.parse(script.textContent || 'null');
          const queue = Array.isArray(value) ? [...value] : [value];

          while (queue.length) {
            const item = queue.shift();
            if (!item || typeof item !== 'object') continue;
            if (Array.isArray(item['@graph'])) queue.push(...item['@graph']);

            const types = Array.isArray(item['@type'])
              ? item['@type']
              : [item['@type']];

            if (
              types.some(
                x => String(x || '').toLowerCase() === 'jobposting'
              )
            ) {
              title = normalize(item.title || item.name || title);
              description = cleanDescription(
                htmlToText(String(item.description || ''))
              );

              if (description.length >= 100) {
                return {
                  job_title: title,
                  job_description: description,
                  job_url: url,
                  job_status: fallbackStatus || '',
                  source: 'indeed_jobs_jsonld'
                };
              }
            }
          }
        } catch (_) {}
      }
    } catch (_) {}

    const selectors = [
      '#jobDescriptionText',
      '[data-testid*="job-description" i]',
      '[data-testid*="jobDescription" i]',
      '[class*="jobDescription" i]',
      '[class*="job-description" i]',
      '[id*="jobDescription" i]',
      '[aria-label*="job description" i]'
    ];

    for (const selector of selectors) {
      try {
        for (const el of [...doc.querySelectorAll(selector)]) {
          const text = cleanDescription(
            el.innerText || el.textContent || ''
          );

          if (
            text.length > description.length
            && text.length <= 40000
          ) {
            description = text;
          }
        }
      } catch (_) {}
    }

    return {
      job_title: title,
      job_description: description,
      job_url: url,
      job_status: fallbackStatus || '',
      source: 'indeed_jobs_page'
    };
  };

  const fetchWithTimeout = async (url, timeoutMs = 4500) => {
    const controller = new AbortController();
    const timer = setTimeout(
      () => controller.abort(),
      timeoutMs
    );

    try {
      return await fetch(url, {
        credentials: 'include',
        redirect: 'follow',
        cache: 'no-store',
        signal: controller.signal
      });
    } finally {
      clearTimeout(timer);
    }
  };

    const candidateQueues = unique
        .filter(item => item.candidate_all_url)
        .map(item => ({
            job_title: item.title,
            job_status: item.job_status,
            job_url: item.href,
            candidate_total: item.candidate_total,
            candidate_new: item.candidate_new,
            url: item.candidate_all_url,
        }));

    const results = await Promise.all(
        unique.slice(0, MAX_JOBS).map(async item => {
      // IMPORTANT: role listing data is enough to make the role visible.
      // A failed/slow JD fetch must never hide the role.
      const fallback = {
        job_title: item.title,
        job_description: '',
        job_url: item.href,
        job_status: item.job_status || 'Open',
        candidate_total: item.candidate_total,
        candidate_new: item.candidate_new,
        candidate_all_url: item.candidate_all_url,
        snapshot_total_hint: totalHint,
        source: 'indeed_jobs_2026_listing'
      };

      try {
        const response = await fetchWithTimeout(item.href, 4500);
        if (!response.ok) return fallback;

        const raw = await response.text();
        const parsed = parseJobPage(
          raw,
          response.url || item.href,
          item.title,
          item.job_status
        );

        return {
          ...fallback,
          ...parsed,
          candidate_total: item.candidate_total,
          candidate_new: item.candidate_new,
          candidate_all_url: item.candidate_all_url,
          snapshot_total_hint: totalHint
        };
      } catch (_) {
        return fallback;
      }
    })
  );

    const discovered = results.filter(x => x && x.job_title);
    if (discovered.length) discovered[0].candidate_queues = candidateQueues;
    return discovered;
})()
"""


def discover_employer_job_descriptions(max_jobs=25):
    """
    Discover current roles from ONE persistent Indeed Jobs tab.

    The Jobs tab is reused forever and is never closed by the discovery worker.
    """
    c = get_shared_chrome()
    target_id = None
    sid = None
    selected_url = ""

    try:
        ensured = ensure_indeed_jobs_tab(c)
        target = ensured.get("target") or {}
        target_id = target.get("targetId")
        selected_url = str(
            target.get("url")
            or INDEED_CURRENT_JOBS_URL
        )

        if not target_id:
            set_state(
                "indeed_jobs_discovery_last_error",
                "Indeed Jobs target could not be found.",
            )
            return []

        sid = c.attach(target_id)

        expression = EMPLOYER_JOB_DISCOVERY_JS.replace(
            "__MAX_JOBS__",
            str(max(1, min(int(max_jobs), 25))),
        )

        rows = c.evaluate(
            sid,
            expression,
            await_promise=True,
            timeout=120,
        ) or []

        clean_rows = [
            x
            for x in rows
            if isinstance(x, dict)
            and str(x.get("job_title") or "").strip()
        ]

        # NUNES exact Indeed role snapshot:
        # - candidate_total = the Jobs-page "All" value
        # - candidate_new   = the Jobs-page "New" value
        # Keep duplicate job titles separate by stable posting/job URL.
        normalized_rows = []
        exact_total_applicants = 0
        exact_total_new = 0
        total_hint = 0

        for row in clean_rows:
            item = dict(row)

            try:
                candidate_total = max(0, int(item.get("candidate_total") or 0))
            except Exception:
                candidate_total = 0

            try:
                candidate_new = max(0, int(item.get("candidate_new") or 0))
            except Exception:
                candidate_new = 0

            job_title = re.sub(
                r"\s+",
                " ",
                str(item.get("job_title") or ""),
            ).strip()
            job_url = str(item.get("job_url") or "").strip()

            # Job URL is the primary identity. Title is included only as a
            # safety discriminator for legacy Indeed URLs.
            role_identity = (job_url + "::" + job_title).encode(
                "utf-8",
                "ignore",
            )
            role_key = hashlib.sha1(role_identity).hexdigest()

            item["role_key"] = role_key
            item["candidate_total"] = candidate_total
            item["candidate_new"] = candidate_new

            # Compatibility aliases used by the dashboard / role-review layer.
            item["candidate_total_hint"] = candidate_total
            item["candidate_new_hint"] = candidate_new
            item["applicant_count"] = candidate_total
            item["active_applicant_count"] = candidate_total

            exact_total_applicants += candidate_total
            exact_total_new += candidate_new

            try:
                total_hint = max(
                    total_hint,
                    int(item.get("snapshot_total_hint") or 0),
                )
            except Exception:
                pass

            normalized_rows.append(item)

        clean_rows = normalized_rows

        # Persist one authoritative Jobs-page snapshot so overview/dashboard
        # code can use Indeed "All" / "New" rather than local processed counts.
        snapshot_payload = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "role_count": len(clean_rows),
            "total_applicants": exact_total_applicants,
            "total_new": exact_total_new,
            "roles": clean_rows,
        }

        set_state(
            "indeed_jobs_role_snapshot",
            json.dumps(snapshot_payload, ensure_ascii=False),
        )
        set_state(
            "indeed_jobs_total_applicants",
            str(exact_total_applicants),
        )
        set_state(
            "indeed_jobs_total_new",
            str(exact_total_new),
        )
        set_state(
            "indeed_jobs_role_count",
            str(len(clean_rows)),
        )
        set_state(
            "indeed_jobs_role_snapshot_at",
            snapshot_payload["captured_at"],
        )

        set_state(
            "indeed_jobs_discovery_target_url",
            selected_url,
        )
        set_state(
            "indeed_jobs_discovery_rows",
            str(len(clean_rows)),
        )
        set_state(
            "indeed_jobs_discovery_total_hint",
            str(total_hint),
        )
        set_state(
            "indeed_jobs_discovery_used_existing_tab",
            "1",
        )
        set_state(
            "indeed_jobs_discovery_last_at",
            datetime.now(timezone.utc).isoformat(),
        )
        set_state(
            "indeed_jobs_discovery_last_error",
            "",
        )

        if clean_rows:
            set_state(
                "indeed_auth_status",
                "SIGNED_IN",
            )
            set_state(
                "indeed_auth_confirmed_at",
                datetime.now(timezone.utc).isoformat(),
            )
            sample = " | ".join(
                (
                    str(x.get("job_title") or "")
                    + " ["
                    + str(x.get("job_status") or "UNKNOWN")
                    + "]"
                )
                for x in clean_rows[:6]
            )
            set_state(
                "indeed_jobs_discovery_sample",
                sample[:2000],
            )

        return clean_rows

    except Exception as exc:
        set_state(
            "indeed_jobs_discovery_last_error",
            str(exc)[:2000],
        )
        raise

    finally:
        if sid:
            try:
                c.detach(sid)
            except Exception:
                pass

        try:
            cleanup_recruitment_indeed_tabs(c)
        except Exception:
            pass



def current_indeed_jobs_snapshot():
    """Return the last exact Indeed Jobs-page role/count snapshot.

    This is read-only and safe for dashboard/API polling.
    """
    try:
        payload = json.loads(
            get_state("indeed_jobs_role_snapshot", "{}") or "{}"
        )
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}

    roles = payload.get("roles")
    if not isinstance(roles, list):
        roles = []

    def _safe_int(value):
        try:
            return max(0, int(value or 0))
        except Exception:
            return 0

    return {
        "captured_at": payload.get("captured_at")
            or get_state("indeed_jobs_role_snapshot_at", ""),
        "role_count": _safe_int(
            payload.get("role_count")
            or get_state("indeed_jobs_role_count", "0")
        ),
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


def chrome_status(connect_if_needed=False):
    """
    Status calls from the dashboard are read-only and never create a Chrome
    connection. The background connection manager owns automatic reconnect.
    """
    snapshot = shared_connection_snapshot()

    if not snapshot["connected"] and not connect_if_needed:
        return {
            "ok": False,
            "chrome_connected": False,
            "indeed_found": False,
            "error": (
                "Automatic Chrome connection is starting."
            ),
            "message": "CHROME NOT CONNECTED",
            "permission_prompt_expected": False,
        }

    try:
        c = (
            connect_shared_chrome(timeout=60)
            if connect_if_needed
            else get_shared_chrome()
        )

        saved = load_settings().get("indeed_candidates_url", "")
        target, targets = find_indeed_target(c, saved)

        if target:
            return {
                "ok": True,
                "chrome_connected": True,
                "indeed_found": True,
                "url": target.get("url") or "",
                "title": target.get("title") or "",
                "page_count": len(targets),
                "target_id": target.get("targetId"),
                "devtools_port": c.endpoint["port"],
                "message": "CHROME CONNECTED • INDEED FOUND",
                "persistent_session": True,
            }

        return {
            "ok": True,
            "chrome_connected": True,
            "indeed_found": False,
            "url": "",
            "title": "",
            "page_count": len(targets),
            "target_id": None,
            "devtools_port": c.endpoint["port"],
            "message": "CHROME CONNECTED • OPEN INDEED",
            "warning": "Chrome is connected, but no Indeed tab is open yet.",
            "persistent_session": True,
        }

    except Exception as e:
        return {
            "ok": False,
            "chrome_connected": False,
            "indeed_found": False,
            "error": str(e),
            "message": "CHROME NOT CONNECTED",
        }


def current_indeed_page():
    c = get_shared_chrome()
    target, _ = choose_indeed_target(
        c,
        load_settings().get("indeed_candidates_url", ""),
    )
    return {
        "url": target.get("url") or "",
        "title": target.get("title") or "",
        "target_id": target.get("targetId"),
    }


def stable_candidate_key(href):
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

    url = urldefrag(href or "")[0]
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    id_keys = [
        "candidateId",
        "candidateid",
        "applicantId",
        "applicantid",
        "applicationId",
        "applicationid",
        "id",
    ]

    for key in id_keys:
        value = query.get(key)
        if value and len(value) >= 5:
            return hashlib.sha1(
                f"{key.lower()}:{value}".encode("utf-8", "ignore")
            ).hexdigest()

    m = re.search(
        r"/(?:candidate|applicant|application)/([^/?#]{5,})",
        parsed.path,
        flags=re.I,
    )
    if m:
        return hashlib.sha1(
            f"path:{m.group(1)}".encode("utf-8", "ignore")
        ).hexdigest()

    volatile = {
        "status", "stage", "sort", "page", "from", "source", "view",
        "tab", "filter", "jobId", "jobid",
    }
    stable_query = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if k not in volatile
    ]
    canonical = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/"),
            "",
            urlencode(stable_query),
            "",
        )
    )

    return hashlib.sha1(
        canonical.encode("utf-8", "ignore")
    ).hexdigest()



def stable_row_candidate_key(
    candidate_name,
    job_title,
    location_text="",
    identity_hint="",
):
    hint = str(identity_hint or "").strip()

    # Prioritize candidate ID from Indeed URL or candidateid attribute
    m = re.search(r"(?:candidate_?id|applicant_?id|application_?id)=([a-zA-Z0-9_-]{5,})", hint, re.I)
    if m:
        return hashlib.sha1(f"candidate:{m.group(1).lower()}".encode("utf-8")).hexdigest()

    m2 = re.search(r"[?&]id=([a-f0-9]{6,})", hint, re.I)
    if m2:
        return hashlib.sha1(f"candidate:{m2.group(1).lower()}".encode("utf-8")).hexdigest()

    clean_name = re.sub(r"\s+", " ", (candidate_name or "").strip().lower())
    clean_job = re.sub(r"\s+", " ", (job_title or "").strip().lower())
    if clean_job in {"the position", "position", "day", "days", "with", "in a", "role", "unknown"}:
        clean_job = ""

    canonical = f"{clean_name}|{clean_job}"
    return hashlib.sha1(("row:" + canonical).encode("utf-8", "ignore")).hexdigest()


def build_row_click_js(entry):
    """
    Build JS that finds the live Indeed candidate block matching
    the candidate or direct anchor, then clicks the candidate control.
    """
    name = json.dumps(entry.get("label") or "")
    job = json.dumps(entry.get("job_title") or "")
    location = json.dumps(entry.get("location_text") or "")
    href = json.dumps(entry.get("href") or "")

    return f"""
(async () => {{
  const targetName = {name};
  const targetJob = {job};
  const targetLocation = {location};
  const targetHref = {href};

  const normalize = (s) => (s || '').replace(/\\s+/g, ' ').trim();
  const eq = (a, b) => normalize(a).toLowerCase() === normalize(b).toLowerCase();

  let row = null;

  // Direct candidate URL anchor match
  if (targetHref) {{
    try {{
      const anchor = document.querySelector(`a[href="${{targetHref}}"]`);
      if (anchor) {{
        row = anchor.closest('[role="row"],tr,li,article,div') || anchor;
      }}
    }} catch (_) {{}}
  }}

  if (!row) {{
    const candidateBlocks = [];

    for (const el of document.querySelectorAll(
      '[data-testid*="candidate" i],'
      + '[data-testid*="applicant" i],'
      + '[data-testid*="application" i],'
      + '[role="row"],tr,li,article,div'
    )) {{
      const raw = el.innerText || '';
      if (!raw || raw.length > 2600) continue;

      const hasName =
        !targetName || targetName === 'Candidate' || targetName === 'Download Cv'
        || raw.toLowerCase().includes(targetName.toLowerCase());

      const hasJob =
        !targetJob
        || ['the position','day','with','in a','unknown'].includes(targetJob.toLowerCase())
        || raw.toLowerCase().includes(('applied to: ' + targetJob).toLowerCase())
        || raw.toLowerCase().includes(targetJob.toLowerCase());

      const hasLocation =
        !targetLocation
        || raw.toLowerCase().includes(targetLocation.toLowerCase());

      if (hasName && hasJob && hasLocation) {{
        candidateBlocks.push(el);
      }}
    }}

    candidateBlocks.sort(
      (a, b) => (a.innerText || '').length - (b.innerText || '').length
    );

    row = candidateBlocks[0];
  }}

  if (!row) {{
    return {{
      clicked: false,
      reason: 'matching candidate row was not found',
      beforeUrl: location.href,
    }};
  }}

  row.scrollIntoView({{
    block: 'center',
    inline: 'nearest',
    behavior: 'instant',
  }});

  await new Promise(resolve => setTimeout(resolve, 180));

  let target = null;

  // Prefer a candidate-name anchor/button/control.
  for (const el of row.querySelectorAll(
    'a,button,[role="button"],'
    + '[data-testid*="name" i],'
    + '[data-testid*="candidate" i]'
  )) {{
    const t = normalize(el.innerText || el.textContent || '');
    if (targetName && eq(t, targetName)) {{
      target = el;
      break;
    }}
  }}

  if (!target) {{
    // Find the smallest element whose text is exactly the candidate name.
    const descendants = [...row.querySelectorAll('*')]
      .filter(el => eq(el.innerText || el.textContent || '', targetName))
      .sort(
        (a, b) =>
          (a.innerText || '').length - (b.innerText || '').length
      );

    target = descendants[0] || row;
  }}

  const fire = (type) => {{
    try {{
      target.dispatchEvent(
        new MouseEvent(type, {{
          bubbles: true,
          cancelable: true,
          view: window,
          button: 0,
        }})
      );
    }} catch (_) {{}}
  }};

  fire('pointerdown');
  fire('mousedown');
  fire('pointerup');
  fire('mouseup');
  fire('click');

  try {{
    target.click();
  }} catch (_) {{}}

  await new Promise(resolve => setTimeout(resolve, 1300));

  return {{
    clicked: true,
    beforeUrl: location.href,
    candidateName: targetName,
    jobTitle: targetJob,
    rowText: (row.innerText || '').slice(0, 1800),
  }};
}})()
"""


GENERIC_CANDIDATE_LABELS = {
    "candidate",
    "candidates",
    "view candidate",
    "view candidates",
    "applicant",
    "applicants",
    "view applicant",
    "view applicants",
    "application",
    "applications",
    "view applications",
    "manage candidates",
    "find candidates",
    "download cv",
    "download resume",
    "view cv",
    "view resume",
    "open cv",
    "open resume",
    "resume",
    "cv",
    "core skills",
    "contact information",
    "contact info",
    "activity",
    "interest",
    "matches to job post",
    "education",
    "yes",
    "no",
}


def _usable_candidate_name(value):
    """Return a real-looking candidate name or an empty string.

    This is deliberately conservative: action labels, statuses, role text and
    navigation/UI labels must never become a candidate identity.
    """
    name = re.sub(r"\s+", " ", str(value or "")).strip(" |:-")

    # Indeed sometimes renders contact email next to the candidate heading.
    # Never persist an email address as part of candidate_name.
    name = re.sub(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        " ",
        name,
    )
    name = re.sub(r"\s+", " ", name).strip(" |:-")
    low = name.casefold()

    if not name or len(name) < 2 or len(name) > 100:
        return ""

    if low in GENERIC_CANDIDATE_LABELS:
        return ""

    if low.startswith((
        "applied to",
        "applied ",
        "new ",
        "reviewing",
        "contacting",
        "interviewing",
        "rejected",
        "hired",
        "selected",
        "not selected",
        "withdrawn",
        "archived",
        "sort by",
        "filter",
        "all open",
    )):
        return ""

    if re.search(r"\b(?:download|resume|curriculum|contact information|manage candidates|cv|candidate|applicant)\b", low):
        return ""

    # Candidate names should contain letters and should not be a sentence.
    if not re.search(r"[A-Za-z]", name):
        return ""

    words = re.findall(r"[A-Za-z][A-Za-z.'-]*", name)
    if not (1 <= len(words) <= 7):
        return ""

    return name


def candidate_link_score(href, text, context_text="", data_test_id=""):
    h = (href or "").lower().strip()
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    ctx = re.sub(r"\s+", " ", (context_text or "").lower()).strip()
    dt = (data_test_id or "").lower()

    if "indeed." not in h:
        return -100

    if any(
        x in h
        for x in [
            "/help/",
            "/hire/resources",
            "/company/",
            "/employers/cs/login",
        ]
    ):
        return -100

    if t in GENERIC_CANDIDATE_LABELS:
        return -100

    path_no_query = h.split("?", 1)[0].rstrip("/")
    has_unique_query_id = bool(
        re.search(
            r"(?:candidate|applicant|application)(?:id|uid|key)=",
            h,
        )
    )

    if (
        re.search(
            r"/(?:candidate|candidates|applicant|applicants|applications)$",
            path_no_query,
        )
        and not has_unique_query_id
    ):
        return -100

    score = 0

    if has_unique_query_id:
        score += 24

    if re.search(
        r"/(?:candidate|applicant|application)/[^/?#]{5,}",
        h,
    ):
        score += 20

    if any(k in dt for k in ["candidate", "applicant", "application"]):
        score += 8

    if "candidate" in h:
        score += 5
    if "applicant" in h:
        score += 5
    if "application" in h:
        score += 3

    if (
        2 <= len(t) <= 100
        and t not in GENERIC_CANDIDATE_LABELS
        and not t.startswith("view ")
    ):
        score += 3

    if any(
        x in ctx
        for x in [
            "awaiting review",
            "new",
            "applied",
            "application",
            "resume",
            "screening",
            "contacting",
            "reviewed",
        ]
    ):
        score += 4

    return score


INDEED_PIPELINE_STATUS_PATTERNS = [
    ("Not Selected", re.compile(r"(?i)(?:^|[\n•·|—-])\s*not\s+selected\s*(?:$|[\n•·|—-])")),
    ("Selected", re.compile(r"(?i)(?:^|[\n•·|—-])\s*selected\s*(?:$|[\n•·|—-])")),
    ("Hired", re.compile(r"(?i)(?:^|[\n•·|—-])\s*hired\s*(?:$|[\n•·|—-])")),
    ("Rejected", re.compile(r"(?i)(?:^|[\n•·|—-])\s*rejected\s*(?:$|[\n•·|—-])")),
    ("Withdrawn", re.compile(r"(?i)(?:^|[\n•·|—-])\s*withdrawn\s*(?:$|[\n•·|—-])")),
    ("Archived", re.compile(r"(?i)(?:^|[\n•·|—-])\s*archived\s*(?:$|[\n•·|—-])")),
    ("Interviewing", re.compile(r"(?i)(?:^|[\n•·|—-])\s*interviewing\s*(?:$|[\n•·|—-])")),
    ("Contacting", re.compile(r"(?i)(?:^|[\n•·|—-])\s*contacting\s*(?:$|[\n•·|—-])")),
    ("Reviewing", re.compile(r"(?i)(?:^|[\n•·|—-])\s*reviewing\s*(?:$|[\n•·|—-])")),
    ("New", re.compile(r"(?i)(?:^|[\n•·|—-])\s*new\s*(?:$|[\n•·|—-])")),
]


def candidate_pipeline_status(text, explicit=None):
    explicit_value = re.sub(r"\s+", " ", str(explicit or "")).strip()
    canonical = {
        "new": "New",
        "reviewing": "Reviewing",
        "contacting": "Contacting",
        "interviewing": "Interviewing",
        "hired": "Hired",
        "selected": "Selected",
        "not selected": "Not Selected",
        "rejected": "Rejected",
        "withdrawn": "Withdrawn",
        "archived": "Archived",
    }
    if explicit_value.lower() in canonical:
        return canonical[explicit_value.lower()]
    raw = str(text or "")
    for label, pattern in INDEED_PIPELINE_STATUS_PATTERNS:
        if pattern.search(raw):
            return label
    return explicit_value[:40]


def is_current_new_candidate(text):
    """
    Indeed candidate row status used for the one-time catch-up.

    Examples from the live queue:
      New • Applied Today
      New · Applied Today
      New
    """
    s = re.sub(r"\s+", " ", (text or "").strip().lower())

    if not s:
        return False

    # Require "new" as its own word/status, not a substring.
    has_new = bool(re.search(r"(^|[\s•·|—-])new([\s•·|—-]|$)", s))
    if not has_new:
        return False

    # Strong status/application context avoids navigation labels.
    has_application_context = any(
        token in s
        for token in [
            "applied",
            "application",
            "awaiting review",
            "resume",
            "matches to job post",
        ]
    )

    return has_new and has_application_context


def is_new_status_hint(text):
    # Kept as compatibility alias for older internal calls.
    return is_current_new_candidate(text)


COLLECT_LINKS_JS = r"""
(async () => {
  const originalY = window.scrollY;
  const records = new Map();

  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
  const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const pipelineStatusFromText = (raw) => {
    const text = String(raw || '');
    const patterns = [
      ['Not Selected', /(?:^|[\n•·|—-])\s*not\s+selected\s*(?:$|[\n•·|—-])/i],
      ['Selected', /(?:^|[\n•·|—-])\s*selected\s*(?:$|[\n•·|—-])/i],
      ['Hired', /(?:^|[\n•·|—-])\s*hired\s*(?:$|[\n•·|—-])/i],
      ['Rejected', /(?:^|[\n•·|—-])\s*rejected\s*(?:$|[\n•·|—-])/i],
      ['Withdrawn', /(?:^|[\n•·|—-])\s*withdrawn\s*(?:$|[\n•·|—-])/i],
      ['Archived', /(?:^|[\n•·|—-])\s*archived\s*(?:$|[\n•·|—-])/i],
      ['Interviewing', /(?:^|[\n•·|—-])\s*interviewing\s*(?:$|[\n•·|—-])/i],
      ['Contacting', /(?:^|[\n•·|—-])\s*contacting\s*(?:$|[\n•·|—-])/i],
      ['Reviewing', /(?:^|[\n•·|—-])\s*reviewing\s*(?:$|[\n•·|—-])/i],
      ['New', /(?:^|[\n•·|—-])\s*new\s*(?:$|[\n•·|—-])/i],
    ];
    for (const [label, pattern] of patterns) {
      if (pattern.test(text)) return label;
    }
    return '';
  };

  const parseCandidateBlock = (row) => {
    if (!row) return null;

    const raw = (row.innerText || '').trim();
    if (!raw || raw.length < 10 || raw.length > 3000) return null;

    const lines = raw
      .split(/\n+/)
      .map(normalize)
      .filter(Boolean);

    let jobTitle = '';

    // Same-line form: "Applied to: Purchase Executive"
    const sameLineJob = raw.match(
      /(?:^|\n)\s*Applied\s+to\s*:?\s*([^\n]{2,180})/i
    );

    if (sameLineJob) {
      const possible = normalize(sameLineJob[1]);
      if (
        possible
        && !/^new\b/i.test(possible)
        && !/^applied\b/i.test(possible)
      ) {
        jobTitle = possible;
      }
    }

    // Split-line DOM form:
    // Applied to:
    // Purchase Executive
    if (!jobTitle) {
      for (let i=0; i<lines.length; i++) {
        const m = lines[i].match(/^Applied\s+to\s*:?\s*(.*)$/i);
        if (!m) continue;

        const inline = normalize(m[1] || '');
        if (inline && inline.length >= 2) {
          jobTitle = inline;
          break;
        }

        if (lines[i+1]) {
          jobTitle = normalize(lines[i+1]);
          break;
        }
      }
    }

    // DOM anchor fallback. Indeed often renders the role as a separate link.
    if (!jobTitle) {
      for (const a of [...row.querySelectorAll('a[href]')]) {
        const href = (a.href || '').toLowerCase();
        const text = normalize(a.innerText || a.textContent || '');

        if (
          text
          && text.length >= 2
          && text.length <= 180
          && /(viewjob|jobdetail|jobkey|\/job\/|\/jobs\/)/i.test(href)
        ) {
          jobTitle = text;
          break;
        }
      }
    }

    // Keep the candidate even if role is still unavailable. The detail page
    // gets one more chance to recover the exact role; sending remains blocked
    // until a real role is found.
    jobTitle = normalize(jobTitle);

    const rejectName = (line) => {
      const l = (line || '').toLowerCase();
      return (
        !line ||
        line.length > 120 ||
        l === 'candidates' ||
        l === 'candidate' ||
        l === 'activity' ||
        l === 'interest' ||
        l === 'matches to job post' ||
        l === 'all open and paused jobs' ||
        l === 'all jobs' ||
        l === 'education' ||
        l === 'yes' ||
        l === 'no' ||
        l === 'manage candidates' ||
        l === 'find candidates' ||
        l === 'download cv' ||
        l === 'download resume' ||
        l === 'core skills' ||
        l === 'resume' ||
        l === 'contact information' ||
        l.startsWith('applied to:') ||
        /^new(?:\s|$)/i.test(line) ||
        /^reviewing(?:\s|$)/i.test(line) ||
        /^contacting(?:\s|$)/i.test(line) ||
        /^interviewing(?:\s|$)/i.test(line) ||
        /^rejected(?:\s|$)/i.test(line) ||
        /^hired(?:\s|$)/i.test(line)
      );
    };

    let candidateName = '';

    const explicitNames = [
      ...row.querySelectorAll(
        '[data-testid*="candidate-name" i],'
        + '[data-testid*="applicant-name" i],'
        + '[class*="candidateName" i],'
        + '[class*="candidate-name" i],'
        + 'strong'
      )
    ];

    for (const el of explicitNames) {
      const t = normalize(el.innerText || el.textContent || '');
      if (t && !rejectName(t)) {
        candidateName = t;
        break;
      }
    }

    if (!candidateName) {
      for (const line of lines.slice(0, 10)) {
        if (!rejectName(line)) {
          candidateName = line;
          break;
        }
      }
    }

    if (!candidateName) return null;

    let locationText = '';
    for (const line of lines.slice(1, 8)) {
      const l = line.toLowerCase();
      if (
        line === candidateName ||
        l.startsWith('applied to:') ||
        /^new(?:\s|$)/.test(l) ||
        /^reviewing(?:\s|$)/.test(l) ||
        /^contacting(?:\s|$)/.test(l) ||
        /^interviewing(?:\s|$)/.test(l) ||
        /^rejected(?:\s|$)/.test(l) ||
        /^hired(?:\s|$)/.test(l)
      ) {
        continue;
      }
      if (line.length <= 100) {
        locationText = line;
        break;
      }
    }

    const currentNew =
      /(?:^|[\s•·|—-])new(?:[\s•·|—-]|$)/i.test(raw)
      && /\bapplied\b/i.test(raw);

    // Candidate-name anchor may exist even when the URL itself does not
    // contain "candidate" or "application".
    let candidateHref = '';

    for (const a of [...row.querySelectorAll('a[href]')]) {
      const href = a.href || '';
      const text = normalize(a.innerText || a.textContent || '');
      if (!href || !/indeed\./i.test(href) || /\/login/i.test(href)) {
        continue;
      }

      const lowHref = href.toLowerCase();
      const nameMatches =
        text.toLowerCase() === candidateName.toLowerCase();

      const explicitCandidateUrl =
        /(candidate|applicant|application)/i.test(lowHref)
        && !/(viewjob|jobdetail|jobkey|\/jobs?\/)/i.test(lowHref);

      if (nameMatches || explicitCandidateUrl) {
        candidateHref = href;
        break;
      }
    }

    // Pull stable React / Indeed identifiers from attributes when possible.
    const identityBits = [];
    const nodes = [row, ...row.querySelectorAll('*')].slice(0, 300);

    for (const el of nodes) {
      for (const attr of [...(el.attributes || [])]) {
        const n = (attr.name || '').toLowerCase();
        const v = (attr.value || '').trim();

        if (!v || v.length > 220) continue;

        if (
          /(candidate|applicant|application).*(id|uid|key)/i.test(n)
          || /^(data-(id|key|uid)|candidateid|applicantid|applicationid)$/i.test(n)
        ) {
          identityBits.push(`${n}=${v}`);
        }

        if (
          /(candidate|applicant|application)/i.test(v)
          && /[a-z0-9_-]{6,}/i.test(v)
          && identityBits.length < 16
        ) {
          identityBits.push(`${n}=${v}`);
        }
      }

      if (identityBits.length >= 16) break;
    }

    return {
      recordType: 'row',
      href: candidateHref,
      text: candidateName,
      candidateName,
      jobTitle,
      locationText,
      contextText: raw.slice(0, 2000),
      dataTestId: (
        row.getAttribute?.('data-testid')
        || ''
      ).slice(0, 240),
      identityHint: identityBits.join('|').slice(0, 2000),
      currentNew,
      pipelineStatus: pipelineStatusFromText(raw),
    };
  };

  const smallestCandidateRows = () => {
    const result = new Set();

    const looksLikeCandidateRow = (el) => {
      if (!el) return false;

      const t = (el.innerText || '').trim();
      if (!t || t.length < 8 || t.length > 3500) return false;

      const meta = [
        el.getAttribute?.('data-testid') || '',
        el.getAttribute?.('aria-label') || '',
        el.className || '',
        el.getAttribute?.('role') || '',
      ].join(' ');

      const applied = /\bApplied\s+to\s*:?\s*/i.test(t);

      const semantic = /(candidate|applicant|application)/i.test(meta);

      const candidateLink = [...el.querySelectorAll('a[href]')].some(a =>
        /(candidate|applicant|application)/i.test(a.href || '')
      );

      const workflow = /\b(new|reviewing|contacting|interviewing|selected|rejected|hired|withdrawn|archived|resume|application|applied)\b/i.test(t);

      const lines = t.split(/\n+/).filter(Boolean);

      return (
        applied ||
        (
          (semantic || candidateLink)
          && workflow
          && lines.length >= 2
          && lines.length <= 60
        )
      );
    };

    for (const el of document.querySelectorAll(
      '[data-testid*="candidate" i],'
      + '[data-testid*="applicant" i],'
      + '[data-testid*="application" i],'
      + '[role="row"],tr,li,article'
    )) {
      if (looksLikeCandidateRow(el)) {
        result.add(el);
      }
    }

    for (const el of [...document.querySelectorAll('div')]) {
      const t = el.innerText || '';

      if (
        !/\bApplied\s+to\s*:?\s*/i.test(t)
        || t.length < 10
        || t.length > 2300
      ) {
        continue;
      }

      let smallerChild = false;

      for (const child of [...el.children]) {
        const ct = child.innerText || '';

        if (
          /\bApplied\s+to\s*:?\s*/i.test(ct)
          && ct.length >= 10
          && ct.length < t.length
          && ct.length <= 2000
        ) {
          smallerChild = true;
          break;
        }
      }

      if (!smallerChild) {
        result.add(el);
      }
    }

    return [...result];
  };

  const collect = () => {
    for (const row of smallestCandidateRows()) {
      const rec = parseCandidateBlock(row);
      if (!rec) continue;

      const key = [
        rec.identityHint || '',
        rec.candidateName.toLowerCase(),
        rec.jobTitle.toLowerCase(),
        rec.locationText.toLowerCase(),
      ].join('|||');

      const old = records.get(key);
      if (
        !old
        || (rec.contextText || '').length > (old.contextText || '').length
      ) {
        records.set(key, rec);
      }
    }

    // Compatibility fallback for pages that still expose proper candidate URLs.
    for (const a of [...document.querySelectorAll('a[href]')]) {
      const href = a.href || '';
      const low = href.toLowerCase();
      if (
        !/indeed\./i.test(href)
        || /\/login/i.test(href)
        || !/(candidate|applicant|application)/i.test(low)
      ) {
        continue;
      }

      const text = normalize(a.innerText || a.textContent || '');
      const container =
        a.closest(
          '[data-testid*="candidate" i],'
          + '[data-testid*="applicant" i],'
          + '[role="row"],tr,li,article'
        )
        || a.parentElement;

      const contextText = (container?.innerText || '').trim().slice(0, 2000);

      const key = `href|||${href}`;
      if (!records.has(key)) {
        records.set(key, {
          recordType: 'link',
          href,
          text,
          candidateName: text,
          jobTitle: '',
          locationText: '',
          contextText,
          dataTestId: (
            container?.getAttribute?.('data-testid')
            || a.getAttribute('data-testid')
            || ''
          ).slice(0, 240),
          identityHint: '',
          currentNew:
            /(?:^|[\s•·|—-])new(?:[\s•·|—-]|$)/i.test(contextText)
            && /\bapplied\b/i.test(contextText),
          pipelineStatus: pipelineStatusFromText(contextText),
        });
      }
    }
  };

    collect();

    const documentScroller = document.scrollingElement || document.documentElement;
    const scrollTargets = [...new Set([
        documentScroller,
        ...document.querySelectorAll('*'),
    ])]
        .map(el => {
            const maxScroll = el.scrollHeight - el.clientHeight;
            if (maxScroll <= 120) return null;
            const style = getComputedStyle(el);
            if (
                el !== documentScroller
                && !/(auto|scroll|overlay|hidden)/i.test(style.overflowY || '')
            ) return null;
            const text = el.innerText || '';
            const rowSignals = (text.match(/\bApplied\s+to\s*:/gi) || []).length;
            return { element: el, maxScroll, rowSignals };
        })
        .filter(Boolean)
        .sort((a, b) => b.rowSignals - a.rowSignals || b.maxScroll - a.maxScroll);

    const scrollTarget = scrollTargets[0]?.element || documentScroller;
    const usesWindowScroll = (
        scrollTarget === documentScroller
        || scrollTarget === document.body
        || scrollTarget === document.documentElement
    );
    const originalScrollTop = usesWindowScroll ? window.scrollY : scrollTarget.scrollTop;
    const getScrollTop = () => (
        usesWindowScroll ? window.scrollY : scrollTarget.scrollTop
    );
    const getMaxScroll = () => (
        usesWindowScroll
            ? Math.max(0, document.documentElement.scrollHeight - window.innerHeight)
            : Math.max(0, scrollTarget.scrollHeight - scrollTarget.clientHeight)
    );
    const setScrollTop = value => {
        if (usesWindowScroll) window.scrollTo(0, value);
        else scrollTarget.scrollTop = value;
    };

    let lastCount = records.size;
    let stableRounds = 0;
    let reachedBottom = false;
    let scrollPasses = 0;

    for (let i = 0; i < 200; i++) {
        const maxY = getMaxScroll();
        const currentY = getScrollTop();

        if (currentY >= maxY - 8) {
            reachedBottom = true;
            await sleep(250);
            collect();
            const nextMaxY = getMaxScroll();
            if (records.size !== lastCount || nextMaxY > maxY + 8) {
                stableRounds = 0;
                lastCount = records.size;
            } else {
                stableRounds += 1;
            }
            if (stableRounds >= 5) break;
        } else {
            setScrollTop(
                Math.min(
                    maxY,
                    currentY
                        + Math.max(420, Math.floor((
                            usesWindowScroll ? window.innerHeight : scrollTarget.clientHeight
                        ) * 0.72))
                )
            );
            scrollPasses += 1;
            await sleep(250);
            collect();

            if (records.size === lastCount) {
                stableRounds += 1;
            } else {
                stableRounds = 0;
                lastCount = records.size;
            }
        }
    }

    setScrollTop(originalScrollTop);
  await sleep(140);

  return {
    url: location.href,
    title: document.title,
    bodyPreview: (
      document.body?.innerText
      || ''
    ).slice(0, 12000),
    links: [...records.values()],
    collectionComplete: reachedBottom,
    collectedLinkCount: records.size,
    scrollContainerFound: !usesWindowScroll,
    scrollPasses,
  };
})()
"""


COLLECT_VISIBLE_LINKS_JS = r"""
(async () => {
  window.scrollTo(0, 0);

  const title = document.title || '';
  const bodyText = document.body?.innerText || '';
  if (/just a moment/i.test(title) || /turnstile/i.test(bodyText) || /checking if the site connection is secure/i.test(bodyText)) {
    return {
      url: location.href,
      title: document.title,
      bodyPreview: bodyText.slice(0, 5000),
      links: [],
      collectionComplete: false,
      collectedLinkCount: 0,
      fastVisible: true,
      challenge: true,
    };
  }

  const records = new Map();
  const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim();

  const pipelineStatusFromText = (raw) => {
    const text = String(raw || '');
    const patterns = [
      ['Not Selected', /(?:^|[\n•·|—-])\s*not\s+selected\s*(?:$|[\n•·|—-])/i],
      ['Selected', /(?:^|[\n•·|—-])\s*selected\s*(?:$|[\n•·|—-])/i],
      ['Hired', /(?:^|[\n•·|—-])\s*hired\s*(?:$|[\n•·|—-])/i],
      ['Rejected', /(?:^|[\n•·|—-])\s*rejected\s*(?:$|[\n•·|—-])/i],
      ['Withdrawn', /(?:^|[\n•·|—-])\s*withdrawn\s*(?:$|[\n•·|—-])/i],
      ['Archived', /(?:^|[\n•·|—-])\s*archived\s*(?:$|[\n•·|—-])/i],
      ['Interviewing', /(?:^|[\n•·|—-])\s*interviewing\s*(?:$|[\n•·|—-])/i],
      ['Contacting', /(?:^|[\n•·|—-])\s*contacting\s*(?:$|[\n•·|—-])/i],
      ['Reviewing', /(?:^|[\n•·|—-])\s*reviewing\s*(?:$|[\n•·|—-])/i],
      ['New', /(?:^|[\n•·|—-])\s*new\s*(?:$|[\n•·|—-])/i],
    ];
    for (const [label, pattern] of patterns) {
      if (pattern.test(text)) return label;
    }
    return '';
  };

  const parseCandidateBlock = (row) => {
    if (!row) return null;

    const raw = (row.innerText || '').trim();
    if (!raw || raw.length < 8 || raw.length > 3500) return null;

    const lines = raw
      .split(/\n+/)
      .map(normalize)
      .filter(Boolean);

    let jobTitle = '';

    const sameLineJob = raw.match(
      /(?:^|\n)\s*Applied\s+to\s*:?\s*([^\n]{2,180})/i
    );

    if (sameLineJob) {
      const possible = normalize(sameLineJob[1]);
      if (possible && !/^new\b/i.test(possible) && !/^applied\b/i.test(possible)) {
        jobTitle = possible;
      }
    }

    if (!jobTitle) {
      for (let i=0; i<lines.length; i++) {
        const m = lines[i].match(/^Applied\s+to\s*:?\s*(.*)$/i);
        if (!m) continue;
        const inline = normalize(m[1] || '');
        if (inline && inline.length >= 2) {
          jobTitle = inline;
          break;
        }
        if (lines[i+1]) {
          jobTitle = normalize(lines[i+1]);
          break;
        }
      }
    }

    if (!jobTitle) {
      for (const a of [...row.querySelectorAll('a[href]')]) {
        const href = (a.href || '').toLowerCase();
        const text = normalize(a.innerText || a.textContent || '');
        if (
          text
          && text.length >= 2
          && text.length <= 180
          && /(viewjob|jobdetail|jobkey|\/job\/|\/jobs\/)/i.test(href)
        ) {
          jobTitle = text;
          break;
        }
      }
    }

    // Direct role matching for the core recruitment positions
    if (!jobTitle) {
      if (/purchase\s+executive/i.test(raw)) {
        jobTitle = 'Purchase Executive';
      } else if (/marketing\s+&(?:amp;)?\s+lead\s+coordination/i.test(raw) || /marketing\s+executive/i.test(raw)) {
        jobTitle = 'Marketing & Lead Coordination Executive';
      } else if (/driver\s+cum\s+electrician/i.test(raw)) {
        jobTitle = 'Driver cum Electrician – Technical Support Assistant';
      }
    }

    if (jobTitle && ['the position', 'position', 'day', 'days', 'with', 'in a', 'role', 'unknown'].includes(jobTitle.toLowerCase())) {
      jobTitle = '';
    }

    const rejectName = (line) => {
      const l = (line || '').toLowerCase();
      return (
        !line ||
        line.length > 120 ||
        [
          'candidates','candidate','activity','interest',
          'matches to job post','all open and paused jobs','all jobs',
          'education','yes','no','manage candidates','find candidates',
          'download cv','download resume','view cv','view resume','open cv','open resume',
          'core skills','resume','cv','contact information','applicant','applicants'
        ].includes(l) ||
        l.startsWith('applied to:') ||
        /^new(?:\s|$)/i.test(line) ||
        /^reviewing(?:\s|$)/i.test(line) ||
        /^contacting(?:\s|$)/i.test(line) ||
        /^interviewing(?:\s|$)/i.test(line) ||
        /^rejected(?:\s|$)/i.test(line) ||
        /^hired(?:\s|$)/i.test(line) ||
        /@/.test(line)
      );
    };

    let candidateName = '';

    for (const el of row.querySelectorAll(
      '[data-testid*="candidate-name" i],'
      + '[data-testid*="applicant-name" i],'
      + '[class*="candidateName" i],'
      + '[class*="candidate-name" i],strong'
    )) {
      const t = normalize(el.innerText || el.textContent || '');
      if (t && !rejectName(t)) {
        candidateName = t;
        break;
      }
    }

    if (!candidateName) {
      for (const line of lines.slice(0, 10)) {
        if (!rejectName(line)) {
          candidateName = line;
          break;
        }
      }
    }

    let locationText = '';
    for (const line of lines.slice(1, 8)) {
      const l = line.toLowerCase();
      if (
        line === candidateName ||
        l.startsWith('applied to:') ||
        /^new(?:\s|$)/.test(l) ||
        /^reviewing(?:\s|$)/.test(l)
      ) continue;
      if (line.length <= 100 && !/@/.test(line)) {
        locationText = line;
        break;
      }
    }

    const currentNew =
      /(?:^|[\s•·|—-])new(?:[\s•·|—-]|$)/i.test(raw)
      && /\bapplied\b/i.test(raw);

    let candidateHref = '';
    let candidateId = '';

    for (const a of [...row.querySelectorAll('a[href]')]) {
      const href = a.href || '';
      const text = normalize(a.innerText || a.textContent || '');
      if (!href || !/indeed\./i.test(href) || /\/login/i.test(href)) continue;

      const lowHref = href.toLowerCase();
      const m = href.match(/[?&]id=([a-f0-9]+)/i) || href.match(/\/candidates\/view\?.*?id=([a-f0-9]+)/i) || href.match(/\/candidate\/([a-f0-9]+)/i);
      if (m && !candidateId) {
        candidateId = m[1];
      }

      const nameMatches = candidateName && text.toLowerCase() === candidateName.toLowerCase();
      const explicitCandidateUrl =
        /(candidate|applicant|application)/i.test(lowHref)
        && !/(viewjob|jobdetail|jobkey|\/jobs?\/)/i.test(lowHref);

      if (nameMatches || explicitCandidateUrl) {
        candidateHref = href;
        if (candidateId) break;
      }
    }

    if (!candidateId) {
      for (const attr of [...(row.attributes || [])]) {
        const n = (attr.name || '').toLowerCase();
        const v = (attr.value || '').trim();
        if (/(candidate|applicant|application).*(id|uid|key)/i.test(n) && v) {
          candidateId = v;
          break;
        }
      }
    }

    const identityHint = candidateId
      ? `candidate_id=${candidateId}`
      : `${candidateName || 'unknown'}|${normalize(jobTitle)}|${locationText}`;

    return {
      recordType: 'row',
      href: candidateHref,
      text: candidateName || 'Candidate',
      candidateName: candidateName || 'Candidate',
      jobTitle: normalize(jobTitle),
      locationText,
      contextText: raw.slice(0, 2000),
      dataTestId: (row.getAttribute?.('data-testid') || '').slice(0, 240),
      identityHint,
      currentNew,
      pipelineStatus: pipelineStatusFromText(raw),
    };
  };

  const isCandidateRow = (el) => {
    if (!el) return false;
    const raw = (el.innerText || '').trim();
    if (raw.length < 10 || raw.length > 3000) return false;

    const hasAppliedTo = /\bApplied\s+to\s*:?\s*/i.test(raw);
    const hasCandidateLink = [...el.querySelectorAll('a[href]')].some(a =>
      /(?:candidate|applicant|application|\/candidates\/view)/i.test(a.href || '')
    );
    const hasCandidateTestId = (
      el.getAttribute?.('data-testid') || ''
    ).toLowerCase().includes('candidate');
    const isTableRow = el.matches?.('tr, [role="row"]');
    const hasWorkflow = /\b(New|Reviewing|Contacting|Interviewing|Selected|Not Selected|Rejected|Hired)\b/i.test(raw);
    const hasDate = /\b(?:day|days|month|months|year|years|today|yesterday)\s+ago\b/i.test(raw) || /\bapplied\b/i.test(raw);

    return hasAppliedTo || ((hasCandidateLink || hasCandidateTestId || isTableRow) && (hasWorkflow || hasDate));
  };

  const candidates = [];

  for (const el of document.querySelectorAll(
    '[data-testid*="candidate" i],'
    + '[data-testid*="applicant" i],'
    + '[data-testid*="application" i],'
    + '[role="row"],tr,li,article,div'
  )) {
    if (!isCandidateRow(el)) continue;

    let smallerChild = false;
    for (const child of [...el.children]) {
      if (isCandidateRow(child) && (child.innerText || '').length < (el.innerText || '').length) {
        smallerChild = true;
        break;
      }
    }

    if (!smallerChild) candidates.push(el);
  }

  for (const row of candidates) {
    const rec = parseCandidateBlock(row);
    if (!rec) continue;

    const key = rec.identityHint || `${rec.candidateName.toLowerCase()}|||${rec.locationText.toLowerCase()}`;
    if (!records.has(key)) records.set(key, rec);
  }

  return {
    url: location.href,
    title: document.title,
    bodyPreview: (document.body?.innerText || '').slice(0, 12000),
    links: [...records.values()],
    collectionComplete: false,
    collectedLinkCount: records.size,
    fastVisible: true,
  };
})()
"""

CANDIDATE_PAYLOAD_JS = r"""
(async () => {
  const TARGET_CANDIDATE_NAME = __TARGET_CANDIDATE_NAME__;
  const TARGET_ROW_JOB = __TARGET_ROW_JOB__;
  const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

  // Let the Indeed SPA finish rendering the selected candidate.
  await sleep(900);

  const isVisible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const style = getComputedStyle(el);
    return (
      r.width > 0 &&
      r.height > 0 &&
      style.display !== 'none' &&
      style.visibility !== 'hidden'
    );
  };

  // Expand the candidate sections that can contain contact/resume details.
  // We only click button-like elements so we do not navigate away from the
  // candidate page through an arbitrary anchor.
  const buttonLike = [
    ...document.querySelectorAll(
      'button,[role="button"],'
      + '[data-testid*="resume" i],'
      + '[data-testid*="contact" i],'
      + '[aria-label*="resume" i],'
      + '[aria-label*="contact" i]'
    )
  ];

  const clicked = new Set();

  const shouldExpand = (el) => {
    const text = (
      el.innerText
      || el.textContent
      || el.getAttribute?.('aria-label')
      || ''
    ).trim().toLowerCase();

    if (!text || text.length > 160) return false;

    return (
      /^resume$/.test(text) ||
      /view\s+resume/.test(text) ||
      /show\s+resume/.test(text) ||
      /candidate\s+resume/.test(text) ||
      /^cv$/.test(text) ||
      /contact\s+info/.test(text) ||
      /contact\s+information/.test(text) ||
      /contact\s+details/.test(text) ||
      /show\s+contact/.test(text) ||
      /view\s+contact/.test(text) ||
      /download\s+(?:cv|resume)/.test(text) ||
      /open\s+(?:cv|resume)/.test(text)
    );
  };

  for (const el of buttonLike) {
    if (clicked.size >= 14) break;
    if (!isVisible(el) || !shouldExpand(el)) continue;

    const key = (
      (el.innerText || el.textContent || '')
      + '|'
      + (el.getAttribute?.('aria-label') || '')
    ).trim().toLowerCase();

    if (clicked.has(key)) continue;
    clicked.add(key);

    try {
      el.click();
      await sleep(650);
    } catch (_) {}
  }

  const normalize = (s) => (s || '').replace(/\s+/g, ' ').trim();

  // Find the selected candidate detail/drawer. Email extraction must stay
  // inside this scope to avoid reading another applicant from the list.
  const rootCandidates = [
    ...document.querySelectorAll(
      '[role="dialog"],aside,'
      + '[data-testid*="candidate-detail" i],'
      + '[data-testid*="candidate-panel" i],'
      + '[data-testid*="application-detail" i],'
      + '[data-testid*="candidate-drawer" i],'
      + '[class*="candidateDetail" i],'
      + '[class*="candidate-detail" i],'
      + '[class*="drawer" i]'
    )
  ].filter(isVisible);

  const targetNameLow = normalize(TARGET_CANDIDATE_NAME).toLowerCase();
  const currentUrl = (location.href || '').toLowerCase();
  const isDirectCandidatePage = (
    /(?:\/candidates\/view|\/candidate\/)/i.test(currentUrl)
    && /[?&]id=[a-f0-9]+/i.test(currentUrl)
  );

  let detailRoot = null;
  if (isDirectCandidatePage) {
    detailRoot = document.body;
  } else {
    for (const root of rootCandidates) {
      const text = normalize(root.innerText || '');
      if (!text) continue;
      if (targetNameLow && !['candidate', 'download cv', 'download resume'].includes(targetNameLow)) {
        if (text.toLowerCase().includes(targetNameLow)) {
          if (!detailRoot || text.length < normalize(detailRoot.innerText || '').length) {
            detailRoot = root;
          }
        }
      } else {
        if (!detailRoot || text.length < normalize(detailRoot.innerText || '').length) {
          detailRoot = root;
        }
      }
    }
  }

  if (!detailRoot && targetNameLow && !['candidate', 'download cv', 'download resume'].includes(targetNameLow)) {
    const matching = [...document.querySelectorAll('section,article,div')]
      .filter(isVisible)
      .map(el => ({
        el,
        text: normalize(el.innerText || ''),
      }))
      .filter(x =>
        x.text
        && x.text.length >= targetNameLow.length
        && x.text.length <= 30000
        && x.text.toLowerCase().includes(targetNameLow)
        && /(resume|contact|application|applied)/i.test(x.text)
      )
      .sort((a,b) => a.text.length - b.text.length);

    detailRoot = matching[0]?.el || null;
  }

  if (!detailRoot) {
    const pageText = normalize(document.body?.innerText || '');
    if (
      /(candidate|applicant|application)/i.test(currentUrl)
      && /(resume|contact|applied\s+to|application)/i.test(pageText)
      && (targetNameLow && !['candidate', 'download cv'].includes(targetNameLow) ? pageText.toLowerCase().includes(targetNameLow) : true)
    ) {
      detailRoot = document.body;
    }
  }

  const scopeRoot = detailRoot || document.body;
  const scopeIsCandidateSpecific = Boolean(detailRoot);

  const body = (scopeRoot?.innerText || document.body?.innerText || '')
    .slice(0, 180000);

  const headings = [...scopeRoot.querySelectorAll(
    'h1,h2,h3,[data-testid*="candidate-name" i],'
    + '[data-testid*="name" i]'
  )]
    .map(x => (x.innerText || '').trim())
    .filter(Boolean)
    .slice(0, 30);

  let recoveredCandidateName = '';
  for (const h of headings) {
    const norm = normalize(h);
    const low = norm.toLowerCase();
    if (
      norm
      && norm.length >= 2
      && norm.length <= 80
      && !['candidate','candidates','download cv','download resume','view cv','view resume','core skills','resume','cv','contact information'].includes(low)
      && !low.startsWith('applied to')
      && !low.includes('@')
      && !/^(new|reviewing|contacting|interviewing|rejected|hired)$/i.test(low)
    ) {
      recoveredCandidateName = norm;
      break;
    }
  }

  const resumeNodes = [...scopeRoot.querySelectorAll(
    '[data-testid*="resume" i],'
    + '[id*="resume" i],'
    + '[class*="resume" i],'
    + '[aria-label*="resume" i],'
    + '[data-testid*="cv" i],'
    + '[id*="cv" i],'
    + '[class*="cv" i]'
  )];

  let resumeText = '';
  for (const n of resumeNodes) {
    const t = (n.innerText || '').trim();
    if (t.length > resumeText.length && t.length < 120000) {
      resumeText = t;
    }
  }

  // Read same-origin resume/profile frames if Indeed renders them in an iframe.
  for (const frame of [...scopeRoot.querySelectorAll('iframe')]) {
    try {
      const t = (frame.contentDocument?.body?.innerText || '').trim();
      if (t.length > resumeText.length && t.length < 120000) {
        resumeText = t;
      }
    } catch (_) {}
  }

  // Resume viewers can be mounted outside the original candidate drawer (portal/
  // overlay rendering). Read only visible resume/CV-labelled nodes and keep the
  // largest text block.
  for (const n of [...document.querySelectorAll(
    '[role="dialog"] [data-testid*="resume" i],'
    + '[role="dialog"] [class*="resume" i],'
    + '[role="dialog"] [id*="resume" i],'
    + '[role="dialog"] [data-testid*="cv" i],'
    + '[role="dialog"] [class*="cv" i],'
    + '[role="dialog"] [id*="cv" i]'
  )]) {
    if (!isVisible(n)) continue;
    const t = (n.innerText || n.textContent || '').trim();
    if (t.length > resumeText.length && t.length < 120000) {
      resumeText = t;
    }
  }

  const links = [...scopeRoot.querySelectorAll('a[href]')].map(a => ({
    href: a.href || '',
    text: (a.innerText || a.textContent || '').trim().slice(0, 180)
  }));

  const embeddedUrls = [
    ...scopeRoot.querySelectorAll('iframe[src],embed[src],object[data]')
  ].map(el => el.src || el.data || '').filter(Boolean);

  const resourceUrls = [];

  const candidateUrls = [];

  const addCandidateUrl = (href, text='') => {
    if (!href) return;
    try {
      const u = new URL(href, location.href);
      if (!/indeed\./i.test(u.hostname)) return;

      const h = u.href.toLowerCase();
      const t = (text || '').toLowerCase();

      let score = 0;
      if (h.includes('resume') || h.includes('cv')) score += 7;
      if (h.includes('download')) score += 4;
      if (h.includes('.pdf') || h.includes('.docx')) score += 7;
      if (t.includes('resume') || t.includes('cv')) score += 5;
      if (t.includes('download')) score += 2;

      if (score >= 4) {
        candidateUrls.push({href: u.href, text, score});
      }
    } catch (_) {}
  };

  for (const x of links) addCandidateUrl(x.href, x.text);
  for (const x of embeddedUrls) addCandidateUrl(x, 'embedded resume');
  for (const x of resourceUrls) addCandidateUrl(x, 'resource resume');

  const unique = new Map();
  for (const x of candidateUrls.sort((a,b) => b.score-a.score)) {
    if (!unique.has(x.href)) unique.set(x.href, x);
  }

  const resumeLinks = [...unique.values()].slice(0, 18);

  let downloaded = null;
  let downloadError = null;
  let fetchedResumeText = '';

  const htmlToText = (html) => {
    try {
      const doc = new DOMParser().parseFromString(html, 'text/html');
      return (doc.body?.innerText || '').trim();
    } catch (_) {
      return html;
    }
  };

  for (const link of resumeLinks) {
    try {
      const r = await fetch(link.href, {
        credentials: 'include',
        redirect: 'follow',
        cache: 'no-store'
      });
      if (!r.ok) continue;

      const ct = (r.headers.get('content-type') || '').toLowerCase();
      const buf = await r.arrayBuffer();

      if (buf.byteLength > 12000000) {
        downloadError = 'Resume file is larger than inline processing limit.';
        continue;
      }

      const bytes = new Uint8Array(buf);

      const isPdf =
        ct.includes('pdf') ||
        (
          bytes.length >= 4 &&
          bytes[0] === 37 &&
          bytes[1] === 80 &&
          bytes[2] === 68 &&
          bytes[3] === 70
        );

      const isDocx =
        ct.includes('wordprocessingml') ||
        ct.includes('msword');

      if (isPdf || isDocx) {
        let binary = '';
        const chunk = 0x7000;
        for (let i=0; i<bytes.length; i+=chunk) {
          binary += String.fromCharCode(...bytes.subarray(i, i+chunk));
        }

        downloaded = {
          mime: ct || (isPdf ? 'application/pdf' : 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
          filename: isPdf ? 'indeed_resume.pdf' : 'indeed_resume.docx',
          base64: btoa(binary)
        };
        break;
      }

      // Some Indeed resume endpoints return HTML/text/JSON instead of a file.
      // Preserve useful text, especially contact details.
      if (
        ct.includes('text/') ||
        ct.includes('html') ||
        ct.includes('json') ||
        ct.includes('javascript')
      ) {
        const raw = new TextDecoder('utf-8').decode(bytes);
        let text = raw;

        if (ct.includes('html')) {
          text = htmlToText(raw);
        }

        if (
          text.length > fetchedResumeText.length &&
          text.length < 140000
        ) {
          fetchedResumeText = text;
        }
      }
    } catch (e) {
      downloadError = String(e);
    }
  }

  if (fetchedResumeText.length > resumeText.length) {
    resumeText = fetchedResumeText;
  }

  const expandedBody = (scopeRoot?.innerText || body).slice(0, 180000);

  // Indeed has multiple candidate-detail layouts. In some layouts the resume is
  // rendered inside ordinary <div>/<section> nodes without "resume" or "cv" in
  // their attributes. The old selector-only logic therefore produced an empty
  // resume_text_cache even though the candidate's resume was visibly on screen.
  //
  // Use the selected candidate detail ONLY (never the applicant list) and require
  // multiple resume-like signals before accepting the detail body as inline resume
  // evidence. This keeps ranking grounded in candidate-specific content.
  const resumeEvidencePatterns = [
    /\bwork\s+experience\b/i,
    /\bprofessional\s+experience\b/i,
    /\bemployment\s+(?:history|experience)\b/i,
    /\beducation\b/i,
    /\bskills?\b/i,
    /\bprojects?\b/i,
    /\bcertifications?\b/i,
    /\bqualification(?:s)?\b/i,
    /\bcareer\s+(?:objective|summary)\b/i,
    /\bprofessional\s+summary\b/i,
    /\btechnical\s+skills?\b/i,
    /\bresponsibilit(?:y|ies)\b/i,
    /\blanguages?\b/i,
  ];

  const cleanInlineResume = (value) => {
    let out = (value || '').replace(/\r/g, '\n');
    out = out
      .split('\n')
      .map(x => x.trim())
      .filter(Boolean)
      .filter(x => !/^(?:download|open|view)\s+(?:cv|resume)$/i.test(x))
      .filter(x => !/^(?:message|reject|move|schedule interview|contact candidate)$/i.test(x))
      .join('\n');
    return out.slice(0, 120000);
  };

  if (
    scopeIsCandidateSpecific
    && (!resumeText || resumeText.trim().length < 220)
  ) {
    const candidateDetailText = cleanInlineResume(expandedBody);
    const resumeSignalCount = resumeEvidencePatterns.reduce(
      (count, pattern) => count + (pattern.test(candidateDetailText) ? 1 : 0),
      0
    );

    // Require a meaningful amount of candidate-specific text and at least two
    // independent resume signals. Contact-only drawers will not satisfy this.
    if (
      candidateDetailText.length >= 350
      && resumeSignalCount >= 2
    ) {
      resumeText = candidateDetailText;
    }
  }

  const emailRe = /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/ig;
  const phoneRe = /(?:\+?\d[\d\s().-]{7,}\d)/g;

  const emails = new Set();
  const phones = new Set();

  for (const a of [...scopeRoot.querySelectorAll('a[href^="mailto:"]')]) {
    const value = (a.href || '').replace(/^mailto:/i, '').split('?')[0].trim();
    if (value) emails.add(value);
  }

  for (const a of [...scopeRoot.querySelectorAll('a[href^="tel:"]')]) {
    const value = (a.href || '').replace(/^tel:/i, '').trim();
    if (value) phones.add(value);
  }

  // Resume evidence is always safe. Candidate-scope text is included only
  // when the detail/drawer could be positively tied to the selected applicant.
  for (const source of [resumeText]) {
    for (const match of (source.match(emailRe) || [])) {
      emails.add(match);
    }
    for (const match of (source.match(phoneRe) || [])) {
      phones.add(match);
    }
  }

  if (scopeIsCandidateSpecific) {
    for (const match of (expandedBody.match(emailRe) || [])) {
      emails.add(match);
    }
    for (const match of (expandedBody.match(phoneRe) || [])) {
      phones.add(match);
    }
  }

  const decodeHtml = (value) => {
    try {
      const textarea = document.createElement('textarea');
      textarea.innerHTML = value || '';
      return textarea.value || '';
    } catch (_) {
      return value || '';
    }
  };

  const htmlText = scopeIsCandidateSpecific
    ? decodeHtml((scopeRoot?.innerHTML || '').slice(0, 280000))
    : '';

  if (scopeIsCandidateSpecific) {
    for (const match of (htmlText.match(emailRe) || [])) {
      emails.add(match);
    }
  }

  // Job title + official Indeed job-description candidates.
  const jobCandidates = [];
  const jobLinks = [];

  const addJob = (value, score=0) => {
    const text = (value || '').replace(/\s+/g, ' ').trim();
    const low = text.toLowerCase();

    if (
      !text
      || text.length < 2
      || text.length > 180
      || [
        'the position',
        'position',
        'jobs',
        'job',
        'manage candidates',
        'candidates',
      ].includes(low)
    ) {
      return;
    }

    jobCandidates.push({text, score});
  };

  for (const a of [...scopeRoot.querySelectorAll('a[href]')]) {
    const rawHref = a.href || '';
    const href = rawHref.toLowerCase();
    const text = (a.innerText || a.textContent || '').trim();

    if (
      /(viewjob|jobdetail|jobkey|\/job\/|\/jobs\/)/i.test(href)
      && text
    ) {
      addJob(text, 100);
      if (/indeed\./i.test(rawHref)) {
        jobLinks.push({href: rawHref, text});
      }
    }
  }

  for (const el of scopeRoot.querySelectorAll(
    '[data-testid*="job-title" i],'
    + '[data-testid*="jobTitle" i],'
    + '[class*="jobTitle" i],'
    + '[aria-label*="job title" i]'
  )) {
    addJob(el.innerText || el.textContent || '', 110);
  }

  const bodyLines = expandedBody
    .split(/\n+/)
    .map(x => x.replace(/\s+/g, ' ').trim())
    .filter(Boolean);

  for (let i=0; i<bodyLines.length; i++) {
    const line = bodyLines[i];

    let m = line.match(/^Applied\s+to\s*:?\s*(.+)$/i);
    if (m) addJob(m[1], 130);

    m = line.match(/^(?:Job|Position|Application\s+for)\s*:?\s*(.+)$/i);
    if (m) addJob(m[1], 90);

    if (/^Applied\s+to\s*:?\s*$/i.test(line) && bodyLines[i+1]) {
      addJob(bodyLines[i+1], 125);
    }
  }

  if (
    TARGET_ROW_JOB
    && !['the position','position','job','jobs'].includes(
      normalize(TARGET_ROW_JOB).toLowerCase()
    )
  ) {
    addJob(TARGET_ROW_JOB, 200);
  }

  jobCandidates.sort((a,b) => b.score - a.score);

  let jobDescription = '';
  let jobDescriptionSource = '';
  let jobUrl = '';

  const cleanDescription = (value) =>
    (value || '')
      .replace(/\r/g, '\n')
      .replace(/[ \t]+/g, ' ')
      .replace(/\n{3,}/g, '\n\n')
      .trim();

  const extractJobPosting = (doc, fallbackUrl='') => {
    let title = '';
    let description = '';

    try {
      for (const script of [...doc.querySelectorAll('script[type="application/ld+json"]')]) {
        try {
          const raw = JSON.parse(script.textContent || 'null');
          const queue = Array.isArray(raw) ? [...raw] : [raw];
          while (queue.length) {
            const item = queue.shift();
            if (!item || typeof item !== 'object') continue;
            if (Array.isArray(item['@graph'])) queue.push(...item['@graph']);
            const kind = item['@type'];
            const types = Array.isArray(kind) ? kind : [kind];
            if (types.some(x => String(x || '').toLowerCase() === 'jobposting')) {
              title = String(item.title || item.name || '').trim();
              description = cleanDescription(htmlToText(String(item.description || '')));
              if (description.length >= 100) {
                return {title, description, url: fallbackUrl};
              }
            }
          }
        } catch (_) {}
      }
    } catch (_) {}

    const selectors = [
      '#jobDescriptionText',
      '[data-testid*="job-description" i]',
      '[data-testid*="jobDescription" i]',
      '[class*="jobDescription" i]',
      '[class*="job-description" i]',
      '[id*="jobDescription" i]',
      '[aria-label*="job description" i]'
    ];

    for (const selector of selectors) {
      try {
        for (const el of [...doc.querySelectorAll(selector)]) {
          const text = cleanDescription(el.innerText || el.textContent || '');
          if (text.length > description.length && text.length <= 40000) {
            description = text;
          }
        }
      } catch (_) {}
    }

    const titleNode = doc.querySelector(
      '[data-testid*="job-title" i],h1,[class*="jobTitle" i]'
    );
    if (!title && titleNode) {
      title = (titleNode.innerText || titleNode.textContent || '').trim();
    }

    return {title, description, url: fallbackUrl};
  };

  // Some candidate detail panels already contain the official description.
  try {
    const local = extractJobPosting(scopeRoot, location.href);
    if (local.description && local.description.length >= 100) {
      jobDescription = local.description;
      jobDescriptionSource = 'indeed_candidate_detail';
      jobUrl = local.url || location.href;
    }
  } catch (_) {}

  // Prefer the linked official job page. Fetch is same-origin/authenticated
  // and does not move the user's Candidates tab.
  if (!jobDescription) {
    const uniqueJobLinks = [];
    const seenJobLinks = new Set();
    for (const link of jobLinks) {
      if (!link.href || seenJobLinks.has(link.href)) continue;
      seenJobLinks.add(link.href);
      uniqueJobLinks.push(link);
      if (uniqueJobLinks.length >= 4) break;
    }

    for (const link of uniqueJobLinks) {
      try {
        const r = await fetch(link.href, {
          credentials: 'include',
          redirect: 'follow',
          cache: 'no-store'
        });
        if (!r.ok) continue;
        const raw = await r.text();
        const doc = new DOMParser().parseFromString(raw, 'text/html');
        const found = extractJobPosting(doc, r.url || link.href);
        if (found.title) addJob(found.title, 150);
        if (found.description && found.description.length >= 100) {
          jobDescription = found.description.slice(0, 40000);
          jobDescriptionSource = 'indeed_job_page';
          jobUrl = found.url || link.href;
          break;
        }
      } catch (_) {}
    }
  }

  return {
    url: location.href,
    title: document.title,
    body: expandedBody,
    headings,
    recoveredCandidateName,
    resumeText: resumeText.slice(0, 180000),
    resumeLinks,
    downloaded,
    downloadError,
    visibleEmails: [...emails].slice(0, 40),
    visiblePhones: [...phones].slice(0, 30),
    contactTexts: [
      resumeText.slice(0, 180000),
      ...(scopeIsCandidateSpecific
        ? [
            expandedBody.slice(0, 180000),
            htmlText.slice(0, 180000),
          ]
        : []),
    ],
    scopeIsCandidateSpecific,
    jobCandidates: jobCandidates.slice(0, 20).map(x => x.text),
    jobDescription: jobDescription.slice(0, 40000),
    jobDescriptionSource,
    jobUrl,
    expandedControls: [...clicked],
  };
})()
"""


def build_candidate_payload_js(entry):
    return (
        CANDIDATE_PAYLOAD_JS
        .replace(
            "__TARGET_CANDIDATE_NAME__",
            json.dumps(entry.get("label") or ""),
        )
        .replace(
            "__TARGET_ROW_JOB__",
            json.dumps(entry.get("job_title") or ""),
        )
    )


INVALID_ROLES = {
    "the position", "position", "job", "jobs", "the job",
    "day", "days", "today", "yesterday", "month", "months",
    "year", "years", "ago", "all", "new", "matches",
    "with", "in a", "role", "unknown", "candidates", "manage candidates"
}


def _is_invalid_job_title(val):
    if not val:
        return True
    low = val.lower().strip()
    if low in INVALID_ROLES:
        return True
    if re.fullmatch(r"(?:\d+\s+)?(?:day|days|month|months|year|years)\s+ago", low):
        return True
    if low.startswith(("with ", "in a ", "applied with", "applied to")):
        return True
    return False


def _job_title_from_context(context_text):
    """
    Read the job directly from the Indeed candidate-list row.
    """
    text = context_text or ""

    # Direct core role match in context
    low_text = text.lower()
    if "purchase executive" in low_text:
        return "Purchase Executive"
    if "marketing" in low_text and ("lead" in low_text or "coordination" in low_text or "executive" in low_text):
        return "Marketing & Lead Coordination Executive"
    if "driver" in low_text and "electrician" in low_text:
        return "Driver cum Electrician – Technical Support Assistant"

    patterns = [
        r"(?im)^\s*applied\s+to\s*:\s*(.{2,160})$",
        r"(?im)^\s*applied\s+for\s*:\s*(.{2,160})$",
        r"(?im)^\s*job\s*:\s*(.{2,160})$",
        r"(?im)^\s*position\s*:\s*(.{2,160})$",
    ]

    for pat in patterns:
        m = re.search(pat, text)
        if not m:
            continue

        value = re.sub(r"\s+", " ", m.group(1)).strip(" |•·–—-")
        if value and not _is_invalid_job_title(value):
            return value[:160]

    # Fallback for wrapped row text.
    compact = re.sub(r"\s+", " ", text)
    m = re.search(
        r"\bApplied\s+to\s*:\s*(.{2,160}?)(?=\s+(?:Activity|New\b|Reviewing\b|Contacting\b|Interviewing\b|Rejected\b|Hired\b|$))",
        compact,
        flags=re.I,
    )
    if m:
        value = re.sub(r"\s+", " ", m.group(1)).strip(" |•·–—-")
        if value and not _is_invalid_job_title(value):
            return value[:160]

    return None


def _job_title_from_body(body):
    text = body or ""

    low_text = text.lower()
    if "purchase executive" in low_text:
        return "Purchase Executive"
    if "marketing" in low_text and ("lead" in low_text or "coordination" in low_text or "executive" in low_text):
        return "Marketing & Lead Coordination Executive"
    if "driver" in low_text and "electrician" in low_text:
        return "Driver cum Electrician – Technical Support Assistant"

    patterns = [
        r"(?im)^\s*applied\s+to\s*:?\s*(.{2,160})$",
        r"(?im)^\s*applied\s+for\s*:?\s*(.{2,160})$",
        r"(?im)^\s*application\s+for\s*:?\s*(.{2,160})$",
        r"(?im)^\s*job\s*:?\s*(.{2,160})$",
        r"(?im)^\s*position\s*:?\s*(.{2,160})$",
    ]

    for pat in patterns:
        m = re.search(pat, text)
        if m:
            value = re.sub(r"\s+", " ", m.group(1)).strip(" |•·–—-")
            if value and not _is_invalid_job_title(value):
                return value[:160]

    lines = [
        re.sub(r"\s+", " ", x).strip()
        for x in text.splitlines()
        if x.strip()
    ]

    for i, line in enumerate(lines):
        if re.fullmatch(r"(?i)applied\s+to\s*:?", line) and i + 1 < len(lines):
            value = lines[i + 1].strip(" |•·–—-")
            if value and not _is_invalid_job_title(value):
                return value[:160]

    return "the position"




def _candidate_link_from_page(c, target):
    """
    Inspect one already-open Indeed page for a Candidates / Applicants link.
    Returns a URL or None. This does not create a new Chrome connection.
    """
    sid = None
    try:
        sid = c.attach(target["targetId"])
        value = c.evaluate(
            sid,
            """(() => {
              const anchors = [...document.querySelectorAll('a[href]')];
              const scored = anchors.map(a => {
                const href = a.href || '';
                const text = (a.innerText || a.textContent || '').trim();
                const h = href.toLowerCase();
                const t = text.toLowerCase();
                let score = 0;

                if (!h.includes('indeed.')) return null;
                if (h.includes('/login')) return null;

                if (h.includes('candidate')) score += 20;
                if (h.includes('applicant')) score += 20;
                if (h.includes('application')) score += 10;

                if (t === 'candidates' || t.includes('view candidates')) score += 25;
                if (t === 'applicants' || t.includes('view applicants')) score += 25;
                if (t.includes('candidate')) score += 10;
                if (t.includes('applicant')) score += 10;

                return {href, text, score};
              }).filter(Boolean).filter(x => x.score >= 20);

              scored.sort((a, b) => b.score - a.score);
              return scored[0] || null;
            })()""",
            await_promise=False,
            timeout=15,
        )
        if isinstance(value, dict):
            href = value.get("href") or ""
            if "indeed." in href.lower() and "/login" not in href.lower():
                return href
    except Exception:
        return None
    finally:
        if sid:
            c.detach(sid)

    return None


def indeed_candidate_permission_issue(url="", title="", body_preview=""):
    """
    Detect the exact Indeed chooser/permission screen shown when the signed-in
    Employer user does not have Manage candidates / Hosted_Candidate access.

    This is NOT treated as a Chrome failure. Jobs/roles can remain live while
    candidate processing waits for account permission.
    """
    low_url = str(url or "").lower()
    low_title = str(title or "").lower()
    low_body = str(body_preview or "").lower()

    permission = ""
    try:
        parsed = urllib.parse.urlparse(str(url or ""))
        query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
        values = []
        for key, items in query.items():
            if str(key or "").lower() in {"missingpermissions", "missingpermission", "permission"}:
                values.extend(items or [])
        for value in values:
            candidate = urllib.parse.unquote_plus(str(value or "")).strip()
            if candidate and candidate.lower() not in {"true", "false", "1", "0"}:
                permission = candidate
                break
    except Exception:
        permission = ""

    explicit_denial = any(
        token in low_body
        for token in (
            "you do not have permission to the page you tried to visit",
            "contact an employer account administrator to request access",
            "missing permission",
        )
    )

    chooser_denial = (
        "choose-product" in low_url
        and (
            "missingpermissions" in low_url
            or "hosted_candidate" in low_url
            or explicit_denial
        )
    )

    if explicit_denial or chooser_denial:
        return {
            "missing": True,
            "permission": permission or "Hosted_Candidate",
            "url": str(url or ""),
            "title": str(title or ""),
            "account_access": "MANAGE_CANDIDATES_REQUIRED",
        }

    return {
        "missing": False,
        "permission": "",
        "url": str(url or ""),
        "title": str(title or ""),
        "account_access": "UNKNOWN",
    }


def _raise_if_candidate_permission_missing(url="", title="", body_preview=""):
    issue = indeed_candidate_permission_issue(url, title, body_preview)
    if not issue.get("missing"):
        return issue

    permission = issue.get("permission") or "Hosted_Candidate"
    raise IndeedCandidatePermissionError(
        (
            "Indeed Employer is signed in, but this account does not have "
            "Manage candidates access for Nunes Instruments. "
            f"Missing permission: {permission}. Roles can continue from Manage Jobs, "
            "but candidate/resume monitoring and acknowledgement email must wait until "
            "an Employer administrator grants access or an authorized account is used."
        ),
        permission=permission,
        url=issue.get("url") or "",
    )


def navigate_to_candidates_from_open_indeed():
    """
    Check Manage candidates without ever replacing the persistent Jobs tab.

    A temporary probe tab is used when no real Candidates tab exists. Permission
    chooser/error tabs are closed immediately after the backend records the
    result.
    """
    c = get_shared_chrome()

    try:
        existing = detect_candidates_page()
        if existing:
            return existing
    except IndeedCandidatePermissionError:
        raise
    except Exception:
        pass

    saved = str(
        load_settings().get("indeed_candidates_url")
        or ""
    ).strip()

    target_url = (
        saved
        or "https://employers.indeed.com/candidates?tab=manage"
    )

    if "indeed." not in target_url.lower():
        target_url = (
            "https://employers.indeed.com/candidates?"
            "tab=manage"
        )

    result = c.command(
        "Target.createTarget",
        {"url": target_url},
        timeout=20,
    )
    target_id = result.get("targetId")

    if not target_id:
        raise ChromeConnectionError(
            "Chrome did not create the temporary Candidates check."
        )

    sid = None
    keep_target = False

    try:
        sid = c.attach(target_id)
        c.navigate(
            sid,
            target_url,
            timeout=45,
        )
        time.sleep(0.8)

        info = c.evaluate(
            sid,
            """(() => ({
              url: location.href,
              title: document.title,
              bodyPreview: (document.body?.innerText || '').slice(0, 16000)
            }))()""",
            await_promise=False,
            timeout=15,
        ) or {}

        _raise_if_candidate_permission_missing(
            info.get("url") or target_url,
            info.get("title") or "",
            info.get("bodyPreview") or "",
        )

        if _looks_like_candidates_page(
            info.get("url") or target_url,
            info.get("title") or "",
            info.get("bodyPreview") or "",
        ):
            keep_target = True
            return {
                "targetId": target_id,
                "url": info.get("url") or target_url,
                "title": info.get("title") or "Indeed Candidates",
            }

        raise ChromeConnectionError(
            "Indeed is signed in, but Manage candidates did not become available."
        )

    finally:
        if sid:
            try:
                c.detach(sid)
            except Exception:
                pass

        if not keep_target:
            try:
                c.command(
                    "Target.closeTarget",
                    {"targetId": target_id},
                    timeout=8,
                )
            except Exception:
                pass

        try:
            cleanup_recruitment_indeed_tabs(c)
        except Exception:
            pass

def ensure_candidates_page():
    """
    Detect the real Candidates page, or automatically navigate there from an
    already-open Employer dashboard page.
    """
    try:
        return detect_candidates_page()
    except Exception:
        return navigate_to_candidates_from_open_indeed()


def detect_candidates_page():
    """
    Search every Indeed page for the real Manage candidates workspace.

    Permission-denied chooser pages and Cloudflare challenge pages are detected
    explicitly so the system can wait/recover safely without producing errors.
    """
    c = get_shared_chrome()
    targets = c.targets()
    candidates = []
    permission_issues = []

    # Check for Cloudflare Turnstile challenge page first
    for target in targets:
        u = (target.get("url") or "").lower()
        t = (target.get("title") or "").lower()
        if "indeed." in u and ("just a moment..." in t or "checking your browser" in t):
            raise IndeedChallengeError(
                "Indeed is displaying a Cloudflare security verification page ('Just a moment...'). "
                "Please solve the verification challenge in the Recruitment Chrome browser."
            )

    for target in targets:
        url = target.get("url") or ""
        if "indeed." not in url.lower():
            continue
        if "/login" in url.lower():
            continue

        sid = None
        try:
            sid = c.attach(target["targetId"])
            data = c.evaluate(
                sid,
                """(() => ({
                  url: location.href,
                  title: document.title,
                  bodyPreview: (document.body?.innerText || '').slice(0, 12000)
                }))()""",
                await_promise=False,
                timeout=12,
            ) or {}

            issue = indeed_candidate_permission_issue(
                data.get("url") or url,
                data.get("title") or target.get("title") or "",
                data.get("bodyPreview") or "",
            )
            if issue.get("missing"):
                permission_issues.append(issue)
                continue

            if _looks_like_candidates_page(
                data.get("url") or url,
                data.get("title") or target.get("title") or "",
                data.get("bodyPreview") or "",
            ):
                candidates.append({
                    "targetId": target["targetId"],
                    "url": data.get("url") or url,
                    "title": data.get("title") or target.get("title") or "",
                })
        except Exception:
            pass
        finally:
            if sid:
                c.detach(sid)

    if candidates:
        candidates.sort(
            key=lambda x: (
                0 if any(
                    k in (x.get("url") or "").lower()
                    for k in ["candidate", "applicant", "application"]
                ) else 1,
                len(x.get("url") or ""),
            )
        )
        return candidates[0]

    if permission_issues:
        issue = permission_issues[0]
        permission = issue.get("permission") or "Hosted_Candidate"
        raise IndeedCandidatePermissionError(
            (
                "Indeed Employer is connected, but this signed-in account does not "
                "have Manage candidates access. "
                f"Missing permission: {permission}."
            ),
            permission=permission,
            url=issue.get("url") or "",
        )

    raise ChromeConnectionError(
        "Chrome is connected, but the Indeed Employer Candidates page is not open yet."
    )


def _looks_like_candidates_page(url, title, body_preview=""):
    low_url = (url or "").lower()
    low_title = (title or "").lower()
    low_body = (body_preview or "").lower()

    if "/login" in low_url:
        return False

    if "just a moment..." in low_title or "turnstile" in low_body or "checking if the site connection is secure" in low_body:
        return False

    # CRITICAL: Hosted_Candidate in a permission URL contains the word
    # "candidate". Older detection treated this chooser page as a real
    # Candidates page. Reject it before normal candidate URL heuristics.
    if indeed_candidate_permission_issue(url, title, body_preview).get("missing"):
        return False

    if "choose-product" in low_url and "manage candidates" in low_body:
        return False

    strong_url = any(
        k in low_url
        for k in ["/candidate", "/applicant", "/application"]
    )
    strong_title = any(
        k in low_title
        for k in ["candidate", "applicant"]
    )
    body_signal = (
        "candidates" in low_body
        and any(
            k in low_body
            for k in [
                "awaiting review",
                "reviewed",
                "contacting",
                "application",
                "applicant",
            ]
        )
        and "you do not have permission" not in low_body
    )

    return strong_url or strong_title or body_signal


def _review_retry_due(existing):
    """
    Keep checking unresolved candidate details forever, without a user-facing
    timer setting.

    Adaptive internal backoff:
      early failures -> faster retries
      repeated no-resume/no-contact -> progressively calmer retries
    """
    if not existing:
        return True

    attempts = int(existing.get("extraction_attempts") or 0)

    # Internal only. This is deliberately not exposed in Settings.
    if attempts <= 1:
        delay_seconds = 20
    elif attempts <= 3:
        delay_seconds = 45
    elif attempts <= 6:
        delay_seconds = 90
    else:
        delay_seconds = 180

    # Retry age must be based on the last DETAIL/extraction attempt.
    # last_seen_at is only visibility evidence and is refreshed during list scans;
    # using it here can prevent unresolved candidates from ever becoming due.
    stamp = (
        existing.get("application_checked_at")
        or existing.get("updated_at")
        or existing.get("first_seen_at")
    )

    if not stamp:
        return True

    try:
        previous = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
        if previous.tzinfo is None:
            previous = previous.replace(tzinfo=timezone.utc)

        elapsed = (
            datetime.now(timezone.utc) - previous.astimezone(timezone.utc)
        ).total_seconds()

        return elapsed >= delay_seconds
    except Exception:
        return True


ALL_CANDIDATE_BACKFILL_BATCH = 1000000  # ALL active candidates; no New-only/backfill cap
# Keep each role sticky until its current backlog is exhausted.  Batching avoids
# one large role blocking the live loop for hours while still guaranteeing that
# every active applicant is revisited until processed.
ALL_CANDIDATE_DETAIL_BATCH = 25
FAST_CANDIDATE_DETAIL_BATCH = 12
TERMINAL_CANDIDATE_STATUSES = {
    "hired", "selected", "not selected", "rejected", "withdrawn", "archived"
}


def _entry_terminal(entry):
    status = re.sub(r"\s+", " ", str((entry or {}).get("indeed_status") or "")).strip().lower()
    return status in TERMINAL_CANDIDATE_STATUSES


def select_entries_for_processing(entries, settings, fast_only=False):
    """Select candidates from the ALL-applicants view.

    NUNES ALL-candidates policy:
      - The scanner uses the unfiltered Indeed All view.
      - New/unseen applicants remain first priority.
      - Every active applicant that has never been processed is eligible.
      - Terminal candidates are not emailed/ranked and are marked processed.
      - Unresolved candidates continue to retry with adaptive backoff.
    """
    all_backfill_pending = bool(
        settings.get("process_all_active_candidates_once", True)
        and not all_candidates_backfill_completed()
    )

    selected = []

    # First register every visible candidate. This makes the old New-only ledger
    # automatically expand to the complete All-applicants population.
    for entry in entries:
        if not is_seen_candidate(entry["source_key"]):
            remember_seen_candidate(
                entry["source_key"],
                entry.get("url"),
                entry.get("label"),
                initial_new=bool(entry.get("current_new")),
            )

        if _entry_terminal(entry):
            # We deliberately do not send/rank completed/rejected/withdrawn rows.
            mark_seen_processed(entry["source_key"])

    # Live pass: process ALL active applicants from the All-candidates view.
    # New candidates are still naturally included, but older active applicants are
    # no longer excluded just because Indeed does not mark them as "New".
    if fast_only:
        for entry in entries:
            if _entry_terminal(entry):
                continue

            existing = get_by_source_key(entry["source_key"])
            ledger = seen_candidate(entry["source_key"]) or {}

            # Any active applicant that has never been detail-processed must run.
            if int(ledger.get("processed") or 0) == 0:
                selected.append(entry)
                continue

            # Retry unresolved extraction for any active applicant, not only New.
            if existing:
                status = existing.get("extraction_status") or ""
                sent_status = (existing.get("send_status") or "").upper()

                if (
                    status.startswith("NEEDS_REVIEW_")
                    and sent_status != "SENT"
                    and _review_retry_due(existing)
                ):
                    selected.append(entry)

        return selected, all_backfill_pending

    # Full pass: all current New first, then a controlled slice of older active
    # candidates that have never been detail-processed.
    unprocessed_new = []
    unprocessed_old = []

    for entry in entries:
        if _entry_terminal(entry):
            continue
        ledger = seen_candidate(entry["source_key"]) or {}
        if int(ledger.get("processed") or 0) == 0:
            if bool(entry.get("current_new")):
                unprocessed_new.append(entry)
            else:
                unprocessed_old.append(entry)

    # Process the complete active population in the All view.
    # Keep New first for responsiveness, then process every older unprocessed row.
    selected.extend(unprocessed_new)
    selected.extend(unprocessed_old)

    selected_keys = {x.get("source_key") for x in selected}

    # Retry unresolved detail extraction on full reconciliation.
    for entry in entries:
        if entry.get("source_key") in selected_keys or _entry_terminal(entry):
            continue
        existing = get_by_source_key(entry["source_key"])
        if not existing:
            continue
        status = existing.get("extraction_status") or ""
        sent_status = (existing.get("send_status") or "").upper()
        existing_job = re.sub(
            r"\s+", " ", str(existing.get("job_title") or "")
        ).strip().lower()

        if (
            sent_status == "SENT"
            and existing_job in {
                "", "the position", "position", "job", "the job", "unknown"
            }
        ):
            selected.append(entry)
            selected_keys.add(entry.get("source_key"))
            continue

        if (
            status.startswith("NEEDS_REVIEW_")
            and sent_status != "SENT"
            and _review_retry_due(existing)
        ):
            selected.append(entry)
            selected_keys.add(entry.get("source_key"))

    return selected, all_backfill_pending
def scan_existing_chrome(fast_only=False):
    settings = load_settings()
    c = get_shared_chrome()
    sid = None

    try:
        candidate_queues = []
        try:
            candidate_queues = json.loads(
                get_state("indeed_job_candidate_queues", "[]") or "[]"
            )
        except Exception:
            candidate_queues = []

        candidate_queues = [
            item
            for item in candidate_queues
            if isinstance(item, dict)
            and "indeed." in str(item.get("url") or "").lower()
            and any(
                token in str(item.get("url") or "").lower()
                for token in ("/candidate", "/applicant", "/application")
            )
            and str(item.get("job_status") or "OPEN").upper()
                in {"OPEN", "PAUSED", "FLAGGED", "UNKNOWN"}
        ]

        candidate_queue_role = None
        candidate_queue_url = ""
        candidate_queue_cursor = 0
        candidate_queue_index = 0
        if candidate_queues:
            try:
                candidate_queue_cursor = int(
                    get_state("indeed_job_candidate_queue_cursor", "0") or 0
                )
            except Exception:
                candidate_queue_cursor = 0

            # IMPORTANT: do not advance the role cursor here.  The old behaviour
            # rotated to the next role on every scan, so a role with hundreds of
            # applicants could take an extremely long time to finish.  We now
            # stay on the same role until a complete scan finds no eligible work.
            candidate_queue_index = candidate_queue_cursor % len(candidate_queues)
            selected_queue = candidate_queues[candidate_queue_index]
            candidate_queue_role = str(selected_queue.get("job_title") or "").strip()
            candidate_queue_url = str(selected_queue.get("url") or "").strip()

            set_state("indeed_backlog_current_role", candidate_queue_role)
            set_state("indeed_backlog_current_role_index", str(candidate_queue_index))
            set_state("indeed_backlog_current_role_url", candidate_queue_url)

        saved = all_candidates_queue_url(
            candidate_queue_url
            or (settings.get("indeed_candidates_url") or "").strip()
        )

        target = None

        # Prefer the saved Candidates page if its target is still open.
        if saved:
            try:
                detected = detect_candidates_page()
                if urldefrag(detected.get("url") or "")[0] == urldefrag(saved)[0]:
                    target = detected
            except Exception:
                pass

        if target is None:
            target = detect_candidates_page()

        target_id = target["targetId"]
        sid = c.attach(target_id)

        if saved and urldefrag(target.get("url") or "")[0] != urldefrag(saved)[0]:
            if "/login" not in saved.lower():
                c.navigate(sid, saved, timeout=45)

        try:
            c.evaluate(
                sid,
                WAIT_CANDIDATES_READY_JS,
                await_promise=True,
                timeout=12,
            )
        except Exception:
            pass

        collector_name = "fast-visible" if fast_only else "full"
        try:
            link_data = (
                c.evaluate(
                    sid,
                    (
                        COLLECT_VISIBLE_LINKS_JS
                        if fast_only
                        else COLLECT_LINKS_JS
                    ),
                    await_promise=True,
                    timeout=45,
                )
                or {}
            )
        except Exception as primary_error:
            # A fast DOM collector must never stop live monitoring. If Indeed
            # changed one fast-page selector/runtime path, retry once using the
            # independent full collector before declaring the scan failed.
            if not fast_only:
                raise ChromeConnectionError(
                    f"JavaScript error in Indeed page ({collector_name} collector): {primary_error}"
                )

            try:
                link_data = (
                    c.evaluate(
                        sid,
                        COLLECT_LINKS_JS,
                        await_promise=True,
                        timeout=45,
                    )
                    or {}
                )
                collector_name = "full-fallback"
            except Exception as fallback_error:
                raise ChromeConnectionError(
                    "JavaScript error in Indeed page: "
                    f"fast collector failed ({primary_error}); "
                    f"full fallback failed ({fallback_error})"
                )

        list_url = link_data.get("url") or target.get("url") or ""
        title = link_data.get("title") or ""
        body_preview = link_data.get("bodyPreview") or ""

        _raise_if_candidate_permission_missing(
            list_url,
            title,
            body_preview,
        )

        if not _looks_like_candidates_page(
            list_url,
            title,
            body_preview,
        ):
            raise ChromeConnectionError(
                "The connected Indeed page is not the Employer Candidates page."
            )

        scored = []
        seen_keys = set()

        for x in link_data.get("links", []):
            record_type = (x.get("recordType") or "link").lower()
            href = urldefrag(x.get("href") or "")[0]
            text = (
                x.get("candidateName")
                or x.get("text")
                or ""
            ).strip()
            context_text = x.get("contextText") or ""
            data_test_id = x.get("dataTestId") or ""
            row_job = (
                x.get("jobTitle")
                or _job_title_from_context(context_text)
            )
            location_text = x.get("locationText") or ""
            identity_hint = x.get("identityHint") or ""

            invalid_ui_names = {
                "all open and paused jobs",
                "all jobs",
                "jobs",
                "education",
                "yes",
                "no",
                "candidates",
                "candidate",
                "applicants",
                "manage candidates",
                "find candidates",
                "download cv",
                "download resume",
                "core skills",
                "resume",
                "contact information",
            }

            if text.strip().lower() in invalid_ui_names:
                continue

            if record_type == "row":
                if not text:
                    continue

                source_key = stable_row_candidate_key(
                    text,
                    row_job,
                    location_text,
                    identity_hint,
                )
                score = 100
            else:
                score = candidate_link_score(
                    href,
                    text,
                    context_text,
                    data_test_id,
                )

                if score < 10 or not href:
                    continue

                source_key = stable_candidate_key(href)

            if source_key in seen_keys:
                continue

            seen_keys.add(source_key)

            scored.append({
                "record_type": record_type,
                "url": href,
                "label": text[:120],
                "location_text": location_text[:120],
                "identity_hint": identity_hint[:2000],
                "context_text": context_text[:2000],
                "job_title": row_job,
                "score": score,
                "source_key": source_key,
                "new_status_hint": (
                    bool(x.get("currentNew"))
                    or is_new_status_hint(context_text)
                ),
                "current_new": (
                    bool(x.get("currentNew"))
                    or is_current_new_candidate(context_text)
                ),
                "indeed_status": candidate_pipeline_status(
                    context_text,
                    x.get("pipelineStatus"),
                ),
            })

        scored.sort(key=lambda x: x["score"], reverse=True)

        scan_seen_at = datetime.now(timezone.utc).isoformat()

        # Update live presence for every existing applicant seen in this scan,
        # even when the resume/email does not need reprocessing.
        try:
            touch_visible_applications(
                [entry.get("source_key") for entry in scored],
                seen_at=scan_seen_at,
            )
        except Exception:
            pass

        set_state(
            "last_scan_at",
            __import__("datetime").datetime.now(
                __import__("datetime").timezone.utc
            ).isoformat(),
        )
        set_state("last_visible_count", str(len(scored)))

        entries_to_process, catch_up_pending = (
            select_entries_for_processing(
                scored,
                settings,
                fast_only=fast_only,
            )
        )

        # Process the current role in bounded chunks.  This keeps the UI/live
        # monitor responsive and lets restart/resume continue from the persistent
        # seen-candidate ledger instead of redoing already completed candidates.
        total_eligible_before_batch = len(entries_to_process)
        detail_batch_limit = (
            FAST_CANDIDATE_DETAIL_BATCH
            if fast_only
            else ALL_CANDIDATE_DETAIL_BATCH
        )
        entries_to_process = entries_to_process[:detail_batch_limit]

        set_state("indeed_backlog_visible_count", str(len(scored)))
        set_state("indeed_backlog_eligible_count", str(total_eligible_before_batch))
        set_state("indeed_backlog_batch_size", str(len(entries_to_process)))
        set_state("indeed_backlog_batch_limit", str(detail_batch_limit))
        set_state("indeed_backlog_last_scan_role", candidate_queue_role or "")

        # Advance only after a FULL collection of this role says there is no
        # remaining eligible applicant.  A later scan then moves to the next role.
        collection_complete_now = bool(link_data.get("collectionComplete"))
        role_complete_now = bool(
            candidate_queues
            and not fast_only
            and collection_complete_now
            and total_eligible_before_batch == 0
        )
        if role_complete_now:
            next_cursor = candidate_queue_cursor + 1
            set_state("indeed_job_candidate_queue_cursor", str(next_cursor))
            set_state("indeed_backlog_last_completed_role", candidate_queue_role or "")
            set_state("indeed_backlog_last_completed_at", datetime.now(timezone.utc).isoformat())
            set_state("indeed_backlog_role_complete", "1")
        else:
            set_state("indeed_backlog_role_complete", "0")

        results = []

        for entry in entries_to_process:
            url = entry.get("url") or ""
            label = entry["label"]
            source_key = entry["source_key"]
            existing_before = get_by_source_key(source_key)

            try:
                # Use a real candidate URL when Indeed exposes one.
                if url:
                    c.navigate(sid, url, timeout=45)
                else:
                    # Live Manage Candidates often uses a JS-clickable row/card
                    # with no applicant URL. Click that exact row in the user's
                    # already-open Chrome session.
                    click_result = (
                        c.evaluate(
                            sid,
                            build_row_click_js(entry),
                            await_promise=True,
                            timeout=35,
                            user_gesture=True,
                        )
                        or {}
                    )

                    if not click_result.get("clicked"):
                        raise ChromeConnectionError(
                            "Candidate row could not be opened: "
                            + str(click_result.get("reason") or "unknown row click error")
                        )

                    time.sleep(0.8)


                # Wait for the exact selected candidate detail/drawer before
                # reading resume/contact data.
                try:
                    detail_wait_js = f"""(async () => {{
                      const target = {json.dumps(label)};
                      const low = (target || '').toLowerCase();
                      const sleep = ms => new Promise(r => setTimeout(r, ms));
                      const started = Date.now();

                      while (Date.now() - started < 7000) {{
                        const body = document.body?.innerText || '';
                        const dialogs = [
                          ...document.querySelectorAll(
                            '[role="dialog"],aside,'
                            + '[data-testid*="candidate-detail" i],'
                            + '[data-testid*="candidate-panel" i],'
                            + '[data-testid*="application-detail" i],'
                            + '[class*="candidateDetail" i],'
                            + '[class*="drawer" i]'
                          )
                        ];

                        const matched = dialogs.some(el => {{
                          const text = (el.innerText || '').toLowerCase();
                          return !low || text.includes(low);
                        }});

                        if (
                          matched
                          || (
                            body.toLowerCase().includes(low)
                            && /(resume|contact|applied\\s+to|application)/i.test(body)
                          )
                        ) {{
                          return true;
                        }}

                        await sleep(200);
                      }}

                      return false;
                    }})()"""

                    c.evaluate(
                        sid,
                        detail_wait_js,
                        await_promise=True,
                        timeout=10,
                    )
                except Exception:
                    pass

                payload = (
                    c.evaluate(
                        sid,
                        build_candidate_payload_js(entry),
                        await_promise=True,
                        timeout=100,
                        user_gesture=True,
                    )
                    or {}
                )

                resume_path = existing_before.get("resume_path") if existing_before else None
                resume_download = payload.get("downloaded")
                resume_text = payload.get("resumeText") or ""
                resume_found = bool(resume_download or resume_path or resume_text)

                headings = payload.get("headings") or []
                recovered_from_drawer = _usable_candidate_name(payload.get("recoveredCandidateName"))

                candidate_name = _usable_candidate_name(label)
                if not candidate_name or candidate_name.lower() in GENERIC_CANDIDATE_LABELS:
                    if recovered_from_drawer:
                        candidate_name = recovered_from_drawer
                    else:
                        for heading in headings:
                            candidate_name = _usable_candidate_name(heading)
                            if candidate_name and candidate_name.lower() not in GENERIC_CANDIDATE_LABELS:
                                break

                if not candidate_name:
                    candidate_name = "Candidate"

                body = payload.get("body") or ""

                detail_job = _job_title_from_body(body)
                row_job = entry.get("job_title") or candidate_queue_role

                browser_jobs = [
                    re.sub(r"\s+", " ", str(x or "")).strip()
                    for x in (payload.get("jobCandidates") or [])
                ]
                browser_job = next(
                    (
                        x for x in browser_jobs
                        if x
                        and not _is_invalid_job_title(x)
                    ),
                    None,
                )

                invalid_jobs = {
                    "the position", "position", "job", "jobs",
                    "candidates", "manage candidates", "day", "days",
                    "with", "in a", "role", "unknown"
                }

                clean_row_job = row_job if row_job and row_job.lower() not in invalid_jobs else None
                clean_browser_job = browser_job if browser_job and browser_job.lower() not in invalid_jobs else None
                clean_detail_job = detail_job if detail_job and detail_job.lower() not in invalid_jobs else None

                final_job = clean_row_job or clean_browser_job or clean_detail_job or "the position"

                results.append({
                    "source_key": source_key,
                    "profile_url": payload.get("url") or url or list_url,
                    "candidate_name": candidate_name,
                    "job_title": final_job,
                    "job_description": payload.get("jobDescription") or "",
                    "job_description_source": payload.get("jobDescriptionSource") or "",
                    "job_url": payload.get("jobUrl") or "",
                    "resume_path": resume_path,
                    "resume_text": resume_text,
                    "resume_download": resume_download,
                    "resume_found": resume_found,
                    "resume_error": payload.get("downloadError"),
                    "candidate_page_text": body[:90000],
                    "profile_emails": (
                        payload.get("visibleEmails") or []
                        if payload.get("scopeIsCandidateSpecific")
                        else []
                    ),
                    "profile_phones": (
                        payload.get("visiblePhones") or []
                        if payload.get("scopeIsCandidateSpecific")
                        else []
                    ),
                    "contact_texts": payload.get("contactTexts") or [],
                    "scope_is_candidate_specific": bool(
                        payload.get("scopeIsCandidateSpecific")
                    ),
                    "indeed_status": entry.get("indeed_status") or "",
                    "current_new": bool(entry.get("current_new")),
                    "new_status_hint": bool(entry.get("new_status_hint")),
                    "new_applicant": existing_before is None,
                })

                if entry.get("record_type") == "row":
                    try:
                        c.navigate(
                            sid,
                            all_candidates_queue_url(list_url),
                            timeout=45,
                        )
                        try:
                            c.evaluate(
                                sid,
                                WAIT_CANDIDATES_READY_JS,
                                await_promise=True,
                                timeout=12,
                            )
                        except Exception:
                            pass
                    except Exception:
                        pass

            except Exception as e:
                results.append({
                    "source_key": source_key,
                    "profile_url": url or list_url,
                    "candidate_name": (
                        _usable_candidate_name(label)
                        or "Candidate"
                    ),
                    "job_title": entry.get("job_title") or "the position",
                    "resume_path": None,
                    "resume_text": "",
                    "resume_found": False,
                    "resume_error": f"Candidate page failed: {e}",
                    "candidate_page_text": "",
                    "indeed_status": entry.get("indeed_status") or "",
                    "current_new": bool(entry.get("current_new")),
                    "new_status_hint": bool(entry.get("new_status_hint")),
                    "new_applicant": existing_before is None,
                })

        try:
            c.navigate(
                sid,
                all_candidates_queue_url(list_url),
                timeout=45,
            )
        except Exception:
            pass

        truly_new = sum(
            1 for r in results if r.get("new_applicant")
        )
        set_state("last_new_count", str(truly_new))

        return {
            "current_url": all_candidates_queue_url(list_url),
            "candidate_queue_role": candidate_queue_role,
            "candidate_queue_index": candidate_queue_index,
            "candidate_queue_count": len(candidate_queues),
            "eligible_before_batch": total_eligible_before_batch,
            "detail_batch_limit": detail_batch_limit,
            "detail_batch_size": len(entries_to_process),
            "role_complete": role_complete_now,
            "collector": collector_name,
            "found_links": len(scored),
            "collection_complete": (
                False
                if fast_only
                else bool(link_data.get("collectionComplete"))
            ),
            "collected_link_count": int(link_data.get("collectedLinkCount") or 0),
            "collection_diagnostics": {
                "page_title": title,
                "body_chars": len(body_preview),
                "has_applied_to_label": bool(
                    re.search(r"\bapplied\s+to\b", body_preview, re.I)
                ),
                "has_empty_state": bool(
                    re.search(
                        r"no (?:applicants|candidates|applications)",
                        body_preview,
                        re.I,
                    )
                ),
                "has_pagination_text": bool(
                    re.search(
                        r"\b(?:page\s+\d+|\d+\s*[-–]\s*\d+\s+(?:of|to)\s+\d+)\b",
                        body_preview,
                        re.I,
                    )
                ),
                "row_records": sum(
                    1
                    for item in link_data.get("links", [])
                    if (item.get("recordType") or "").lower() == "row"
                ),
                "link_records": sum(
                    1
                    for item in link_data.get("links", [])
                    if (item.get("recordType") or "link").lower() == "link"
                ),
                "scroll_container_found": bool(
                    link_data.get("scrollContainerFound")
                ),
                "scroll_passes": int(link_data.get("scrollPasses") or 0),
            },
            "new_candidates": truly_new,
            "processed_candidates": len(results),
            "catch_up_pending": catch_up_pending,
            "all_backfill_pending": catch_up_pending,
            "pending_all_backfill": pending_seen_unprocessed_count(),
            "results": results,
            "fast_only": bool(fast_only),
            "message": "OK",
        }

    finally:
        if sid:
            c.detach(sid)
