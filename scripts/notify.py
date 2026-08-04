"""Windows toast notification utility.

Sends a Windows 11 toast notification. No-ops silently on non-Windows
platforms or when win11toast is not installed.

Called by scripts/notify-wrap.py — never imported by diagnostic scripts
directly. Notifications are a task-wrapper concern, not a script concern.
"""

import sys


def notify(title: str, message: str) -> None:
    if sys.platform != "win32":
        return
    try:
        from win11toast import toast

        toast(title, message)
    except Exception:  # noqa: S110 - a toast must never break the wrapped task
        pass


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        notify(sys.argv[1], sys.argv[2])
