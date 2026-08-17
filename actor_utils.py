def run_actor_items_checked(client, actor_id, run_input):
    run = client.actor(actor_id).call(run_input=run_input)
    if not run:
        raise RuntimeError(f"{actor_id}: Apify не вернул данные запуска")
    status = str(run.get("status") or "").upper()
    if status and status not in {"SUCCEEDED"}:
        message = run.get("statusMessage") or run.get("status_message") or status
        raise RuntimeError(f"{actor_id}: {message}")
    dataset_id = run.get("defaultDatasetId")
    if not dataset_id:
        return []
    return list(client.dataset(dataset_id).iterate_items())
