"""V29 hard-budget overlay for the three-platform V28 radar.

The product scope stays unchanged: Instagram + TikTok + YouTube, five strict
post-level hashtags, 14 days and mandatory Gemini speech/timing screening.
V29 changes only spend control:
- 60 requested items per tag/platform (300 max per platform),
- explicit Apify max_items on every discovery run,
- explicit pay-per-event max_total_charge_usd on every discovery run,
- a $2.80 hard discovery ceiling, leaving $2.20 headroom inside the $5 target,
- a smaller 150-candidate Gemini queue so downstream refresh work cannot explode.
"""

from __future__ import annotations

import sys
from decimal import Decimal

import radar_budget_v10 as budget
import radar_growth_v6 as growth
import radar_hardening_v19 as hardening
import radar_multiplatform_v28 as v28
import radar_omni_veo_v21 as v21
import radar_request_job as radar_job
import radar_resilient_v17 as v17
import radar_service
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log

MODE_VERSION = "multiplatform_speech_v29_budget5"
SCREENING_PROFILE = MODE_VERSION
SOURCE_MARKER = "STRICT_MULTIPLATFORM_BUDGET_V29"
RESULTS_PER_TAG = 60
AI_ANALYZE_LIMIT = 150
KEEP_LIMIT = 180
MAX_TOTAL_TARGET_USD = 5.00
SOURCE_CHARGE_CAPS_USD = {
    "instagram": 0.85,
    "tiktok": 1.15,
    "youtube": 0.80,
}
DISCOVERY_HARD_CAP_USD = round(sum(SOURCE_CHARGE_CAPS_USD.values()), 2)
RESERVED_HEADROOM_USD = round(MAX_TOTAL_TARGET_USD - DISCOVERY_HARD_CAP_USD, 2)

# User is currently on the free Apify tier. These planning rates match the
# currently visible free-tier/store rates used for conservative sizing.
PLANNING_USD_PER_1000 = {
    "instagram": 2.70,
    "tiktok": 3.70,
    "youtube": 2.50,
}

_APPLIED = False


def budget_breakdown_v29():
    max_per_platform = RESULTS_PER_TAG * len(v28.TARGET_TAGS)
    estimates = {
        name: round(max_per_platform / 1000.0 * rate, 3)
        for name, rate in PLANNING_USD_PER_1000.items()
    }
    return {
        "budget_profile": MODE_VERSION,
        "hard_total_target_usd": MAX_TOTAL_TARGET_USD,
        "hard_apify_discovery_caps_usd": DISCOVERY_HARD_CAP_USD,
        "reserved_headroom_usd": RESERVED_HEADROOM_USD,
        "source_caps_usd": dict(SOURCE_CHARGE_CAPS_USD),
        "planning_usd_per_1000": dict(PLANNING_USD_PER_1000),
        "estimated_discovery_usd_at_full_300_each": round(sum(estimates.values()), 3),
        "estimated_by_platform_usd": estimates,
        "results_per_tag_per_platform": RESULTS_PER_TAG,
        "max_items_per_platform": max_per_platform,
        "max_raw_requested": max_per_platform * len(v28.PLATFORMS),
        "gemini_analyze_limit": AI_ANALYZE_LIMIT,
        "keep_limit": KEEP_LIMIT,
    }


def build_sources_v29():
    # v28.build_v28_sources reads RESULTS_PER_TAG dynamically from its module.
    # Keep the familiar input schemas but attach V29 budget truth to each source.
    sources = v28.build_v28_sources()
    for name, source in sources.items():
        max_items = RESULTS_PER_TAG * len(v28.TARGET_TAGS)
        source["requested_max"] = max_items
        source["max_items"] = max_items
        source["max_total_charge_usd"] = float(SOURCE_CHARGE_CAPS_USD[name])
        source["strict_scope_marker"] = SOURCE_MARKER
    return sources


def is_current_source_set_v29(job):
    if str((job or {}).get("profile") or "") != SCREENING_PROFILE:
        return False
    sources = (job or {}).get("sources") or {}
    if set(sources) != {"instagram", "tiktok", "youtube"}:
        return False
    expected_items = RESULTS_PER_TAG * len(v28.TARGET_TAGS)
    for name, source in sources.items():
        if str((source or {}).get("strict_scope_marker") or "") != SOURCE_MARKER:
            return False
        if int((source or {}).get("max_items") or 0) != expected_items:
            return False
        if abs(float((source or {}).get("max_total_charge_usd") or 0) - SOURCE_CHARGE_CAPS_USD[name]) > 1e-9:
            return False
    return True


def reset_stale_job_v29(job, stage="migration-v29-budget"):
    old_profile = str((job or {}).get("profile") or "")
    job["profile"] = SCREENING_PROFILE
    job["phase"] = "queued"
    job["sources"] = build_sources_v29()
    job["candidates"] = []
    job["warnings"] = []
    job["source_failures"] = {}
    job["stats"] = {
        "migrated_from_profile": old_profile,
        "budget_profile": MODE_VERSION,
    }
    job["result"] = {}
    job["error"] = ""
    job["last_error"] = ""
    job["current_source"] = ""
    job["current_ai_index"] = None
    job["current_ai_post_url"] = ""
    job["error_guard"] = {}
    radar_job._persist(job)
    add_radar_log(
        "V29 BUDGET MIGRATION: старая очередь сброшена до платного шага; новый discovery имеет hard cap $2.80.",
        stage=stage,
        details={
            "old_profile": old_profile,
            "new_profile": SCREENING_PROFILE,
            **budget_breakdown_v29(),
        },
    )
    return job


def start_one_source_v29(client, job):
    if not is_current_source_set_v29(job):
        job = reset_stale_job_v29(job)

    job["phase"] = "starting_sources"
    for name, source in (job.get("sources") or {}).items():
        if source.get("run_id"):
            continue

        max_items = int(source.get("max_items") or 0)
        cap_usd = float(source.get("max_total_charge_usd") or 0)
        job["current_source"] = name
        radar_job._persist(job)

        add_radar_log(
            f"V29: запускаю {name} с HARD CAP ${cap_usd:.2f} / maxItems={max_items}.",
            stage="apify-budget",
            details={
                "source": name,
                "actor": source.get("actor_id"),
                "max_items": max_items,
                "max_total_charge_usd": cap_usd,
                "discovery_hard_cap_usd": DISCOVERY_HARD_CAP_USD,
            },
        )

        run = client.actor(source["actor_id"]).start(
            run_input=dict(source.get("input") or {}),
            max_items=max_items,
            max_total_charge_usd=Decimal(str(cap_usd)),
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
        job["current_source"] = ""
        if all(x.get("run_id") for x in job["sources"].values()):
            job["phase"] = "discovering"
        radar_job._persist(job)

        started = sum(1 for x in job["sources"].values() if x.get("run_id"))
        total = len(job["sources"])
        set_radar_status(
            "running",
            "Запускаю бюджетный discovery",
            4 + int(8 * started / max(1, total)),
            300,
            f"Запущено платформ: {started}/{total}. Hard cap discovery: ${DISCOVERY_HARD_CAP_USD:.2f}; общий целевой бюджет < ${MAX_TOTAL_TARGET_USD:.2f}.",
            details={
                "run_id": job.get("run_id"),
                "sources_started": started,
                "sources_total": total,
                "current_source": name,
                "current_source_cap_usd": cap_usd,
                **budget_breakdown_v29(),
            },
        )
        return job

    job["phase"] = "discovering"
    radar_job._persist(job)
    return job


def _install_budget_response_guard(app_module):
    app = getattr(app_module, "app", None)
    if app is None or getattr(app, "_v29_budget_response_guard", False):
        return
    from flask import request

    @app.after_request
    def v29_budget_response_guard(response):
        if not response.is_json:
            return response
        if request.path not in {"/api/status", "/api/radar/status", "/health"}:
            return response
        data = response.get_json(silent=True)
        if not isinstance(data, dict):
            return response

        fields = {
            "radar_budget_profile": MODE_VERSION,
            "radar_hard_total_target_usd": MAX_TOTAL_TARGET_USD,
            "radar_apify_discovery_hard_cap_usd": DISCOVERY_HARD_CAP_USD,
            "radar_budget_headroom_usd": RESERVED_HEADROOM_USD,
            "radar_source_caps_usd": dict(SOURCE_CHARGE_CAPS_USD),
            "radar_results_per_tag_per_platform": RESULTS_PER_TAG,
            "radar_max_raw_requested": RESULTS_PER_TAG * len(v28.TARGET_TAGS) * len(v28.PLATFORMS),
            "radar_ai_analyze_limit": AI_ANALYZE_LIMIT,
        }
        if request.path == "/api/radar/status":
            details = dict(data.get("details") or {})
            details.update(fields)
            data["details"] = details
        else:
            data.update(fields)
        response.set_data(app.json.dumps(data))
        response.mimetype = "application/json"
        return response

    app._v29_budget_response_guard = True


def apply_budget_v29():
    global _APPLIED
    if _APPLIED:
        return {
            "mode": MODE_VERSION,
            "screening_profile": SCREENING_PROFILE,
            "platforms": list(v28.PLATFORMS),
            "hashtags": list(v28.TARGET_TAGS),
            "lookback_days": v28.LOOKBACK_DAYS,
            "results_per_tag_per_platform": RESULTS_PER_TAG,
            "max_raw_requested": RESULTS_PER_TAG * len(v28.TARGET_TAGS) * len(v28.PLATFORMS),
            "analyze_limit": AI_ANALYZE_LIMIT,
            "keep_limit": KEEP_LIMIT,
            "budget": budget_breakdown_v29(),
        }
    _APPLIED = True

    # Make already-installed V28 guards/functions report and persist the new
    # identity. Module globals are resolved dynamically by those functions.
    v28.MODE_VERSION = MODE_VERSION
    v28.SCREENING_PROFILE = SCREENING_PROFILE
    v28.SOURCE_MARKER = SOURCE_MARKER
    v28.RESULTS_PER_TAG = RESULTS_PER_TAG
    v28.AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    v28.KEEP_LIMIT = KEEP_LIMIT

    budget.PROFILE_VERSION = SCREENING_PROFILE
    growth.PROFILE_VERSION = SCREENING_PROFILE
    hardening.PROFILE_VERSION = SCREENING_PROFILE
    v17.PROFILE_VERSION = SCREENING_PROFILE

    radar_job.RADAR_AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = KEEP_LIMIT
    radar_service.RADAR_KEEP_LIMIT = KEEP_LIMIT

    radar_job._build_sources = build_sources_v29
    radar_job._start_one_source = start_one_source_v29
    growth._is_current_source_set = is_current_source_set_v29
    growth._reset_stale_job = reset_stale_job_v29
    v21._is_current_source_set = is_current_source_set_v29
    v21._reset_stale_job = reset_stale_job_v29

    app_module = sys.modules.get("app")
    if app_module is not None:
        app_module.PROFILE_VERSION = SCREENING_PROFILE
        app_module.KEEP_LIMIT = KEEP_LIMIT
        app_module.BUDGET_INFO = budget_breakdown_v29()
        app_module.budget_breakdown = budget_breakdown_v29
        _install_budget_response_guard(app_module)

    # Force any current V28 cache/output to be rechecked under the budgeted V29
    # profile. An active old job is reset by the existing hardening wrapper before
    # its next paid tick because hardening.PROFILE_VERSION changed above.
    with db_conn() as conn:
        conn.execute(
            "UPDATE radar_posts SET ai_checked=0,ai_match=0 WHERE ai_match=1 AND COALESCE(screening_profile,'')<>?",
            (SCREENING_PROFILE,),
        )
        conn.commit()

    info = {
        "mode": MODE_VERSION,
        "screening_profile": SCREENING_PROFILE,
        "platforms": list(v28.PLATFORMS),
        "hashtags": list(v28.TARGET_TAGS),
        "lookback_days": v28.LOOKBACK_DAYS,
        "results_per_tag_per_platform": RESULTS_PER_TAG,
        "max_raw_requested": RESULTS_PER_TAG * len(v28.TARGET_TAGS) * len(v28.PLATFORMS),
        "analyze_limit": AI_ANALYZE_LIMIT,
        "keep_limit": KEEP_LIMIT,
        "speech_required": True,
        "strict_actual_hashtag": True,
        "youtube_direct_gemini": True,
        "budget": budget_breakdown_v29(),
    }
    add_radar_log(
        "V29 BUDGET READY: 3 платформы сохранены; discovery hard cap $2.80; общий целевой бюджет <$5.",
        stage="startup",
        details=info,
    )
    return info
