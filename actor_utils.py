import os
import time

from radar_logs import add_radar_log

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
POLL_SECONDS = 10


def _max_wait_seconds(actor_id):
    actor = str(actor_id or "").lower()
    if "search-scraper" in actor:
        return int(os.environ.get("APIFY_SEARCH_MAX_WAIT_SECONDS", "90"))
    if "hashtag" in actor:
        return int(os.environ.get("APIFY_HASHTAG_MAX_WAIT_SECONDS", "240"))
    return int(os.environ.get("APIFY_ACTOR_MAX_WAIT_SECONDS", "180"))


def _safe_input(run_input):
    return {
        k: v for k, v in dict(run_input or {}).items()
        if k not in {"token", "apiToken"}
    }


def _start_actor(client, actor_id, run_input):
    add_radar_log(
        f"Запускаю Apify Actor {actor_id}",
        stage="apify",
        details=_safe_input(run_input),
    )
    try:
        run = client.actor(actor_id).start(run_input=run_input)
    except Exception as exc:
        add_radar_log(
            f"Apify Actor {actor_id} не запустился: {exc}",
            level="ERROR",
            stage="apify",
        )
        raise

    if not run:
        raise RuntimeError(f"{actor_id}: Apify не вернул данные запуска")

    run_id = run.get("id") or run.get("runId") or ""
    if not run_id:
        raise RuntimeError(f"{actor_id}: Apify не вернул runId")

    add_radar_log(
        f"Apify Actor принят платформой: runId {run_id}",
        stage="apify",
        details={"actor": actor_id, "run_id": run_id},
    )
    return {
        "actor_id": actor_id,
        "run_id": run_id,
        "run_client": client.run(run_id),
        "started": time.monotonic(),
        "max_wait": _max_wait_seconds(actor_id),
        "final_run": run,
        "last_status": "",
        "last_message": "",
        "last_heartbeat_bucket": -1,
    }


def _poll_one(state):
    actor_id = state["actor_id"]
    run_id = state["run_id"]
    elapsed = int(time.monotonic() - state["started"])

    try:
        current = state["run_client"].get() or state["final_run"] or {}
    except Exception as exc:
        add_radar_log(
            f"Не удалось получить статус runId {run_id}: {exc}. Повторю.",
            level="WARN",
            stage="apify",
            details={"actor": actor_id, "run_id": run_id, "elapsed_seconds": elapsed},
        )
        if elapsed >= state["max_wait"]:
            raise RuntimeError(
                f"{actor_id}: не удалось дождаться статуса Apify за {state['max_wait']} сек"
            ) from exc
        return None

    state["final_run"] = current
    status = str(current.get("status") or "UNKNOWN").upper()
    status_message = str(
        current.get("statusMessage") or current.get("status_message") or ""
    ).strip()
    heartbeat_bucket = elapsed // 20

    if (
        status != state["last_status"]
        or status_message != state["last_message"]
        or heartbeat_bucket != state["last_heartbeat_bucket"]
    ):
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
                "max_wait_seconds": state["max_wait"],
            },
        )
        state["last_status"] = status
        state["last_message"] = status_message
        state["last_heartbeat_bucket"] = heartbeat_bucket

    if status in TERMINAL_STATUSES:
        return current

    if elapsed >= state["max_wait"]:
        try:
            state["run_client"].abort(gracefully=True)
        except Exception:
            pass
        raise RuntimeError(
            f"{actor_id}: превышено время ожидания {state['max_wait']} сек, runId {run_id} остановлен"
        )
    return None


def _items_from_success(client, state, final_run):
    actor_id = state["actor_id"]
    run_id = state["run_id"]
    status = str(final_run.get("status") or "").upper()
    if status != "SUCCEEDED":
        message = final_run.get("statusMessage") or final_run.get("status_message") or status
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

    # Materialize only after the remote run has completed. Actor execution itself
    # happens on Apify; Render keeps just one lightweight polling loop in memory.
    items = list(client.dataset(dataset_id).iterate_items())
    add_radar_log(
        f"Apify Actor {actor_id} успешно завершён: {len(items)} элементов.",
        stage="apify",
        details={"run_id": run_id, "dataset_id": dataset_id, "items": len(items)},
    )
    return items


def run_actor_group_checked(client, source_specs):
    """Start multiple Actors on Apify, poll them from ONE local thread.

    The Actors still execute concurrently on Apify, but the Render Starter process
    does not create multiple ApifyClient/Python worker threads. Returns
    (results_by_name, errors_by_name); one failed source never kills the others.
    """
    states = {}
    results = {}
    errors = {}

    # Start calls are deliberately sequential and short. Once started, all Actor
    # runs execute concurrently on Apify infrastructure.
    for name, spec in dict(source_specs or {}).items():
        actor_id, run_input = spec
        try:
            states[name] = _start_actor(client, actor_id, run_input)
        except Exception as exc:
            errors[name] = str(exc)
            results[name] = []

    pending = set(states.keys())
    while pending:
        finished_this_round = []
        for name in list(pending):
            state = states[name]
            try:
                final_run = _poll_one(state)
                if final_run is None:
                    continue
                try:
                    results[name] = _items_from_success(client, state, final_run)
                except Exception as exc:
                    errors[name] = str(exc)
                    results[name] = []
                    add_radar_log(
                        f"Источник {name} завершился с ошибкой: {exc}",
                        level="WARN",
                        stage="apify",
                    )
                finished_this_round.append(name)
            except Exception as exc:
                errors[name] = str(exc)
                results[name] = []
                add_radar_log(
                    f"Источник {name} остановлен: {exc}",
                    level="WARN",
                    stage="apify",
                )
                finished_this_round.append(name)

        for name in finished_this_round:
            pending.discard(name)
        if pending:
            time.sleep(POLL_SECONDS)

    return results, errors


def run_actor_items_checked(client, actor_id, run_input):
    """Backward-compatible single-Actor helper."""
    results, errors = run_actor_group_checked(
        client,
        {"single": (actor_id, run_input)},
    )
    if errors.get("single"):
        raise RuntimeError(errors["single"])
    return results.get("single", [])
