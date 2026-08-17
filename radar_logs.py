import json
from datetime import datetime, timezone

from db import db_conn

MAX_LOG_ROWS = 300


def add_radar_log(message, level="INFO", stage="", details=None):
    message = str(message or "").strip()
    if not message:
        return
    level = str(level or "INFO")[:16]
    stage = str(stage or "")[:80]
    payload = ""
    if details is not None:
        try:
            payload = json.dumps(details, ensure_ascii=False, default=str)[:4000]
        except Exception:
            payload = str(details)[:4000]
    created_at = datetime.now(timezone.utc).isoformat()

    try:
        extra = f" | {payload}" if payload else ""
        print(f"[RADAR][{level}][{stage}] {message}{extra}", flush=True)
    except Exception:
        pass

    try:
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO radar_logs(created_at,level,stage,message,details) VALUES(?,?,?,?,?)",
                (created_at, level, stage, message[:1000], payload),
            )
            conn.execute(
                "DELETE FROM radar_logs WHERE id NOT IN (SELECT id FROM radar_logs ORDER BY id DESC LIMIT ?)",
                (MAX_LOG_ROWS,),
            )
            conn.commit()
    except Exception:
        pass


def get_radar_logs(limit=100):
    limit = max(1, min(int(limit or 100), MAX_LOG_ROWS))
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT id,created_at,level,stage,message,details FROM radar_logs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = []
    for row in reversed(rows):
        x = dict(row)
        try:
            x["details"] = json.loads(x.get("details") or "null")
        except Exception:
            pass
        result.append(x)
    return result


def clear_radar_logs():
    with db_conn() as conn:
        conn.execute("DELETE FROM radar_logs")
        conn.commit()
