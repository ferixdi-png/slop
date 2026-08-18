"""Dialogue radar v16: reject static-image Reels and scale useful output.

Goal: materially more short funny spoken scenes without increasing the $5 run
budget. Discovery is rebalanced toward high-yield comedy/dialogue sources; static
image MP4s are rejected locally before Gemini; AI origin remains metadata only.
"""

from google.genai import types

import gemini_service
import radar_budget_v10 as budget
import radar_dialogue_v14 as dialogue
import radar_growth_v6 as growth
import radar_quality
import radar_request_job as radar_job
import radar_service
from config import RADAR_MAX_DURATION_SEC, RADAR_MIN_DURATION_SEC
from db import db_conn
from media_duration import measure_video_duration
from models import RadarAssessment
from progress import set_radar_status
from radar_logs import add_radar_log
from static_video_gate import inspect_visual_motion


PROFILE_VERSION = "dialogue_motion_v16_scale3_budget5"
TARGET_MATCHES = 180
KEEP_LIMIT = 180
GEMINI_ANALYZE_LIMIT = 420
RADAR_CLASSIFICATION_FPS = 1.0
MAX_CLASSIFICATION_OUTPUT_TOKENS = 320
FINAL_QUERY_LIMIT = 360

# Same actor set and same platform-side dollar hard caps as before. We only spend
# the available discovery budget on broader/high-yield comedy feeds.
SEARCH_LIMIT = 20
HASHTAG_LIMIT = 24
KEYWORD_RESULTS_LIMIT = 12

SEARCH_TERMS = [
    "смешной диалог",
    "смешная сценка",
    "короткий прикол",
    "короткий скетч",
    "вопрос ответ прикол",
    "смешная перепалка",
    "муж жена прикол",
    "отношения прикол",
    "семейный прикол",
    "бабушка прикол",
    "дед прикол",
    "funny dialogue",
    "funny conversation",
    "comedy dialogue",
    "comedy skit",
    "short comedy skit",
    "funny question answer",
    "funny argument",
    "couple comedy",
    "relationship comedy",
    "marriage comedy",
    "family comedy",
    "grandma comedy",
    "POV comedy",
]

HASHTAGS = [
    "юмор",
    "приколы",
    "смешно",
    "скетч",
    "комедия",
    "comedy",
    "funny",
    "skit",
    "funnyreels",
    "comedyreels",
    "relatable",
    "couplecomedy",
    "relationshiphumor",
    "familycomedy",
    "ai",
    "aivideo",
    "grok",
    "veo",
    "omni",
    "kling",
]

KEYWORD_TERMS = [
    "смешной диалог",
    "прикол",
    "скетч",
    "отношения юмор",
    "семейный юмор",
    "funny dialogue",
    "comedy skit",
    "couple comedy",
    "family comedy",
    "Grok",
    "Veo",
    "Kling",
]


def _reject_assessment(reason: str, measured: float = 0.0) -> RadarAssessment:
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
        reason=reason,
        has_spoken_dialogue=False,
        dialogue_is_comedic=False,
        dialogue_summary="",
        detected_language="",
    )


def matches_dialogue_v16(a: RadarAssessment) -> bool:
    """High-recall target: a funny spoken bit is enough; AI origin never decides PASS."""
    if a.is_tutorial_or_review:
        return False
    return bool(a.has_spoken_dialogue and a.dialogue_is_comedic)


def classify_dialogue_v16(file_path: str, caption: str = "") -> RadarAssessment:
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < RADAR_MIN_DURATION_SEC or measured > RADAR_MAX_DURATION_SEC:
        return _reject_assessment(
            f"DURATION_GATE: фактическая длительность MP4 {measured:.2f} сек вне диапазона "
            f"{RADAR_MIN_DURATION_SEC:.1f}–{RADAR_MAX_DURATION_SEC:.2f} сек",
            measured,
        )

    motion = inspect_visual_motion(file_path)
    if motion.checked and motion.is_static_image_video:
        add_radar_log(
            "STATIC IMAGE GATE: Reel отклонён локально до Gemini.",
            level="WARN",
            stage="static-gate",
            details={
                "duration_sec": motion.duration_sec,
                "expected_samples": motion.expected_samples,
                "retained_motion_frames": motion.retained_motion_frames,
                "retained_ratio": motion.retained_ratio,
            },
        )
        return _reject_assessment(f"REJECT_STATIC_IMAGE: {motion.reason}", measured)

    prompt = f"""Ты high-recall классификатор очень коротких Instagram Reels для радара смешных ДИАЛОГОВ до 10 секунд.

ЦЕЛЬ: пропускать как можно больше реально полезных коротких юморных сценок с СЛЫШИМОЙ речью. Видео может быть обычной реальной съёмкой или AI — происхождение не влияет на PASS.

PASS если одновременно:
1 есть реально слышимая речь: диалог, вопрос-ответ, короткая перепалка или одна смешная реплика/панчлайн;
2 именно речь создаёт шутку, неожиданность или смешную реакцию;
3 это не tutorial, обзор, обучалка или обычное информационное объяснение.

Не требуй идеальной вирусной структуры, нескольких персонажей или явной AI-генерации. Один человек с короткой смешной репликой тоже подходит. Иностранный язык подходит: production позже естественно адаптирует речь на русский с сохранением ролей, смысла и тайминга.

СТАТИЧНЫЕ КАРТИНКИ — ЖЁСТКИЙ REJECT:
Если Reel визуально является одной неподвижной картинкой, иллюстрацией, текстовой карточкой, цитатой, постером, скриншотом или почти неподвижным слайдом без настоящего действия в кадре — REJECT даже если поверх есть музыка, озвучка, субтитры, лёгкий zoom/pan или микроскопическая анимация. Нужна настоящая движущаяся видеосцена с человеком/персонажем/объектами и наблюдаемым действием или реакцией.

is_ai_video — только справочное поле.
has_spoken_dialogue=true только если речь реально слышна.
dialogue_is_comedic=true если речь/ответ/реплика создаёт юмористический эффект.
is_talking_head=true только для информационного монолога; короткая шутка в камеру допустима.
reproducible_format оценивай отдельно, но false само по себе НЕ должно мешать dialogue_is_comedic=true.
dialogue_summary — краткий смысл без длинной транскрипции.
detected_language — фактический язык.
Ответ строго по JSON-схеме и максимально кратко.
Caption вторичен: {str(caption or '')[:1000]}""".strip()

    def run(client, uploaded):
        response = client.models.generate_content(
            model=gemini_service.RADAR_MODEL,
            contents=types.Content(parts=[
                gemini_service.video_part(uploaded, RADAR_CLASSIFICATION_FPS),
                types.Part(text=prompt),
            ]),
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                response_mime_type="application/json",
                response_schema=RadarAssessment,
                max_output_tokens=MAX_CLASSIFICATION_OUTPUT_TOKENS,
            ),
        )
        return gemini_service.parse_response(response, RadarAssessment)

    return gemini_service.with_uploaded_file(file_path, run)


def finalize_scale_v16(job):
    """Same finalization semantics as v5, but allow a real TOP up to 180 rows."""
    candidates = job.get("candidates") or []
    set_radar_status(
        "running",
        "Формирую расширенный TOP",
        92,
        45,
        "Пересчитываю качество, аномалии и мету недели. Выдача может содержать до 180 подходящих роликов.",
        details={
            "ai_total": len(candidates),
            "ai_done": sum(1 for x in candidates if x.get("ai_done")),
            "run_id": job.get("run_id"),
            "keep_limit": KEEP_LIMIT,
        },
    )

    with db_conn() as conn:
        radar_quality.refresh_recent_scores_quality(conn)
        rows = conn.execute(
            f"""SELECT * FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-7 days') AND ai_match=1
               ORDER BY viral_score_v2 DESC,views_per_hour DESC,views DESC
               LIMIT {int(FINAL_QUERY_LIMIT)}"""
        ).fetchall()
    top_rows = [dict(row) for row in rows if dialogue.top_eligible_dialogue(dict(row))][:KEEP_LIMIT]

    meta_error = ""
    if top_rows:
        try:
            with db_conn() as conn:
                radar_quality.save_meta_report_quality(conn, top_rows)
                conn.commit()
        except Exception as exc:
            meta_error = str(exc)[:300]
            add_radar_log(f"Мета недели не собрана: {exc}", level="WARN", stage="meta")

    try:
        radar_job.save_radar_snapshot()
    except Exception as exc:
        add_radar_log(f"Финальный snapshot не сохранён: {exc}", level="WARN", stage="snapshot")

    done = sum(1 for item in candidates if item.get("ai_done"))
    matched = sum(1 for item in candidates if item.get("ai_done") and item.get("ai_match"))
    errors = sum(1 for item in candidates if item.get("ai_error"))
    stats = job.get("stats") or {}
    static_rejected = sum(
        1 for item in candidates
        if str(((item.get("assessment") or {}).get("reason") or "")).startswith("REJECT_STATIC_IMAGE")
    )
    result = {
        "raw": stats.get("raw", 0),
        "after_numeric_filter": stats.get("numeric_candidates", 0),
        "ai_checked": done,
        "matched": matched,
        "errors": errors,
        "source_errors": len(job.get("source_failures") or {}),
        "static_rejected": static_rejected,
        "kept": len(top_rows),
        "meta_error": meta_error,
    }
    job["phase"] = "done"
    job["completed_at"] = radar_job._now_iso()
    job["result"] = result
    job["error"] = ""
    job["current_ai_index"] = None
    job["current_ai_post_url"] = ""
    radar_job._persist(job)

    set_radar_status(
        "done",
        "Поиск завершён",
        100,
        0,
        f"Собрано {result['raw']} → проверено {done} → статичных картинок отброшено {static_rejected} → в TOP {len(top_rows)}.",
        warning=(f"Мета: {meta_error}" if meta_error else ""),
        details=result,
    )
    add_radar_log("DIALOGUE MOTION v16 DONE.", stage="done", details=result)
    return job


def apply_scale_v16_overrides():
    # Cost guard: keep existing platform-side actor dollar caps unchanged. Only
    # rebalance result depth and make each Gemini radar check cheaper.
    budget.PROFILE_VERSION = PROFILE_VERSION
    budget.SEARCH_LIMIT = SEARCH_LIMIT
    budget.HASHTAG_LIMIT = HASHTAG_LIMIT
    budget.KEYWORD_RESULTS_LIMIT = KEYWORD_RESULTS_LIMIT
    budget.AI_ANALYZE_LIMIT = GEMINI_ANALYZE_LIMIT
    budget.KEEP_LIMIT = KEEP_LIMIT
    budget.MAX_GEMINI_OUTPUT_TOKENS = MAX_CLASSIFICATION_OUTPUT_TOKENS
    budget.SEARCH_TERMS = list(SEARCH_TERMS)
    budget.HASHTAGS = list(HASHTAGS)
    budget.KEYWORD_TERMS = list(KEYWORD_TERMS)

    growth.PROFILE_VERSION = PROFILE_VERSION
    growth.TARGET_MATCHES = TARGET_MATCHES
    growth.MIN_AI_CHECKS_BEFORE_EARLY_STOP = TARGET_MATCHES
    growth.AI_ANALYZE_LIMIT = GEMINI_ANALYZE_LIMIT
    growth.KEEP_LIMIT = KEEP_LIMIT
    growth.SEARCH_LIMIT = SEARCH_LIMIT
    growth.HASHTAG_LIMIT = HASHTAG_LIMIT
    growth.SEARCH_QUERY = growth._sanitize_search_csv(", ".join(SEARCH_TERMS))
    growth.RAW_SEARCH_QUERY = ", ".join(SEARCH_TERMS)
    growth.HASHTAGS_V7 = list(HASHTAGS)
    growth.RAW_KEYWORD_TERMS = list(KEYWORD_TERMS)
    growth.KEYWORD_TERMS = growth._sanitize_keyword_terms(KEYWORD_TERMS)

    radar_job.RADAR_AI_ANALYZE_LIMIT = GEMINI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = KEEP_LIMIT
    radar_service.RADAR_KEEP_LIMIT = KEEP_LIMIT

    gemini_service.classify_radar_video = classify_dialogue_v16
    radar_job.matches = matches_dialogue_v16
    radar_service.matches = matches_dialogue_v16
    radar_quality.top_eligible = dialogue.top_eligible_dialogue
    radar_job.top_eligible = dialogue.top_eligible_dialogue

    # growth._finalize_v6 calls this global at runtime, so replacing it here lifts
    # the old SQL LIMIT 120 without disturbing the durable request-state machine.
    growth._ORIGINAL_FINALIZE = finalize_scale_v16

    info = budget._assert_budget()
    add_radar_log(
        "DIALOGUE MOTION v16: статичные image-Reels режутся локально; цель/выдача до 180; AI не обязателен; hard budget <$5 сохранён.",
        stage="startup",
        details={
            "profile": PROFILE_VERSION,
            "target_matches": TARGET_MATCHES,
            "keep_limit": KEEP_LIMIT,
            "gemini_analyze_limit": GEMINI_ANALYZE_LIMIT,
            "radar_video_fps": RADAR_CLASSIFICATION_FPS,
            "classification_output_tokens": MAX_CLASSIFICATION_OUTPUT_TOKENS,
            "hashtags": len(HASHTAGS),
            "search_terms": len(SEARCH_TERMS),
            **info,
        },
    )
    return info
