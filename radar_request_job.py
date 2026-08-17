import os
import time
import uuid
from datetime import datetime, timezone
from threading import Lock

from apify_client import ApifyClient

from cloud_state import (
    load_radar_job,
    restore_radar_snapshot_if_empty,
    save_radar_job,
    save_radar_snapshot,
)
from config import (
    APIFY_CREATOR_ACTOR,
    APIFY_HASHTAG_ACTOR,
    APIFY_SEARCH_ACTOR,
    HASHTAGS,
    HASHTAG_LIMIT,
    RADAR_AI_ANALYZE_LIMIT,
    RADAR_KEEP_LIMIT,
    RADAR_MODEL,
    SEARCH_LIMIT,
    SEARCH_TERMS,
)
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log, reset_radar_run_id, set_radar_run_id
from radar_normalize import normalize_reel
from radar_quality import (
    refresh_recent_scores_quality,
    save_meta_report_quality,
    save_post_preserve_ai,
    top_eligible,
)
from radar_service import load_creator_stats, matches, update_creator_baselines
from reel_media import download_reel_for_analysis

RUNTIME = "request_state_machine_v5"
ACTIVE_PHASES = {
    "queued",
    "starting_sources",
    "discovering",
    "preparing",
    "ai",
    "finalizing",
}
TERMINAL_SOURCE_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}

_tick_lock = Lock()
_snapshot_checked = False


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _new_run_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _is_active(job):
    return bool(job) and str(job.get("phase") or "") in ACTIVE_PHASES


def _persist(job):
    job["runtime"] = RUNTIME
    job["updated_at"] = _now_iso()
    job["updated_by_commit"] = str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12]
    job["updated_by_instance"] = os.environ.get("RENDER_INSTANCE_ID", "")
    return save_radar_job(job)


def _ensure_snapshot_once():
    global _snapshot_checked
    if _snapshot_checked:
        return
    try:
        restored = restore_radar_snapshot_if_empty()
        add_radar_log(
            "Локальный radar cache восстановлен из Apify snapshot." if restored else "Локальный radar cache уже заполнен или snapshot пока пуст.",
            stage="snapshot",
        )
    except Exception as exc:
        add_radar_log(
            f"Не удалось восстановить snapshot: {exc}",
            level="WARN",
            stage="snapshot",
        )
    finally:
        _snapshot_checked = True


def _tracked_creators():
    with db_conn() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT username FROM tracked_creators ORDER BY best_views_per_hour DESC LIMIT 100"
            ).fetchall()
        ]


def _build_sources():
    term = SEARCH_TERMS[0] if SEARCH_TERMS else ""
    sources = {
        "popular": {
            "actor_id": APIFY_SEARCH_ACTOR,
            "input": {
                "search": term,
                "searchType": "popular",
                "searchLimit": SEARCH_LIMIT,
            },
        },
        "hashtags": {
            "actor_id": APIFY_HASHTAG_ACTOR,
            "input": {
                "hashtags": HASHTAGS,
                "resultsType": "reels",
                "resultsLimit": HASHTAG_LIMIT,
            },
        },
    }

    tracked = _tracked_creators()
    if tracked:
        sources["creators"] = {
            "actor_id": APIFY_CREATOR_ACTOR,
            "input": {
                "username": tracked,
                "resultsLimit": 10,
                "onlyPostsNewerThan": "7 days",
                "skipPinnedPosts": True,
                "includeTranscript": False,
                "includeDownloadedVideo": False,
            },
        }

    for source in sources.values():
        source.update(
            run_id="",
            status="NOT_STARTED",
            dataset_id="",
            status_message="",
            started_at="",
        )
    return sources


def _source_summary(job):
    return {
        name: {
            "status": source.get("status"),
            "run_id": source.get("run_id"),
            "dataset_id": source.get("dataset_id"),
        }
        for name, source in (job.get("sources") or {}).items()
    }


def public_job(job=None, busy=False):
    job = job or load_radar_job() or {}
    candidates = job.get("candidates") or []
    ai_done = sum(1 for item in candidates if item.get("ai_done"))
    return {
        "ok": True,
        "runtime": RUNTIME,
        "active": _is_active(job),
        "busy": bool(busy),
        "run_id": job.get("run_id"),
        "phase": job.get("phase") or "idle",
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "sources": _source_summary(job),
        "candidates_total": len(candidates),
        "ai_done": ai_done,
        "ai_total": len(candidates),
        "result": job.get("result") or {},
        "error": job.get("error") or "",
    }


def create_or_resume_job():
    _ensure_snapshot_once()
    existing = load_radar_job()
    if _is_active(existing):
        add_radar_log(
            "Кнопка запуска: активный durable job уже существует, продолжаю его без дубля.",
            stage="launch",
            details={"run_id": existing.get("run_id"), "phase": existing.get("phase")},
        )
        return {
            **public_job(existing),
            "accepted": True,
            "completed": False,
            "resumed": True,
            "message": "Незавершённый поиск найден в Apify KVS и будет продолжен короткими запросами.",
        }, 202

    run_id = _new_run_id()
    token = set_radar_run_id(run_id)
    try:
        job = {
            "version": 5,
            "runtime": RUNTIME,
            "run_id": run_id,
            "phase": "queued",
            "created_at": _now_iso(),
            "sources": _build_sources(),
            "candidates": [],
            "warnings": [],
            "source_failures": {},
            "stats": {},
            "result": {},
            "error": "",
        }
        _persist(job)
        set_radar_status(
            "running",
            "Поиск принят",
            1,
            600,
            "Durable job сохранён в Apify KVS. Дальше каждый короткий запрос двигает поиск на один этап; фонового worker внутри Render больше нет.",
            details={
                "run_id": run_id,
                "runtime": RUNTIME,
                "persistent_job": True,
            },
        )
        add_radar_log(
            "Создан request-driven durable radar job. Background thread не используется.",
            stage="launch",
            details={"run_id": run_id, "sources": list(job["sources"].keys())},
        )
        return {
            **public_job(job),
            "accepted": True,
            "completed": False,
            "resumed": False,
            "message": "Поиск принят. Состояние сохранено в Apify KVS.",
        }, 202
    finally:
        reset_radar_run_id(token)


def _client():
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан APIFY_API_TOKEN")
    return ApifyClient(token)


def _start_one_source(client, job):
    job["phase"] = "starting_sources"
    for name, source in (job.get("sources") or {}).items():
        if source.get("run_id"):
            continue

        job["current_source"] = name
        _persist(job)
        add_radar_log(
            f"Запускаю Apify source {name}: {source.get('actor_id')}",
            stage="apify",
            details={"source": name, "actor": source.get("actor_id")},
        )

        run = client.actor(source["actor_id"]).start(run_input=dict(source.get("input") or {})) or {}
        run_id = run.get("id") or run.get("runId") or ""
        if not run_id:
            raise RuntimeError(f"{source.get('actor_id')}: Apify не вернул runId")

        source["run_id"] = run_id
        source["status"] = str(run.get("status") or "READY").upper()
        source["dataset_id"] = run.get("defaultDatasetId") or run.get("default_dataset_id") or ""
        source["status_message"] = str(run.get("statusMessage") or run.get("status_message") or "")[:500]
        source["started_at"] = _now_iso()
        job["current_source"] = ""

        if all(x.get("run_id") for x in (job.get("sources") or {}).values()):
            job["phase"] = "discovering"
        _persist(job)

        started = sum(1 for x in job["sources"].values() if x.get("run_id"))
        total = len(job["sources"])
        set_radar_status(
            "running",
            "Запускаю источники Apify",
            4 + int(8 * started / max(1, total)),
            300,
            f"Запущено источников: {started}/{total}. Run ID сразу сохранён в KVS.",
            details={"run_id": job.get("run_id"), "sources_started": started, "sources_total": total},
        )
        return job

    job["phase"] = "discovering"
    _persist(job)
    return job


def _poll_sources(client, job):
    sources = job.get("sources") or {}
    completed = 0
    failures = {}

    for name, source in sources.items():
        run_id = source.get("run_id")
        if not run_id:
            continue
        previous = str(source.get("status") or "")
        info = client.run(run_id).get() or {}
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
        if status in TERMINAL_SOURCE_STATUSES:
            completed += 1
            if status != "SUCCEEDED":
                failures[name] = source.get("status_message") or status

    job["source_failures"] = failures
    job["phase"] = "preparing" if completed >= len(sources) else "discovering"
    _persist(job)

    set_radar_status(
        "running",
        "Apify собирает Reels",
        12 + int(18 * completed / max(1, len(sources))),
        240,
        f"Готово источников: {completed}/{len(sources)}. Render ничего не держит в фоне — следующий запрос продолжит отсюда.",
        warning=(f"Источников с ошибкой: {len(failures)}" if failures else ""),
        details={
            "run_id": job.get("run_id"),
            "sources_done": completed,
            "sources_total": len(sources),
            "runtime": RUNTIME,
        },
    )
    return job


def _collect_source_rows(client, job):
    results = {}
    failures = dict(job.get("source_failures") or {})
    for name, source in (job.get("sources") or {}).items():
        if str(source.get("status") or "").upper() != "SUCCEEDED":
            results[name] = []
            continue
        dataset_id = source.get("dataset_id")
        if not dataset_id:
            info = client.run(source.get("run_id")).get() or {}
            dataset_id = info.get("defaultDatasetId") or info.get("default_dataset_id") or ""
            source["dataset_id"] = dataset_id
        if not dataset_id:
            failures[name] = "Apify run завершён без defaultDatasetId"
            results[name] = []
            continue
        rows = list(client.dataset(dataset_id).iterate_items())
        results[name] = rows
        add_radar_log(
            f"Dataset {name}: {len(rows)} элементов.",
            stage="apify",
            details={"source": name, "dataset_id": dataset_id, "items": len(rows)},
        )
    job["source_failures"] = failures
    return results


def _prepare_candidates(client, job):
    source_results = _collect_source_rows(client, job)
    creator_rows = source_results.get("creators") or []
    if creator_rows:
        try:
            with db_conn() as conn:
                update_creator_baselines(conn, creator_rows)
        except Exception as exc:
            job.setdefault("warnings", []).append(f"baseline: {str(exc)[:180]}")

    raw_items = []
    for row in source_results.get("popular") or []:
        row.setdefault("searchTerm", row.get("searchTerm") or "ключевой поиск")
        raw_items.append((row, "Popular Reels"))
    for row in source_results.get("hashtags") or []:
        raw_items.append((row, f"хештег: {row.get('hashtag') or row.get('searchTerm') or ''}"))
    raw_items.extend((row, "наблюдаемый автор") for row in creator_rows)

    with db_conn() as conn:
        creator_stats = load_creator_stats(conn)

    unique = {}
    rejected = 0
    for raw, source in raw_items:
        item = normalize_reel(raw, source, creator_stats)
        if not item:
            rejected += 1
            continue
        current = unique.get(item["post_url"])
        if current is None or item["viral_score_v2"] > current["viral_score_v2"]:
            unique[item["post_url"]] = item

    candidates = sorted(
        unique.values(),
        key=lambda x: (x["viral_score_v2"], x["views_per_hour"], x["views"]),
        reverse=True,
    )[:RADAR_AI_ANALYZE_LIMIT]

    with db_conn() as conn:
        for item in candidates:
            previous = conn.execute(
                "SELECT ai_checked,ai_match FROM radar_posts WHERE post_url=?",
                (item["post_url"],),
            ).fetchone()
            item["ai_done"] = bool(previous and int(previous["ai_checked"] or 0) == 1)
            item["ai_match"] = bool(previous and int(previous["ai_match"] or 0) == 1)
            item["ai_attempts"] = 0
            item["ai_error"] = ""
            save_post_preserve_ai(conn, item, None)
        conn.commit()

    job["candidates"] = candidates
    job["stats"] = {
        "raw": len(raw_items),
        "rejected_or_invalid": rejected,
        "numeric_candidates": len(unique),
        "ai_total": len(candidates),
    }
    job["phase"] = "ai" if candidates else "finalizing"
    _persist(job)

    try:
        save_radar_snapshot()
    except Exception as exc:
        add_radar_log(f"Snapshot кандидатов не сохранён: {exc}", level="WARN", stage="snapshot")

    cached = sum(1 for item in candidates if item.get("ai_done"))
    add_radar_log(
        "Числовой фильтр завершён; очередь Gemini сохранена в durable job.",
        stage="filter",
        details={
            "raw": len(raw_items),
            "unique": len(unique),
            "ai_total": len(candidates),
            "already_checked": cached,
        },
    )
    set_radar_status(
        "running",
        "Кандидаты готовы",
        36,
        max(60, max(0, len(candidates) - cached) * 14),
        f"Получено {len(raw_items)} записей → {len(unique)} сильных ≤10 сек → Gemini очередь {len(candidates)}. Уже проверено ранее: {cached}.",
        warning=" · ".join(job.get("warnings") or []),
        details={
            "raw": len(raw_items),
            "numeric_candidates": len(unique),
            "ai_total": len(candidates),
            "ai_done": cached,
            "run_id": job.get("run_id"),
        },
    )
    return job


def _next_ai_index(job):
    for index, item in enumerate(job.get("candidates") or []):
        if not item.get("ai_done"):
            return index
    return None


def _process_one_ai(job):
    index = _next_ai_index(job)
    candidates = job.get("candidates") or []
    if index is None:
        job["phase"] = "finalizing"
        _persist(job)
        return job

    item = candidates[index]
    item["ai_attempts"] = int(item.get("ai_attempts") or 0) + 1
    job["current_ai_index"] = index
    job["current_ai_post_url"] = item.get("post_url")
    _persist(job)

    done_before = sum(1 for x in candidates if x.get("ai_done"))
    creator = item.get("creator", "")
    set_radar_status(
        "running",
        f"Gemini проверяет {done_before + 1}/{len(candidates)}",
        40 + int(44 * done_before / max(1, len(candidates))),
        max(30, (len(candidates) - done_before) * 14),
        f"Сейчас: @{creator}. Один Reel обрабатывается одним HTTP-запросом; если Render заменит instance, этот Reel просто повторится на следующем запросе.",
        details={
            "raw": (job.get("stats") or {}).get("raw", 0),
            "numeric_candidates": (job.get("stats") or {}).get("numeric_candidates", 0),
            "ai_total": len(candidates),
            "ai_done": done_before,
            "run_id": job.get("run_id"),
        },
    )
    add_radar_log(
        f"AI tick {done_before + 1}/{len(candidates)}: @{creator}, попытка {item['ai_attempts']}.",
        stage="gemini-radar",
        details={"post_url": item.get("post_url"), "views": item.get("views")},
    )

    tmp = None
    try:
        from gemini_service import classify_radar_video

        tmp, refreshed_duration = download_reel_for_analysis(item)
        if refreshed_duration and 0 < float(refreshed_duration) <= 10.05:
            item["duration_sec"] = float(refreshed_duration)
        assessment = classify_radar_video(tmp, item.get("caption") or "")
        passed = matches(assessment)
        with db_conn() as conn:
            save_post_preserve_ai(conn, item, assessment)
            conn.commit()
        item["ai_done"] = True
        item["ai_match"] = bool(passed)
        item["ai_error"] = ""
        item["assessment"] = assessment.model_dump()
        add_radar_log(
            f"AI {'PASS' if passed else 'REJECT'} @{creator}: {str(getattr(assessment, 'reason', '') or '')[:280]}",
            level="INFO" if passed else "WARN",
            stage="gemini-radar",
        )
    except Exception as exc:
        item["ai_error"] = str(exc)[:700]
        add_radar_log(
            f"AI ERROR @{creator}, попытка {item['ai_attempts']}: {exc}",
            level="ERROR",
            stage="gemini-radar",
        )
        if item["ai_attempts"] >= 3:
            item["ai_done"] = True
            item["ai_match"] = False
            try:
                with db_conn() as conn:
                    save_post_preserve_ai(conn, item, None)
                    conn.commit()
            except Exception:
                pass
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    if _next_ai_index(job) is None:
        job["phase"] = "finalizing"
    _persist(job)
    try:
        save_radar_snapshot()
    except Exception as exc:
        add_radar_log(f"Checkpoint snapshot не сохранён: {exc}", level="WARN", stage="snapshot")

    done_now = sum(1 for x in candidates if x.get("ai_done"))
    matched_now = sum(1 for x in candidates if x.get("ai_done") and x.get("ai_match"))
    set_radar_status(
        "running",
        "Gemini проверяет видео" if job["phase"] == "ai" else "Gemini закончил проверку",
        40 + int(44 * done_now / max(1, len(candidates))),
        max(20, (len(candidates) - done_now) * 14),
        f"Проверено {done_now}/{len(candidates)}. Подошло: {matched_now}. Прогресс сохранён в KVS и snapshot.",
        details={"ai_total": len(candidates), "ai_done": done_now, "matched": matched_now, "run_id": job.get("run_id")},
    )
    return job


def _finalize(job):
    candidates = job.get("candidates") or []
    set_radar_status(
        "running",
        "Формирую итоговый TOP",
        92,
        45,
        "Пересчитываю качество, аномалии и мету недели. Это последний короткий этап.",
        details={"ai_total": len(candidates), "ai_done": sum(1 for x in candidates if x.get("ai_done")), "run_id": job.get("run_id")},
    )

    with db_conn() as conn:
        refresh_recent_scores_quality(conn)
        rows = conn.execute(
            """SELECT * FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-7 days') AND ai_match=1
               ORDER BY viral_score_v2 DESC,views_per_hour DESC,views DESC
               LIMIT 120"""
        ).fetchall()
    top_rows = [dict(row) for row in rows if top_eligible(dict(row))][:RADAR_KEEP_LIMIT]

    meta_error = ""
    if top_rows:
        try:
            with db_conn() as conn:
                save_meta_report_quality(conn, top_rows)
                conn.commit()
        except Exception as exc:
            meta_error = str(exc)[:300]
            add_radar_log(f"Мета недели не собрана: {exc}", level="WARN", stage="meta")

    try:
        save_radar_snapshot()
    except Exception as exc:
        add_radar_log(f"Финальный snapshot не сохранён: {exc}", level="WARN", stage="snapshot")

    done = sum(1 for item in candidates if item.get("ai_done"))
    matched = sum(1 for item in candidates if item.get("ai_done") and item.get("ai_match"))
    errors = sum(1 for item in candidates if item.get("ai_error"))
    stats = job.get("stats") or {}
    result = {
        "raw": stats.get("raw", 0),
        "after_numeric_filter": stats.get("numeric_candidates", 0),
        "ai_checked": done,
        "matched": matched,
        "errors": errors,
        "source_errors": len(job.get("source_failures") or {}),
        "kept": len(top_rows),
        "meta_error": meta_error,
    }
    job["phase"] = "done"
    job["completed_at"] = _now_iso()
    job["result"] = result
    job["error"] = ""
    job["current_ai_index"] = None
    job["current_ai_post_url"] = ""
    _persist(job)

    set_radar_status(
        "done",
        "Поиск завершён",
        100,
        0,
        f"Собрано {result['raw']} → после фильтра {result['after_numeric_filter']} → Gemini {done} → в TOP {len(top_rows)}.",
        warning=(f"Мета: {meta_error}" if meta_error else ""),
        details=result,
    )
    add_radar_log("Request-driven radar DONE.", stage="done", details=result)
    return job


def _advance(job):
    phase = str(job.get("phase") or "")
    client = None

    if phase in {"queued", "starting_sources"}:
        client = _client()
        return _start_one_source(client, job)
    if phase == "discovering":
        client = _client()
        return _poll_sources(client, job)
    if phase == "preparing":
        client = _client()
        return _prepare_candidates(client, job)
    if phase == "ai":
        return _process_one_ai(job)
    if phase == "finalizing":
        return _finalize(job)
    return job


def tick_job():
    _ensure_snapshot_once()
    if not _tick_lock.acquire(blocking=False):
        job = load_radar_job()
        return {**public_job(job, busy=True), "message": "Предыдущий короткий шаг ещё выполняется."}, 202

    job = None
    token = None
    try:
        job = load_radar_job()
        if not job:
            return {**public_job({}), "message": "Durable job пока не создан."}, 200
        if not _is_active(job):
            return {**public_job(job), "message": "Активного поиска нет."}, 200

        token = set_radar_run_id(job.get("run_id"))
        started = time.monotonic()
        add_radar_log(
            f"TICK START phase={job.get('phase')}",
            stage="tick",
            details={"run_id": job.get("run_id"), "phase": job.get("phase")},
        )
        job = _advance(job)
        elapsed = round(time.monotonic() - started, 2)
        add_radar_log(
            f"TICK DONE phase={job.get('phase')} за {elapsed} сек.",
            stage="tick",
            details={"run_id": job.get("run_id"), "phase": job.get("phase"), "elapsed_seconds": elapsed},
        )
        return {**public_job(job), "message": "Шаг выполнен."}, 200
    except Exception as exc:
        add_radar_log(f"TICK ERROR: {exc}", level="ERROR", stage="tick")
        try:
            if job and _is_active(job):
                job["last_error"] = str(exc)[:1200]
                job["last_error_at"] = _now_iso()
                _persist(job)
        except Exception:
            pass
        set_radar_status(
            "running" if job and _is_active(job) else "error",
            "Временная ошибка шага" if job and _is_active(job) else "Поиск остановлен",
            1,
            60,
            f"{str(exc)[:300]}. Следующий tick попробует продолжить с сохранённого этапа.",
            warning=str(exc)[:300],
            details={"run_id": (job or {}).get("run_id"), "runtime": RUNTIME},
        )
        return {
            **public_job(job or {}),
            "transient_error": True,
            "message": str(exc)[:500],
        }, 200
    finally:
        if token is not None:
            reset_radar_run_id(token)
        _tick_lock.release()
