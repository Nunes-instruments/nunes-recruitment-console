from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from config import STATE_DIR

BASE_DIR = Path(__file__).resolve().parent
TASK_NAME = "Nunes Recruitment Console"
BACKGROUND_CMD = STATE_DIR / "START_BACKGROUND.cmd"
BACKGROUND_VBS = STATE_DIR / "START_BACKGROUND.vbs"


def runtime_json():
    return (
        STATE_DIR
        / "FastRuntime"
        / "runtime.json"
    )


def runtime_system_python():
    try:
        value = json.loads(
            runtime_json().read_text(
                encoding="utf-8"
            )
        )
        p = Path(
            value.get("system_python") or ""
        )

        if p.exists():
            return p
    except Exception:
        pass

    return Path(sys.executable)



def runtime_background_python():
    exe = runtime_system_python()

    if os.name == "nt":
        pythonw = exe.parent / "pythonw.exe"
        if pythonw.exists():
            return pythonw

    return exe


def write_background_vbs():
    """
    Windows Task Scheduler starts WScript, not a CMD file.

    This prevents the periodic/minimized terminal window that could briefly
    appear during login or restart.
    """
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    launcher = BASE_DIR / "launcher.py"
    exe = runtime_background_python()

    command = (
        '"'
        + str(exe)
        + '" "'
        + str(launcher)
        + '" --background'
    )

    escaped = command.replace('"', '""')

    text = (
        'Set shell = CreateObject("WScript.Shell")\\r\\n'
        + f'shell.Run "{escaped}", 0, False\\r\\n'
    )

    BACKGROUND_VBS.write_text(
        text,
        encoding="utf-8",
    )

    return BACKGROUND_VBS


def write_background_cmd():
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    launcher = BASE_DIR / "launcher.py"
    exe = runtime_system_python()

    text = (
        "@echo off\r\n"
        f'cd /d "{BASE_DIR}"\r\n'
        f'start "" /min "{exe}" "{launcher}" --background\r\n'
        "exit /b 0\r\n"
    )

    BACKGROUND_CMD.write_text(
        text,
        encoding="utf-8",
    )

    return BACKGROUND_CMD


def install():
    write_background_cmd()  # legacy fallback retained
    vbs = write_background_vbs()

    if os.name != "nt":
        return (
            True,
            "Windows auto-start is only applicable on Windows.",
        )

    try:
        proc = subprocess.run(
            [
                "schtasks",
                "/Create",
                "/TN",
                TASK_NAME,
                "/SC",
                "ONLOGON",
                "/DELAY",
                "0000:05",
                "/TR",
                f'wscript.exe "{vbs}"',
                "/RL",
                "LIMITED",
                "/F",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

        if proc.returncode == 0:
            return (
                True,
                "Windows auto-start registered.",
            )

        return (
            False,
            (
                proc.stderr
                or proc.stdout
                or "Task Scheduler registration failed."
            ).strip(),
        )

    except Exception as exc:
        return False, str(exc)


def remove():
    if os.name != "nt":
        return True, "Nothing to remove."

    try:
        proc = subprocess.run(
            [
                "schtasks",
                "/Delete",
                "/TN",
                TASK_NAME,
                "/F",
            ],
            capture_output=True,
            text=True,
            timeout=20,
        )

        return (
            proc.returncode == 0,
            (proc.stdout or proc.stderr).strip(),
        )

    except Exception as exc:
        return False, str(exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--install",
        action="store_true",
    )
    parser.add_argument(
        "--remove",
        action="store_true",
    )
    args = parser.parse_args()

    if args.remove:
        ok, message = remove()
    else:
        ok, message = install()

    print(message)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
