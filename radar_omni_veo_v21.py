"""Omni/Veo-only momentum radar profile.

Final discovery override applied after the existing v20 edge guards.
The production pipeline stays intact; only discovery scope, depth and ranking
are narrowed to Instagram Reels found under #omni and #veo.
"""

from __future__ import annotations

import math
import os
import sys
from decimal import Decimal

import radar_budget_v10 as budget
import radar_dialogue_v14 as dialogue
import radar_growth_v6 as growth
import radar_hardening_v19 as hardening
import radar_quality
import radar_request_job as radar_job
import radar_resilient_v17 as v17
import radar_scale_v16 as scale
import radar_service
import radar_source_aggregation_v20 as aggregation
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log

PROFILE_VERSION = "omni_veo_v21_momentum"
HASHTAGS = ("omni", "veo")


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


def _env_float(name: str, default: float, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(low, min(high, value))


# Official Apify hashtag scraper applies resultsLimit per hashtag. Two independent
# sources make the requested volume and per-tag diagnostics explicit.
HASHTAG_LIMIT = _env_int("OMNI_VEO_HASHTAG_LIMIT", 250, 50, 500)
AI_ANALYZE_LIMIT = _env_int("OMNI_VEO_ANALYZE_LIMIT", 500, 100, 500)
KEEP_LIMIT = _env_int("OMNI_VEO_KEEP_LIMIT", 250, 60, 300)
FINAL_QUERY_LIMIT = max(500, KEEP_LIMIT * 2)
SOURCE_CAP_USD = _env_float("OMNI_VEO_SOURCE_CAP_USD", 1.00, 0.25, 1.50)
TARGET_MATCHES = min(KEEP_LIMIT, AI_ANALYZE_LIMIT)

_APPLIED = False
_ORIGINAL_DIALOGUE_NORMALIZE = dialogue.normalize_dialogue_candidate
_ORIGINAL_SOURCE_LABEL = aggregation._source_label
_ORIGINAL_REFRESH_SCORES = radar_quality.refresh_recent_scores_quality


def _source_name(tag: str) -> str:
    return f"hashtag_{tag}"


def _build_sources():
    sources = {}
    for tag in HASHTAGS:
        sources[_source_name(tag)] = {
            "actor_id": radar_job.APIFY_HASHTAG_ACTOR,
            "input": {
                "hashtags": [tag],
                "resultsType": "reels",
                "resultsLimit": HASHTAG_LIMIT,
            },
            "run_id": "",
            "status": "NOT_STARTED",
            "dataset_id": "",
            "status_message": "",
            "started_at": "",
        }
    return sources


def _is_current_source_set(job) -> bool:
    if str((job or {}).get("profile") or "") != PROFILE_VERSION:
        return False
    sources = (job or {}).get("sources") or {}
    if set(sources) != {_source_name(tag) for tag in HASHTAGS}:
        return False
    for tag in HASHTAGS:
        source = sources.get(_source_name(tag)) or {}
        payload = source.get("input") or {}
        if list(payload.get("hashtags") or []) != [tag]:
            return False
        if str(payload.get("resultsType") or "").lower() != "reels":
            return False
        if int(payload.get("resultsLimit") or 0) != HASHTAG_LIMIT:
            return False
    return True


def _reset_stale_job(job, stage="migration-v21"):
    old_profile = str((job or {}).get("profile") or "")
    job["profile"] = PROFILE_VERSION
    job["phase"] = "queued"
    job["sources"] = _build_sources()
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
    add_radar_log(
        "OMNI/VEO V21: старая широкая очередь сброшена; discovery теперь только #omni и #veo.",
        stage=stage,
        details={
            "old_profile": old_profile,
            "new_profile": PROFILE_VERSION,
            "hashtags": list(HASHTAGS),
            "limit_each": HASHTAG_LIMIT,
        },
    )
    return job


def _start_one_source(client, job):
    if not _is_current_source_set(job):
        job = _reset_stale_job(job)

    job["phase"] = "starting_sources"
    for name, source in (job.get("sources") or {}).items():
        if source.get("run_id"):
            continue

        tag = name.removeprefix("hashtag_")
        job["current_source"] = name
        radar_job._persist(job)
        add_radar_log(
            f"Запускаю глубокий сбор #{tag}: до {HASHTAG_LIMIT} Reels.",
            stage="apify",
            details={
                "source": name,
                "tag": tag,
                "max_items": HASHTAG_LIMIT,
                "max_total_charge_usd": SOURCE_CAP_USD,
            },
        )

        run = client.actor(source["actor_id"]).start(
            run_input=dict(source.get("input") or {}),
            max_items=int(HASHTAG_LIMIT),
            max_total_charge_usd=Decimal(str(SOURCE_CAP_USD)),
            restart_on_error=False,
        ) or {}
        run_id = run.get("id") or run.get("runId") or ""
        if not run_id:
            raise RuntimeError(f"{source.get('actor_id')}: Apify не вернул runId")

        source["run_id"] = run_id
        source["status"] = str(run.get("status") or "READY").upper()
        source["dataset_id"] = run.get("defaultDatasetId") or run.get("default_dataset_id") or ""
        source["status_message"] = str(run.get("statusMessage") or run.get("status_message") or "")[:500]
        source["started_at"] = radar_job._now_iso()
        source["max_items"] = HASHTAG_LIMIT
        source["max_total_charge_usd"] = SOURCE_CAP_USD
        job["current_source"] = ""

        if all(x.get("run_id") for x in (job.get("sources") or {}).values()):
            job["phase"] = "discovering"
        radar_job._persist(job)

        started = sum(1 for x in job["sources"].values() if x.get("run_id"))
        total = len(job["sources"])
        set_radar_status(
            "running",
            "Собираю #omni и #veo",
            4 + int(8 * started / max(1, total)),
            300,
            f"Запущено {started}/{total} хештегов. Лимит: до {HASHTAG_LIMIT} Reels на каждый.",
            details={
                "run_id": job.get("run_id"),
                "sources_started": started,
                "sources_total": total,
                "hashtags": list(HASHTAGS),
                "hashtag_limit_each": HASHTAG_LIMIT,
            },
        )
        return job

    job["phase"] = "discovering"
    radar_job._persist(job)
    return job


def _source_label(name, kind, row):
    for tag in HASHTAGS:
        if str(name or "").lower() == _source_name(tag):
            return f"hashtag: {tag}"
    return _ORIGINAL_SOURCE_LABEL(name, kind, row)


def _tag_from_source(source: str) -> str:
    value = str(source or "").strip().lower()
    for tag in HASHTAGS:
        if value in {tag, f"#{tag}", f"hashtag: {tag}", _source_name(tag)}:
            return tag
        if f"hashtag: {tag}" in value:
            return tag
    return ""


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _momentum_score(views: int, views_per_hour: float, hours: float, base_score: float) -> float:
    """Velocity-first score: rising speed dominates absolute historical popularity."""
    velocity = _clamp01(math.log1p(max(0.0, views_per_hour)) / math.log1p(100_000))
    freshness = _clamp01(1.0 - max(0.0, hours) / (24.0 * 7.0))
    proof = _clamp01(math.log1p(max(0, views)) / math.log1p(1_000_000))
    base = _clamp01(max(0.0, base_score) / 100.0)
    score = 100.0 * (0.72 * velocity + 0.16 * freshness + 0.09 * proof + 0.03 * base)
    return round(_clamp01(score / 100.0) * 100.0, 1)


def normalize_omni_veo_candidate(raw, source, creator_stats=None):
    tag = _tag_from_source(source)
    if not tag:
        return None

    enriched = dict(raw or {})
    enriched["searchTerm"] = tag
    item = _ORIGINAL_DIALOGUE_NORMALIZE(enriched, f"hashtag: {tag}", creator_stats)
    if not item:
        return None

    item["search_term"] = tag
    item["viral_score_v2"] = _momentum_score(
        int(item.get("views") or 0),
        float(item.get("views_per_hour") or 0),
        float(item.get("hours_since_publish") or 0),
        float(item.get("viral_score_v2") or 0),
    )
    item["momentum_profile"] = PROFILE_VERSION
    return item


def refresh_momentum_scores(conn):
    # Preserve creator anomaly/reach/engagement metrics from the existing pipeline,
    # then replace only the final ranking score for the two target hashtags.
    _ORIGINAL_REFRESH_SCORES(conn)
    rows = conn.execute(
        """SELECT id,views,hours_since_publish,views_per_hour,viral_score_v2
           FROM radar_posts
           WHERE datetime(published_at)>=datetime('now','-7 days')
             AND LOWER(COALESCE(search_term,'')) IN ('omni','veo')"""
    ).fetchall()
    for row in rows:
        score = _momentum_score(
            int(row["views"] or 0),
            float(row["views_per_hour"] or 0),
            float(row["hours_since_publish"] or 0),
            float(row["viral_score_v2"] or 0),
        )
        conn.execute("UPDATE radar_posts SET viral_score_v2=? WHERE id=?", (score, row["id"]))
    conn.commit()


def _invalidate_noncurrent_passes():
    with db_conn() as conn:
        cur = conn.execute(
            """UPDATE radar_posts
               SET ai_checked=0, ai_match=0
               WHERE datetime(published_at)>=datetime('now','-7 days')
                 AND ai_match=1
                 AND COALESCE(screening_profile,'')<>?""",
            (PROFILE_VERSION,),
        )
        changed = int(cur.rowcount or 0)
        conn.commit()
    return changed


def apply_omni_veo_v21():
    global _APPLIED
    if _APPLIED:
        return {
            "profile": PROFILE_VERSION,
            "hashtags": list(HASHTAGS),
            "hashtag_limit_each": HASHTAG_LIMIT,
        }
    _APPLIED = True

    # Make every old runtime layer report the new contract instead of silently
    # resetting the queue to its previous broad dialogue/AI source set.
    budget.PROFILE_VERSION = PROFILE_VERSION
    growth.PROFILE_VERSION = PROFILE_VERSION
    hardening.PROFILE_VERSION = PROFILE_VERSION
    v17.PROFILE_VERSION = PROFILE_VERSION

    # Truthful budget metadata: there is no keyword/search/creator discovery now.
    budget.SEARCH_TERMS = []
    budget.HASHTAGS = list(HASHTAGS)
    budget.KEYWORD_TERMS = []
    budget.SEARCH_LIMIT = 0
    budget.HASHTAG_LIMIT = HASHTAG_LIMIT
    budget.KEYWORD_RESULTS_LIMIT = 0
    budget.MAX_TRACKED_CREATORS = 0
    budget.CREATOR_RESULTS_LIMIT = 0
    budget.AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    budget.KEEP_LIMIT = KEEP_LIMIT
    budget.ACTOR_CAPS_USD = {
        _source_name("omni"): SOURCE_CAP_USD,
        _source_name("veo"): SOURCE_CAP_USD,
    }

    growth.TARGET_MATCHES = TARGET_MATCHES
    growth.MIN_AI_CHECKS_BEFORE_EARLY_STOP = TARGET_MATCHES
    growth.AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    growth.KEEP_LIMIT = KEEP_LIMIT
    growth.SEARCH_LIMIT = 0
    growth.HASHTAG_LIMIT = HASHTAG_LIMIT
    growth.SEARCH_QUERY = ""
    growth.RAW_SEARCH_QUERY = ""
    growth.HASHTAGS_V7 = list(HASHTAGS)
    growth.RAW_KEYWORD_TERMS = []
    growth.KEYWORD_TERMS = []

    scale.TARGET_MATCHES = TARGET_MATCHES
    scale.KEEP_LIMIT = KEEP_LIMIT
    scale.GEMINI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    scale.FINAL_QUERY_LIMIT = FINAL_QUERY_LIMIT

    v17.TARGET_MATCHES = TARGET_MATCHES
    v17.KEEP_LIMIT = KEEP_LIMIT
    v17.GEMINI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT

    radar_job.RADAR_AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = KEEP_LIMIT
    radar_service.RADAR_KEEP_LIMIT = KEEP_LIMIT

    # Replace discovery only. Existing <=10s duration gate, static-image gate,
    # dialogue screening, production prompts, retries, stop marker and v20 source
    # aggregation all remain active.
    radar_job._build_sources = _build_sources
    radar_job._start_one_source = _start_one_source
    growth._is_current_source_set = _is_current_source_set
    growth._reset_stale_job = _reset_stale_job
    radar_job.normalize_reel = normalize_omni_veo_candidate
    aggregation._source_label = _source_label
    radar_quality.refresh_recent_scores_quality = refresh_momentum_scores

    invalidated = _invalidate_noncurrent_passes()
    info = budget._assert_budget()

    app_module = sys.modules.get("app")
    if app_module is not None:
        app_module.PROFILE_VERSION = PROFILE_VERSION
        app_module.KEEP_LIMIT = KEEP_LIMIT
        app_module.BUDGET_INFO = info

    add_radar_log(
        "OMNI/VEO V21 READY: только #omni + #veo, до 10 сек, глубокий сбор, momentum-first ranking.",
        stage="startup",
        details={
            "profile": PROFILE_VERSION,
            "hashtags": list(HASHTAGS),
            "hashtag_limit_each": HASHTAG_LIMIT,
            "max_raw_requested": HASHTAG_LIMIT * len(HASHTAGS),
            "ai_analyze_limit": AI_ANALYZE_LIMIT,
            "keep_limit": KEEP_LIMIT,
            "source_cap_usd_each": SOURCE_CAP_USD,
            "stale_passes_invalidated": invalidated,
            "ranking": "72% velocity + 16% freshness + 9% absolute proof + 3% prior score",
            **info,
        },
    )
    return {
        "profile": PROFILE_VERSION,
        "hashtags": list(HASHTAGS),
        "hashtag_limit_each": HASHTAG_LIMIT,
        "max_raw_requested": HASHTAG_LIMIT * len(HASHTAGS),
        "ai_analyze_limit": AI_ANALYZE_LIMIT,
        "keep_limit": KEEP_LIMIT,
        "source_cap_usd_each": SOURCE_CAP_USD,
        "stale_passes_invalidated": invalidated,
        "budget": info,
    }
