import json
import os
import time
from datetime import datetime, timezone

from apify_client import ApifyClient

from db import db_conn

STORE_NAME = "slop-fabrika-state"
RECORD_KEY = "RADAR_SNAPSHOT_V1"
JOB_RECORD_KEY = "RADAR_JOB_V2"
LOCAL_PREFIX = "cloud_state_fallback:"

_APIFY_CIRCUIT_UNTIL = 0.0
_APIFY_CIRCUIT_REASON = ""
_APIFY_CIRCUIT_TOKEN = ""


def _obj_id(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get("id")
    return getattr(value, "id", None)


def _record_value(record):
    if record is None:
        return None
    if isinstance(record, dict):
        return record.get("value")
    return getattr(record, "value", None)


def _is_monthly_limit_error(exc):
    text = str(exc or "").lower()
    return "monthly usage hard limit exceeded" in text or "monthly usage limit" in text


def _set_circuit(exc, token):
    global _APIFY_CIRCUIT_UNTIL, _APIFY_CIRCUIT_REASON, _APIFY_CIRCUIT_TOKEN
    _APIFY_CIRCUIT_TOKEN = token
    _APIFY_CIRCUIT_REASON = str(exc or "")[:500]
    _APIFY_CIRCUIT_UNTIL = time.time() + (900 if _is_monthly_limit_error(exc) else 45)


def apify_cloud_blocked():
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if token != _APIFY_CIRCUIT_TOKEN:
        return False, ""
    if time.time() < _APIFY_CIRCUIT_UNTIL:
        return True, _APIFY_CIRCUIT_REASON
    return False, ""


def _local_key(key):
    return LOCAL_PREFIX + str(key)


def _local_save(key, payload):
    try:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with db_conn() as conn:
            conn.execute(
                "INSERT INTO app_state(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (_local_key(key), encoded),
            )
            conn.commit()
        return True
    except Exception:
        return False


def _local_load(key):
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key=?",
                (_local_key(key),),
            ).fetchone()
        if not row:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _local_delete(key):
    try:
        with db_conn() as conn:
            conn.execute("DELETE FROM app_state WHERE key=?", (_local_key(key),))
            conn.commit()
        return True
    except Exception:
        return False


def _store_client():
    global _APIFY_CIRCUIT_UNTIL, _APIFY_CIRCUIT_REASON, _APIFY_CIRCUIT_TOKEN
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        return None

    if token != _APIFY_CIRCUIT_TOKEN:
        _APIFY_CIRCUIT_UNTIL = 0.0
        _APIFY_CIRCUIT_REASON = ""
        _APIFY_CIRCUIT_TOKEN = token

    if time.time() < _APIFY_CIRCUIT_UNTIL:
        return None

    try:
        client = ApifyClient(token)
        meta = client.key_value_stores().get_or_create(name=STORE_NAME)
        store_id = _obj_id(meta)
        if not store_id:
            return None
        return client.key_value_store(store_id)
    except Exception as exc:
        _set_circuit(exc, token)
        return None


def _table_rows(conn, table, where_sql="", params=(), limit=200):
    sql = f"SELECT * FROM {table}"
    if where_sql:
        sql += " WHERE " + where_sql
    sql += f" LIMIT {int(limit)}"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def save_cloud_record(key, payload):
    """Save locally first; mirror to Apify KVS only when available."""
    local_ok = _local_save(key, payload)
    store = _store_client()
    if not store:
        return local_ok
    try:
        store.set_record(str(key), payload, content_type="application/json")
        return True
    except Exception as exc:
        _set_circuit(exc, os.environ.get("APIFY_API_TOKEN", "").strip())
        return local_ok


def load_cloud_record(key):
    store = _store_client()
    if store:
        try:
            record = store.get_record(str(key))
            value = _record_value(record)
            if isinstance(value, dict):
                _local_save(key, value)
                return value
        except Exception as exc:
            _set_circuit(exc, os.environ.get("APIFY_API_TOKEN", "").strip())
    return _local_load(key)


def delete_cloud_record(key):
    local_ok = _local_delete(key)
    store = _store_client()
    if not store:
        return local_ok
    try:
        store.delete_record(str(key))
        return True
    except Exception as exc:
        _set_circuit(exc, os.environ.get("APIFY_API_TOKEN", "").strip())
        return local_ok


def save_radar_job(job):
    payload = dict(job or {})
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if not save_cloud_record(JOB_RECORD_KEY, payload):
        raise RuntimeError("Не удалось сохранить состояние радара даже в локальный fallback")
    return payload


def load_radar_job():
    return load_cloud_record(JOB_RECORD_KEY)


def clear_radar_job():
    return delete_cloud_record(JOB_RECORD_KEY)


def save_radar_snapshot():
    """Best-effort backup locally and in Apify KVS when quota/API allows it."""
    with db_conn() as conn:
        posts = _table_rows(
            conn,
            "radar_posts",
            "datetime(published_at)>=datetime('now','-7 days')",
            limit=200,
        )
        creators = _table_rows(conn, "tracked_creators", limit=300)
        meta = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM radar_meta ORDER BY id DESC LIMIT 3"
            ).fetchall()
        ]

    payload = {
        "version": 1,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "posts": posts,
        "tracked_creators": creators,
        "radar_meta": meta,
    }
    return save_cloud_record(RECORD_KEY, payload)


def _restore_rows(conn, table, rows):
    if not rows:
        return 0
    allowed = {
        row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }
    count = 0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        keys = [key for key in raw.keys() if key in allowed]
        if not keys:
            continue
        placeholders = ",".join("?" for _ in keys)
        columns = ",".join(keys)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({columns}) VALUES ({placeholders})",
            [raw.get(key) for key in keys],
        )
        count += 1
    return count


def restore_radar_snapshot_if_empty():
    """Restore the last stable radar from KVS or local fallback."""
    with db_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM radar_posts").fetchone()[0]
    if existing:
        return False

    payload = load_cloud_record(RECORD_KEY)
    if not isinstance(payload, dict):
        return False

    with db_conn() as conn:
        _restore_rows(conn, "radar_posts", payload.get("posts") or [])
        _restore_rows(conn, "tracked_creators", payload.get("tracked_creators") or [])
        _restore_rows(conn, "radar_meta", payload.get("radar_meta") or [])
        conn.commit()
    return True
