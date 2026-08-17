from radar_logs import add_radar_log


def run_actor_items_checked(client, actor_id, run_input):
    safe_input = dict(run_input or {})
    # Keep the website log readable; never log secrets.
    add_radar_log(
        f"Запускаю Apify Actor {actor_id}",
        stage="apify",
        details={k: v for k, v in safe_input.items() if k not in {"token", "apiToken"}},
    )
    try:
        run = client.actor(actor_id).call(run_input=run_input)
    except Exception as exc:
        add_radar_log(f"Apify Actor {actor_id} не запустился: {exc}", level="ERROR", stage="apify")
        raise

    if not run:
        message = f"{actor_id}: Apify не вернул данные запуска"
        add_radar_log(message, level="ERROR", stage="apify")
        raise RuntimeError(message)

    status = str(run.get("status") or "").upper()
    run_id = run.get("id") or run.get("runId") or ""
    if status and status not in {"SUCCEEDED"}:
        message = run.get("statusMessage") or run.get("status_message") or status
        add_radar_log(
            f"Apify Actor {actor_id} завершился {status}: {message}",
            level="WARN",
            stage="apify",
            details={"run_id": run_id},
        )
        raise RuntimeError(f"{actor_id}: {message}")

    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        add_radar_log(
            f"Apify Actor {actor_id} завершён, но dataset пуст.",
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
