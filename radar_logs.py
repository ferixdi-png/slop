import json
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

_RUN_ID = ContextVar("radar_run_id", default="-")


def set_radar_run_id(run_id):
    """Attach a stable run id to every log line emitted by the current radar thread."""
    return _RUN_ID.set(str(run_id or "-"))


def reset_radar_run_id(token):
    try:
        _RUN_ID.reset(token)
    except Exception:
        pass


def get_radar_run_id():
    return _RUN_ID.get()


def _compact_details(details):
    if details is None:
        return ""
    try:
        payload = json.dumps(details, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        payload = str(details)
    return payload[:6000]


def add_radar_log(message, level="INFO", stage="", details=None):
    """Write one authoritative diagnostic line to stdout/stderr for Render Logs.

    Logs intentionally do NOT touch SQLite. Diagnostics must never contend with the
    production database or disappear because Render replaced the ephemeral disk.
    """
    message = str(message or "").strip()
    if not message:
        return

    level = str(level or "INFO").upper()[:16]
    stage = str(stage or "general")[:80]
    run_id = get_radar_run_id()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = _compact_details(details)
    suffix = f" | {payload}" if payload else ""
    line = f"{timestamp} [RADAR][run={run_id}][{level}][{stage}] {message}{suffix}"

    stream = sys.stderr if level in {"ERROR", "CRITICAL"} else sys.stdout
    try:
        print(line, file=stream, flush=True)
    except Exception:
        # Logging must never be able to break the radar itself.
        pass
