import json
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

_RUN_ID = ContextVar("radar_run_id", default="-")


def set_radar_run_id(run_id):
    """Attach a stable run id to every log line emitted by the current radar execution."""
    return _RUN_ID.set(str(run_id or "-"))


def reset_radar_run_id(token):
    try:
        _RUN_ID.reset(token)
    except Exception:
        pass


def get_radar_run_id():
    return _RUN_ID.get()


def _current_rss_mb():
    """Best-effort current resident memory on Linux/Render."""
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    kb = float(line.split()[1])
                    return round(kb / 1024.0, 1)
    except Exception:
        pass
    return None


def _runtime_meta():
    return {
        "commit": str(os.environ.get("RENDER_GIT_COMMIT", "local"))[:12],
        "instance": str(os.environ.get("RENDER_INSTANCE_ID", "local"))[-16:],
        "pid": os.getpid(),
        "rss_mb": _current_rss_mb(),
    }


def _compact_details(details):
    runtime = _runtime_meta()
    if details is None:
        merged = runtime
    elif isinstance(details, dict):
        merged = {**runtime, **details}
    else:
        merged = {**runtime, "details": details}
    try:
        payload = json.dumps(merged, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        payload = str(merged)
    return payload[:6000]


def add_radar_log(message, level="INFO", stage="", details=None):
    """Write one authoritative diagnostic line to stdout/stderr for Render Logs.

    Every line carries the deployed Git commit, Render instance suffix, PID and
    current RSS. Logs intentionally do NOT touch SQLite.
    """
    message = str(message or "").strip()
    if not message:
        return

    level = str(level or "INFO").upper()[:16]
    stage = str(stage or "general")[:80]
    run_id = get_radar_run_id()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    payload = _compact_details(details)
    line = f"{timestamp} [RADAR][run={run_id}][{level}][{stage}] {message} | {payload}"

    stream = sys.stderr if level in {"ERROR", "CRITICAL"} else sys.stdout
    try:
        print(line, file=stream, flush=True)
    except Exception:
        pass
