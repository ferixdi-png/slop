"""Prevent automatic screening from spawning uncapped paid Apify refresh runs.

Discovery already returns the media URLs needed by Instagram/TikTok screening,
and YouTube is screened directly by Gemini from the public Short URL. If a
candidate's discovery media is missing or expired, V29 rejects/retries that
candidate instead of starting another paid Actor. Manual user-triggered detailed
analysis keeps the original refresh-capable downloader because it is outside the
automatic search budget.
"""

from __future__ import annotations

import radar_multiplatform_v28 as v28
from radar_logs import add_radar_log

_APPLIED = False


def download_search_candidate_v29(row):
    platform = str((row or {}).get("platform") or "")
    media_url = str((row or {}).get("video_url") or "").strip()
    metadata_duration = float((row or {}).get("duration_sec") or 0)

    if not media_url:
        raise RuntimeError(
            f"BUDGET_GUARD_NO_PAID_REFRESH: {platform or 'candidate'} has no discovery media URL; "
            "automatic search will not start another paid Apify Actor"
        )

    try:
        tmp, measured = v28._download_from_url(media_url, metadata_duration)
    except Exception as exc:
        raise RuntimeError(
            f"BUDGET_GUARD_NO_PAID_REFRESH: discovery media expired/unavailable for {platform or 'candidate'}; "
            "automatic search will not start another paid Apify Actor"
        ) from exc

    v28._persist_media(row, media_url, measured)
    return tmp, measured


def apply_search_budget_guard_v29():
    global _APPLIED
    if _APPLIED:
        return {"automatic_paid_refreshes": False}
    _APPLIED = True

    # process_one_v28_base resolves this name from the v28 module at runtime, so
    # this patch affects automatic Gemini screening only. app.py already captured
    # the original downloader for explicit detailed analysis before this layer.
    v28.download_multiplatform_video = download_search_candidate_v29

    add_radar_log(
        "V29 SEARCH BUDGET GUARD READY: automatic candidate screening cannot spawn paid refresh Actors.",
        stage="startup",
        details={"automatic_paid_refreshes": False},
    )
    return {"automatic_paid_refreshes": False}
