from datetime import datetime, timedelta, timezone
from typing import Any

from config import RADAR_MAX_DURATION_SEC, RADAR_MIN_DURATION_SEC
from radar_quality import apply_quality_score
from radar_service import calculate_viral_score


AI_HINTS = (
    "ai", "ии", "нейро", "нейросет", "veo", "kling", "seedance", "sora",
    "генерац", "сгенер", "aivideo", "aicomedy", "aislop", "нейрослоп",
)


def parse_dt(value: Any):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        try:
            stamp = float(value)
            stamp = stamp / 1000 if stamp > 10_000_000_000 else stamp
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except Exception:
            return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(timezone.utc)
    except Exception:
        return None


def safe_int(*values):
    for value in values:
        if value is None or value == "":
            continue
        try:
            return max(0, int(float(value)))
        except Exception:
            continue
    return 0


def safe_float(*values):
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except Exception:
            continue
    return 0.0


def creator_of(raw):
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    author_obj = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    author_scalar = raw.get("author") if isinstance(raw.get("author"), str) else ""
    return str(
        raw.get("ownerUsername")
        or raw.get("owner_username")
        or raw.get("authorUsername")
        or raw.get("author_username")
        or author_scalar
        or raw.get("username")
        or raw.get("profileUsername")
        or raw.get("profile_username")
        or owner.get("username")
        or user.get("username")
        or author_obj.get("username")
        or ""
    ).strip().lstrip("@")


def views_of(raw):
    return safe_int(
        raw.get("videoPlayCount"),
        raw.get("video_play_count"),
        raw.get("playCount"),
        raw.get("play_count"),
        raw.get("playsCount"),
        raw.get("plays_count"),
        raw.get("igPlayCount"),
        raw.get("ig_play_count"),
        raw.get("viewsCount"),
        raw.get("views_count"),
        raw.get("videoViewCount"),
        raw.get("video_view_count"),
        raw.get("viewCount"),
        raw.get("view_count"),
        raw.get("views"),
    )


def followers_of(raw):
    owner = raw.get("owner") if isinstance(raw.get("owner"), dict) else {}
    user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
    author = raw.get("author") if isinstance(raw.get("author"), dict) else {}
    return safe_int(
        raw.get("ownerFollowersCount"),
        raw.get("owner_followers_count"),
        raw.get("authorFollowersCount"),
        raw.get("author_follower_count"),
        raw.get("followersCount"),
        raw.get("followers_count"),
        raw.get("followers"),
        owner.get("followersCount"),
        owner.get("followers_count"),
        user.get("followersCount"),
        user.get("followers_count"),
        author.get("follower_count"),
        author.get("followers_count"),
    )


def normalize_reel(raw, source, creator_stats=None):
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
    creator = creator_of(raw)
    caption = raw.get("caption") or raw.get("text") or raw.get("description") or ""
    duration = safe_float(
        raw.get("videoDuration"),
        raw.get("video_duration"),
        raw.get("video_duration_secs"),
        raw.get("duration_seconds"),
        raw.get("durationSeconds"),
        raw.get("duration"),
    )
    views = views_of(raw)
    likes = safe_int(
        raw.get("likesCount"), raw.get("likeCount"), raw.get("like_count"), raw.get("likes")
    )
    comments = safe_int(
        raw.get("commentsCount"), raw.get("commentCount"), raw.get("comment_count"),
        raw.get("comments_count"), raw.get("comments")
    )
    published = parse_dt(
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
    # Product contract: any short Reel up to 10 seconds is eligible. The MP4 is
    # measured again immediately before Gemini, so bad Instagram metadata cannot
    # leak into the final TOP.
    if duration < RADAR_MIN_DURATION_SEC or duration > RADAR_MAX_DURATION_SEC:
        return None
    if published < datetime.now(timezone.utc) - timedelta(days=7):
        return None

    stats = (creator_stats or {}).get(creator, {})
    followers = followers_of(raw) or int(stats.get("followers_count", 0))
    usual_views = float(stats.get("usual_views", 0))
    age_hours = max(0.25, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    vph = round(views / age_hours, 2)

    # High-recall pre-Gemini gate. Discovery is already AI-specific and the queue
    # is ranked by viral score afterwards, so only remove near-empty/no-signal rows
    # here. Do NOT discard a fresh short AI Reel just because likes accumulated
    # slowly; Gemini should decide content, ranking should decide strength.
    if views < 50 and likes < 1 and comments < 1 and vph < 300:
        return None

    score = calculate_viral_score(
        views,
        likes,
        comments,
        round(age_hours, 2),
        vph,
        followers,
        usual_views,
    )

    search_term = raw.get("searchTerm") or raw.get("hashtag") or raw.get("hashtagName") or raw.get("inputUrl") or source
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
        "search_term": search_term,
        "caption": str(caption)[:4000],
        **score,
    }
    item = apply_quality_score(item)

    # Queue likely-AI short material first. Near-10s gets a small bonus, but 2–8s
    # clips remain fully eligible because the user's target is <=10 seconds.
    hint_blob = f"{caption} {search_term}".lower()
    ai_hint = any(token in hint_blob for token in AI_HINTS)
    duration_bonus = 8.0 if duration >= 9.7 else 5.0 if duration >= 7.0 else 3.0
    ai_bonus = 10.0 if ai_hint else 0.0
    item["viral_score_v2"] = round(
        min(100.0, float(item.get("viral_score_v2") or 0) + duration_bonus + ai_bonus),
        1,
    )
    item["ai_discovery_hint"] = ai_hint
    return item
