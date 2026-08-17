import os

from apify_client import ApifyClient

from actor_utils import run_actor_items_checked
from config import APIFY_CREATOR_ACTOR
from db import db_conn
from radar_service import download_temp_video


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return ""


def _float(*values):
    for value in values:
        if value in (None, ""):
            continue
        try:
            return float(value)
        except Exception:
            continue
    return 0.0


def _extract_media(raw):
    video_url = _first(
        raw.get("videoUrl"),
        raw.get("video_url"),
        raw.get("videoSrc"),
        raw.get("downloadedVideoUrl"),
        raw.get("downloaded_video_url"),
        raw.get("mediaDownloadUrl") if isinstance(raw.get("mediaDownloadUrl"), str) else "",
    )
    duration = _float(
        raw.get("videoDuration"),
        raw.get("video_duration"),
        raw.get("video_duration_secs"),
        raw.get("duration_seconds"),
        raw.get("duration"),
    )
    return str(video_url or ""), duration


def refresh_reel_media(post_url):
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Нельзя обновить ссылку Reel: APIFY_API_TOKEN не задан")

    client = ApifyClient(token)
    rows = run_actor_items_checked(
        client,
        APIFY_CREATOR_ACTOR,
        {
            "username": [post_url],
            "resultsLimit": 1,
            "includeTranscript": False,
            "includeDownloadedVideo": False,
        },
    )
    if not rows:
        raise RuntimeError("Apify не смог заново получить выбранный Reel")

    video_url, duration = _extract_media(rows[0])
    if not video_url:
        raise RuntimeError("Apify обновил Reel, но не вернул прямой video URL")
    return video_url, duration


def download_reel_for_analysis(row):
    """Return (temp_file_path, exact_duration). Refresh expired Instagram CDN URLs automatically."""
    original_error = None
    video_url = str(row.get("video_url") or "").strip()
    duration = float(row.get("duration_sec") or 0)

    if video_url:
        try:
            return download_temp_video(video_url), duration
        except Exception as exc:
            original_error = exc

    fresh_url, fresh_duration = refresh_reel_media(row.get("post_url") or "")
    if 0 < fresh_duration <= 10.05:
        duration = fresh_duration

    try:
        tmp = download_temp_video(fresh_url)
    except Exception as exc:
        if original_error:
            raise RuntimeError(
                f"Не удалось скачать Reel ни по исходной, ни по обновлённой ссылке: {exc}"
            ) from exc
        raise

    with db_conn() as conn:
        conn.execute(
            "UPDATE radar_posts SET video_url=?, duration_sec=? WHERE id=?",
            (fresh_url, duration, row.get("id")),
        )
        conn.commit()

    return tmp, duration
