import os
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types

import gemini_service
import radar_normalize as normalizer
import radar_quality
import radar_request_job as radar_job
import radar_service
from db import db_conn
from models import RadarAssessment
from radar_logs import add_radar_log


PROFILE_VERSION = "more_10s_ai_v6"
MIN_TARGET_DURATION = 7.0
MAX_TARGET_DURATION = 10.05
AI_ANALYZE_LIMIT = 60

# Deliberately AI-specific. The previous pool used generic #юмор / #бабушка /
# #деревня tags, which produced a lot of ordinary non-AI Reels.
POPULAR_SEARCH_TERMS = [
    "нейроюмор",
    "ИИ юмор",
    "AI юмор",
    "нейросеть прикол",
    "AI прикол",
    "нейровидео юмор",
    "ИИ видео юмор",
    "AI video comedy russian",
    "AI бабушка юмор",
    "нейросеть бабушка",
    "AI дед юмор",
    "нейросеть дед",
    "AI деревня юмор",
    "нейросеть деревня юмор",
    "AI муж жена юмор",
    "нейросеть муж жена",
    "AI семья юмор",
    "нейросеть семья прикол",
    "AI животные юмор",
    "нейросеть животные прикол",
    "Veo 3 юмор",
    "Veo 3 прикол",
    "AI slop русский",
    "нейрослоп",
]

# Instagram Hashtag Scraper supports keywordSearch=true. These are passed as
# keyword queries rather than generic hashtags so the pool is much denser in AI.
KEYWORD_REEL_TERMS = [
    "нейроюмор",
    "ии юмор",
    "ai юмор",
    "нейровидео",
    "ии видео",
    "ai видео",
    "нейросеть прикол",
    "ai прикол",
    "нейросеть бабушка",
    "ai бабушка",
    "нейросеть деревня",
    "ai деревня",
    "нейросеть дед",
    "ai дед",
    "veo 3 юмор",
    "veo3 прикол",
    "ai slop",
    "нейрослоп",
]


_ORIGINAL_SAVE_POST = radar_quality._legacy_save_post
_ORIGINAL_ENSURE_SNAPSHOT = radar_job._ensure_snapshot_once


def _targeted_sources():
    sources = {
        "popular": {
            "actor_id": radar_job.APIFY_SEARCH_ACTOR,
            "input": {
                "search": ", ".join(POPULAR_SEARCH_TERMS),
                "searchType": "popular",
                # Per search phrase. 40 x targeted phrases is still below the
                # theoretical previous broad pool while giving much better recall.
                "searchLimit": 40,
            },
        },
        "hashtags": {
            "actor_id": radar_job.APIFY_HASHTAG_ACTOR,
            "input": {
                "hashtags": KEYWORD_REEL_TERMS,
                "keywordSearch": True,
                "resultsType": "reels",
                "resultsLimit": 18,
            },
        },
    }

    tracked = radar_job._tracked_creators()[:60]
    if tracked:
        sources["creators"] = {
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
        source.update(
            run_id="",
            status="NOT_STARTED",
            dataset_id="",
            status_message="",
            started_at="",
        )
    return sources


def _normalize_reel(raw, source, creator_stats=None):
    shortcode = raw.get("shortCode") or raw.get("shortcode") or raw.get("short_code") or raw.get("code") or ""
    url = (
        raw.get("url")
        or raw.get("reelUrl")
        or raw.get("reel_url")
        or raw.get("postUrl")
        or raw.get("post_url")
        or raw.get("original_url")
        or ""
    )
    if not url and shortcode:
        url = f"https://www.instagram.com/reel/{shortcode}/"

    video_url = (
        raw.get("videoUrl")
        or raw.get("video_url")
        or raw.get("videoSrc")
        or raw.get("video_src")
        or raw.get("downloadedVideoUrl")
        or raw.get("downloaded_video_url")
        or (raw.get("mediaDownloadUrl") if isinstance(raw.get("mediaDownloadUrl"), str) else "")
        or ""
    )
    preview = (
        raw.get("displayUrl")
        or raw.get("display_url")
        or raw.get("thumbnailUrl")
        or raw.get("thumbnail_url")
        or raw.get("thumbnail_src")
        or raw.get("image")
        or ""
    )
    creator = normalizer.creator_of(raw)
    caption = raw.get("caption") or raw.get("text") or raw.get("description") or ""
    duration = normalizer.safe_float(
        raw.get("videoDuration"),
        raw.get("video_duration"),
        raw.get("video_duration_secs"),
        raw.get("duration_seconds"),
        raw.get("durationSeconds"),
        raw.get("duration"),
    )
    views = normalizer.views_of(raw)
    likes = normalizer.safe_int(
        raw.get("likesCount"), raw.get("likeCount"), raw.get("like_count"), raw.get("likes")
    )
    comments = normalizer.safe_int(
        raw.get("commentsCount"), raw.get("commentCount"), raw.get("comment_count"),
        raw.get("comments_count"), raw.get("comments")
    )
    published = normalizer.parse_dt(
        raw.get("timestamp")
        or raw.get("timestampUnix")
        or raw.get("takenAtTimestamp")
        or raw.get("taken_at_timestamp")
        or raw.get("takenAt")
        or raw.get("taken_at")
        or raw.get("publishedAt")
        or raw.get("published_at")
        or raw.get("posted_at")
        or raw.get("upload_date")
        or raw.get("date")
    )

    if not url or not creator or not published:
        return None
    # User target: short near-10-second Reels. Keep enough room for 7–10 second
    # clips, but remove 2–5 second fragments and everything actually over 10 sec.
    if duration < MIN_TARGET_DURATION or duration > MAX_TARGET_DURATION:
        return None
    if published < datetime.now(timezone.utc) - timedelta(days=7):
        return None

    stats = (creator_stats or {}).get(creator, {})
    followers = normalizer.followers_of(raw) or int(stats.get("followers_count", 0))
    usual_views = float(stats.get("usual_views", 0))
    age_hours = max(0.25, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    vph = round(views / age_hours, 2)

    # Much softer pre-Gemini gate. The old gate removed too many fresh Reels
    # before Gemini even had a chance to see whether they were AI comedy.
    if views < 400 and likes < 5 and vph < 1_000:
        return None
    if views < 1_200 and likes < 2 and vph < 2_500:
        return None

    score = radar_service.calculate_viral_score(
        views,
        likes,
        comments,
        round(age_hours, 2),
        vph,
        followers,
        usual_views,
    )

    item = {
        "platform": "Instagram Reels",
        "creator": creator,
        "post_url": url,
        "video_url": str(video_url or ""),
        "preview_url": str(preview or ""),
        "published_at": published.isoformat(),
        "duration_sec": duration,
        "views": views,
        "likes": likes,
        "comments": comments,
        "hours_since_publish": round(age_hours, 2),
        "views_per_hour": vph,
        "followers_count": followers,
        "creator_usual_views": round(usual_views, 1),
        "search_term": raw.get("searchTerm") or raw.get("hashtag") or raw.get("hashtagName") or raw.get("inputUrl") or source,
        "caption": str(caption)[:4000],
        **score,
    }
    item = radar_quality.apply_quality_score(item)

    # Prefer actual 9–10 second clips without throwing away good 7–9 second hits.
    if duration >= 9.0:
        duration_bonus = 9.0
    elif duration >= 8.0:
        duration_bonus = 5.0
    else:
        duration_bonus = 2.0
    item["viral_score_v2"] = round(min(100.0, float(item["viral_score_v2"]) + duration_bonus), 1)
    return item


def matches_v6(assessment):
    humor_ok = bool(assessment.is_comedy_scene or assessment.one_clear_joke_or_twist)
    return all([
        assessment.is_russian,
        assessment.is_ai_video,
        humor_ok,
        not assessment.is_tutorial_or_review,
        not assessment.is_talking_head,
        assessment.simple_situation,
        assessment.reproducible_format,
    ])


def classify_radar_video_v6(file_path, caption=""):
    def run(client, uploaded):
        prompt = f"""
Ты high-recall классификатор коротких российских AI-видео для радара вирусных механик.
Нужно НЕ искать только классический скетч из двух актёров. Наша цель шире: найти максимум реально AI-сделанных коротких юмористических роликов, которые можно повторить.

PASS-КЛАСС:
1 ролик явно сгенерирован или существенно создан AI
2 он рассчитан на русскую аудиторию: русская речь ИЛИ русский текст/подпись ИЛИ однозначный российский бытовой/культурный контекст
3 есть юмористическая механика: бытовая сценка, абсурд, короткий гэг, нелепая реакция, AI-персонаж с одной смешной репликой, животное/бабушка/дед/семья в абсурдной ситуации, визуальный панчлайн
4 механику можно повторить в новом AI-видео
5 ситуация понимается быстро без длинного контекста

ВАЖНЫЕ ОПРЕДЕЛЕНИЯ ДЛЯ ПОЛЕЙ:
is_russian = TRUE если есть русская речь; также TRUE для явно русского контекста без речи или когда видео нейтрально по языку, но русская подпись однозначно задаёт русский юмористический контекст
is_ai_video = TRUE для синтетических людей/животных/локаций, генеративного видео, очевидной AI-анимации или существенной AI-трансформации сцены
is_comedy_scene = TRUE не только для диалога нескольких персонажей; один AI-персонаж, один короткий абсурдный эпизод или визуальный гэг тоже считается comedy scene
is_talking_head = TRUE ТОЛЬКО для обычного реального автора/эксперта, который говорит в камеру как блогер, объясняет или комментирует. Сгенерированный AI-персонаж, который смотрит в камеру и выдаёт смешную реплику внутри гега, НЕ talking head
one_clear_joke_or_twist = TRUE если есть хотя бы один понятный комедийный бит, панчлайн, абсурд или смешная реакция
simple_situation = TRUE если механику можно объяснить одной-двумя фразами
reproducible_format = TRUE если идею реально пересобрать другим персонажем/локацией/репликой

ЖЁСТКО REJECT:
обычная не-AI съёмка
обучалка, инструкция, обзор сервиса, демонстрация как генерировать
реальный блогер talking head
обычный мем из чужого реального видео
слайдшоу без самостоятельного гега
чистая AI-трансформация до/после без шутки или сюжетного бита
статичный дом/пейзаж/предмет без комедийного события

Не занижай recall из-за того, что ролик примитивный, странный, однокадровый, с одним персонажем или выглядит как AI slop. Для этого радара это может быть как раз целевой формат.
Оцени САМО ВИДЕО И АУДИО. Подпись используй как вторичный сигнал для языка, контекста и намерения.

Подпись Instagram:
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

    if duration < MIN_TARGET_DURATION or duration > MAX_TARGET_DURATION:
        return False
    if score < 36:
        return False
    if views < 1_200 and vph < 3_000:
        return False
    if likes == 0:
        return views >= 30_000 and score >= 45
    return likes >= 8 or comments >= 3 or views >= 10_000 or vph >= 7_000


def _save_post_and_learn_creator(conn, item, assessment):
    _ORIGINAL_SAVE_POST(conn, item, assessment)
    if not assessment:
        return
    # Learn from Russian AI creators even when this particular Reel misses the
    # comedy filter. The next radar run then scans that creator's recent Reels.
    if not (assessment.is_ai_video and assessment.is_russian and not assessment.is_tutorial_or_review):
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
            item.get("creator", ""),
            now,
            now,
            float(item.get("views_per_hour") or 0),
            int(item.get("followers_count") or 0),
            float(item.get("creator_usual_views") or 0),
        ),
    )


def _reset_old_classifier_cache_if_needed():
    with db_conn() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key='radar_classifier_profile'").fetchone()
        current = str(row["value"] if row else "")
        if current == PROFILE_VERSION:
            return False

        # Re-run Gemini on recent cached verdicts once because the old classifier
        # was intentionally much stricter and would otherwise remain cached forever.
        conn.execute(
            """UPDATE radar_posts SET
                ai_checked=0, ai_match=0,
                scene_description='', characters_json='[]', joke='', hook='', ending='',
                reproducible=0, reason=''
               WHERE datetime(published_at)>=datetime('now','-7 days')"""
        )
        conn.execute(
            """INSERT INTO app_state(key,value) VALUES('radar_classifier_profile',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (PROFILE_VERSION,),
        )
        conn.commit()
    add_radar_log(
        "Classifier cache сброшен один раз: новые high-recall правила AI-slop должны заново проверить свежие Reels.",
        stage="startup",
        details={"profile": PROFILE_VERSION},
    )
    return True


def _ensure_snapshot_then_profile():
    _ORIGINAL_ENSURE_SNAPSHOT()
    _reset_old_classifier_cache_if_needed()


def apply_growth_overrides():
    # Discovery volume / ranking.
    radar_job.RADAR_AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    radar_job._build_sources = _targeted_sources
    radar_job.normalize_reel = _normalize_reel

    # Classification recall.
    radar_service.matches = matches_v6
    radar_job.matches = matches_v6
    gemini_service.classify_radar_video = classify_radar_video_v6

    # TOP should include strong early winners instead of requiring mature social proof.
    radar_quality.top_eligible = top_eligible_v6
    radar_job.top_eligible = top_eligible_v6

    # Learn AI creators from near-misses and revisit old strict cached verdicts once.
    radar_quality._legacy_save_post = _save_post_and_learn_creator
    radar_job._ensure_snapshot_once = _ensure_snapshot_then_profile
    _reset_old_classifier_cache_if_needed()

    add_radar_log(
        "Growth profile v6 включён: targeted AI discovery, 7–10s duration preference, high-recall Gemini, до 60 AI-кандидатов.",
        stage="startup",
        details={
            "profile": PROFILE_VERSION,
            "min_duration": MIN_TARGET_DURATION,
            "max_duration": MAX_TARGET_DURATION,
            "ai_limit": AI_ANALYZE_LIMIT,
            "popular_terms": len(POPULAR_SEARCH_TERMS),
            "keyword_terms": len(KEYWORD_REEL_TERMS),
        },
    )
