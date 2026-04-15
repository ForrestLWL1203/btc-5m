"""macOS notification wrapper."""

import subprocess
import logging

log = logging.getLogger(__name__)


def notify(title: str, body: str) -> None:
    """Send a macOS notification via osascript."""
    script = f'display notification "{_escape(body)}" with title "{_escape(title)}"'
    try:
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
    except Exception as e:
        log.warning("Failed to send notification: %s", e)
    log.info("[%s] %s", title, body)


def _escape(s: str) -> str:
    """Basic escaping for osascript strings."""
    return s.replace("\\", "\\\\").replace('"', '\\"')
