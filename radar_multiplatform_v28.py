"""V28 final product overlay: Instagram + TikTok + YouTube, strict tags, speech and timing.

This module is applied after the proven V27 stack. It deliberately preserves the
request-driven durable job, Render liveness, retry/cancel guards, strict duration
rules and V27 production reconstruction. It replaces only the final product
scope: three platforms, five real post-level hashtags, a 14-day window and a
mandatory Gemini speech/timing verdict for every non-static candidate.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from apify_client import ApifyClient
from google import genai
from google.genai import types

from actor_utils import run_actor_items_checked
import gemini_service
import radar_budget_v10 as budget
import radar_growth_v6 as growth
import radar_hardening_v19 as hardening
import radar_omni_veo_v21 as v21
import radar_omni_veo_veo3_v24 as v24
import radar_quality
import radar_request_job as radar_job
import radar_resilient_v17 as v17
import radar_service
from db import db_conn
from media_duration import measure_video_duration
from models import RadarAssessment
from progress import set_radar_status
from radar_logs import add_radar_log
from reel_media import download_reel_for_analysis as download_instagram_video
from static_video_gate import inspect_visual_motion

MODE_VERSION = "multiplatform_speech_v28_strict14d"
SCREENING_PROFILE = MODE_VERSION
SOURCE_MARKER = "STRICT_MULTIPLATFORM_SPEECH_V28"
TARGET_TAGS = ("omni", "veo", "veo3", "ai", "ии")
TARGET_SET = frozenset(TARGET_TAGS)
PLATFORMS = ("Instagram Reels", "TikTok", "YouTube Shorts")
PLATFORM_SET = frozenset(PLATFORMS)
LOOKBACK_DAYS = 14
RESULTS_PER_TAG = 120
AI_ANALYZE_LIMIT = 420
KEEP_LIMIT = 180
DIRECT_MAX_DURATION_SEC = 10.05
SOURCE_MAX_DURATION_SEC = 15.05
COMPRESSED_TARGET_SEC = 10.00
PASS_PREFIX = "PASS_SPEECH_TIMING_V28:"

INSTAGRAM_ACTOR = os.environ.get("APIFY_V28_INSTAGRAM_ACTOR", "apify/instagram-scraper").strip()
TIKTOK_ACTOR = os.environ.get("APIFY_V28_TIKTOK_ACTOR", "clockworks/tiktok-scraper").strip()
YOUTUBE_ACTOR = os.environ.get(
    "APIFY_V28_YOUTUBE_ACTOR", "constructive_calm/youtube-shorts-scraper-pro"
).strip()

_APPLIED = False
_HASHTAG_RE = re.compile(r"(?<!\w)#([\w]+)", re.UNICODE)


def _clamp01(value):
    return max(0.0, min(1.0, float(value or 0)))


def _safe_int(*values):
    for value in values:
        if value in (None, ""):
            continue
        try:
            return max(0, int(float(value)))
        except Exception:
            continue
    return 0


def _safe_float(*values):
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return 0.0


def _dict(value):
    return value if isinstance(value, dict) else {}


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _parse_dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        try:
            stamp = float(value)
            stamp = stamp / 1000 if stamp > 10_000_000_000 else stamp
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except Exception:
            return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _clean_tag(value):
    text = str(value or "").strip().lower().lstrip("#")
    return text if text and re.fullmatch(r"[\w]+", text, re.UNICODE) else ""


def _actual_hashtags(raw):
    """Read hashtags from the post itself; source/query provenance never counts."""
    raw = raw or {}
    found = set()
    for key in (
        "hashtags", "captionHashtags", "caption_hashtags", "captionTags", "caption_tags", "tags"
    ):
        value = raw.get(key)
        values = value if isinstance(value, (list, tuple, set)) else [value] if value else []
        for entry in values:
            if isinstance(entry, dict):
                entry = entry.get("name") or entry.get("tag") or entry.get("hashtag") or entry.get("title") or ""
            if not isinstance(entry, str):
                continue
            explicit = _HASHTAG_RE.findall(entry)
            if explicit:
                for tag in explicit:
                    cleaned = _clean_tag(tag)
                    if cleaned:
                        found.add(cleaned)
            else:
                for part in re.split(r"[\s,;]+", entry):
                    cleaned = _clean_tag(part)
                    if cleaned:
                        found.add(cleaned)

    blob = " ".join(
        str(raw.get(key) or "")
        for key in ("caption", "text", "description", "title")
        if raw.get(key)
    )
    for tag in _HASHTAG_RE.findall(blob):
        cleaned = _clean_tag(tag)
        if cleaned:
            found.add(cleaned)
    return found


def _verified_target_tag(raw):
    actual = _actual_hashtags(raw)
    for tag in TARGET_TAGS:
        if tag in actual:
            return tag
    return ""


def _platform_from(raw, source=""):
    source_text = str(source or "").lower()
    url = str(
        _first(
            (raw or {}).get("webVideoUrl"),
            (raw or {}).get("url"),
            (raw or {}).get("postUrl"),
            (raw or {}).get("post_url"),
        )
    ).lower()
    if "tiktok" in source_text or "tiktok.com" in url:
        return "TikTok"
    if "youtube" in source_text or "youtube.com" in url or "youtu.be" in url:
        return "YouTube Shorts"
    if "instagram" in source_text or "instagram.com" in url:
        return "Instagram Reels"
    return ""


def _media_url(raw, platform):
    if platform == "TikTok":
        video_meta = _dict(raw.get("videoMeta"))
        values = [
            raw.get("videoUrl"), raw.get("video_url"), raw.get("downloadUrl"),
            video_meta.get("downloadAddr"), video_meta.get("downloadUrl"),
        ]
        media = raw.get("mediaUrls") or []
        if isinstance(media, list):
            for entry in media:
                if isinstance(entry, str):
                    values.append(entry)
                elif isinstance(entry, dict):
                    values.extend([
                        entry.get("downloadLink"), entry.get("downloadUrl"),
                        entry.get("url"), entry.get("uri"),
                    ])
        return str(_first(*values) or "")
    if platform == "YouTube Shorts":
        return str(_first(raw.get("videoDownloadUrl"), raw.get("video_url"), raw.get("videoUrl")) or "")
    return str(
        _first(
            raw.get("videoUrl"), raw.get("video_url"), raw.get("videoSrc"),
            raw.get("video_src"), raw.get("downloadedVideoUrl"),
            raw.get("downloaded_video_url"),
            raw.get("mediaDownloadUrl") if isinstance(raw.get("mediaDownloadUrl"), str) else "",
        ) or ""
    )


def _momentum_score(views, vph, hours, base_score, acceleration=0.0, has_history=False):
    velocity = _clamp01(math.log1p(max(0.0, float(vph or 0))) / math.log1p(100_000))
    freshness = _clamp01(1.0 - max(0.0, float(hours or 0)) / (24.0 * LOOKBACK_DAYS))
    proof = _clamp01(math.log1p(max(0, int(views or 0))) / math.log1p(1_000_000))
    base = _clamp01(float(base_score or 0) / 100.0)
    if has_history:
        accel = _clamp01(math.log1p(max(0.0, float(acceleration or 0))) / math.log1p(10.0))
        score = 100.0 * (0.58 * velocity + 0.18 * accel + 0.14 * freshness + 0.08 * proof + 0.02 * base)
    else:
        score = 100.0 * (0.72 * velocity + 0.16 * freshness + 0.09 * proof + 0.03 * base)
    return round(_clamp01(score / 100.0) * 100.0, 1)


def normalize_multiplatform_candidate(raw, source, creator_stats=None):
    raw = dict(raw or {})
    tag = _verified_target_tag(raw)
    if not tag:
        return None

    platform = _platform_from(raw, source)
    if platform not in PLATFORM_SET:
        return None
    if platform == "TikTok" and bool(raw.get("isSlideshow")):
        return None

    owner = _dict(raw.get("owner"))
    user = _dict(raw.get("user"))
    author = _dict(raw.get("author"))
    author_meta = _dict(raw.get("authorMeta"))
    video_meta = _dict(raw.get("videoMeta"))

    if platform == "TikTok":
        post_url = str(raw.get("webVideoUrl") or "")
        creator = str(author_meta.get("name") or author_meta.get("nickName") or raw.get("author") or "")
        caption = str(raw.get("text") or raw.get("description") or "")
        duration = _safe_float(video_meta.get("duration"), raw.get("videoMeta.duration"), raw.get("duration"))
        views = _safe_int(raw.get("playCount"), raw.get("views"))
        likes = _safe_int(raw.get("diggCount"), raw.get("likeCount"), raw.get("likes"))
        comments = _safe_int(raw.get("commentCount"), raw.get("comments"))
        followers = _safe_int(author_meta.get("fans"), raw.get("authorMeta.fans"))
        published = _parse_dt(raw.get("createTimeISO") or raw.get("createTime"))
        preview = str(video_meta.get("coverUrl") or raw.get("videoMeta.coverUrl") or "")
    elif platform == "YouTube Shorts":
        post_url = str(raw.get("url") or raw.get("shortUrl") or raw.get("videoUrl") or "")
        creator = str(raw.get("channelName") or raw.get("channelTitle") or raw.get("author") or "")
        caption = "\n".join(x for x in [str(raw.get("title") or ""), str(raw.get("description") or "")] if x)
        duration = _safe_float(raw.get("durationSeconds"), raw.get("duration_sec"), raw.get("duration"))
        views = _safe_int(raw.get("viewCount"), raw.get("views"))
        likes = _safe_int(raw.get("likeCount"), raw.get("likes"))
        comments = _safe_int(raw.get("commentCount"), raw.get("comments"))
        followers = _safe_int(raw.get("channelSubscribers"), raw.get("subscriberCount"))
        published = _parse_dt(raw.get("publishedAt") or raw.get("published_at"))
        preview = str(raw.get("thumbnail") or raw.get("thumbnailUrl") or "")
    else:
        shortcode = raw.get("shortCode") or raw.get("shortcode") or raw.get("short_code") or raw.get("code") or ""
        post_url = str(raw.get("url") or raw.get("reelUrl") or raw.get("postUrl") or raw.get("post_url") or "")
        if not post_url and shortcode:
            post_url = f"https://www.instagram.com/reel/{shortcode}/"
        creator = str(
            raw.get("ownerUsername") or raw.get("owner_username") or raw.get("authorUsername")
            or raw.get("username") or owner.get("username") or user.get("username")
            or author.get("username") or ""
        )
        caption = str(raw.get("caption") or raw.get("text") or raw.get("description") or "")
        duration = _safe_float(
            raw.get("videoDuration"), raw.get("video_duration"), raw.get("durationSeconds"), raw.get("duration")
        )
        views = _safe_int(
            raw.get("videoPlayCount"), raw.get("playCount"), raw.get("viewsCount"),
            raw.get("videoViewCount"), raw.get("viewCount"), raw.get("views"),
        )
        likes = _safe_int(raw.get("likesCount"), raw.get("likeCount"), raw.get("likes"))
        comments = _safe_int(raw.get("commentsCount"), raw.get("commentCount"), raw.get("comments"))
        followers = _safe_int(
            raw.get("ownerFollowersCount"), raw.get("followersCount"), owner.get("followersCount"),
            user.get("followersCount"), author.get("followers_count"),
        )
        published = _parse_dt(
            raw.get("timestamp") or raw.get("takenAtTimestamp") or raw.get("takenAt")
            or raw.get("publishedAt") or raw.get("published_at")
        )
        preview = str(raw.get("displayUrl") or raw.get("thumbnailUrl") or raw.get("image") or "")

    creator = creator.strip().lstrip("@")
    if not post_url or not creator or not published:
        return None
    if published < datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS):
        return None
    if duration > 0 and not (1.0 <= duration <= SOURCE_MAX_DURATION_SEC):
        return None

    stats = (creator_stats or {}).get(creator, {})
    followers = followers or int(stats.get("followers_count", 0) or 0)
    usual_views = float(stats.get("usual_views", 0) or 0)
    age_hours = max(0.25, (datetime.now(timezone.utc) - published).total_seconds() / 3600.0)
    vph = round(views / age_hours, 2)
    if views < 20 and likes < 1 and comments < 1 and vph < 120:
        return None

    score = radar_service.calculate_viral_score(
        views, likes, comments, round(age_hours, 2), vph, followers, usual_views
    )
    item = {
        "platform": platform,
        "creator": creator,
        "post_url": post_url,
        "video_url": _media_url(raw, platform),
        "preview_url": preview,
        "published_at": published.isoformat(),
        "duration_sec": duration,
        "duration_unknown": not bool(duration > 0),
        "views": views,
        "likes": likes,
        "comments": comments,
        "hours_since_publish": round(age_hours, 2),
        "views_per_hour": vph,
        "followers_count": followers,
        "creator_usual_views": round(usual_views, 1),
        "search_term": tag,
        "caption": caption[:4000],
        **score,
    }
    item = radar_quality.apply_quality_score(item)
    item["viral_score_v2"] = _momentum_score(
        views, vph, age_hours, float(item.get("viral_score_v2") or 0)
    )
    item["strict_hashtag_verified"] = True
    item["verified_hashtag"] = tag
    return item


def build_v28_sources():
    tag_urls = [f"https://www.instagram.com/explore/tags/{tag}/" for tag in TARGET_TAGS]
    sources = {
        "instagram": {
            "actor_id": INSTAGRAM_ACTOR,
            "input": {
                "directUrls": tag_urls,
                "resultsType": "reels",
                "resultsLimit": RESULTS_PER_TAG,
                "onlyPostsNewerThan": f"{LOOKBACK_DAYS} days",
                "addParentData": True,
            },
            "requested_max": RESULTS_PER_TAG * len(TARGET_TAGS),
        },
        "tiktok": {
            "actor_id": TIKTOK_ACTOR,
            "input": {
                "hashtags": list(TARGET_TAGS),
                "resultsPerPage": RESULTS_PER_TAG,
                "shouldDownloadVideos": True,
                "shouldDownloadCovers": False,
                "shouldDownloadSubtitles": False,
                "shouldDownloadSlideshowImages": False,
                "scrapeRelatedVideos": False,
            },
            "requested_max": RESULTS_PER_TAG * len(TARGET_TAGS),
        },
        "youtube": {
            "actor_id": YOUTUBE_ACTOR,
            "input": {
                "startUrls": [f"#{tag}" for tag in TARGET_TAGS],
                "maxResults": RESULTS_PER_TAG,
                "publishedAfter": f"{LOOKBACK_DAYS} days",
                "sortOrder": "newest",
                "includeComments": False,
                "downloadVideos": False,
            },
            "requested_max": RESULTS_PER_TAG * len(TARGET_TAGS),
        },
    }
    for source in sources.values():
        source.update(
            run_id="",
            status="NOT_STARTED",
            dataset_id="",
            status_message="",
            started_at="",
            strict_scope_marker=SOURCE_MARKER,
        )
    return sources


def is_current_v28_source_set(job):
    if str((job or {}).get("profile") or "") != SCREENING_PROFILE:
        return False
    sources = (job or {}).get("sources") or {}
    if set(sources) != {"instagram", "tiktok", "youtube"}:
        return False
    return all(
        str((source or {}).get("strict_scope_marker") or "") == SOURCE_MARKER
        for source in sources.values()
    )


def reset_stale_job_v28(job, stage="migration-v28"):
    old_profile = str((job or {}).get("profile") or "")
    job["profile"] = SCREENING_PROFILE
    job["phase"] = "queued"
    job["sources"] = build_v28_sources()
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
        "V28 MIGRATION: старая очередь заменена на Instagram + TikTok + YouTube / 14 дней / speech gate.",
        stage=stage,
        details={"old_profile": old_profile, "new_profile": SCREENING_PROFILE},
    )
    return job


def start_one_source_v28(client, job):
    if not is_current_v28_source_set(job):
        job = reset_stale_job_v28(job)
    job["phase"] = "starting_sources"
    for name, source in (job.get("sources") or {}).items():
        if source.get("run_id"):
            continue
        job["current_source"] = name
        radar_job._persist(job)
        add_radar_log(
            f"V28: запускаю {name}, до {int(source.get('requested_max') or 0)} записей до локальных strict-фильтров.",
            stage="apify",
            details={"source": name, "actor": source.get("actor_id")},
        )
        run = client.actor(source["actor_id"]).start(run_input=dict(source.get("input") or {})) or {}
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
            "Запускаю Instagram + TikTok + YouTube",
            4 + int(8 * started / max(1, total)),
            300,
            f"Запущено платформ: {started}/{total}. После сбора останутся только реальные 5 тегов и последние 14 дней.",
            details={
                "run_id": job.get("run_id"), "sources_started": started,
                "sources_total": total, "platforms": list(PLATFORMS), "hashtags": list(TARGET_TAGS),
            },
        )
        return job
    job["phase"] = "discovering"
    radar_job._persist(job)
    return job


def _reject(reason, *, spoken=False, language=""):
    return RadarAssessment(
        is_russian=str(language or "").lower() in {"russian", "русский", "ru"},
        is_ai_video=False,
        is_comedy_scene=False,
        is_tutorial_or_review=reason.startswith("TUTORIAL_OR_REVIEW"),
        is_talking_head=reason.startswith("INFORMATION_TALKING_HEAD"),
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
        has_spoken_dialogue=bool(spoken),
        dialogue_is_comedic=False,
        dialogue_summary="",
        detected_language=str(language or ""),
    )


def _screen_prompt(caption, measured, platform):
    long_rule = (
        "Because this source is longer than 10.05 seconds, reproducible_format=true ONLY if all essential spoken lines, "
        "speaker ownership/order, setup, action, payoff and minimum reaction can be naturally re-timed to EXACTLY 10.00 seconds "
        "by removing only dead air/redundancy. No global speed-up, rushed speech or deletion of a required line."
        if measured > DIRECT_MAX_DURATION_SEC
        else
        f"Because this source is {measured:.2f}s, reproducible_format=true ONLY if the audible speech, speaker order, pauses, "
        "actions and reaction timing can be recreated naturally inside this exact source duration without adding or deleting a required beat."
    )
    return f"""You are the mandatory speech-and-timing gate for a short viral-video radar.
Platform: {platform}. Measured/metadata duration: {measured:.2f}s.

WATCH AND LISTEN TO THE ENTIRE VIDEO. Caption is only secondary evidence.

PASS requires ALL of these:
1. has_spoken_dialogue=true: audible human or synthetic spoken words are actually present in the video. Music, singing-only, sound effects, text cards and silent mouth movement do not count.
2. The spoken part belongs to the reusable scene/mechanic rather than being irrelevant background audio.
3. This is not primarily a tutorial/review/news/explainer/informational talking head.
4. simple_situation=true: the core setup/action/reaction is understandable without long outside context.
5. Timing is reproducible under the rule below.

TIMING RULE:
{long_rule}

FIELDS:
- detected_language: actual spoken language. Any language is allowed; production later adapts speech to Russian while preserving speaker ownership and timing.
- dialogue_summary: one short factual summary of what is said; do not invent words.
- dialogue_is_comedic: true only when speech itself carries humor/reaction; this field is informative and is NOT required for PASS.
- is_talking_head=true only for an informational/expert/news monologue, not for a character delivering a short scene line to camera.
- reproducible_format is the strict timing-feasibility flag described above.
- reason: concise explanation of speech presence and timing feasibility.

Caption/title secondary context:
{str(caption or '')[:1000]}
""".strip()


def _finalize_screening(assessment, measured):
    if not assessment.has_spoken_dialogue:
        assessment.reason = "NO_SPOKEN_DIALOGUE: Gemini did not hear actual spoken words in the source"
        assessment.reproducible_format = False
        return assessment
    if assessment.is_tutorial_or_review:
        assessment.reason = "TUTORIAL_OR_REVIEW: spoken content is primarily instructional/review content"
        assessment.reproducible_format = False
        return assessment
    if assessment.is_talking_head:
        assessment.reason = "INFORMATION_TALKING_HEAD: speech is primarily informational rather than a reusable scene"
        assessment.reproducible_format = False
        return assessment
    if not assessment.simple_situation or not assessment.reproducible_format:
        assessment.reason = "TIMING_OR_MECHANIC_REJECT: speech exists but the complete spoken mechanic cannot be preserved naturally in target timing"
        assessment.reproducible_format = False
        return assessment
    if measured > DIRECT_MAX_DURATION_SEC:
        assessment.reason = (
            f"{PASS_PREFIX}COMPRESSIBLE_TO_10S: spoken source {measured:.2f}s -> exactly {COMPRESSED_TARGET_SEC:.2f}s; "
            "Gemini confirmed natural dialogue/action retiming without rushed speech or lost required beat"
        )
    else:
        assessment.reason = (
            f"{PASS_PREFIX}DIRECT: audible speech and speaker/action timing verified; preserve exact {measured:.2f}s source duration"
        )
    assessment.reproducible_format = True
    return assessment


def classify_file_v28(file_path, caption="", platform=""):
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < 1.0 or measured > SOURCE_MAX_DURATION_SEC:
        return _reject(f"DURATION_GATE: actual video duration {measured:.2f}s is outside 1.00-{SOURCE_MAX_DURATION_SEC:.2f}s")
    motion = inspect_visual_motion(file_path)
    if motion.checked and motion.is_static_image_video:
        return _reject(f"REJECT_STATIC_IMAGE: {motion.reason}")

    prompt = _screen_prompt(caption, measured, platform or "short video")
    def run(client, uploaded):
        response = client.models.generate_content(
            model=gemini_service.RADAR_MODEL,
            contents=types.Content(parts=[
                gemini_service.video_part(uploaded, 1.0),
                types.Part(text=prompt),
            ]),
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                temperature=0,
                response_mime_type="application/json",
                response_schema=RadarAssessment,
                max_output_tokens=420,
            ),
        )
        return gemini_service.parse_response(response, RadarAssessment)
    return _finalize_screening(gemini_service.with_uploaded_file(file_path, run), measured)


def classify_youtube_url_v28(url, caption, measured):
    if measured < 1.0 or measured > SOURCE_MAX_DURATION_SEC:
        return _reject(f"DURATION_GATE: YouTube metadata duration {measured:.2f}s is outside 1.00-{SOURCE_MAX_DURATION_SEC:.2f}s")
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("На сервере не задан GEMINI_API_KEY")
    client = genai.Client(api_key=key)
    try:
        interaction = client.interactions.create(
            model=gemini_service.RADAR_MODEL,
            input=[
                {"type": "video", "uri": str(url)},
                {"type": "text", "text": _screen_prompt(caption, measured, "YouTube Shorts")},
            ],
            response_format={
                "type": "text",
                "mime_type": "application/json",
                "schema": RadarAssessment.model_json_schema(),
            },
            store=False,
        )
        assessment = RadarAssessment.model_validate_json(interaction.output_text)
        return _finalize_screening(assessment, measured)
    finally:
        try:
            client.close()
        except Exception:
            pass


def matches_v28(assessment):
    return str(getattr(assessment, "reason", "") or "").startswith(PASS_PREFIX)


def _apify_client():
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Не задан APIFY_API_TOKEN")
    return ApifyClient(token)


def _persist_media(row, video_url, duration):
    if row.get("id"):
        with db_conn() as conn:
            conn.execute(
                "UPDATE radar_posts SET video_url=?,duration_sec=? WHERE id=?",
                (str(video_url or ""), float(duration or 0), row.get("id")),
            )
            conn.commit()


def _download_from_url(url, fallback_duration=0):
    tmp = radar_service.download_temp_video(str(url))
    measured = float(measure_video_duration(tmp, fallback=fallback_duration) or fallback_duration or 0)
    return tmp, measured


def _refresh_tiktok(row):
    rows = run_actor_items_checked(
        _apify_client(),
        TIKTOK_ACTOR,
        {
            "postURLs": [row.get("post_url")],
            "resultsPerPage": 1,
            "shouldDownloadVideos": True,
            "shouldDownloadCovers": False,
            "scrapeRelatedVideos": False,
        },
    )
    if not rows:
        raise RuntimeError("TikTok refresh не вернул видео")
    video_url = _media_url(rows[0], "TikTok")
    duration = _safe_float(_dict(rows[0].get("videoMeta")).get("duration"), rows[0].get("videoMeta.duration"))
    if not video_url:
        raise RuntimeError("TikTok refresh не вернул downloadable video URL")
    return video_url, duration


def _refresh_youtube(row):
    rows = run_actor_items_checked(
        _apify_client(),
        YOUTUBE_ACTOR,
        {
            "startUrls": [row.get("post_url")],
            "maxResults": 1,
            "includeComments": False,
            "downloadVideos": True,
        },
    )
    if not rows:
        raise RuntimeError("YouTube refresh не вернул Short")
    video_url = _media_url(rows[0], "YouTube Shorts")
    duration = _safe_float(rows[0].get("durationSeconds"), rows[0].get("duration"))
    if not video_url:
        raise RuntimeError("YouTube refresh не вернул videoDownloadUrl")
    return video_url, duration


def download_multiplatform_video(row):
    platform = str((row or {}).get("platform") or "")
    if platform == "Instagram Reels":
        return download_instagram_video(row)

    metadata_duration = float((row or {}).get("duration_sec") or 0)
    original_url = str((row or {}).get("video_url") or "").strip()
    if original_url:
        try:
            tmp, measured = _download_from_url(original_url, metadata_duration)
            _persist_media(row, original_url, measured)
            return tmp, measured
        except Exception:
            pass

    if platform == "TikTok":
        fresh_url, fresh_duration = _refresh_tiktok(row)
    elif platform == "YouTube Shorts":
        fresh_url, fresh_duration = _refresh_youtube(row)
    else:
        raise RuntimeError(f"Неизвестная платформа для скачивания: {platform}")
    tmp, measured = _download_from_url(fresh_url, fresh_duration or metadata_duration)
    _persist_media(row, fresh_url, measured)
    return tmp, measured


def process_one_v28_base(job):
    index = radar_job._next_ai_index(job)
    candidates = job.get("candidates") or []
    if index is None:
        job["phase"] = "finalizing"
        radar_job._persist(job)
        return job

    item = candidates[index]
    item["ai_attempts"] = int(item.get("ai_attempts") or 0) + 1
    job["current_ai_index"] = index
    job["current_ai_post_url"] = item.get("post_url")
    radar_job._persist(job)
    done_before = sum(1 for x in candidates if x.get("ai_done"))
    creator = item.get("creator", "")
    platform = str(item.get("platform") or "")
    set_radar_status(
        "running",
        f"Gemini слушает речь {done_before + 1}/{len(candidates)}",
        40 + int(44 * done_before / max(1, len(candidates))),
        max(30, (len(candidates) - done_before) * 14),
        f"{platform}: @{creator}. Проверяю реальную речь, порядок спикеров и возможность сохранить тайминг.",
        details={
            "raw": (job.get("stats") or {}).get("raw", 0),
            "numeric_candidates": (job.get("stats") or {}).get("numeric_candidates", 0),
            "ai_total": len(candidates), "ai_done": done_before, "run_id": job.get("run_id"),
            "speech_required": True,
        },
    )
    add_radar_log(
        f"V28 speech tick {done_before + 1}/{len(candidates)}: {platform} @{creator}, попытка {item['ai_attempts']}.",
        stage="gemini-radar",
        details={"post_url": item.get("post_url"), "views": item.get("views")},
    )

    tmp = None
    try:
        assessment = None
        if platform == "YouTube Shorts" and float(item.get("duration_sec") or 0) > 0:
            try:
                assessment = classify_youtube_url_v28(
                    item.get("post_url") or "", item.get("caption") or "", float(item.get("duration_sec") or 0)
                )
            except Exception as exc:
                add_radar_log(
                    f"YouTube direct Gemini не сработал, использую один on-demand download: {str(exc)[:220]}",
                    level="WARN", stage="youtube-fallback",
                )
        if assessment is None:
            tmp, refreshed_duration = download_multiplatform_video(item)
            if refreshed_duration and 0 < float(refreshed_duration) <= SOURCE_MAX_DURATION_SEC:
                item["duration_sec"] = float(refreshed_duration)
            assessment = classify_file_v28(tmp, item.get("caption") or "", platform)

        passed = matches_v28(assessment)
        with db_conn() as conn:
            radar_job.save_post_preserve_ai(conn, item, assessment)
            conn.commit()
        item["ai_done"] = True
        item["ai_match"] = bool(passed)
        item["ai_error"] = ""
        item["assessment"] = assessment.model_dump()
        add_radar_log(
            f"V28 {'PASS' if passed else 'REJECT'} {platform} @{creator}: {str(assessment.reason or '')[:320]}",
            level="INFO" if passed else "WARN", stage="gemini-radar",
        )
    except Exception as exc:
        item["ai_error"] = str(exc)[:700]
        add_radar_log(
            f"V28 AI ERROR {platform} @{creator}, попытка {item['ai_attempts']}: {exc}",
            level="ERROR", stage="gemini-radar",
        )
        if item["ai_attempts"] >= 3:
            item["ai_done"] = True
            item["ai_match"] = False
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    if radar_job._next_ai_index(job) is None:
        job["phase"] = "finalizing"
    radar_job._persist(job)
    try:
        radar_job.save_radar_snapshot()
    except Exception as exc:
        add_radar_log(f"V28 checkpoint snapshot не сохранён: {exc}", level="WARN", stage="snapshot")

    done_now = sum(1 for x in candidates if x.get("ai_done"))
    matched_now = sum(1 for x in candidates if x.get("ai_done") and x.get("ai_match"))
    set_radar_status(
        "running",
        "Gemini слушает речь" if job["phase"] == "ai" else "Gemini закончил speech/timing проверку",
        40 + int(44 * done_now / max(1, len(candidates))),
        max(20, (len(candidates) - done_now) * 14),
        f"Проверено {done_now}/{len(candidates)}. С реальной речью и подходящим таймингом: {matched_now}.",
        details={
            "ai_total": len(candidates), "ai_done": done_now, "matched": matched_now,
            "run_id": job.get("run_id"), "speech_required": True,
        },
    )
    return job


def refresh_scores_v28(conn):
    try:
        v24._ensure_momentum_schema()
    except Exception:
        pass
    stats = radar_service.load_creator_stats(conn)
    placeholders = ",".join("?" for _ in TARGET_TAGS)
    rows = conn.execute(
        f"""SELECT * FROM radar_posts
            WHERE datetime(published_at)>=datetime('now','-{LOOKBACK_DAYS} days')
              AND LOWER(COALESCE(search_term,'')) IN ({placeholders})""",
        TARGET_TAGS,
    ).fetchall()
    now = datetime.now(timezone.utc)
    for row in rows:
        x = dict(row)
        creator_stat = stats.get(x.get("creator", ""), {})
        followers = int(x.get("followers_count") or 0) or int(creator_stat.get("followers_count", 0) or 0)
        usual_views = float(creator_stat.get("usual_views", 0) or 0)
        base = radar_service.calculate_viral_score(
            int(x.get("views") or 0), int(x.get("likes") or 0), int(x.get("comments") or 0),
            float(x.get("hours_since_publish") or 0), float(x.get("views_per_hour") or 0),
            followers, usual_views,
        )
        scored = dict(x)
        scored.update(base)
        scored["followers_count"] = followers
        scored["creator_usual_views"] = usual_views
        quality_base = radar_quality.quality_adjusted_score(scored)

        previous = None
        try:
            previous = conn.execute(
                "SELECT observed_at,views,average_views_per_hour FROM radar_momentum_history WHERE post_url=?",
                (x.get("post_url"),),
            ).fetchone()
        except Exception:
            previous = None
        measured_vph = float(x.get("views_per_hour") or 0)
        acceleration = 0.0
        has_history = False
        elapsed_hours = None
        if previous:
            observed = _parse_dt(previous["observed_at"])
            if observed:
                elapsed_hours = max(0.0, (now - observed).total_seconds() / 3600.0)
                prev_views = int(previous["views"] or 0)
                current_views = int(x.get("views") or 0)
                if elapsed_hours >= (10.0 / 60.0) and current_views >= prev_views:
                    measured_vph = max(0.0, (current_views - prev_views) / max(elapsed_hours, 1e-6))
                    previous_average = max(1.0, float(previous["average_views_per_hour"] or 0))
                    acceleration = measured_vph / previous_average
                    has_history = True

        score = _momentum_score(
            int(x.get("views") or 0), measured_vph, float(x.get("hours_since_publish") or 0),
            quality_base, acceleration, has_history,
        )
        conn.execute(
            """UPDATE radar_posts SET followers_count=?,creator_usual_views=?,anomaly_multiplier=?,
               follower_reach=?,like_rate=?,comment_rate=?,viral_score_v2=?,
               measured_growth_per_hour=?,growth_acceleration=? WHERE id=?""",
            (
                followers, usual_views, base["anomaly_multiplier"], base["follower_reach"],
                base["like_rate"], base["comment_rate"], score,
                measured_vph if has_history else 0.0, acceleration if has_history else 0.0, x["id"],
            ),
        )
        try:
            if previous is None or (elapsed_hours is not None and elapsed_hours >= (10.0 / 60.0)):
                conn.execute(
                    """INSERT INTO radar_momentum_history(post_url,observed_at,views,average_views_per_hour,search_term)
                       VALUES(?,?,?,?,?) ON CONFLICT(post_url) DO UPDATE SET
                       observed_at=excluded.observed_at,views=excluded.views,
                       average_views_per_hour=excluded.average_views_per_hour,search_term=excluded.search_term""",
                    (
                        x.get("post_url"), now.isoformat(), int(x.get("views") or 0),
                        float(x.get("views_per_hour") or 0), str(x.get("search_term") or "").lower(),
                    ),
                )
        except Exception:
            pass
    conn.commit()


def top_eligible_v28(row):
    duration = float((row or {}).get("duration_sec") or 0)
    term = str((row or {}).get("search_term") or "").strip().lower()
    platform = str((row or {}).get("platform") or "")
    reason = str((row or {}).get("reason") or "")
    return bool(
        1.0 <= duration <= SOURCE_MAX_DURATION_SEC
        and term in TARGET_SET
        and platform in PLATFORM_SET
        and reason.startswith(PASS_PREFIX)
    )


def _install_response_guard(app_module):
    app = getattr(app_module, "app", None)
    if app is None:
        return
    funcs = list((app.after_request_funcs or {}).get(None, []))
    banned = {
        "omni_veo_v21_response_guard",
        "omni_veo_veo3_v24_response_guard",
        "strict_scope_v27_response_guard",
    }
    app.after_request_funcs[None] = [fn for fn in funcs if getattr(fn, "__name__", "") not in banned]
    if getattr(app, "_multiplatform_v28_response_guard", False):
        return
    from flask import request

    @app.after_request
    def multiplatform_v28_response_guard(response):
        if not response.is_json:
            return response
        data = response.get_json(silent=True)
        changed = False
        if request.path in {"/api/radar", "/api/radar/candidates"} and isinstance(data, list):
            cleaned = []
            for raw in data:
                item = dict(raw or {})
                if str(item.get("search_term") or "").strip().lower() not in TARGET_SET:
                    continue
                platform = str(item.get("platform") or "")
                if platform and platform not in PLATFORM_SET:
                    continue
                duration = float(item.get("duration_sec") or 0)
                item["requires_compression"] = bool(duration > DIRECT_MAX_DURATION_SEC)
                item["target_duration_sec"] = COMPRESSED_TARGET_SEC if duration > DIRECT_MAX_DURATION_SEC else round(duration, 2)
                item["speech_required"] = True
                cleaned.append(item)
            data = cleaned
            changed = True
        elif request.path in {"/api/status", "/api/radar/status", "/health"} and isinstance(data, dict):
            scope = {
                "radar_mode": MODE_VERSION,
                "radar_platforms": list(PLATFORMS),
                "radar_hashtags": list(TARGET_TAGS),
                "radar_lookback_days": LOOKBACK_DAYS,
                "radar_speech_required": True,
                "radar_strict_actual_hashtag": True,
                "radar_source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
                "radar_direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
                "radar_compressed_target_sec": COMPRESSED_TARGET_SEC,
                "radar_timing_rule": "<=10.05s preserve exact timing; 10.05-15.05s only when spoken mechanic naturally fits exactly 10.00s",
            }
            if request.path == "/api/radar/status":
                details = dict(data.get("details") or {})
                details.update(scope)
                data["details"] = details
            else:
                data.update(scope)
            changed = True
        if changed:
            response.set_data(app.json.dumps(data))
            response.mimetype = "application/json"
        return response

    app._multiplatform_v28_response_guard = True


def apply_multiplatform_v28():
    global _APPLIED
    if _APPLIED:
        return {
            "mode": MODE_VERSION, "screening_profile": SCREENING_PROFILE,
            "platforms": list(PLATFORMS), "hashtags": list(TARGET_TAGS), "lookback_days": LOOKBACK_DAYS,
        }
    _APPLIED = True

    # Dynamic globals used by the already-tested wrappers. This gives V28 its own
    # screening cache identity so a V27 PASS can never leak into the new speech gate.
    budget.PROFILE_VERSION = SCREENING_PROFILE
    growth.PROFILE_VERSION = SCREENING_PROFILE
    hardening.PROFILE_VERSION = SCREENING_PROFILE
    v17.PROFILE_VERSION = SCREENING_PROFILE
    radar_job.RADAR_AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = KEEP_LIMIT
    radar_service.RADAR_KEEP_LIMIT = KEEP_LIMIT

    # Source construction and stale-job recovery.
    radar_job._build_sources = build_v28_sources
    radar_job._start_one_source = start_one_source_v28
    growth._is_current_source_set = is_current_v28_source_set
    growth._reset_stale_job = reset_stale_job_v28
    v21._is_current_source_set = is_current_v28_source_set
    v21._reset_stale_job = reset_stale_job_v28

    # Source-agnostic aggregation already exists; only normalize into one canonical schema.
    radar_job.normalize_reel = normalize_multiplatform_candidate

    # Preserve V19 -> V17 -> V9 wrapper chain and replace only its deepest per-video operation.
    growth._ORIGINAL_PROCESS_AI = process_one_v28_base
    gemini_service.classify_radar_video = classify_file_v28
    radar_job.matches = matches_v28
    radar_service.matches = matches_v28

    # 14-day ranking + five-tag/platform final eligibility.
    radar_quality.refresh_recent_scores_quality = refresh_scores_v28
    radar_job.refresh_recent_scores_quality = refresh_scores_v28
    radar_quality.top_eligible = top_eligible_v28
    radar_job.top_eligible = top_eligible_v28

    app_module = sys.modules.get("app")
    if app_module is not None:
        app_module.PROFILE_VERSION = SCREENING_PROFILE
        app_module.KEEP_LIMIT = KEEP_LIMIT
        app_module.top_eligible = top_eligible_v28
        app_module.download_reel_for_analysis = download_multiplatform_video
        _install_response_guard(app_module)

    # Old visible passes remain in SQLite for history, but are not current cache/output.
    with db_conn() as conn:
        conn.execute(
            """UPDATE radar_posts SET ai_checked=0,ai_match=0
               WHERE ai_match=1 AND COALESCE(screening_profile,'')<>?""",
            (SCREENING_PROFILE,),
        )
        conn.commit()

    info = {
        "mode": MODE_VERSION,
        "screening_profile": SCREENING_PROFILE,
        "platforms": list(PLATFORMS),
        "hashtags": list(TARGET_TAGS),
        "lookback_days": LOOKBACK_DAYS,
        "results_per_tag_per_platform": RESULTS_PER_TAG,
        "max_raw_requested": RESULTS_PER_TAG * len(TARGET_TAGS) * len(PLATFORMS),
        "analyze_limit": AI_ANALYZE_LIMIT,
        "keep_limit": KEEP_LIMIT,
        "speech_required": True,
        "strict_actual_hashtag": True,
        "youtube_direct_gemini": True,
        "direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
        "source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
        "compressed_target_sec": COMPRESSED_TARGET_SEC,
    }
    add_radar_log(
        "V28 READY: Instagram + TikTok + YouTube; #omni/#veo/#veo3/#ai/#ии; 14 дней; обязательная речь + timing gate.",
        stage="startup", details=info,
    )
    return info
