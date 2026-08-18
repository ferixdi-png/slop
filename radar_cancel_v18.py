"""Immediate cooperative hard-stop for the durable radar job.

A stop request must never sit behind a long Gemini/Apify tick for minutes. The
endpoint first writes a durable out-of-band cancel marker, then best-effort aborts
remote Actor runs. If the tick lock is free we finalize cancellation immediately;
otherwise the in-flight v19 tick observes the marker before returning and makes
the job terminal. This keeps the STOP HTTP request short and restart-safe.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import radar_request_job as radar_job
from cloud_state import (
    clear_radar_cancel_request,
    load_radar_job,
    request_radar_cancel,
)
from progress import set_radar_status
from radar_logs import add_radar_log, reset_radar_run_id, set_radar_run_id

CANCEL_PROFILE_VERSION = "force_stop_v19_marker"
_ABORTABLE_STATUSES = {"READY", "RUNNING", "ABORTING"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _abort_apify_runs(sources: dict) -> dict:
    run_ids = []
    for name, source in (sources or {}).items():
        run_id = str((source or {}).get("run_id") or "").strip()
        if run_id:
            run_ids.append((name, run_id))

    if not run_ids or not os.environ.get("APIFY_API_TOKEN", "").strip():
        return {"requested": len(run_ids), "aborted": 0, "errors": 0}

    aborted = 0
    errors = 0
    try:
        client = radar_job._client()
    except Exception as exc:
        add_radar_log(
            f"Force-stop marker сохранён, но Apify client недоступен: {exc}",
            level="WARN",
            stage="stop",
        )
        return {"requested": len(run_ids), "aborted": 0, "errors": len(run_ids)}

    for name, run_id in run_ids:
        try:
            run_client = client.run(run_id)
            info = run_client.get() or {}
            status = str(info.get("status") or "").upper()
            if status in _ABORTABLE_STATUSES:
                run_client.abort(gracefully=False)
                aborted += 1
                add_radar_log(
                    f"Force-stop: Apify source {name} abort отправлен.",
                    stage="stop",
                    details={"source": name, "run_id": run_id, "previous_status": status},
                )
        except Exception as exc:
            errors += 1
            add_radar_log(
                f"Force-stop: не удалось abort Apify source {name}: {exc}",
                level="WARN",
                stage="stop",
                details={"source": name, "run_id": run_id},
            )
    return {"requested": len(run_ids), "aborted": aborted, "errors": errors}


def _finalize_now(job):
    if not job or not radar_job._is_active(job):
        clear_radar_cancel_request()
        return job or {}
    job["phase"] = "cancelled"
    job["cancelled_at"] = _now_iso()
    job["cancelled_by_user"] = True
    job["cancel_profile"] = CANCEL_PROFILE_VERSION
    job["current_source"] = ""
    job["current_ai_index"] = None
    job["current_ai_post_url"] = ""
    job["error"] = ""
    job["result"] = {
        **dict(job.get("result") or {}),
        "cancelled": True,
        "cancelled_at": job["cancelled_at"],
    }
    radar_job._persist(job)
    clear_radar_cancel_request()
    set_radar_status(
        "cancelled",
        "Поиск остановлен",
        0,
        0,
        "Текущий run принудительно остановлен. Можно сразу запускать новый поиск.",
        warning="",
        details={
            "run_id": job.get("run_id"),
            "force_stopped": True,
            "cancelled_at": job["cancelled_at"],
            "cancel_profile": CANCEL_PROFILE_VERSION,
        },
    )
    return job


def cancel_active_job(wait_seconds: float = 1.5):
    """Request stop immediately; never block the HTTP edge on a long tick."""
    existing = load_radar_job() or {}
    if not radar_job._is_active(existing):
        clear_radar_cancel_request()
        return {
            **radar_job.public_job(existing),
            "cancelled": False,
            "already_stopped": True,
            "message": "Активного поиска уже нет.",
            "cancel_profile": CANCEL_PROFILE_VERSION,
        }, 200

    requested_run_id = str(existing.get("run_id") or "")
    token = set_radar_run_id(requested_run_id or None)
    try:
        if not request_radar_cancel(requested_run_id):
            raise RuntimeError("не удалось сохранить durable stop-marker")
        add_radar_log(
            "FORCE STOP MARKER: stop сохранён вне очереди текущего tick.",
            level="WARN",
            stage="stop",
            details={"run_id": requested_run_id, "phase": existing.get("phase")},
        )
        try:
            set_radar_status(
                "running",
                "Остановка запрошена",
                0,
                15,
                "Stop-marker сохранён. Новые шаги заблокированы; завершается только уже начатый атомарный шаг.",
                warning="Принудительная остановка подтверждена сервером.",
                details={"run_id": requested_run_id, "force_stop_requested": True},
            )
        except Exception:
            pass

        # Abort remote source runs without waiting for the local tick lock.
        abort_stats = _abort_apify_runs(dict(existing.get("sources") or {}))

        acquired = radar_job._tick_lock.acquire(timeout=max(0.0, min(float(wait_seconds), 2.0)))
        if acquired:
            try:
                job = load_radar_job() or existing
                if requested_run_id and str(job.get("run_id") or "") == requested_run_id:
                    job = _finalize_now(job)
                    add_radar_log(
                        "FORCE STOP DONE immediately: tick lock был свободен.",
                        level="WARN",
                        stage="stop",
                        details={"run_id": requested_run_id},
                    )
                    return {
                        **radar_job.public_job(job),
                        "cancelled": True,
                        "stop_pending": False,
                        "message": "Поиск принудительно остановлен. Можно запускать новый run.",
                        "apify_abort": abort_stats,
                        "cancel_profile": CANCEL_PROFILE_VERSION,
                    }, 200
            finally:
                radar_job._tick_lock.release()

        # An atomic tick is in flight. v19 checks the marker after that tick and
        # finalizes cancellation before it can schedule/accept another useful step.
        return {
            **radar_job.public_job(load_radar_job() or existing),
            "cancelled": False,
            "stop_pending": True,
            "message": "Остановка подтверждена. Текущий атомарный шаг заканчивается, после него run автоматически станет cancelled.",
            "apify_abort": abort_stats,
            "cancel_profile": CANCEL_PROFILE_VERSION,
        }, 202
    finally:
        reset_radar_run_id(token)
