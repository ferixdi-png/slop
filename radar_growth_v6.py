import json
from datetime import datetime, timezone

from google.genai import types

import gemini_service
import radar_quality
import radar_request_job as radar_job
import radar_service
from config import (
    HASHTAGS,
    HASHTAG_LIMIT,
    RADAR_AI_ANALYZE_LIMIT,
    RADAR_KEEP_LIMIT,
    RADAR_MAX_DURATION_SEC,
    RADAR_MIN_DURATION_SEC,
    SEARCH_LIMIT,
    SEARCH_TERMS,
)
from db import db_conn
from media_duration import measure_video_duration
from models import RadarAssessment
from radar_logs import add_radar_log


PROFILE_VERSION = "mass_10s_ai_v6"
TARGET_MATCHES = 75
MIN_AI_CHECKS_BEFORE_EARLY_STOP = 120
KEYWORD_TERMS = [
    "нейроюмор", "ии юмор", "ai юмор", "нейровидео", "ии видео", "ai видео",
    "нейросеть прикол", "ai прикол", "нейросеть бабушка", "ai бабушка",
    "нейросеть деревня", "ai деревня", "нейросеть дед", "ai дед",
    "нейросеть семья", "ai семья", "нейросеть животные", "ai животные",
    "veo 3 юмор", "veo3 прикол", "kling ai юмор", "seedance юмор",
    "ai slop русский", "нейрослоп",
]

_ORIGINAL_SAVE_POST = radar_quality._legacy_save_post
_ORIGINAL_PREPARE = radar_job._prepare_candidates
_ORIGINAL_PROCESS_AI = radar_job._process_one_ai
_ORIGINAL_FINALIZE = radar_job._finalize
_ORIGINAL_SNAPSHOT = radar_job.save_radar_snapshot
_APPLIED = False
_snapshot_calls = 0


def _build_mass_sources():
    search = SEARCH_TERMS[0] if SEARCH_TERMS else ""
    sources = {
        "popular_ai": {
            "actor_id": radar_job.APIFY_SEARCH_ACTOR,
            "input": {"search": search, "searchType": "popular", "searchLimit": SEARCH_LIMIT},
        },
        "ai_hashtags": {
            "actor_id": radar_job.APIFY_HASHTAG_ACTOR,
            "input": {"hashtags": HASHTAGS, "resultsType": "reels", "resultsLimit": HASHTAG_LIMIT},
        },
        "ai_keywords": {
            "actor_id": radar_job.APIFY_HASHTAG_ACTOR,
            "input": {
                "hashtags": KEYWORD_TERMS,
                "keywordSearch": True,
                "resultsType": "reels",
                "resultsLimit": 18,
            },
        },
    }

    tracked = radar_job._tracked_creators()[:100]
    if tracked:
        sources["known_ai_creators"] = {
            "actor_id": radar_job.APIFY_CREATOR_ACTOR,
            "input": {
                "username": tracked,
                "resultsLimit": 12,
                "onlyPostsNewerThan": "7 days",
                "skipPinnedPosts": True,
                "includeTranscript": False,
                "includeDownloadedVideo": False,
            },
        }

    for source in sources.values():
        source.update(run_id="", status="NOT_STARTED", dataset_id="", status_message="", started_at="")
    return sources


def matches_v6(a):
    humor_ok = bool(a.is_comedy_scene or a.one_clear_joke_or_twist)
    situation_ok = bool(a.simple_situation or a.one_clear_joke_or_twist)
    return all([
        a.is_russian,
        a.is_ai_video,
        humor_ok,
        not a.is_tutorial_or_review,
        not a.is_talking_head,
        situation_ok,
        a.reproducible_format,
    ])


def _hard_duration_reject(measured):
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
            f"Фактическая длительность MP4 {measured:.2f} сек не входит в строгий диапазон "
            f"{RADAR_MIN_DURATION_SEC:.1f}–{RADAR_MAX_DURATION_SEC:.2f} сек для 10-секундного радара"
        ),
    )


def classify_radar_video_v6(file_path, caption=""):
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < RADAR_MIN_DURATION_SEC or measured > RADAR_MAX_DURATION_SEC:
        return _hard_duration_reject(measured)

    def run(client, uploaded):
        prompt = f"""
Ты high-recall, но строгий по AI классификатор российского AI-slop/AI-comedy для радара повторяемых вирусных механик.

ЦЕЛЬ: найти максимум настоящих AI-сделанных коротких юмористических роликов около 10 секунд, которые можно пересобрать своим персонажем.

PASS только если ключевое верно:
1 is_ai_video TRUE: само ВИДЕО явно сгенерировано или существенно создано генеративным AI. Обычная съёмка с AI-подписью не считается.
2 is_russian TRUE: русская речь ИЛИ однозначно российский бытовой/культурный контекст; для ролика без речи допустима русская подпись как вторичный сигнал.
3 есть юмористический бит: бытовая сценка, абсурд, короткий гэг, странная реакция, один AI-персонаж с панчлайном, AI-животное, бабушка, дед, семья, деревня, визуальный прикол или нелепый поворот.
4 механику реально повторить в новом генеративном видео.
5 это НЕ урок, НЕ обзор AI-сервиса и НЕ реальный блогер talking head.

is_comedy_scene TRUE также для одного AI-персонажа, однокадрового абсурда и визуального гэга — диалог нескольких людей не обязателен.
one_clear_joke_or_twist TRUE если есть один понятный абсурд, панчлайн, смешная реакция или визуальный поворот.
is_talking_head TRUE только для обычного реального автора/эксперта. Сгенерированный AI-персонаж, который смотрит в камеру внутри короткого гэга, НЕ talking head.
simple_situation TRUE если идею можно пересказать в 1–2 предложениях.
reproducible_format TRUE если можно заменить персонажа/локацию/реплику, сохранив механику.

ЖЁСТКО REJECT: обычная не-AI съёмка; реальный мем из чужого видео; обучалка; обзор нейросети; реальный talking head; статичный дом/пейзаж/товар без шутки; чистое до/после без юмористического бита; музыкальный монтаж без понятной шутки.

НЕ REJECT только потому что ролик примитивный, кринжовый, абсурдный, с одним персонажем или выглядит как дешёвый AI slop. Для этого радара это целевая категория.
Смотри всё видео и слушай аудио. Caption используй только как дополнительный сигнал, а не как доказательство AI.

Instagram caption:
{caption[:2000]}
""".strip()
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
            ),
        )
        return gemini_service.parse_response(response, RadarAssessment)

    return gemini_service.with_uploaded_file(file_path, run)


def top_eligible_v6(row):
    duration = float(row.get("duration_sec") or 0)
    score = float(row.get("viral_score_v2") or 0)
    views = int(row.get("views") or 0)
    likes = int(row.get("likes") or 0)
    comments = int(row.get("comments") or 0)
    vph = float(row.get("views_per_hour") or 0)

    if duration < RADAR_MIN_DURATION_SEC or duration > RADAR_MAX_DURATION_SEC:
        return False
    if score < 24:
        return False
    if views < 500 and vph < 1_500:
        return False
    if likes == 0 and comments == 0:
        return views >= 3_000 or vph >= 4_000
    return likes >= 3 or comments >= 1 or views >= 2_000 or vph >= 3_000


def _save_post_and_learn_ai_creator(conn, item, assessment):
    _ORIGINAL_SAVE_POST(conn, item, assessment)
    if not assessment:
        return
    if not (
        assessment.is_ai_video
        and assessment.is_russian
        and not assessment.is_tutorial_or_review
        and item.get("creator")
    ):
        return

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO tracked_creators(
            username,first_seen_at,last_seen_at,best_views_per_hour,matching_reels,
            followers_count,usual_views,sample_size
        ) VALUES(?,?,?,?,0,?,?,0)
        ON CONFLICT(username) DO UPDATE SET
            last_seen_at=excluded.last_seen_at,
            best_views_per_hour=MAX(tracked_creators.best_views_per_hour,excluded.best_views_per_hour),
            followers_count=CASE WHEN excluded.followers_count>0 THEN excluded.followers_count ELSE tracked_creators.followers_count END,
            usual_views=CASE WHEN excluded.usual_views>0 THEN excluded.usual_views ELSE tracked_creators.usual_views END""",
        (
            item.get("creator", ""), now, now,
            float(item.get("views_per_hour") or 0),
            int(item.get("followers_count") or 0),
            float(item.get("creator_usual_views") or 0),
        ),
    )


def _prepare_candidates_v6(client, job):
    job = _ORIGINAL_PREPARE(client, job)
    candidates = job.get("candidates") or []
    for item in candidates:
        item["ai_done"] = False
        item["ai_match"] = False
        item["ai_attempts"] = 0
        item["ai_error"] = ""
        item.pop("assessment", None)
    job.setdefault("stats", {})["ai_total"] = len(candidates)
    radar_job._persist(job)
    add_radar_log(
        "Mass AI queue prepared: все кандидаты текущего запуска будут просмотрены Gemini заново.",
        stage="filter",
        details={"ai_total": len(candidates), "target_matches": TARGET_MATCHES},
    )
    return job


def _snapshot_throttled():
    global _snapshot_calls
    _snapshot_calls += 1
    if _snapshot_calls == 1 or _snapshot_calls % 10 == 0:
        return _ORIGINAL_SNAPSHOT()
    return True


def _process_one_ai_v6(job):
    job = _ORIGINAL_PROCESS_AI(job)
    candidates = job.get("candidates") or []
    done = sum(1 for x in candidates if x.get("ai_done"))
    matched = sum(1 for x in candidates if x.get("ai_done") and x.get("ai_match"))
    if job.get("phase") == "ai" and done >= MIN_AI_CHECKS_BEFORE_EARLY_STOP and matched >= TARGET_MATCHES:
        job["phase"] = "finalizing"
        job.setdefault("stats", {})["early_stop_after_ai"] = done
        job["stats"]["early_stop_matches"] = matched
        radar_job._persist(job)
        add_radar_log(
            f"Цель достигнута: {matched} AI-роликов после {done} видео-проверок. Перехожу к TOP.",
            stage="gemini-radar",
            details={"ai_done": done, "matched": matched, "target": TARGET_MATCHES},
        )
    return job


def _rebuild_checked_rows_from_job(job):
    rebuilt = 0
    with db_conn() as conn:
        for item in job.get("candidates") or []:
            payload = item.get("assessment")
            if not (item.get("ai_done") and isinstance(payload, dict)):
                continue
            try:
                assessment = RadarAssessment.model_validate(payload)
                radar_quality.save_post_preserve_ai(conn, item, assessment)
                rebuilt += 1
            except Exception:
                continue
        conn.commit()
    return rebuilt


def _finalize_v6(job):
    rebuilt = _rebuild_checked_rows_from_job(job)
    add_radar_log(f"Перед TOP восстановлено {rebuilt} Gemini-вердиктов из durable job.", stage="finalizing")
    result = _ORIGINAL_FINALIZE(job)
    try:
        _ORIGINAL_SNAPSHOT()
    except Exception as exc:
        add_radar_log(f"Финальный mass snapshot не сохранён: {exc}", level="WARN", stage="snapshot")
    return result


def apply_growth_overrides():
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True

    radar_job.RADAR_AI_ANALYZE_LIMIT = RADAR_AI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = RADAR_KEEP_LIMIT
    radar_service.RADAR_KEEP_LIMIT = RADAR_KEEP_LIMIT
    radar_job._build_sources = _build_mass_sources
    gemini_service.classify_radar_video = classify_radar_video_v6
    radar_job.matches = matches_v6
    radar_service.matches = matches_v6
    radar_quality.top_eligible = top_eligible_v6
    radar_job.top_eligible = top_eligible_v6
    radar_quality._legacy_save_post = _save_post_and_learn_ai_creator
    radar_job._prepare_candidates = _prepare_candidates_v6
    radar_job._process_one_ai = _process_one_ai_v6
    radar_job._finalize = _finalize_v6
    radar_job.save_radar_snapshot = _snapshot_throttled

    add_radar_log(
        "MASS 10s AI profile включён: большой AI-only discovery pool, до 240 Gemini-проверок, цель 75 PASS, TOP до 60.",
        stage="startup",
        details={
            "profile": PROFILE_VERSION,
            "duration_min": RADAR_MIN_DURATION_SEC,
            "duration_max": RADAR_MAX_DURATION_SEC,
            "search_limit_per_term": SEARCH_LIMIT,
            "hashtag_limit_per_tag": HASHTAG_LIMIT,
            "ai_analyze_limit": RADAR_AI_ANALYZE_LIMIT,
            "keep_limit": RADAR_KEEP_LIMIT,
            "target_matches": TARGET_MATCHES,
            "keyword_terms": len(KEYWORD_TERMS),
        },
    )
