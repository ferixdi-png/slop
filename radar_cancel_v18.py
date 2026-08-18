"""User-triggered hard stop for the durable radar job.

The radar is request-driven, so stopping means two things:
1) atomically mark the durable KVS job as terminal so no later tick can resume it;
2) best-effort abort any still-running Apify Actor runs to stop unnecessary spend.

The same process lock used by radar ticks is reused here. If one atomic tick is in
flight, the stop waits for that tick to finish and then cancels the job before any
next tick can start.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import radar_request_job as radar_job
from cloud_state import load_radar_job
from progress import set_radar_status
from radar_logs import add_radar_log, reset_radar_run_id, set_radar_run_id

CANCEL_PROFILE_VERSION = "force_stop_v18"
_ABORTABLE_STATUSES = {"READY", "RUNNING", "ABORTING"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _abort_apify_runs(sources: dict) -> dict:
    """Best-effort remote cancellation. Durable local cancellation never depends on it."""
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
            f"Force-stop: durable job уже остановлен, но Apify client недоступен: {exc}",
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
                # Immediate abort: this is a user-requested hard stop, not a resumable pause.
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


def cancel_active_job(wait_seconds: float = 180.0):
    """Atomically cancel the current radar job and abort its remote source runs.

    Returns ``(payload, status_code)`` for direct Flask use.
    """
    existing = load_radar_job() or {}
    if not radar_job._is_active(existing):
        return {
            **radar_job.public_job(existing),
            "cancelled": False,
            "already_stopped": True,
            "message": "Активного поиска уже нет.",
            "cancel_profile": CANCEL_PROFILE_VERSION,
        }, 200

    requested_run_id = str(existing.get("run_id") or "")
    token = set_radar_run_id(requested_run_id or None)
    add_radar_log(
        "FORCE STOP: пользователь запросил принудительную остановку текущего поиска.",
        level="WARN",
        stage="stop",
        details={"run_id": requested_run_id, "phase": existing.get("phase")},
    )
    try:
        set_radar_status(
            "running",
            "Останавливаю поиск",
            int((existing.get("progress") or 0) if isinstance(existing.get("progress"), (int, float)) else 0),
            30,
            "Жду завершения текущего атомарного шага, затем блокирую все следующие ticks и останавливаю активные источники.",
            warning="Принудительная остановка запрошена пользователем.",
            details={"run_id": requested_run_id, "force_stop_requested": True},
        )
    except Exception:
        pass

    acquired = radar_job._tick_lock.acquire(timeout=max(0.0, float(wait_seconds)))
    if not acquired:
        add_radar_log(
            "FORCE STOP: не удалось дождаться текущего tick в пределах timeout; кнопку можно нажать повторно.",
            level="ERROR",
            stage="stop",
            details={"run_id": requested_run_id, "wait_seconds": wait_seconds},
        )
        return {
            **radar_job.public_job(load_radar_job() or existing),
            "cancelled": False,
            "stop_pending": True,
            "message": "Текущий шаг ещё не завершился. Повтори остановку через несколько секунд.",
            "cancel_profile": CANCEL_PROFILE_VERSION,
        }, 202

    sources = {}
    try:
        job = load_radar_job() or existing
        # A new run may have been started elsewhere while this request waited.
        # Never cancel a different run than the one the user explicitly stopped.
        if requested_run_id and str(job.get("run_id") or "") != requested_run_id:
            return {
                **radar_job.public_job(job),
                "cancelled": False,
                "run_changed": True,
                "message": "Старый run уже сменился новым; новый поиск не остановлен.",
                "cancel_profile": CANCEL_PROFILE_VERSION,
            }, 409

        if not radar_job._is_active(job):
            return {
                **radar_job.public_job(job),
                "cancelled": False,
                "already_stopped": True,
                "message": "Поиск успел завершиться до команды остановки.",
                "cancel_profile": CANCEL_PROFILE_VERSION,
            }, 200

        sources = dict(job.get("sources") or {})
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

        set_radar_status(
            "cancelled",
            "Поиск остановлен",
            0,
            0,
            "Текущий run принудительно остановлен. Можно сразу запускать новый поиск.",
            warning="",
            details={
                "run_id": requested_run_id,
                "force_stopped": True,
                "cancelled_at": job["cancelled_at"],
                "cancel_profile": CANCEL_PROFILE_VERSION,
            },
        )
        add_radar_log(
            "FORCE STOP DONE: durable job переведён в cancelled; следующие ticks его не продолжат.",
            level="WARN",
            stage="stop",
            details={"run_id": requested_run_id, "cancelled_at": job["cancelled_at"]},
        )
    finally:
        radar_job._tick_lock.release()
        reset_radar_run_id(token)

    abort_stats = _abort_apify_runs(sources)
    final_job = load_radar_job() or {}
    return {
        **radar_job.public_job(final_job),
        "cancelled": True,
        "message": "Поиск принудительно остановлен. Можно запускать новый run.",
        "apify_abort": abort_stats,
        "cancel_profile": CANCEL_PROFILE_VERSION,
    }, 200
