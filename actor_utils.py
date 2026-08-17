import os
import time

from radar_logs import add_radar_log

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
POLL_SECONDS = 10


def _max_wait_seconds(actor_id):
    # Popular search is only a discovery source and has a fallback to hashtags,
    # so it must never block the whole radar forever. The heavier hashtag and
    # creator runs get more time.
    actor = str(actor_id or "").lower()
    if "search-scraper" in actor:
        return int(os.environ.get("APIFY_SEARCH_MAX_WAIT_SECONDS", "240"))
    if "hashtag" in actor:
        return int(os.environ.get("APIFY_HASHTAG_MAX_WAIT_SECONDS", "720"))
    return int(os.environ.get("APIFY_ACTOR_MAX_WAIT_SECONDS", "600"))


def run_actor_items_checked(client, actor_id, run_input):
    safe_input = dict(run_input or {})
    safe_details = {k: v for k, v in safe_input.items() if k not in {"token", "apiToken"}}
    add_radar_log(
        f"Запускаю Apify Actor {actor_id}",
        stage="apify",
        details=safe_details,
    )

    try:
        # Do not use ActorClient.call() here: it blocks until completion and leaves
        # the user with a silent log. start() returns the run immediately, then we
        # poll it so the website can show a heartbeat and exact runId/status.
        run = client.actor(actor_id).start(run_input=run_input)
    except Exception as exc:
        add_radar_log(f"Apify Actor {actor_id} не запустился: {exc}", level="ERROR", stage="apify")
        raise

    if not run:
        message = f"{actor_id}: Apify не вернул данные запуска"
        add_radar_log(message, level="ERROR", stage="apify")
        raise RuntimeError(message)

    run_id = run.get("id") or run.get("runId") or ""
    if not run_id:
        message = f"{actor_id}: Apify не вернул runId"
        add_radar_log(message, level="ERROR", stage="apify")
        raise RuntimeError(message)

    add_radar_log(
        f"Apify Actor принят платформой: runId {run_id}",
        stage="apify",
        details={"actor": actor_id, "run_id": run_id},
    )

    run_client = client.run(run_id)
    started = time.monotonic()
    max_wait = _max_wait_seconds(actor_id)
    last_status = ""
    last_message = ""
    last_heartbeat_bucket = -1
    final_run = run

    while True:
        try:
            current = run_client.get() or final_run or {}
        except Exception as exc:
            elapsed = int(time.monotonic() - started)
            add_radar_log(
                f"Не удалось получить статус runId {run_id}: {exc}. Повторю через {POLL_SECONDS} сек.",
                level="WARN",
                stage="apify",
                details={"actor": actor_id, "run_id": run_id, "elapsed_seconds": elapsed},
            )
            if elapsed >= max_wait:
                raise RuntimeError(f"{actor_id}: не удалось дождаться статуса Apify за {max_wait} сек") from exc
            time.sleep(POLL_SECONDS)
            continue

        final_run = current
        status = str(current.get("status") or "UNKNOWN").upper()
        status_message = str(current.get("statusMessage") or current.get("status_message") or "").strip()
        elapsed = int(time.monotonic() - started)
        heartbeat_bucket = elapsed // 20

        # Log immediately on status/message changes, otherwise one heartbeat every ~20 sec.
        if status != last_status or status_message != last_message or heartbeat_bucket != last_heartbeat_bucket:
            text = f"Apify {actor_id}: {status} · {elapsed} сек"
            if status_message:
                text += f" · {status_message}"
            add_radar_log(
                text,
                stage="apify",
                level="INFO" if status in {"READY", "RUNNING", "SUCCEEDED", "UNKNOWN"} else "WARN",
                details={
                    "actor": actor_id,
                    "run_id": run_id,
                    "status": status,
                    "elapsed_seconds": elapsed,
                    "max_wait_seconds": max_wait,
                },
            )
            last_status = status
            last_message = status_message
            last_heartbeat_bucket = heartbeat_bucket

        if status in TERMINAL_STATUSES:
            break

        if elapsed >= max_wait:
            try:
                run_client.abort(gracefully=True)
            except Exception:
                pass
            message = f"{actor_id}: превышено время ожидания {max_wait} сек, runId {run_id} остановлен"
            add_radar_log(message, level="WARN", stage="apify")
            raise RuntimeError(message)

        time.sleep(POLL_SECONDS)

    status = str(final_run.get("status") or "").upper()
    if status != "SUCCEEDED":
        message = final_run.get("statusMessage") or final_run.get("status_message") or status
        add_radar_log(
            f"Apify Actor {actor_id} завершился {status}: {message}",
            level="WARN",
            stage="apify",
            details={"run_id": run_id},
        )
        raise RuntimeError(f"{actor_id}: {message}")

    dataset_id = final_run.get("defaultDatasetId") or final_run.get("default_dataset_id")
    if not dataset_id:
        add_radar_log(
            f"Apify Actor {actor_id} завершён, но dataset отсутствует.",
            level="WARN",
            stage="apify",
            details={"run_id": run_id},
        )
        return []

    items = list(client.dataset(dataset_id).iterate_items())
    add_radar_log(
        f"Apify Actor {actor_id} успешно завершён: {len(items)} элементов.",
        stage="apify",
        details={"run_id": run_id, "dataset_id": dataset_id, "items": len(items)},
    )
    return items
