import os
from datetime import datetime, timezone

from apify_client import ApifyClient

from db import db_conn

STORE_NAME = "slop-fabrika-state"
RECORD_KEY = "RADAR_SNAPSHOT_V1"
JOB_RECORD_KEY = "RADAR_JOB_V2"


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


def _store_client():
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        return None
    client = ApifyClient(token)
    meta = client.key_value_stores().get_or_create(name=STORE_NAME)
    store_id = _obj_id(meta)
    if not store_id:
        return None
    return client.key_value_store(store_id)


def _table_rows(conn, table, where_sql="", params=(), limit=200):
    sql = f"SELECT * FROM {table}"
    if where_sql:
        sql += " WHERE " + where_sql
    sql += f" LIMIT {int(limit)}"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def save_cloud_record(key, payload):
    """Persist JSON in the named Apify KVS used by this project."""
    store = _store_client()
    if not store:
        return False
    store.set_record(str(key), payload, content_type="application/json")
    return True


def load_cloud_record(key):
    store = _store_client()
    if not store:
        return None
    record = store.get_record(str(key))
    value = _record_value(record)
    return value if isinstance(value, dict) else None


def delete_cloud_record(key):
    store = _store_client()
    if not store:
        return False
    try:
        store.delete_record(str(key))
    except Exception:
        return False
    return True


def save_radar_job(job):
    payload = dict(job or {})
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    if not save_cloud_record(JOB_RECORD_KEY, payload):
        raise RuntimeError("Не удалось сохранить состояние радара в Apify KVS")
    return payload


def load_radar_job():
    return load_cloud_record(JOB_RECORD_KEY)


def clear_radar_job():
    return delete_cloud_record(JOB_RECORD_KEY)


def save_radar_snapshot():
    """Best-effort persistent backup using the already-connected Apify account."""
    store = _store_client()
    if not store:
        return False

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
    store.set_record(RECORD_KEY, payload, content_type="application/json")
    return True


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
    """Restore the last stable radar after a Render redeploy/restart."""
    with db_conn() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM radar_posts").fetchone()[0]
    if existing:
        return False

    store = _store_client()
    if not store:
        return False
    record = store.get_record(RECORD_KEY)
    payload = _record_value(record)
    if not isinstance(payload, dict):
        return False

    with db_conn() as conn:
        _restore_rows(conn, "radar_posts", payload.get("posts") or [])
        _restore_rows(conn, "tracked_creators", payload.get("tracked_creators") or [])
        _restore_rows(conn, "radar_meta", payload.get("radar_meta") or [])
        conn.commit()
    return True
