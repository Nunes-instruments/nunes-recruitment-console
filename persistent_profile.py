from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from threading import RLock

from config import STATE_DIR, load_settings

PROFILE_FILE = STATE_DIR / "persistent_profile.json"
PROFILE_BACKUP = STATE_DIR / "persistent_profile.json.bak"
PROFILE_SCHEMA_VERSION = 1
GITHUB_REPOSITORY = "https://github.com/Nunes-instruments/indeed_auomation.git"

_lock = RLock()


def _stamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_write(payload):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    temp = PROFILE_FILE.with_suffix(".json.tmp")

    if PROFILE_FILE.exists():
        try:
            shutil.copy2(PROFILE_FILE, PROFILE_BACKUP)
        except Exception:
            pass

    temp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temp, PROFILE_FILE)


def _load_existing():
    try:
        data = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def sync_persistent_profile(settings=None):
    """
    Store durable connection/preferences metadata only.

    Credentials are deliberately not copied into this file:
    - Gmail app passwords remain in the stable Windows-user settings store.
    - Indeed login remains in the persistent Recruitment Chrome profile.
    - GitHub authentication remains in Windows Git Credential Manager.

    This file records what the product should REUSE after restart/upgrade.
    """
    with _lock:
        s = settings or load_settings()
        previous = _load_existing()

        payload = {
            "schema_version": PROFILE_SCHEMA_VERSION,
            "saved_at": _stamp(),
            "storage_scope": "windows_user",
            "connection_policy": "reuse_saved_until_user_changes",
            "connections": {
                "candidate_gmail": {
                    "account": "nuneslead@gmail.com",
                    "saved": bool((s.get("smtp_app_password") or "").strip()),
                    "reuse": True,
                },
                "hr_report_gmail": {
                    "account": "nunescbe@gmail.com",
                    "saved": bool((s.get("hr_report_smtp_app_password") or "").strip()),
                    "reuse": True,
                },
                "indeed": {
                    "expected_google_account": str(
                        s.get("indeed_google_account_email")
                        or "nuneslead@gmail.com"
                    ).strip().lower(),
                    "saved_workspace_url": str(s.get("indeed_candidates_url") or ""),
                    "reuse_chrome_profile": True,
                    "auto_reconnect": True,
                    "password_stored_by_app": False,
                },
                "github": {
                    "repository": GITHUB_REPOSITORY,
                    "branch": "main",
                    "reuse_windows_git_credentials": True,
                },
            },
            "preferences": {
                "company_name": str(s.get("company_name") or ""),
                "daily_report_time": str(s.get("daily_report_time") or "19:00"),
                "daily_report_enabled": bool(s.get("daily_consolidated_report_enabled", True)),
                "ranking_weights": s.get("ranking_weights") or {},
                "ranking_thresholds": {
                    "strong": float(s.get("ranking_strong_threshold", 85.0)),
                    "good": float(s.get("ranking_good_threshold", 70.0)),
                    "moderate": float(s.get("ranking_moderate_threshold", 50.0)),
                },
                "ranking_embedding_model": str(s.get("ranking_embedding_model") or "BAAI/bge-small-en-v1.5"),
            },
        }

        # Preserve future metadata written by a newer build when possible.
        if isinstance(previous.get("custom"), dict):
            payload["custom"] = previous["custom"]

        _atomic_write(payload)
        return payload


def persistence_status(settings=None):
    s = settings or load_settings()

    with _lock:
        current = _load_existing()

        # First run or old version upgrade: create the profile automatically.
        if not current:
            current = sync_persistent_profile(
                settings=s,
            )

    connections = current.get("connections") or {}

    return {
        "saved_at": current.get("saved_at"),
        "policy": current.get("connection_policy") or "reuse_saved_until_user_changes",
        "scope": current.get("storage_scope") or "windows_user",
        "candidate_gmail_saved": bool(
            (connections.get("candidate_gmail") or {}).get("saved")
            or (s.get("smtp_app_password") or "").strip()
        ),
        "hr_gmail_saved": bool(
            (connections.get("hr_report_gmail") or {}).get("saved")
            or (s.get("hr_report_smtp_app_password") or "").strip()
        ),
        "indeed_reuses_chrome": True,
        "indeed_expected_google_account": str(
            (
                (connections.get("indeed") or {}).get("expected_google_account")
                or s.get("indeed_google_account_email")
                or "nuneslead@gmail.com"
            )
        ).strip().lower(),
        "github_repository": (
            (connections.get("github") or {}).get("repository")
            or GITHUB_REPOSITORY
        ),
        "github_branch": (
            (connections.get("github") or {}).get("branch")
            or "main"
        ),
        "message": (
            "Saved connections and preferences are reused after restart and "
            "software updates. They change only when you explicitly save or replace them."
        ),
    }
