"""Persist the tiny cross-run momentum history independently from visible TOP.

Fresh-run reset intentionally deletes radar_posts/radar_meta. The observation
history is a separate internal signal and is mirrored to Apify/local cloud state
so Render instance replacement does not erase acceleration baselines.
"""

from __future__ import annotations

from datetime import datetime, timezone

import cloud_state
import radar_omni_veo_veo3_v24 as v24
import radar_quality
from db import db_conn
from radar_logs import add_radar_log

MOMENTUM_RECORD_KEY = "RADAR_MOMENTUM_V25"
MAX_HISTORY_ROWS = 1500
MAX_THREE_SOURCE_CAP_USD = 1.25

_APPLIED = False
_BASE_REFRESH = None


def _dump_history(conn):
    rows = conn.execute(
        """SELECT post_url,observed_at,views,average_views_per_hour,search_term
           FROM radar_momentum_history
           ORDER BY datetime(observed_at) DESC
           LIMIT ?""",
        (MAX_HISTORY_ROWS,),
    ).fetchall()
    return [dict(row) for row in rows]


def save_momentum_checkpoint(conn=None):
    owns = conn is None
    if owns:
        conn = db_conn()
    try:
        rows = _dump_history(conn)
        payload = {
            "version": 25,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "hashtags": list(v24.HASHTAGS),
            "rows": rows,
        }
        return bool(cloud_state.save_cloud_record(MOMENTUM_RECORD_KEY, payload))
    finally:
        if owns:
            conn.close()


def restore_momentum_checkpoint():
    payload = cloud_state.load_cloud_record(MOMENTUM_RECORD_KEY)
    if not isinstance(payload, dict):
        return 0
    rows = payload.get("rows") or []
    if not isinstance(rows, list):
        return 0

    restored = 0
    with db_conn() as conn:
        for row in rows[:MAX_HISTORY_ROWS]:
            if not isinstance(row, dict) or not row.get("post_url") or not row.get("observed_at"):
                continue
            existing = conn.execute(
                "SELECT observed_at FROM radar_momentum_history WHERE post_url=?",
                (row.get("post_url"),),
            ).fetchone()
            existing_at = str(existing["observed_at"] or "") if existing else ""
            incoming_at = str(row.get("observed_at") or "")
            if existing_at and existing_at >= incoming_at:
                continue
            conn.execute(
                """INSERT INTO radar_momentum_history(post_url,observed_at,views,average_views_per_hour,search_term)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(post_url) DO UPDATE SET
                     observed_at=excluded.observed_at,
                     views=excluded.views,
                     average_views_per_hour=excluded.average_views_per_hour,
                     search_term=excluded.search_term""",
                (
                    str(row.get("post_url")),
                    incoming_at,
                    int(row.get("views") or 0),
                    float(row.get("average_views_per_hour") or 0),
                    str(row.get("search_term") or "").lower(),
                ),
            )
            restored += 1
        conn.commit()
    return restored


def refresh_and_checkpoint(conn):
    _BASE_REFRESH(conn)
    try:
        saved = save_momentum_checkpoint(conn)
        if not saved:
            add_radar_log(
                "V25: momentum checkpoint остался только в локальном fallback.",
                level="WARN",
                stage="momentum-cloud",
            )
    except Exception as exc:
        add_radar_log(
            f"V25: не удалось сохранить momentum checkpoint: {exc}",
            level="WARN",
            stage="momentum-cloud",
        )


def apply_momentum_cloud_v25():
    global _APPLIED, _BASE_REFRESH
    if _APPLIED:
        return {"momentum_cloud_version": 25, "momentum_record_key": MOMENTUM_RECORD_KEY}
    _APPLIED = True

    # Three hashtag actors must still fit the global <$5 contract even if an old
    # Render env contains a larger two-source cap from a previous deployment.
    v24.v21.SOURCE_CAP_USD = min(float(v24.v21.SOURCE_CAP_USD), MAX_THREE_SOURCE_CAP_USD)

    scope_info = v24.apply_omni_veo_veo3_v24()
    restored = restore_momentum_checkpoint()

    _BASE_REFRESH = radar_quality.refresh_recent_scores_quality
    radar_quality.refresh_recent_scores_quality = refresh_and_checkpoint

    # V21 keeps a module reference to radar_quality and the scale finalizer calls
    # radar_quality.refresh_recent_scores_quality dynamically, so this final
    # wrapper covers all production finalization paths without changing them.
    add_radar_log(
        "V25 READY: momentum history survives Render restarts independently from visible radar output.",
        stage="startup",
        details={
            "momentum_record_key": MOMENTUM_RECORD_KEY,
            "momentum_rows_restored": restored,
            "effective_source_cap_usd_each": v24.v21.SOURCE_CAP_USD,
            **scope_info,
        },
    )
    return {
        "momentum_cloud_version": 25,
        "momentum_record_key": MOMENTUM_RECORD_KEY,
        "momentum_rows_restored": restored,
        "effective_source_cap_usd_each": v24.v21.SOURCE_CAP_USD,
        **scope_info,
    }
