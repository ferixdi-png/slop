"""Dialogue-first discovery and classification profile.

Primary target: short funny spoken scenes up to 10 seconds that can be recreated.
The source may be AI-generated or ordinary real video. Non-Russian dialogue is
allowed because the production pipeline localizes it to Russian while preserving
speaker ownership, joke, order and timing.
"""

from google.genai import types

import gemini_service
import radar_budget_v10 as budget
import radar_growth_v6 as growth
import radar_normalize
import radar_quality
import radar_request_job as radar_job
import radar_service
from config import RADAR_MAX_DURATION_SEC, RADAR_MIN_DURATION_SEC
from media_duration import measure_video_duration
from models import RadarAssessment
from radar_logs import add_radar_log

PROFILE_VERSION = "dialogue_first_v14_core5"
TARGET_MATCHES = 60
AI_ANALYZE_LIMIT = 420
KEEP_LIMIT = 60
SEARCH_LIMIT = 20
HASHTAG_LIMIT = 24
KEYWORD_RESULTS_LIMIT = 12

HASHTAGS = [
    "ai",
    "ии",
    "aivideo",
    "grok",
    "veo",
    "omni",
    "kling",
    "klingai",
    "seedance",
    "юмор",
    "приколы",
    "прикол",
    "смешно",
    "скетч",
    "комедия",
    "funny",
    "comedy",
    "skit",
]

SEARCH_TERMS = [
    "смешной диалог",
    "юмор диалог",
    "короткий скетч",
    "смешная сценка",
    "прикол диалог",
    "муж жена прикол",
    "семейный прикол",
    "бабушка прикол",
    "funny dialogue",
    "comedy dialogue",
    "comedy skit",
    "funny skit",
    "short comedy",
    "couple comedy",
    "family comedy",
    "AI funny",
    "Grok",
    "Veo",
    "Omni",
    "Kling",
]

KEYWORD_TERMS = [
    "юмор",
    "прикол",
    "скетч",
    "смешной диалог",
    "funny",
    "comedy",
    "Grok",
    "Veo",
    "Omni",
    "Kling",
]

DIALOGUE_HINTS = (
    "диалог", "юмор", "прикол", "смеш", "скетч", "сценк", "комед",
    "шутк", "муж", "жена", "семья", "бабуш", "дед", "funny",
    "comedy", "skit", "dialogue", "dialog",
)


def matches_dialogue_first(a: RadarAssessment) -> bool:
    """Accept a reusable funny dialogue OR a strong reusable AI gag."""
    if a.is_tutorial_or_review:
        return False
    dialogue_hit = bool(a.has_spoken_dialogue and a.dialogue_is_comedic)
    ai_gag_hit = bool(
        a.is_ai_video
        and (a.is_comedy_scene or a.one_clear_joke_or_twist or a.simple_situation)
    )
    repeatable = bool(
        a.reproducible_format
        or (a.simple_situation and (dialogue_hit or a.one_clear_joke_or_twist))
    )
    return bool(repeatable and (dialogue_hit or ai_gag_hit))


def top_eligible_dialogue(row) -> bool:
    """After Gemini PASS, metrics rank the item but no longer hide it."""
    duration = float(row.get("duration_sec") or 0)
    return RADAR_MIN_DURATION_SEC <= duration <= RADAR_MAX_DURATION_SEC


def normalize_dialogue_candidate(raw, source, creator_stats=None):
    """Undo the old AI-only ranking bias and prioritize comedy/dialogue signals."""
    item = radar_normalize.normalize_reel(raw, source, creator_stats)
    if not item:
        return None

    score = float(item.get("viral_score_v2") or 0)
    if item.get("ai_discovery_hint"):
        # radar_normalize added +12 for AI. Keep AI useful, but no longer dominant.
        score = max(0.0, score - 12.0) + 5.0

    blob = f"{item.get('caption','')} {item.get('search_term','')}".lower()
    dialogue_hint = any(token in blob for token in DIALOGUE_HINTS)
    if dialogue_hint:
        score += 14.0
    item["dialogue_discovery_hint"] = dialogue_hint
    item["viral_score_v2"] = round(min(100.0, score), 1)
    return item


def _duration_reject(measured: float) -> RadarAssessment:
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
        reason=(
            f"Фактическая длительность MP4 {measured:.2f} сек вне диапазона "
            f"{RADAR_MIN_DURATION_SEC:.1f}–{RADAR_MAX_DURATION_SEC:.2f} сек"
        ),
        has_spoken_dialogue=False,
        dialogue_is_comedic=False,
        dialogue_summary="",
        detected_language="",
    )


def classify_dialogue_first(file_path, caption=""):
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < RADAR_MIN_DURATION_SEC or measured > RADAR_MAX_DURATION_SEC:
        return _duration_reject(measured)

    prompt = f"""Ты high-recall классификатор коротких Instagram Reels для радара повторяемых вирусных сценок.

ГЛАВНАЯ ЦЕЛЬ: найти КОРОТКИЙ СМЕШНОЙ ДИАЛОГ или мини-сценку до 10 секунд, которую можно переснять/сгенерировать с другими персонажами. Видео НЕ обязано быть AI. Обычная реальная съёмка с хорошим юморным диалогом — отличный кандидат.

ПРИОРИТЕТ PASS:
1. В ролике есть слышимая речь/диалог/короткая реплика с шуткой, конфликтом, неожиданным ответом, бытовым приколом или панчлайном.
2. Ситуация понятна без длинного контекста и помещается в 1–2 предложения.
3. Механику можно повторить с другими персонажами, сохранив структуру диалога и панчлайн.
4. Исходный язык ЛЮБОЙ. is_russian только фиксирует язык. Иностранную речь потом переведём на естественный русский с тем же таймингом и спикерами.
5. AI-видео без диалога тоже может PASS как второй приоритет, если есть очень понятный визуальный гэг/абсурд/реакция и его легко повторить.

ПОЛЯ ДИАЛОГА:
has_spoken_dialogue=true если в видео реально слышна человеческая/синтетическая речь, а не только музыка/звуки.
dialogue_is_comedic=true если сама реплика/обмен репликами является юмористической механикой: смешной вопрос-ответ, конфликт, нелепая фраза, неожиданный ответ, бытовой прикол, короткий панчлайн.
dialogue_summary — очень кратко смысл диалога без длинной транскрипции.
detected_language — фактический язык исходной речи.

НЕ СТАВЬ REJECT только потому что видео реальное, снято на телефон, не AI, не русское, примитивное, кринжовое или с одним персонажем.
REJECT: tutorial/обзор/обучение; реклама без шутки; обычный информационный блог; музыка без диалога/действия; бессюжетный монтаж; диалог без юмористической механики; формат невозможно воспроизвести.

is_talking_head=true только для обычного информационного монолога. Если человек говорит в камеру смешную реплику/панчлайн как часть сценки, это НЕ причина REJECT.
reproducible_format=true если можно повторить структуру с другими героями/локацией/русским переводом.
Ответ строго по JSON-схеме и кратко.
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
                max_output_tokens=budget.MAX_GEMINI_OUTPUT_TOKENS,
            ),
        )
        return gemini_service.parse_response(response, RadarAssessment)

    return gemini_service.with_uploaded_file(file_path, run)


def apply_dialogue_first_overrides():
    budget.PROFILE_VERSION = PROFILE_VERSION
    budget.SEARCH_LIMIT = SEARCH_LIMIT
    budget.HASHTAG_LIMIT = HASHTAG_LIMIT
    budget.KEYWORD_RESULTS_LIMIT = KEYWORD_RESULTS_LIMIT
    budget.AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    budget.KEEP_LIMIT = KEEP_LIMIT
    budget.SEARCH_TERMS = list(SEARCH_TERMS)
    budget.HASHTAGS = list(HASHTAGS)
    budget.KEYWORD_TERMS = list(KEYWORD_TERMS)

    growth.PROFILE_VERSION = PROFILE_VERSION
    growth.TARGET_MATCHES = TARGET_MATCHES
    growth.AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    growth.KEEP_LIMIT = KEEP_LIMIT
    growth.SEARCH_LIMIT = SEARCH_LIMIT
    growth.HASHTAG_LIMIT = HASHTAG_LIMIT
    growth.SEARCH_QUERY = growth._sanitize_search_csv(", ".join(SEARCH_TERMS))
    growth.RAW_SEARCH_QUERY = ", ".join(SEARCH_TERMS)
    growth.HASHTAGS_V7 = list(HASHTAGS)
    growth.RAW_KEYWORD_TERMS = list(KEYWORD_TERMS)
    growth.KEYWORD_TERMS = growth._sanitize_keyword_terms(KEYWORD_TERMS)

    radar_job.RADAR_AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = KEEP_LIMIT
    radar_service.RADAR_KEEP_LIMIT = KEEP_LIMIT

    radar_job.normalize_reel = normalize_dialogue_candidate
    gemini_service.classify_radar_video = classify_dialogue_first
    radar_job.matches = matches_dialogue_first
    radar_service.matches = matches_dialogue_first
    radar_quality.top_eligible = top_eligible_dialogue
    radar_job.top_eligible = top_eligible_dialogue

    info = budget._assert_budget()
    add_radar_log(
        "DIALOGUE-FIRST v14: приоритет смешным диалогам/сценкам до 10 сек; AI не обязателен; иностранная речь допустима и локализуется на русский; hard budget <$5 сохранён.",
        stage="startup",
        details={
            "profile": PROFILE_VERSION,
            "target_matches": TARGET_MATCHES,
            "ai_analyze_limit": AI_ANALYZE_LIMIT,
            "keep_limit": KEEP_LIMIT,
            "hashtags": list(HASHTAGS),
            "search_terms": list(SEARCH_TERMS),
            "keyword_terms": list(KEYWORD_TERMS),
            **info,
        },
    )
    return info
