"""Cost guard for the mass AI radar.

High recall is preserved, but every Apify Actor gets a real platform-side dollar
cap plus max-items cap. The discovery side therefore cannot silently grow into
an expensive run after future hashtag/search changes.
"""

from decimal import Decimal

import radar_growth_v6 as growth
import radar_request_job as radar_job
from cloud_state import load_radar_job, save_radar_job
from progress import set_radar_status
from radar_logs import add_radar_log

PROFILE_VERSION = "mass_global_ai_v10_budget5"
MAX_RUN_BUDGET_USD = 5.00
BUDGET_GUARD_USD = 4.00
BUDGET_HEADROOM_MULTIPLIER = 1.25
BUDGETED_GEMINI_RESERVE_USD = 1.25

# Conservative rate assumptions for planning only. Actual Apify runs are also
# protected by max_total_charge_usd below, which is the real hard stop.
SEARCH_USD_PER_1000 = 2.70
HASHTAG_USD_PER_1000 = 2.70
REEL_USD_PER_1000 = 1.00

SEARCH_LIMIT = 16
HASHTAG_LIMIT = 14
KEYWORD_RESULTS_LIMIT = 12  # fixed in growth._build_mass_sources
MAX_TRACKED_CREATORS = 15
CREATOR_RESULTS_LIMIT = 12  # fixed in growth._build_mass_sources
AI_ANALYZE_LIMIT = 320
KEEP_LIMIT = 60

# Real Apify platform-side caps. Even if a scraper pricing mode changes or a
# query suddenly returns far more data, these four Actor runs are individually
# stopped at these amounts. Maximum Apify charge for one discovery = $3.35.
ACTOR_CAPS_USD = {
    "popular_ai": 1.35,
    "ai_hashtags": 1.35,
    "ai_keywords": 0.35,
    "known_ai_creators": 0.30,
}

# High-yield search phrases. Removed broad tool/image/tutorial queries.
SEARCH_TERMS = [
    "AI comedy",
    "AI funny video",
    "AI generated comedy",
    "AI generated funny",
    "AI slop",
    "AI meme video",
    "AI skit",
    "AI absurd video",
    "AI grandma",
    "AI grandpa",
    "AI family comedy",
    "AI couple comedy",
    "AI animals funny",
    "AI village comedy",
    "AI interview funny",
    "AI POV funny",
    "нейроюмор",
    "ии юмор",
    "AI юмор",
    "AI бабушка",
    "AI дед",
    "AI деревня",
    "Grok AI video",
    "Grok video",
    "Gemini Omni",
    "Google Flow Omni",
    "Omni AI video",
    "Veo 3 funny",
    "Veo 3 comedy",
    "Kling AI funny",
    "Seedance funny",
    "Sora funny",
]

# Curated tags that usually point to generated VIDEO / comedy mechanics.
# Intentionally removed generic #ai, #ии, #нейросеть, #chatgpt, #openai,
# #gpt, #midjourney, #recraft, #ideogram and similar noisy discovery tags.
HASHTAGS = [
    "нейроюмор",
    "ииюмор",
    "аиюмор",
    "нейровидео",
    "иивидео",
    "аивидео",
    "нейросетьюмор",
    "нейросетьприкол",
    "нейрослоп",
    "aicomedy",
    "aihumor",
    "aifunny",
    "funnyai",
    "aivideo",
    "aigeneratedvideo",
    "aislop",
    "aimeme",
    "aiskit",
    "aiabsurd",
    "aiviral",
    "aipov",
    "aigrandma",
    "aigrandpa",
    "aifamily",
    "aicouple",
    "aianimals",
    "aivillage",
    "aiinterview",
    "grokvideo",
    "geminiomni",
    "omniai",
    "googleflowai",
    "veo3video",
    "klingvideo",
]

KEYWORD_TERMS = [
    "AI funny",
    "AI slop",
    "AI grandma",
    "AI animals",
    "Grok video",
    "Gemini Omni",
    "Veo 3 funny",
    "Kling AI funny",
]

_ORIGINAL_TRACKED_CREATORS = radar_job._tracked_creators


def _budget_tracked_creators():
    return _ORIGINAL_TRACKED_CREATORS()[:MAX_TRACKED_CREATORS]


def _max_items_for_source(name):
    return {
        "popular_ai": len(SEARCH_TERMS) * SEARCH_LIMIT,
        "ai_hashtags": len(HASHTAGS) * HASHTAG_LIMIT,
        "ai_keywords": len(KEYWORD_TERMS) * KEYWORD_RESULTS_LIMIT,
        "known_ai_creators": MAX_TRACKED_CREATORS * CREATOR_RESULTS_LIMIT,
    }.get(name, 100)


def budget_breakdown():
    search_results = len(SEARCH_TERMS) * SEARCH_LIMIT
    hashtag_results = len(HASHTAGS) * HASHTAG_LIMIT
    keyword_results = len(KEYWORD_TERMS) * KEYWORD_RESULTS_LIMIT
    creator_results = MAX_TRACKED_CREATORS * CREATOR_RESULTS_LIMIT

    search_cost = search_results / 1000 * SEARCH_USD_PER_1000
    hashtag_cost = hashtag_results / 1000 * HASHTAG_USD_PER_1000
    keyword_cost = keyword_results / 1000 * HASHTAG_USD_PER_1000
    creator_cost = creator_results / 1000 * REEL_USD_PER_1000
    estimated = search_cost + hashtag_cost + keyword_cost + creator_cost
    guarded = estimated * BUDGET_HEADROOM_MULTIPLIER
    actor_hard_cap = sum(ACTOR_CAPS_USD.values())
    designed_total = actor_hard_cap + BUDGETED_GEMINI_RESERVE_USD
    return {
        "search_terms": len(SEARCH_TERMS),
        "hashtags": len(HASHTAGS),
        "keyword_terms": len(KEYWORD_TERMS),
        "max_tracked_creators": MAX_TRACKED_CREATORS,
        "max_search_results": search_results,
        "max_hashtag_results": hashtag_results,
        "max_keyword_results": keyword_results,
        "max_creator_results": creator_results,
        "estimated_apify_usd": round(estimated, 3),
        "guarded_apify_usd": round(guarded, 3),
        "hard_apify_actor_caps_usd": round(actor_hard_cap, 2),
        "budgeted_gemini_reserve_usd": BUDGETED_GEMINI_RESERVE_USD,
        "designed_total_budget_usd": round(designed_total, 2),
        "hard_budget_usd": MAX_RUN_BUDGET_USD,
    }


def _assert_budget():
    info = budget_breakdown()
    if info["guarded_apify_usd"] > BUDGET_GUARD_USD:
        raise RuntimeError(
            f"Radar planning guard: ${info['guarded_apify_usd']:.2f} > ${BUDGET_GUARD_USD:.2f}"
        )
    if info["designed_total_budget_usd"] > MAX_RUN_BUDGET_USD:
        raise RuntimeError(
            f"Radar total budget design: ${info['designed_total_budget_usd']:.2f} > ${MAX_RUN_BUDGET_USD:.2f}"
        )
    return info


def _is_apify_quota_message(value):
    text = str(value or "").lower()
    return "monthly usage hard limit exceeded" in text or "monthly usage limit" in text


def _start_one_source_budget(client, job):
    """Start exactly one source with real Apify maxItems + maxTotalChargeUsd caps."""
    if not growth._is_current_source_set(job):
        job = growth._reset_stale_job(job, "migration")

    job["phase"] = "starting_sources"
    for name, source in (job.get("sources") or {}).items():
        if source.get("run_id"):
            continue

        cap_usd = ACTOR_CAPS_USD.get(name, 0.25)
        max_items = _max_items_for_source(name)
        job["current_source"] = name
        radar_job._persist(job)
        add_radar_log(
            f"Запускаю Apify source {name} с hard cap ${cap_usd:.2f} / maxItems={max_items}.",
            stage="apify",
            details={
                "source": name,
                "actor": source.get("actor_id"),
                "max_total_charge_usd": cap_usd,
                "max_items": max_items,
            },
        )

        run = client.actor(source["actor_id"]).start(
            run_input=dict(source.get("input") or {}),
            max_items=int(max_items),
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
        source["max_total_charge_usd"] = cap_usd
        source["max_items"] = max_items
        job["current_source"] = ""

        if all(x.get("run_id") for x in (job.get("sources") or {}).values()):
            job["phase"] = "discovering"
        radar_job._persist(job)

        started = sum(1 for x in job["sources"].values() if x.get("run_id"))
        total = len(job["sources"])
        set_radar_status(
            "running",
            "Запускаю источники Apify",
            4 + int(8 * started / max(1, total)),
            300,
            f"Запущено источников: {started}/{total}. Каждый Actor имеет отдельный долларовый hard cap.",
            details={
                "run_id": job.get("run_id"),
                "sources_started": started,
                "sources_total": total,
                "current_source_cap_usd": cap_usd,
                **budget_breakdown(),
            },
        )
        return job

    job["phase"] = "discovering"
    radar_job._persist(job)
    return job


def wrap_tick_job(base_tick_job):
    """Turn Apify monthly-limit failures into a clean paused state, not retry spam."""
    def wrapped():
        payload, status_code = base_tick_job()
        message = payload.get("message") if isinstance(payload, dict) else ""
        if not _is_apify_quota_message(message):
            return payload, status_code

        job = load_radar_job() or {}
        if job:
            job["phase"] = "quota_blocked"
            job["error"] = "APIFY_MONTHLY_LIMIT"
            job["last_error"] = str(message)[:1200]
            save_radar_job(job)

        set_radar_status(
            "error",
            "Лимит Apify исчерпан",
            0,
            None,
            "Apify остановил новые Actor-запуски из-за месячного hard limit. Увеличь/пополни лимит Apify и запусти поиск снова. Сайт и локальное состояние не потеряны.",
            warning="Monthly usage hard limit exceeded",
            details={"radar_profile": PROFILE_VERSION, **budget_breakdown()},
        )
        add_radar_log(
            "Apify monthly hard limit: радар поставлен на паузу без повторных платных попыток.",
            level="ERROR",
            stage="apify-quota",
            details=budget_breakdown(),
        )
        return {
            **(payload if isinstance(payload, dict) else {}),
            "active": False,
            "phase": "quota_blocked",
            "quota_exceeded": True,
            "transient_error": False,
            "message": "Лимит Apify исчерпан. Увеличь/пополни месячный лимит Apify и запусти поиск снова.",
        }, 200

    return wrapped


def apply_budget_overrides():
    info = _assert_budget()

    # growth wrappers read these globals dynamically, so this replaces v9
    # discovery while preserving the stable request-state-machine and classifier.
    growth.PROFILE_VERSION = PROFILE_VERSION
    growth.SEARCH_LIMIT = SEARCH_LIMIT
    growth.HASHTAG_LIMIT = HASHTAG_LIMIT
    growth.SEARCH_QUERY = growth._sanitize_search_csv(", ".join(SEARCH_TERMS))
    growth.RAW_SEARCH_QUERY = ", ".join(SEARCH_TERMS)
    growth.HASHTAGS_V7 = list(HASHTAGS)
    growth.RAW_KEYWORD_TERMS = list(KEYWORD_TERMS)
    growth.KEYWORD_TERMS = growth._sanitize_keyword_terms(KEYWORD_TERMS)
    growth.AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    growth.KEEP_LIMIT = KEEP_LIMIT

    radar_job.RADAR_AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = KEEP_LIMIT
    radar_job._tracked_creators = _budget_tracked_creators
    radar_job._start_one_source = _start_one_source_budget

    add_radar_log(
        "BUDGET AI v10 включён: мусорные broad-теги удалены; каждый Apify Actor имеет maxItems + maxTotalChargeUsd; общий план <$5.",
        stage="startup",
        details={"profile": PROFILE_VERSION, "actor_caps": ACTOR_CAPS_USD, **info},
    )
    return info
