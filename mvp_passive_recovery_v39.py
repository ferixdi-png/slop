"""V39 MVP passive recovery.

A Render deploy starts with a fresh local SQLite file. V35 intentionally stopped the
browser from auto-driving /sync or /tick, which is correct for spend safety, but it
also meant the normal GET-only UI could stay empty until the user explicitly
started/resumed a search. The durable KVS snapshot already contains the previous
TOP, so restoring it must be a read/recovery operation, never a search operation.

This layer runs once during the production bootstrap, after V30 has installed its
14-day snapshot merge and V23 has installed its latest-run freshness guard.
It may read Apify KVS and write local SQLite only. It never creates/resumes a job,
never ticks a job, never starts an Actor and never calls Gemini.
"""

from __future__ import annotations

import radar_request_job as radar_job
from db import db_conn
from radar_logs import add_radar_log

PROFILE = "mvp_passive_recovery_v39"
_APPLIED = False
_LAST_INFO = None


def _post_count() -> int:
    try:
        with db_conn() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM radar_posts").fetchone()[0] or 0)
    except Exception:
        return 0


def install_passive_recovery_v39() -> dict:
    global _APPLIED, _LAST_INFO
    if _APPLIED:
        return dict(_LAST_INFO or diagnostics())

    _APPLIED = True
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
    return {
        "profile": PROFILE,
        "attempted": False,
        "restored": False,
        "posts_before": _post_count(),
        "posts_after": _post_count(),
        "local_cache_ready": _post_count() > 0,
        "external_operation": "apify_kvs_read_only",
        "paid_discovery_started": False,
        "gemini_called": False,
        "job_advanced": False,
        "retry_on_manual_start_if_empty": True,
        "error": "",
    }
