"""Final edge-case guards on top of v19 hardening.

These are deliberately tiny wrappers around the tested v19 layer. They close four
race/state edges without changing discovery, semantic screening, ranking or the
<$5 budget contract.
"""

from __future__ import annotations

import sys

import radar_growth_v6 as growth
import radar_hardening_v19 as hardening
import radar_request_job as radar_job
from cloud_state import (
    clear_radar_cancel_request,
    load_radar_cancel_request,
    load_radar_job,
)
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log

EDGE_PROFILE = "v19_edge_guard_1"

_APPLIED = False
_BASE_CREATE = None
_BASE_POLL = None
_BASE_ERROR_GUARD = None
_BASE_FINALIZE = None


def _matching_cancel_marker(job):
    marker = load_radar_cancel_request() or {}
    run_id = str((job or {}).get("run_id") or "")
    return bool(run_id and str(marker.get("run_id") or "") == run_id)


def _pending_stop_payload(job):
    return {
        **radar_job.public_job(job),
        "accepted": False,
        "completed": False,
        "resumed": False,
        "stop_pending": True,
        "message": "Остановка предыдущего run уже подтверждена. Дождись terminal cancelled; новый run не будет создан поверх незавершённого STOP.",
        "edge_profile": EDGE_PROFILE,
    }, 202


def create_or_resume_edge():
    """START can never erase a STOP marker belonging to the active run."""
    before = load_radar_job() or {}
    if radar_job._is_active(before) and _matching_cancel_marker(before):
        add_radar_log(
            "EDGE GUARD: START отклонён, потому что для активного run уже есть stop-marker.",
            level="WARN",
            stage="stop-start-race",
            details={"run_id": before.get("run_id")},
        )
        return _pending_stop_payload(before)

    payload, code = _BASE_CREATE()
    after = load_radar_job() or {}
    marker = load_radar_cancel_request() or {}
    marker_run = str(marker.get("run_id") or "")
    current_run = str(after.get("run_id") or "")

    # A stale marker from an older already-terminal run must not poison a new run.
    if marker_run and marker_run != current_run:
        clear_radar_cancel_request()
    return payload, code


def poll_sources_edge(client, job):
    """A discovering job with an unstarted source returns to starting_sources."""
    sources = job.get("sources") or {}
    missing = [
        name for name, source in sources.items()
        if not str((source or {}).get("run_id") or "")
        and str((source or {}).get("status") or "NOT_STARTED").upper() not in radar_job.TERMINAL_SOURCE_STATUSES
    ]
    if missing:
        job["phase"] = "starting_sources"
        job["current_source"] = ""
        radar_job._persist(job)
        set_radar_status(
            "running",
            "Восстанавливаю запуск источников",
            6,
            300,
            f"Обнаружены незапущенные источники: {len(missing)}. Возвращаю state-machine к безопасному этапу запуска.",
            warning="",
            details={"run_id": job.get("run_id"), "missing_sources": missing[:12]},
        )
        add_radar_log(
            "EDGE GUARD: discovering содержал source без run_id; возвращаю starting_sources.",
            level="WARN",
            stage="source-recovery",
            details={"missing_sources": missing[:12]},
        )
        return job
    return _BASE_POLL(client, job)


def error_guard_edge(payload):
    """A harmless concurrent-tab BUSY response is not proof an error recovered."""
    if isinstance(payload, dict) and payload.get("busy"):
        return payload
    return _BASE_ERROR_GUARD(payload)


def invalidate_stale_recent_matches():
    """TOP/status must only expose verdicts produced by the current screening profile."""
    with db_conn() as conn:
        cur = conn.execute(
            """UPDATE radar_posts
               SET ai_checked=0, ai_match=0
               WHERE datetime(published_at)>=datetime('now','-7 days')
                 AND ai_match=1
                 AND COALESCE(screening_profile,'')<>?""",
            (hardening.PROFILE_VERSION,),
        )
        changed = int(cur.rowcount or 0)
        conn.commit()
    if changed:
        add_radar_log(
            f"EDGE CACHE: убрано {changed} старых PASS из TOP до повторной проверки текущим профилем.",
            level="WARN",
            stage="cache-migration",
            details={"invalidated": changed, "profile": hardening.PROFILE_VERSION},
        )
    return changed


def finalize_edge(job):
    invalidated = invalidate_stale_recent_matches()
    job.setdefault("stats", {})["stale_top_invalidated"] = invalidated
    try:
        radar_job._persist(job)
    except Exception:
        pass
    return _BASE_FINALIZE(job)


def apply_edge_guards():
    global _APPLIED, _BASE_CREATE, _BASE_POLL, _BASE_ERROR_GUARD, _BASE_FINALIZE
    if _APPLIED:
        return {"edge_profile": EDGE_PROFILE}
    _APPLIED = True

    app_module = sys.modules.get("app")
    if app_module is None:
        raise RuntimeError("radar_edge_v19 must be applied from app startup")

    _BASE_CREATE = app_module.create_or_resume_job
    _BASE_POLL = radar_job._poll_sources
    _BASE_ERROR_GUARD = hardening._apply_tick_error_guard
    _BASE_FINALIZE = growth._ORIGINAL_FINALIZE

    app_module.create_or_resume_job = create_or_resume_edge
    radar_job._poll_sources = poll_sources_edge
    hardening._apply_tick_error_guard = error_guard_edge
    growth._ORIGINAL_FINALIZE = finalize_edge

    # Clean old-profile PASS rows immediately on deploy, not only at finalization.
    invalidated = invalidate_stale_recent_matches()
    add_radar_log(
        "V19 EDGE GUARDS READY: STOP→START race, BUSY/error counter, stale TOP and missing run_id recovery закрыты.",
        stage="startup",
        details={
            "edge_profile": EDGE_PROFILE,
            "radar_profile": hardening.PROFILE_VERSION,
            "stale_top_invalidated": invalidated,
        },
    )
    return {"edge_profile": EDGE_PROFILE, "stale_top_invalidated": invalidated}
