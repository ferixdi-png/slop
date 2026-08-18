"""Cost guard for the mass AI radar.

The goal is high recall without letting one discovery run explode in Apify cost.
Pricing constants below are intentionally conservative and only used as a guard.
"""

import radar_growth_v6 as growth
import radar_request_job as radar_job
from cloud_state import load_radar_job, save_radar_job
from progress import set_radar_status
from radar_logs import add_radar_log

PROFILE_VERSION = "mass_global_ai_v10_budget5"
MAX_RUN_BUDGET_USD = 5.00
BUDGET_GUARD_USD = 4.50
BUDGET_HEADROOM_MULTIPLIER = 1.25

# Conservative public-rate assumptions. We intentionally budget above common
# paid-plan rates so a future config change fails closed instead of overspending.
SEARCH_USD_PER_1000 = 2.70
HASHTAG_USD_PER_1000 = 2.70
REEL_USD_PER_1000 = 1.00

SEARCH_LIMIT = 18
HASHTAG_LIMIT = 16
KEYWORD_RESULTS_LIMIT = 12  # fixed by growth._build_mass_sources
MAX_TRACKED_CREATORS = 20
CREATOR_RESULTS_LIMIT = 12  # fixed by growth._build_mass_sources
AI_ANALYZE_LIMIT = 320
KEEP_LIMIT = 60

# High-yield discovery only. Removed generic image/tool tags such as #chatgpt,
# #openai, #gpt, #midjourney, #recraft, #ideogram, generic #ai/#ии and other
# broad tags that create lots of tutorials, screenshots and non-video noise.
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


def budget_breakdown():
    search_results = len(SEARCH_TERMS) * SEARCH_LIMIT
    hashtag_results = len(HASHTAGS) * HASHTAG_LIMIT
    keyword_results = len(KEYWORD_TERMS) * KEYWORD_RESULTS_LIMIT
    creator_results = MAX_TRACKED_CREATORS * CREATOR_RESULTS_LIMIT

    search_cost = search_results / 1000 * SEARCH_USD_PER_1000
    hashtag_cost = hashtag_results / 1000 * HASHTAG_USD_PER_1000
    keyword_cost = keyword_results / 1000 * HASHTAG_USD_PER_1000
    creator_cost = creator_results / 1000 * REEL_USD_PER_1000
    raw = search_cost + hashtag_cost + keyword_cost + creator_cost
    guarded = raw * BUDGET_HEADROOM_MULTIPLIER
    return {
        "search_terms": len(SEARCH_TERMS),
        "hashtags": len(HASHTAGS),
        "keyword_terms": len(KEYWORD_TERMS),
        "max_tracked_creators": MAX_TRACKED_CREATORS,
        "max_search_results": search_results,
        "max_hashtag_results": hashtag_results,
        "max_keyword_results": keyword_results,
        "max_creator_results": creator_results,
        "estimated_apify_usd": round(raw, 3),
        "guarded_apify_usd": round(guarded, 3),
        "hard_budget_usd": MAX_RUN_BUDGET_USD,
    }


def _assert_budget():
    info = budget_breakdown()
    if info["guarded_apify_usd"] > BUDGET_GUARD_USD:
        raise RuntimeError(
            f"Radar budget guard: ${info['guarded_apify_usd']:.2f} > ${BUDGET_GUARD_USD:.2f}"
        )
    return info


def _is_apify_quota_message(value):
    text = str(value or "").lower()
    return "monthly usage hard limit exceeded" in text or "monthly usage limit" in text


def wrap_tick_job(base_tick_job):
    """Turn Apify monthly-limit failures into a clean paused state, not 500/retry spam."""
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
            "Apify остановил новые Actor/KVS-запросы из-за месячного hard limit. Пополни/увеличь лимит Apify и запусти поиск снова. Сайт и локальное состояние не потеряны.",
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

    # growth wrappers read these globals dynamically, so this replaces the v9
    # source set without forking the stable request-state-machine.
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

    add_radar_log(
        "BUDGET AI v10 включён: только высокосигнальные AI-video/comedy источники; один discovery-run защищён hard budget guard <$5.",
        stage="startup",
        details={"profile": PROFILE_VERSION, **info},
    )
    return info
