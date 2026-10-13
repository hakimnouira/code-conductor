"""Optional Windows desktop notifications for durable scheduled workflow outcomes."""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)


def notify_schedule_outcome(name: str, status: str, error: str = "") -> bool:
    """Show a Windows toast only when explicitly enabled; never fail a workflow on toast errors."""
    if os.environ.get("CODE_CONDUCTOR_DESKTOP_NOTIFICATIONS", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    if sys.platform != "win32":
        return False

    normalized = status.strip().lower()
    title = "Code Conductor · Workflow " + ("completed" if normalized == "completed" else "needs attention")
    message = name[:180] if normalized == "completed" else f"{name[:120]}: {(error or status)[:160]}"
    try:
        from winotify import Notification

        Notification(
            app_id="Code Conductor",
            title=title,
            msg=message,
            duration="short",
        ).show()
        return True
    except Exception:
        logger.exception("Could not show Windows scheduled-workflow notification")
        return False
