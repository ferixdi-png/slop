import os
from datetime import datetime, timezone

import requests

from config import APIFY_HASHTAG_ACTOR, RADAR_AI_ANALYZE_LIMIT
from db import db_conn
from radar_normalize import normalize_reel
from radar_service import load_creator_stats, save_post


def _actor_api_id(actor_id):
    return actor_id.replace("/", "~")


def recover_last_successful_hashtag_run(max_age_hours=24):
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        return {"ok": False, "reason": "APIFY_API_TOKEN не задан", "recovered": 0}

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    actor_id = _actor_api_id(APIFY_HASHTAG_ACTOR)
    run_url = f"https://api.apify.com/v2/actors/{actor_id}/runs/last"

    r = requests.get(run_url, params={"status": "SUCCEEDED"}, headers=headers, timeout=15)
    if r.status_code != 200:
        return {"ok": False, "reason": f"Apify last run HTTP {r.status_code}", "recovered": 0}

    run = (r.json() or {}).get("data") or {}
    dataset_id = run.get("defaultDatasetId")
    finished_at = run.get("finishedAt")
    if not dataset_id:
        return {"ok": False, "reason": "У последнего успешного run нет dataset", "recovered": 0}

    if finished_at:
        try:
            dt = datetime.fromisoformat(str(finished_at).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 3600
            if age_hours > max_age_hours:
                return {"ok": False, "reason": f"Последний успешный run старше {max_age_hours} часов", "recovered": 0}
        except Exception:
            pass

    items_url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
    r = requests.get(
        items_url,
        params={"clean": "true", "format": "json", "limit": 1000},
        headers=headers,
        timeout=30,
    )
    if r.status_code != 200:
        return {"ok": False, "reason": f"Dataset HTTP {r.status_code}", "recovered": 0}

    raw_items = r.json() or []
    with db_conn() as conn:
        creator_stats = load_creator_stats(conn)

    unique = {}
    for raw in raw_items:
        item = normalize_reel(raw, "восстановлено из последнего Apify run", creator_stats)
        if item and (
            item["post_url"] not in unique
            or item["viral_score_v2"] > unique[item["post_url"]]["viral_score_v2"]
        ):
            unique[item["post_url"]] = item

    candidates = sorted(
        unique.values(),
        key=lambda x: (x["viral_score_v2"], x["views_per_hour"], x["views"]),
        reverse=True,
    )[:RADAR_AI_ANALYZE_LIMIT]

    with db_conn() as conn:
        for item in candidates:
            save_post(conn, item, None)
        conn.commit()

    return {
        "ok": True,
        "run_id": run.get("id"),
        "dataset_id": dataset_id,
        "raw": len(raw_items),
        "recovered": len(candidates),
        "finished_at": finished_at,
    }
