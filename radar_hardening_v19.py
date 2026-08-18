"""Production hardening layer for the request-driven radar.

The goal is not new product behavior. It closes the most expensive/sticky failure
modes that only show up after long real runs: stale jobs after deploys, endless
retries, stuck Apify runs, one bad source poisoning the run, oversized datasets,
stale semantic cache, repeated permanent media failures, SQLite lock bursts,
stop requests racing a long tick, and frontend/server truth drifting apart.
"""

from __future__ import annotations

import hashlib
import itertools
import os
import sys
from datetime import datetime, timezone

import radar_budget_v10 as budget
import radar_growth_v6 as growth
import radar_request_job as radar_job
import radar_resilient_v17 as v17
from cloud_state import (
    clear_radar_cancel_request,
    load_radar_cancel_request,
    load_radar_job,
)
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log

PROFILE_VERSION = "dialogue_trends_v19_hardened_budget5"
SOURCE_MAX_AGE_SECONDS = int(os.environ.get("RADAR_SOURCE_MAX_AGE_SECONDS", "900"))
MAX_SOURCE_POLL_ERRORS = int(os.environ.get("RADAR_SOURCE_MAX_POLL_ERRORS", "3"))
DATASET_MAX_ITEMS_PER_SOURCE = int(os.environ.get("RADAR_DATASET_MAX_ITEMS_PER_SOURCE", "2500"))
MAX_SAME_TICK_ERRORS = int(os.environ.get("RADAR_MAX_SAME_TICK_ERRORS", "6"))
STRUCTURED_OUTER_ATTEMPTS = 2

_APPLIED = False
_ORIGINAL_PREPARE = None
_ORIGINAL_PROCESS = None
_ORIGINAL_POLL = None
_ORIGINAL_COLLECT = None
_ORIGINAL_SAVE_POST = None

_PERMANENT_AI_ERROR_FRAGMENTS = (
    "404",
    "403 forbidden",
    "not found",
    "private reel",
    "reel is private",
    "не смог заново получить выбранный reel",
    "не вернул прямой video url",
    "unsupported media",
    "invalid media",
    "duration_gate",
    "outside 1.0-10.05",
    "вне диапазона",
)


def _now_iso():
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


def _source_age_seconds(source):
    started = _parse_time((source or {}).get("started_at"))
    if not started:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _cancel_requested_for(job):
    marker = load_radar_cancel_request() or {}
    run_id = str((job or {}).get("run_id") or "")
    return bool(run_id and str(marker.get("run_id") or "") == run_id)


def _mark_cancelled(job, reason="user_stop"):
    if not job or not radar_job._is_active(job):
        clear_radar_cancel_request()
        return job or {}
    job["phase"] = "cancelled"
    job["cancelled_at"] = _now_iso()
    job["cancelled_by_user"] = True
    job["cancel_reason"] = reason
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
        "Stop-marker подтверждён. Старый run больше не может продолжиться.",
        warning="",
        details={"run_id": job.get("run_id"), "hardening_profile": PROFILE_VERSION},
    )
    add_radar_log(
        "V19 CANCEL CONFIRMED: durable job переведён в terminal cancelled.",
        level="WARN",
        stage="stop",
        details={"run_id": job.get("run_id"), "reason": reason},
    )
    return job


def _reset_to_current_profile(job, stage="migration-v19"):
    """Never spend another tick on a queue built by an older screening contract."""
    old_profile = str((job or {}).get("profile") or "")
    job["profile"] = PROFILE_VERSION
    job["phase"] = "queued"
    job["sources"] = radar_job._build_sources()
    job["candidates"] = []
    job["warnings"] = []
    job["source_failures"] = {}
    job["stats"] = {"migrated_from_profile": old_profile}
    job["result"] = {}
    job["error"] = ""
    job["last_error"] = ""
    job["current_source"] = ""
    job["current_ai_index"] = None
    job["current_ai_post_url"] = ""
    job["error_guard"] = {}
    radar_job._persist(job)
    clear_radar_cancel_request()
    set_radar_status(
        "running",
        "Обновляю поиск под новую версию",
        1,
        600,
        "Старая очередь автоматически сброшена до первого платного шага; запускаю новый discovery по текущим правилам.",
        details={
            "run_id": job.get("run_id"),
            "old_profile": old_profile,
            "new_profile": PROFILE_VERSION,
        },
    )
    add_radar_log(
        "V19 PROFILE MIGRATION: stale queue reset before continuing.",
        stage=stage,
        details={"old_profile": old_profile, "new_profile": PROFILE_VERSION},
    )
    return job


def create_or_resume_job_v19():
    payload, status_code = radar_job.create_or_resume_job()
    job = load_radar_job() or {}
    if radar_job._is_active(job):
        if str(job.get("profile") or "") != PROFILE_VERSION:
            job = _reset_to_current_profile(job)
            payload = {
                **radar_job.public_job(job),
                "accepted": True,
                "completed": False,
                "resumed": False,
                "migrated": True,
                "message": "Старый run автоматически переведён на текущий профиль и перезапущен с discovery.",
            }
            status_code = 202
        else:
            clear_radar_cancel_request()
    return payload, status_code


def _poll_sources_v19(client, job):
    """One failing/stuck Actor cannot poison every later tick."""
    sources = job.get("sources") or {}
    completed = 0
    failures = dict(job.get("source_failures") or {})

    for name, source in sources.items():
        run_id = str(source.get("run_id") or "")
        if not run_id:
            continue
        previous = str(source.get("status") or "")
        age = _source_age_seconds(source)

        if previous in radar_job.TERMINAL_SOURCE_STATUSES:
            completed += 1
            if previous != "SUCCEEDED":
                failures[name] = source.get("status_message") or previous
            continue

        if age >= SOURCE_MAX_AGE_SECONDS:
            try:
                client.run(run_id).abort(gracefully=False)
            except Exception:
                pass
            source["status"] = "TIMED-OUT"
            source["status_message"] = f"V19 watchdog: source exceeded {SOURCE_MAX_AGE_SECONDS}s"
            failures[name] = source["status_message"]
            completed += 1
            add_radar_log(
                f"Apify source {name} принудительно остановлен watchdog после {age} сек.",
                level="WARN",
                stage="apify-watchdog",
                details={"source": name, "run_id": run_id, "age_seconds": age},
            )
            continue

        try:
            info = client.run(run_id).get() or {}
            source["poll_errors"] = 0
        except Exception as exc:
            source["poll_errors"] = int(source.get("poll_errors") or 0) + 1
            source["last_poll_error"] = str(exc)[:500]
            add_radar_log(
                f"Apify source {name}: ошибка чтения статуса {source['poll_errors']}/{MAX_SOURCE_POLL_ERRORS}: {exc}",
                level="WARN",
                stage="apify-poll",
                details={"source": name, "run_id": run_id},
            )
            if source["poll_errors"] >= MAX_SOURCE_POLL_ERRORS:
                source["status"] = "FAILED"
                source["status_message"] = f"status polling failed {source['poll_errors']} times"
                failures[name] = source["last_poll_error"]
                completed += 1
            continue

        status = str(info.get("status") or previous or "UNKNOWN").upper()
        source["status"] = status
        source["dataset_id"] = (
            info.get("defaultDatasetId")
            or info.get("default_dataset_id")
            or source.get("dataset_id")
            or ""
        )
        source["status_message"] = str(
            info.get("statusMessage") or info.get("status_message") or ""
        )[:500]
        if status != previous:
            add_radar_log(
                f"Apify source {name}: {status}",
                stage="apify",
                details={"source": name, "run_id": run_id, "status": status},
            )
        if status in radar_job.TERMINAL_SOURCE_STATUSES:
            completed += 1
            if status != "SUCCEEDED":
                failures[name] = source.get("status_message") or status

    job["source_failures"] = failures
    job["phase"] = "preparing" if completed >= len(sources) else "discovering"
    radar_job._persist(job)
    set_radar_status(
        "running",
        "Источники собирают Reels",
        12 + int(18 * completed / max(1, len(sources))),
        240,
        f"Готово источников: {completed}/{len(sources)}. Ошибочный источник изолируется и не останавливает остальные.",
        warning=(f"Источников с ошибкой: {len(failures)}" if failures else ""),
        details={
            "run_id": job.get("run_id"),
            "sources_done": completed,
            "sources_total": len(sources),
            "source_failures": len(failures),
            "source_watchdog_seconds": SOURCE_MAX_AGE_SECONDS,
        },
    )
    return job


def _collect_source_rows_v19(client, job):
    """Bound RAM and turn a broken dataset into one source warning, not a run crash."""
    results = {}
    failures = dict(job.get("source_failures") or {})
    for name, source in (job.get("sources") or {}).items():
        if str(source.get("status") or "").upper() != "SUCCEEDED":
            results[name] = []
            continue
        try:
            dataset_id = str(source.get("dataset_id") or "")
            if not dataset_id:
                info = client.run(source.get("run_id")).get() or {}
                dataset_id = str(info.get("defaultDatasetId") or info.get("default_dataset_id") or "")
                source["dataset_id"] = dataset_id
            if not dataset_id:
                raise RuntimeError("run завершён без defaultDatasetId")
            iterator = client.dataset(dataset_id).iterate_items()
            rows = list(itertools.islice(iterator, DATASET_MAX_ITEMS_PER_SOURCE))
            results[name] = rows
            source["dataset_items_loaded"] = len(rows)
            source["dataset_truncated"] = len(rows) >= DATASET_MAX_ITEMS_PER_SOURCE
            add_radar_log(
                f"Dataset {name}: загружено {len(rows)} элементов (RAM cap {DATASET_MAX_ITEMS_PER_SOURCE}).",
                stage="apify-dataset",
                details={
                    "source": name,
                    "dataset_id": dataset_id,
                    "items": len(rows),
                    "truncated": source["dataset_truncated"],
                },
            )
        except Exception as exc:
            results[name] = []
            failures[name] = f"dataset: {str(exc)[:400]}"
            add_radar_log(
                f"Dataset {name} пропущен из-за ошибки: {exc}",
                level="WARN",
                stage="apify-dataset",
                details={"source": name},
            )
    job["source_failures"] = failures
    return results


def _save_post_profiled(conn, item, assessment):
    _ORIGINAL_SAVE_POST(conn, item, assessment)
    if assessment is not None:
        conn.execute(
            "UPDATE radar_posts SET screening_profile=? WHERE post_url=?",
            (PROFILE_VERSION, item.get("post_url", "")),
        )


def _prepare_candidates_v19(client, job):
    job = _ORIGINAL_PREPARE(client, job)
    candidates = job.get("candidates") or []
    if not candidates:
        job["profile"] = PROFILE_VERSION
        radar_job._persist(job)
        return job

    post_urls = [str(x.get("post_url") or "") for x in candidates if x.get("post_url")]
    profiles = {}
    if post_urls:
        with db_conn() as conn:
            for url in post_urls:
                row = conn.execute(
                    "SELECT screening_profile,ai_checked,ai_match FROM radar_posts WHERE post_url=?",
                    (url,),
                ).fetchone()
                if row:
                    profiles[url] = dict(row)

    invalidated = 0
    stale_urls = []
    for item in candidates:
        if not item.get("ai_done"):
            continue
        url = str(item.get("post_url") or "")
        cached_profile = str((profiles.get(url) or {}).get("screening_profile") or "")
        if cached_profile != PROFILE_VERSION:
            item["ai_done"] = False
            item["ai_match"] = False
            item["ai_attempts"] = 0
            item["ai_error"] = ""
            item.pop("assessment", None)
            stale_urls.append(url)
            invalidated += 1

    if stale_urls:
        with db_conn() as conn:
            conn.executemany(
                "UPDATE radar_posts SET ai_checked=0,ai_match=0,screening_profile='' WHERE post_url=?",
                [(url,) for url in stale_urls],
            )
            conn.commit()

    job["profile"] = PROFILE_VERSION
    job.setdefault("stats", {})["stale_screening_cache_invalidated"] = invalidated
    radar_job._persist(job)
    if invalidated:
        add_radar_log(
            f"V19 CACHE: сброшено {invalidated} старых Gemini-вердиктов от другого профиля.",
            stage="cache-migration",
            details={"invalidated": invalidated, "profile": PROFILE_VERSION},
        )
    return job


def _is_permanent_ai_error(text):
    lowered = str(text or "").lower()
    return any(fragment in lowered for fragment in _PERMANENT_AI_ERROR_FRAGMENTS)


def _process_one_ai_v19(job):
    index = radar_job._next_ai_index(job)
    result = _ORIGINAL_PROCESS(job)
    if index is None:
        return result
    candidates = result.get("candidates") or []
    if index >= len(candidates):
        return result

    item = candidates[index]
    error = str(item.get("ai_error") or "")
    attempts = int(item.get("ai_attempts") or 0)
    if error:
        structured_exhausted = "SEMANTIC_SCREENING_JSON_FAILED" in error and attempts >= STRUCTURED_OUTER_ATTEMPTS
        permanent = _is_permanent_ai_error(error)
        if permanent or structured_exhausted:
            item["ai_done"] = True
            item["ai_match"] = False
            item["terminal_ai_error"] = True
            reason = "permanent_media_error" if permanent else "structured_output_exhausted"
            add_radar_log(
                f"V19 AI RETRY GUARD: @{item.get('creator','')} больше не повторяется ({reason}).",
                level="WARN",
                stage="gemini-retry-guard",
                details={"attempts": attempts, "error": error[:300]},
            )
            if radar_job._next_ai_index(result) is None:
                result["phase"] = "finalizing"
            radar_job._persist(result)
    return result


def _error_fingerprint(message):
    normalized = " ".join(str(message or "").lower().split())[:600]
    return hashlib.sha1(normalized.encode("utf-8", errors="ignore")).hexdigest()[:12]


def _apply_tick_error_guard(payload):
    if not isinstance(payload, dict) or not payload.get("transient_error"):
        job = load_radar_job() or {}
        if job.get("error_guard"):
            job["error_guard"] = {}
            try:
                radar_job._persist(job)
            except Exception:
                pass
        return payload

    job = load_radar_job() or {}
    if not radar_job._is_active(job):
        return payload
    message = str(payload.get("message") or job.get("last_error") or "unknown error")
    fingerprint = _error_fingerprint(message)
    guard = dict(job.get("error_guard") or {})
    if guard.get("fingerprint") == fingerprint:
        count = int(guard.get("count") or 0) + 1
    else:
        count = 1
    guard = {
        "fingerprint": fingerprint,
        "count": count,
        "message": message[:500],
        "updated_at": _now_iso(),
    }
    job["error_guard"] = guard

    if count >= MAX_SAME_TICK_ERRORS:
        job["phase"] = "failed"
        job["error"] = "RETRY_BUDGET_EXCEEDED"
        job["failed_at"] = _now_iso()
        radar_job._persist(job)
        set_radar_status(
            "error",
            "Поиск остановлен защитой от цикла",
            0,
            None,
            f"Одна и та же ошибка повторилась {count} раз. Автоповторы остановлены, чтобы не жечь запросы.",
            warning=message[:300],
            details={"fingerprint": fingerprint, "repeats": count, "run_id": job.get("run_id")},
        )
        add_radar_log(
            "V19 CIRCUIT BREAKER: одинаковая ошибка исчерпала retry budget.",
            level="ERROR",
            stage="retry-guard",
            details={"fingerprint": fingerprint, "repeats": count, "message": message[:300]},
        )
        return {
            **payload,
            "active": False,
            "phase": "failed",
            "transient_error": False,
            "retry_budget_exceeded": True,
            "message": "Автоповторы остановлены: одна и та же ошибка повторилась слишком много раз.",
        }

    radar_job._persist(job)
    payload["same_error_repeats"] = count
    payload["same_error_retry_limit"] = MAX_SAME_TICK_ERRORS
    return payload


def wrap_tick_job_v19(base_tick_job):
    def wrapped():
        before = load_radar_job() or {}
        if radar_job._is_active(before) and str(before.get("profile") or "") != PROFILE_VERSION:
            before = _reset_to_current_profile(before)

        if radar_job._is_active(before) and _cancel_requested_for(before):
            cancelled = _mark_cancelled(before, "marker_before_tick")
            return {**radar_job.public_job(cancelled), "cancelled": True, "message": "Поиск остановлен."}, 200

        payload, status_code = base_tick_job()

        after = load_radar_job() or {}
        if radar_job._is_active(after) and _cancel_requested_for(after):
            after = _mark_cancelled(after, "marker_after_tick")
            return {**radar_job.public_job(after), "cancelled": True, "message": "Поиск остановлен."}, 200

        payload = _apply_tick_error_guard(payload)
        return payload, status_code

    return wrapped


def apply_hardening_v19():
    global _APPLIED, _ORIGINAL_PREPARE, _ORIGINAL_PROCESS, _ORIGINAL_POLL, _ORIGINAL_COLLECT, _ORIGINAL_SAVE_POST
    if _APPLIED:
        return budget._assert_budget()
    _APPLIED = True

    _ORIGINAL_PREPARE = radar_job._prepare_candidates
    _ORIGINAL_PROCESS = radar_job._process_one_ai
    _ORIGINAL_POLL = radar_job._poll_sources
    _ORIGINAL_COLLECT = radar_job._collect_source_rows
    _ORIGINAL_SAVE_POST = radar_job.save_post_preserve_ai

    budget.PROFILE_VERSION = PROFILE_VERSION
    growth.PROFILE_VERSION = PROFILE_VERSION
    v17.PROFILE_VERSION = PROFILE_VERSION

    radar_job._poll_sources = _poll_sources_v19
    radar_job._collect_source_rows = _collect_source_rows_v19
    radar_job.save_post_preserve_ai = _save_post_profiled
    radar_job._prepare_candidates = _prepare_candidates_v19
    radar_job._process_one_ai = _process_one_ai_v19

    info = budget._assert_budget()
    app_module = sys.modules.get("app")
    if app_module is not None:
        app_module.PROFILE_VERSION = PROFILE_VERSION
        app_module.create_or_resume_job = create_or_resume_job_v19
        app_module.tick_job = wrap_tick_job_v19(app_module.tick_job)
        app_module.BUDGET_INFO = info

    add_radar_log(
        "V19 HARDENING READY: stop marker, stale-profile migration, retry circuit breaker, source watchdog, poll isolation, bounded datasets, profile-aware cache, AI retry guard, SQLite lock retry, frontend-safe server contract.",
        stage="startup",
        details={
            "profile": PROFILE_VERSION,
            "source_watchdog_seconds": SOURCE_MAX_AGE_SECONDS,
            "max_source_poll_errors": MAX_SOURCE_POLL_ERRORS,
            "dataset_max_items_per_source": DATASET_MAX_ITEMS_PER_SOURCE,
            "max_same_tick_errors": MAX_SAME_TICK_ERRORS,
            "structured_outer_attempts": STRUCTURED_OUTER_ATTEMPTS,
            **info,
        },
    )
    return info
