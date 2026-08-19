"""V39 MVP durability: passive recovery + deterministic newest-first snapshots.

A Render deploy starts with a fresh local SQLite file. V35 intentionally stopped the
browser from auto-driving /sync or /tick, which is correct for spend safety, but it
also meant the normal GET-only UI could stay empty until the user explicitly
started/resumed a search. The durable KVS snapshot already contains the previous
TOP, so restoring it must be a read/recovery operation, never a search operation.

V39 also closes a durability edge in the V30 snapshot writer: LIMIT 1000 without an
ORDER BY can eventually preserve arbitrary/older rows when several runs coexist in
the 14-day window. V39 makes the snapshot deterministic: newest posts first, then
strongest score/views. It reuses the same KVS key/schema and does not widen budget.

This layer runs once during the production bootstrap, after V30 has installed its
14-day snapshot merge and V23 has installed its latest-run freshness guard.
Startup recovery may read Apify KVS and write local SQLite only. It never
creates/resumes a job, never ticks a job, never starts an Actor and never calls
Gemini.
"""

from __future__ import annotations

from datetime import datetime, timezone

import cloud_state
import radar_audit_v30 as v30
import radar_growth_v6 as growth
import radar_request_job as radar_job
from db import db_conn
from radar_logs import add_radar_log

PROFILE = "mvp_passive_recovery_v39"
_APPLIED = False
_LAST_INFO = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _post_count() -> int:
    try:
        with db_conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM radar_posts").fetchone()[0] or 0)
    except Exception:
        return 0


def save_ordered_snapshot_v39():
    """Persist the most relevant 14-day recovery surface deterministically."""
    with db_conn() as conn:
        posts = [
            dict(row)
            for row in conn.execute(
                f"""SELECT * FROM radar_posts
                    WHERE datetime(published_at)>=datetime('now','-{v30.SNAPSHOT_LOOKBACK_DAYS} days')
                    ORDER BY datetime(published_at) DESC,
                             COALESCE(viral_score_v2,0) DESC,
                             COALESCE(views,0) DESC
                    LIMIT ?""",
                (int(v30.SNAPSHOT_POST_LIMIT),),
            ).fetchall()
        ]
        creators = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM tracked_creators
                   ORDER BY datetime(last_seen_at) DESC,
                            COALESCE(best_views_per_hour,0) DESC
                   LIMIT ?""",
                (int(v30.SNAPSHOT_CREATOR_LIMIT),),
            ).fetchall()
        ]
        meta = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM radar_meta ORDER BY id DESC LIMIT 5"
            ).fetchall()
        ]

    payload = {
        "version": 39,
        "profile": v30.SCREENING_PROFILE,
        "saved_at": _now_iso(),
        "lookback_days": v30.SNAPSHOT_LOOKBACK_DAYS,
        "post_limit": v30.SNAPSHOT_POST_LIMIT,
        "snapshot_order": "published_at_desc_then_viral_score_then_views",
        "posts": posts,
        "tracked_creators": creators,
        "radar_meta": meta,
    }
    return cloud_state.save_cloud_record(cloud_state.RECORD_KEY, payload)


def _install_ordered_snapshot_writer() -> None:
    # V30 has already installed the snapshot wrappers. Replace only their writer
    # with the deterministic V39 equivalent, preserving all existing throttling.
    cloud_state.save_radar_snapshot = save_ordered_snapshot_v39
    growth._ORIGINAL_SNAPSHOT = save_ordered_snapshot_v39


def install_passive_recovery_v39() -> dict:
    global _APPLIED, _LAST_INFO
    if _APPLIED:
        return dict(_LAST_INFO or diagnostics())

    _APPLIED = True
    _install_ordered_snapshot_writer()

    before = _post_count()
    restored = False
    error = ""

    # The function referenced here is the FINAL patched restore chain:
    # V23 guards snapshot freshness against the durable run floor, while V30
    # supplies the 14-day/1000-row merge implementation underneath it.
    try:
        if before <= 0:
            restored = bool(radar_job.restore_radar_snapshot_if_empty())
    except Exception as exc:
        error = str(exc)[:500]

    after = _post_count()

    # Avoid a duplicate cloud read on the first later /sync or /tick only when the
    # passive boot recovery already has a usable local cache. If KVS was temporarily
    # unavailable and SQLite is still empty, leave this false so a later explicit
    # manual action can retry recovery before doing any work.
    if after > 0:
        try:
            radar_job._snapshot_checked = True
        except Exception:
            pass

    info = {
        "profile": PROFILE,
        "attempted": before <= 0,
        "restored": bool(restored),
        "posts_before": before,
        "posts_after": after,
        "local_cache_ready": after > 0,
        "external_operation": "apify_kvs_read_only",
        "paid_discovery_started": False,
        "gemini_called": False,
        "job_advanced": False,
        "retry_on_manual_start_if_empty": after <= 0,
        "snapshot_writer": "newest_first_v39",
        "snapshot_lookback_days": v30.SNAPSHOT_LOOKBACK_DAYS,
        "snapshot_post_limit": v30.SNAPSHOT_POST_LIMIT,
        "error": error,
    }
    _LAST_INFO = dict(info)

    if error:
        add_radar_log(
            f"V39 passive recovery не смог восстановить локальный TOP: {error}",
            level="WARN",
            stage="startup-recovery",
            details=info,
        )
    elif restored:
        add_radar_log(
            f"V39 PASSIVE RECOVERY: восстановлено {after} radar rows из durable snapshot без запуска поиска.",
            stage="startup-recovery",
            details=info,
        )
    elif before > 0:
        add_radar_log(
            f"V39 PASSIVE RECOVERY: локальный radar cache уже содержит {after} rows; cloud restore не нужен.",
            stage="startup-recovery",
            details=info,
        )
    else:
        add_radar_log(
            "V39 PASSIVE RECOVERY: подходящего snapshot пока нет; поиск не запускался.",
            stage="startup-recovery",
            details=info,
        )
    return dict(info)


def diagnostics() -> dict:
    if _LAST_INFO is not None:
        return dict(_LAST_INFO)
    count = _post_count()
    return {
        "profile": PROFILE,
        "attempted": False,
        "restored": False,
        "posts_before": count,
        "posts_after": count,
        "local_cache_ready": count > 0,
        "external_operation": "apify_kvs_read_only",
        "paid_discovery_started": False,
        "gemini_called": False,
        "job_advanced": False,
        "retry_on_manual_start_if_empty": True,
        "snapshot_writer": "newest_first_v39",
        "snapshot_lookback_days": v30.SNAPSHOT_LOOKBACK_DAYS,
        "snapshot_post_limit": v30.SNAPSHOT_POST_LIMIT,
        "error": "",
    }
