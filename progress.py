import json
from datetime import datetime, timezone

from db import db_conn

STALE_RUNNING_SECONDS = 20 * 60


def set_radar_status(stage, label, progress=0, eta_seconds=None, message="", warning="", details=None):
    payload = {
        "stage": stage,
        "label": label,
        "progress": max(0, min(100, int(progress))),
        "eta_seconds": int(eta_seconds) if eta_seconds is not None else None,
        "message": message,
        "warning": warning,
        "details": details or {},
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    with db_conn() as conn:
        conn.execute(
            """INSERT INTO app_state(key,value) VALUES('radar_status',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (json.dumps(payload, ensure_ascii=False),),
        )
        conn.commit()
    return payload


def get_radar_status():
    with db_conn() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key='radar_status'").fetchone()
    if not row:
        return {
            "stage": "idle",
            "label": "Готов к поиску",
            "progress": 0,
            "eta_seconds": None,
            "message": "Нажми «ЗАПУСТИТЬ ПОИСК».",
            "warning": "",
            "details": {},
            "updated_at": None,
        }
    try:
        payload = json.loads(row["value"])
        if payload.get("stage") == "running" and payload.get("updated_at"):
            updated = datetime.fromisoformat(str(payload["updated_at"]).replace("Z", "+00:00"))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
            if age > STALE_RUNNING_SECONDS:
                return {
                    "stage": "error",
                    "label": "Прошлый поиск был прерван",
                    "progress": payload.get("progress", 0),
                    "eta_seconds": None,
                    "message": "Сервер перезапустился или старый запрос был остановлен. Можно запускать новый поиск.",
                    "warning": payload.get("warning", ""),
                    "details": payload.get("details", {}),
                    "updated_at": payload.get("updated_at"),
                }
        return payload
    except Exception:
        return {
            "stage": "idle",
            "label": "Готов к поиску",
            "progress": 0,
            "eta_seconds": None,
            "message": "Статус будет обновлён при следующем запуске.",
            "warning": "",
            "details": {},
            "updated_at": None,
        }
