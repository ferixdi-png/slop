import os
import time
import uuid
from datetime import datetime, timezone
from threading import Lock, Thread

from cloud_state import load_radar_job, save_radar_job
from config import (
    APIFY_CREATOR_ACTOR,
    APIFY_HASHTAG_ACTOR,
    APIFY_SEARCH_ACTOR,
    HASHTAGS,
    HASHTAG_LIMIT,
    SEARCH_LIMIT,
    SEARCH_TERMS,
)
from db import db_conn
from progress import get_radar_status, set_radar_status
from radar_logs import add_radar_log, reset_radar_run_id, set_radar_run_id

ACTIVE_PHASES = {"queued", "starting_sources", "discovering", "processing"}
TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}

_run_lock = Lock()
_spawn_lock = Lock()
_state_lock = Lock()
_state = {"run_id": None, "started_at": None}


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _new_run_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


def _set_active(run_id):
    with _state_lock:
        _state["run_id"] = str(run_id or "") or None
        _state["started_at"] = _now_iso()


def _clear_active(run_id):
    with _state_lock:
        if _state.get("run_id") == run_id:
            _state["run_id"] = None
            _state["started_at"] = None


def runtime_state():
    with _state_lock:
        run_id = _state.get("run_id")
        return {
            "run_id": run_id,
            "started_at": _state.get("started_at"),
            "worker_active": _run_lock.locked(),
            "worker_pending": bool(run_id) and not _run_lock.locked(),
            "server_pid": os.getpid(),
            "runtime": "persistent_apify_job_v3",
        }


def _is_active_job(job):
    return bool(job) and str(job.get("phase") or "") in ACTIVE_PHASES


def _persist(job):
    job["updated_by_commit"] = str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12]
    job["updated_by_instance"] = os.environ.get("RENDER_INSTANCE_ID", "")
    return save_radar_job(job)


def _tracked_creators():
    with db_conn() as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT username FROM tracked_creators "
                "ORDER BY best_views_per_hour DESC LIMIT 100"
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
            started_at="",
            status_message="",
        )
    return sources


def _parse_iso(value):
    try:
        dt = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _elapsed(source):
    started = _parse_iso(source.get("started_at"))
    if not started:
        return 0
    return max(0, int((datetime.now(timezone.utc) - started).total_seconds()))


def _start_missing_sources(client, job):
    job["phase"] = "starting_sources"
    _persist(job)

    for name, source in (job.get("sources") or {}).items():
        if source.get("run_id"):
            add_radar_log(
                f"Source {name} уже имеет сохранённый runId {source.get('run_id')} — повторно не запускаю.",
                stage="resume",
            )
            continue

        actor_id = source.get("actor_id")
        run_input = dict(source.get("input") or {})

        # The job record exists before the external call. A Render replacement can
        # no longer erase the fact that this radar run exists.
        job["current_source"] = name
        _persist(job)
        add_radar_log(
            f"Запускаю Apify source {name}: {actor_id}",
            stage="apify",
            details={"source": name, "actor": actor_id},
        )

        run = client.actor(actor_id).start(run_input=run_input) or {}
        run_id = run.get("id") or run.get("runId") or ""
        if not run_id:
            raise RuntimeError(f"{actor_id}: Apify не вернул runId")

        source["run_id"] = run_id
        source["status"] = str(run.get("status") or "READY").upper()
        source["dataset_id"] = (
            run.get("defaultDatasetId")
            or run.get("default_dataset_id")
            or ""
        )
        source["started_at"] = _now_iso()
        source["status_message"] = str(
            run.get("statusMessage") or run.get("status_message") or ""
        )[:500]
        job["phase"] = "discovering"
        job["current_source"] = ""
        _persist(job)

        add_radar_log(
            f"Apify source {name} принят: runId {run_id}",
            stage="apify",
            details={"source": name, "actor": actor_id, "run_id": run_id},
        )


def _poll_sources(client, job):
    sources = job.get("sources") or {}
    max_wait = int(os.environ.get("RADAR_DISCOVERY_MAX_WAIT_SECONDS", "900"))
    last_persist = 0.0

    while True:
        completed = 0
        failed = 0
        changed = False

        for name, source in sources.items():
            run_id = source.get("run_id")
            if not run_id:
                continue

            previous = str(source.get("status") or "")
            try:
                current = client.run(run_id).get() or {}
                status = str(current.get("status") or previous or "UNKNOWN").upper()
                source["status"] = status
                source["dataset_id"] = (
                    current.get("defaultDatasetId")
                    or current.get("default_dataset_id")
                    or source.get("dataset_id")
                    or ""
                )
                source["status_message"] = str(
                    current.get("statusMessage")
                    or current.get("status_message")
                    or ""
                )[:500]
            except Exception as exc:
                source["status_message"] = f"poll error: {exc}"[:500]
                add_radar_log(
                    f"Не удалось опросить source {name} runId {run_id}: {exc}",
                    level="WARN",
                    stage="apify",
                )

            if source.get("status") != previous:
                changed = True
                add_radar_log(
                    f"Apify source {name}: {source.get('status')}",
                    stage="apify",
                    details={
                        "source": name,
                        "run_id": run_id,
                        "status": source.get("status"),
                        "elapsed_seconds": _elapsed(source),
                    },
                )

            if source.get("status") in TERMINAL_STATUSES:
                completed += 1
                if source.get("status") != "SUCCEEDED":
                    failed += 1
            elif _elapsed(source) > max_wait:
                try:
                    client.run(run_id).abort(gracefully=True)
                except Exception:
                    pass
                source["status"] = "TIMED-OUT"
                source["status_message"] = f"local max wait {max_wait}s"
                completed += 1
                failed += 1
                changed = True

        now = time.monotonic()
        if changed or now - last_persist >= 20:
            job["phase"] = "discovering"
            job["sources_completed"] = completed
            job["sources_total"] = len(sources)
            _persist(job)
            last_persist = now

        progress = 10 + int(20 * completed / max(1, len(sources)))
        set_radar_status(
            "running",
            "Apify собирает источники",
            progress,
            max(20, 240 - completed * 60),
            f"Готово источников: {completed}/{len(sources)}. "
            "RunId сохранены в Apify KVS — рестарт Render не потеряет поиск.",
            warning="" if not failed else f"Источников с ошибкой: {failed}",
            details={
                "run_id": job.get("run_id"),
                "persistent_job": True,
                "sources_total": len(sources),
                "sources_done": completed,
            },
        )

        if completed >= len(sources):
            _persist(job)
            return
        time.sleep(8)


def _collect_results(client, job):
    results = {}
    failures = {}

    for name, source in (job.get("sources") or {}).items():
        status = str(source.get("status") or "").upper()
        if status != "SUCCEEDED":
            failures[name] = source.get("status_message") or status or "source failed"
            results[name] = []
            continue

        dataset_id = source.get("dataset_id")
        if not dataset_id:
            info = client.run(source.get("run_id")).get() or {}
            dataset_id = (
                info.get("defaultDatasetId")
                or info.get("default_dataset_id")
                or ""
            )
            source["dataset_id"] = dataset_id
            _persist(job)

        if not dataset_id:
            failures[name] = "Apify run завершён без defaultDatasetId"
            results[name] = []
            continue

        rows = list(client.dataset(dataset_id).iterate_items())
        results[name] = rows
        add_radar_log(
            f"Dataset source {name} загружен: {len(rows)} элементов.",
            stage="apify",
            details={
                "source": name,
                "run_id": source.get("run_id"),
                "dataset_id": dataset_id,
                "items": len(rows),
            },
        )

    return results, failures


def _process_saved_results(job, source_results, source_failures):
    import radar_service_v2 as radar_pipeline
    from radar_entry import sync_radar

    original_group = radar_pipeline.run_actor_group_checked

    def saved_group(_client, _specs):
        return source_results, source_failures

    radar_pipeline.run_actor_group_checked = saved_group
    try:
        job["phase"] = "processing"
        job["source_counts"] = {
            name: len(rows or []) for name, rows in source_results.items()
        }
        _persist(job)
        set_radar_status(
            "running",
            "Источники готовы — фильтрую и проверяю видео",
            32,
            520,
            "Apify уже закончил сбор. Дальше: ≤10 секунд → Viral Score 2.0 → Gemini → TOP.",
            details={
                "run_id": job.get("run_id"),
                "persistent_job": True,
                "source_counts": job.get("source_counts"),
            },
        )
        return sync_radar()
    finally:
        radar_pipeline.run_actor_group_checked = original_group


def _worker(run_id):
    context_token = set_radar_run_id(run_id)
    acquired = _run_lock.acquire(blocking=False)
    if not acquired:
        reset_radar_run_id(context_token)
        return

    _set_active(run_id)
    try:
        from apify_client import ApifyClient

        token = os.environ.get("APIFY_API_TOKEN", "").strip()
        if not token:
            raise RuntimeError("Не задан APIFY_API_TOKEN")

        job = load_radar_job()
        if not job or job.get("run_id") != run_id:
            raise RuntimeError("Persistent radar job не найден в Apify KVS")

        add_radar_log(
            "Persistent radar worker START.",
            stage="worker",
            details={
                "phase": job.get("phase"),
                "saved_commit": job.get("updated_by_commit"),
                "current_commit": str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12],
                "sources_with_run_id": sum(
                    1
                    for source in (job.get("sources") or {}).values()
                    if source.get("run_id")
                ),
            },
        )

        client = ApifyClient(token)
        _start_missing_sources(client, job)
        _poll_sources(client, job)
        source_results, source_failures = _collect_results(client, job)

        if not any(source_results.values()):
            details = "; ".join(
                f"{name}: {text}" for name, text in source_failures.items()
            )
            raise RuntimeError(f"Все Apify-источники завершились без данных: {details}")

        result = _process_saved_results(job, source_results, source_failures)
        job["phase"] = "done"
        job["completed_at"] = _now_iso()
        job["result"] = result
        job["error"] = ""
        _persist(job)
        add_radar_log("Persistent radar worker DONE.", stage="done", details=result)
    except Exception as exc:
        add_radar_log(
            f"Persistent radar worker ERROR: {exc}",
            level="ERROR",
            stage="worker",
        )
        try:
            job = load_radar_job() or {"run_id": run_id}
            if job.get("run_id") == run_id:
                job["phase"] = "error"
                job["error"] = str(exc)[:1200]
                job["failed_at"] = _now_iso()
                _persist(job)
        except Exception as persist_exc:
            add_radar_log(
                f"Не удалось сохранить ошибку persistent job: {persist_exc}",
                level="ERROR",
                stage="worker",
            )
        try:
            set_radar_status(
                "error",
                "Поиск остановлен",
                0,
                None,
                str(exc)[:300],
                details={
                    "run_id": run_id,
                    "persistent_job": True,
                    "render_commit": str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12],
                },
            )
        except Exception:
            pass
    finally:
        _clear_active(run_id)
        if _run_lock.locked():
            _run_lock.release()
        add_radar_log(
            "Persistent radar worker завершил локальный цикл; состояние сохранено в Apify KVS.",
            stage="worker",
        )
        reset_radar_run_id(context_token)


def ensure_worker(job):
    if not _is_active_job(job):
        return False
    run_id = str(job.get("run_id") or "")
    if not run_id:
        return False

    with _spawn_lock:
        current = runtime_state()
        if current.get("worker_active") or current.get("worker_pending"):
            return current.get("run_id") == run_id

        _set_active(run_id)
        try:
            Thread(
                target=_worker,
                args=(run_id,),
                name=f"radar-{run_id[-6:]}",
                daemon=True,
            ).start()
        except Exception:
            _clear_active(run_id)
            raise
    return True


def resume_if_needed():
    job = load_radar_job()
    if not _is_active_job(job):
        return job

    add_radar_log(
        "Найден незавершённый radar job — автоматически продолжаю после рестарта Render.",
        stage="resume",
        details={
            "run_id": job.get("run_id"),
            "phase": job.get("phase"),
            "sources": {
                name: {
                    "run_id": source.get("run_id"),
                    "status": source.get("status"),
                }
                for name, source in (job.get("sources") or {}).items()
            },
        },
    )
    old = get_radar_status()
    set_radar_status(
        "running",
        "Возобновляю поиск после рестарта",
        max(2, int(old.get("progress") or 2)),
        420,
        "Незавершённый поиск найден в Apify KVS. Продолжаю существующие runId; повторный запуск не нужен.",
        details={
            "run_id": job.get("run_id"),
            "persistent_job": True,
            "resumed_after_restart": True,
        },
    )
    ensure_worker(job)
    return job


def start_or_resume():
    existing = load_radar_job()
    if _is_active_job(existing):
        ensure_worker(existing)
        return {
            "ok": True,
            "accepted": True,
            "completed": False,
            "resumed": True,
            "run_id": existing.get("run_id"),
            "phase": existing.get("phase"),
            "message": "Незавершённый поиск уже существует и продолжен.",
        }, 202

    run_id = _new_run_id()
    context_token = set_radar_run_id(run_id)
    try:
        job = {
            "version": 3,
            "run_id": run_id,
            "phase": "queued",
            "created_at": _now_iso(),
            "sources": _build_sources(),
            "result": {},
            "error": "",
        }
        _persist(job)
        set_radar_status(
            "running",
            "Запускаю устойчивый радар",
            1,
            720,
            "Job сохранён в Apify KVS. HTTP-запрос сейчас закончится, а worker продолжит работу отдельно и восстановится после рестарта Render.",
            details={
                "run_id": run_id,
                "mode": "persistent_apify_job_v3",
                "persistent_job": True,
                "render_commit": str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12],
            },
        )
        add_radar_log(
            "Создан persistent radar job; запуск больше не зависит от жизни HTTP-запроса.",
            stage="launch",
            details={"run_id": run_id, "sources": list(job["sources"].keys())},
        )
        ensure_worker(job)
        return {
            "ok": True,
            "accepted": True,
            "completed": False,
            "resumed": False,
            "run_id": run_id,
            "phase": "queued",
            "message": "Радар запущен в persistent worker.",
        }, 202
    finally:
        reset_radar_run_id(context_token)
