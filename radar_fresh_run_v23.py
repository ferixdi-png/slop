"""Fresh-run isolation for the Omni/Veo/Veo3 radar.

A genuinely new radar run must start from an empty result surface. This layer
clears only transient radar output (radar_posts + radar_meta), never analyses,
tracked creators, credentials, production packages or the hidden momentum
history used to measure real cross-run acceleration. Active-run resume is
idempotent and never clears already collected results.

It also guards snapshot restoration: a Render instance may restore a radar
snapshot only when that snapshot is at least as new as the latest durable run.
That prevents an older TOP from reappearing after a fresh-run reset.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import cloud_state
import radar_request_job as radar_job
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log
from radar_omni_veo_veo3_v24 import apply_omni_veo_veo3_v24

RESET_VERSION = "omni_veo_veo3_v23_fresh_run_reset"

_APPLIED = False
_BASE_CREATE = None
_BASE_ADVANCE = None
_BASE_RESTORE = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _run_floor(job):
    return _parse_time((job or {}).get("dataset_reset_at") or (job or {}).get("created_at"))


def restore_snapshot_for_latest_run_only():
    """Refuse to resurrect a snapshot older than the latest durable radar run."""
    job = cloud_state.load_radar_job() or {}
    floor = _run_floor(job)
    if floor:
        snapshot = cloud_state.load_cloud_record(cloud_state.RECORD_KEY)
        saved_at = _parse_time((snapshot or {}).get("saved_at")) if isinstance(snapshot, dict) else None
        if not saved_at or saved_at < floor:
            add_radar_log(
                "FRESH RUN V23: старый snapshot не восстановлен — он старше последнего radar run.",
                stage="snapshot",
                details={
                    "run_id": job.get("run_id"),
                    "run_floor": floor.isoformat(),
                    "snapshot_saved_at": saved_at.isoformat() if saved_at else "",
                },
            )
            return False
    return _BASE_RESTORE()


def _reset_result_tables(run_id: str):
    """Delete previous visible radar output; preserve internal momentum history."""
    with db_conn() as conn:
        old_posts = int(conn.execute("SELECT COUNT(*) FROM radar_posts").fetchone()[0] or 0)
        old_meta = int(conn.execute("SELECT COUNT(*) FROM radar_meta").fetchone()[0] or 0)
        conn.execute("DELETE FROM radar_posts")
        conn.execute("DELETE FROM radar_meta")
        conn.commit()

    # Write an empty/new snapshot immediately. Even if the cloud mirror is
    # temporarily unavailable, the run-floor guard above prevents older data
    # from being restored over this run after an instance swap.
    snapshot_saved = False
    try:
        snapshot_saved = bool(cloud_state.save_radar_snapshot())
    except Exception as exc:
        add_radar_log(
            f"FRESH RUN V23: пустой snapshot не зеркалирован: {exc}",
            level="WARN",
            stage="snapshot",
            details={"run_id": run_id},
        )

    add_radar_log(
        f"FRESH RUN V23: новый run очищен от старой выдачи: posts={old_posts}, meta={old_meta}.",
        stage="fresh-run-reset",
        details={
            "run_id": run_id,
            "cleared_radar_posts": old_posts,
            "cleared_radar_meta": old_meta,
            "momentum_history_preserved": True,
            "snapshot_saved": snapshot_saved,
        },
    )
    return {
        "cleared_radar_posts": old_posts,
        "cleared_radar_meta": old_meta,
        "momentum_history_preserved": True,
        "snapshot_saved": snapshot_saved,
    }


def _finish_pending_reset(job):
    if not job or not job.get("dataset_reset_pending"):
        return job, None
    if str(job.get("dataset_reset_version") or "") != RESET_VERSION:
        return job, None

    run_id = str(job.get("run_id") or "")
    reset_info = _reset_result_tables(run_id)
    job["dataset_reset_pending"] = False
    job["dataset_reset_done"] = True
    job["dataset_reset_done_at"] = _now_iso()
    job["dataset_reset_info"] = dict(reset_info)
    radar_job._persist(job)

    set_radar_status(
        "running",
        "Новый поиск: старая выдача очищена",
        1,
        600,
        "Предыдущий TOP, пул кандидатов и старая мета удалены. Собираю новый #omni/#veo/#veo3 run с нуля.",
        details={
            "run_id": run_id,
            "fresh_run_reset": True,
            "reset_version": RESET_VERSION,
            **reset_info,
        },
    )
    return job, reset_info


def create_or_resume_fresh_run():
    """Reset exactly once for a new run; resume never wipes current-run results."""
    payload, status_code = _BASE_CREATE()
    if not isinstance(payload, dict) or status_code != 202 or not payload.get("accepted"):
        return payload, status_code

    job = cloud_state.load_radar_job() or {}
    run_id = str(job.get("run_id") or "")
    payload_run_id = str(payload.get("run_id") or "")
    if not run_id or (payload_run_id and payload_run_id != run_id):
        return payload, status_code

    is_new_run = payload.get("resumed") is False
    if is_new_run and not job.get("dataset_reset_done"):
        job["dataset_reset_pending"] = True
        job["dataset_reset_at"] = str(job.get("created_at") or _now_iso())
        job["dataset_reset_version"] = RESET_VERSION
        radar_job._persist(job)

    # If a previous request crashed after marking reset pending but before the
    # SQLite transaction completed, the next START/resume finishes it safely.
    if job.get("dataset_reset_pending") and str(job.get("dataset_reset_version") or "") == RESET_VERSION:
        job, reset_info = _finish_pending_reset(job)
        payload = {
            **payload,
            "results_reset": True,
            "fresh_run_reset": RESET_VERSION,
            **(reset_info or {}),
        }
    return payload, status_code


def advance_after_fresh_reset(job):
    """No Apify/local MP4 step may start while a fresh-run reset is pending."""
    if job and job.get("dataset_reset_pending") and str(job.get("dataset_reset_version") or "") == RESET_VERSION:
        job, _ = _finish_pending_reset(job)
    return _BASE_ADVANCE(job)


def apply_fresh_run_v23():
    global _APPLIED, _BASE_CREATE, _BASE_ADVANCE, _BASE_RESTORE
    if _APPLIED:
        return {"fresh_run_reset_version": RESET_VERSION}
    _APPLIED = True

    app_module = sys.modules.get("app")
    if app_module is None:
        raise RuntimeError("radar_fresh_run_v23 must be applied from app startup")

    # V24 is deliberately applied here: radar_edge_v19 calls V22 immediately
    # before this layer, so V24 becomes the final search scope without changing
    # the proven outer STOP/START race wrapper.
    scope_info = apply_omni_veo_veo3_v24()

    _BASE_CREATE = app_module.create_or_resume_job
    _BASE_ADVANCE = radar_job._advance
    _BASE_RESTORE = radar_job.restore_radar_snapshot_if_empty

    app_module.create_or_resume_job = create_or_resume_fresh_run
    radar_job._advance = advance_after_fresh_reset
    radar_job.restore_radar_snapshot_if_empty = restore_snapshot_for_latest_run_only

    add_radar_log(
        "FRESH RUN V23 READY: новый START очищает старый TOP/meta; resume не очищает текущий run; старый snapshot не воскресает.",
        stage="startup",
        details={"fresh_run_reset_version": RESET_VERSION, **scope_info},
    )
    return {"fresh_run_reset_version": RESET_VERSION, **scope_info}
