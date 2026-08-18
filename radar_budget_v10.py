"""Cost guard for the mass AI radar.

High recall is preserved, but discovery spends money on a compact set of
high-frequency AI/video terms instead of many long-tail hashtags. Every Apify
Actor has a platform-side dollar cap plus max-items cap. Gemini classification
is also bounded to short <=10-second inputs and a small structured output.
"""

from decimal import Decimal

from google.genai import types

import gemini_service
import radar_growth_v6 as growth
import radar_request_job as radar_job
from cloud_state import load_radar_job, save_radar_job
from config import RADAR_MAX_DURATION_SEC, RADAR_MIN_DURATION_SEC
from media_duration import measure_video_duration
from models import RadarAssessment
from progress import set_radar_status
from radar_logs import add_radar_log

PROFILE_VERSION = "mass_global_ai_v11_highfreq5"
MAX_RUN_BUDGET_USD = 5.00
BUDGET_GUARD_USD = 4.00
BUDGET_HEADROOM_MULTIPLIER = 1.25
BUDGETED_GEMINI_RESERVE_USD = 1.25
MAX_GEMINI_OUTPUT_TOKENS = 512

# Conservative planning rates. Actual Apify runs are also protected by
# max_total_charge_usd below, which is the real platform-side hard stop.
SEARCH_USD_PER_1000 = 2.70
HASHTAG_USD_PER_1000 = 2.70
REEL_USD_PER_1000 = 1.00

# Fewer, much stronger terms; spend depth on high-frequency feeds instead of
# spreading the same budget across weak long-tail tags.
SEARCH_LIMIT = 20
HASHTAG_LIMIT = 28
KEYWORD_RESULTS_LIMIT = 12
MAX_TRACKED_CREATORS = 15
CREATOR_RESULTS_LIMIT = 12
AI_ANALYZE_LIMIT = 360
KEEP_LIMIT = 60

# Maximum Apify charge for one discovery = $3.35. This remains unchanged even
# when source lists are edited later, so discovery cannot silently exceed it.
ACTOR_CAPS_USD = {
    "popular_ai": 1.35,
    "ai_hashtags": 1.35,
    "ai_keywords": 0.35,
    "known_ai_creators": 0.30,
}

# High-frequency search layer. Intentionally broad: Gemini performs the semantic
# AI/comedy/reproducibility filter later. Long-tail query variants are removed.
SEARCH_TERMS = [
    "AI video",
    "AI funny",
    "AI comedy",
    "AI generated video",
    "AI reels",
    "AI slop",
    "ИИ видео",
    "ИИ",
    "нейросеть",
    "Grok",
    "Grok AI",
    "Veo",
    "Veo 3",
    "Omni",
    "Omni AI",
    "Gemini",
    "Kling AI",
    "Seedance",
    "Sora AI",
    "OpenAI video",
]

# Only broad/high-frequency AI hashtags and major current video-model brands.
# Explicitly removed long-tail combinations such as #geminiomni, #googleflowai,
# #нейросетьприкол, #seedancevideo, #soravideo, #minimaxvideo, etc.
HASHTAGS = [
    "ai",
    "ии",
    "нейросеть",
    "нейросети",
    "aivideo",
    "aivideos",
    "grok",
    "grokai",
    "veo",
    "veo3",
    "omni",
    "omniai",
    "gemini",
    "chatgpt",
    "openai",
    "klingai",
    "seedance",
    "sora",
]

# Small secondary keyword layer: only the broadest AI/video brands.
KEYWORD_TERMS = [
    "AI",
    "ИИ",
    "Grok",
    "Veo",
    "Omni",
    "Gemini",
    "Kling AI",
    "Seedance",
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
        "gemini_max_output_tokens_per_reel": MAX_GEMINI_OUTPUT_TOKENS,
        "gemini_max_reels": AI_ANALYZE_LIMIT,
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


def classify_budget_video(file_path, caption=""):
    """High-recall AI-gag classifier with bounded output tokens."""
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < RADAR_MIN_DURATION_SEC or measured > RADAR_MAX_DURATION_SEC:
        return RadarAssessment(
            is_russian=False,
            is_ai_video=False,
            is_comedy_scene=False,
            is_tutorial_or_review=False,
            is_talking_head=False,
            simple_situation=False,
            strong_first_frame=False,
            one_clear_joke_or_twist=False,
            characters_count=0,
            scene_description="",
            characters=[],
            joke="",
            hook="",
            ending="",
            reproducible_format=False,
            reason=f"Фактическая длительность MP4 {measured:.2f} сек вне диапазона {RADAR_MIN_DURATION_SEC:.1f}–{RADAR_MAX_DURATION_SEC:.2f}",
        )

    prompt = f"""Проверь короткий Instagram Reel как кандидат для радара повторяемых AI-видео.
PASS нужен с высоким recall. Язык исходника НЕ ограничение: иностранную речь потом локализуем на русский.
Считай подходящими AI-сценки, AI-slop, короткий абсурд, визуальный гэг, реакцию, AI-персонажа в камеру, бабушек/дедов/животных/семью, мини-диалог или один понятный панчлайн.
REJECT только если это обычная реальная съёмка/мем без AI, tutorial/обзор сервиса, обычный реальный talking head, бессюжетный продукт/пейзаж/музмонтаж или механику нельзя воспроизвести.
is_russian = язык исходника, но false не означает reject.
is_talking_head=true только для обычного реального блогера/эксперта; AI-персонаж внутри гэга не talking head.
reproducible_format=true если структуру можно повторить с другими персонажами/локацией/русской репликой.
Ответ строго по схеме и максимально кратко.
Caption вторичен: {str(caption or '')[:1200]}""".strip()

    def run(client, uploaded):
        response = client.models.generate_content(
            model=gemini_service.RADAR_MODEL,
            contents=types.Content(parts=[
                gemini_service.video_part(uploaded, gemini_service.RADAR_VIDEO_FPS),
                types.Part(text=prompt),
            ]),
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                response_mime_type="application/json",
                response_schema=RadarAssessment,
                max_output_tokens=MAX_GEMINI_OUTPUT_TOKENS,
            ),
        )
        return gemini_service.parse_response(response, RadarAssessment)

    return gemini_service.with_uploaded_file(file_path, run)


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
    gemini_service.classify_radar_video = classify_budget_video

    add_radar_log(
        "BUDGET AI v11: high-frequency #ai/#ии + major AI video brands; long-tail tags removed; Apify hard caps; Gemini bounded; общий план <$5.",
        stage="startup",
        details={"profile": PROFILE_VERSION, "actor_caps": ACTOR_CAPS_USD, **info},
    )
    return info