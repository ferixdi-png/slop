"""High-volume discovery source for the single V27 runtime.

The dedicated Instagram Hashtag Scraper can return only a first page on limited
usage, which produced exactly 30 rows per target tag in production. V27 needs a
large recent pool before its strict provenance/duration gates, so discovery uses
the official general Instagram Scraper against the three exact hashtag URLs.

This module changes discovery only. The existing V27 normalizer still proves the
actual hashtag from each post's hashtags/caption before a row can reach the DB.
"""

from __future__ import annotations

import os

TARGET_TAGS = ("omni", "veo", "veo3")
SOURCE_ACTOR = os.environ.get(
    "APIFY_V27_DISCOVERY_ACTOR", "apify/instagram-scraper"
).strip() or "apify/instagram-scraper"
RESULTS_PER_TAG = 250
RECENCY = "7 days"
# Keep the authoritative V27 strict marker stable. The high-volume source is
# distinguished by actor_id + exact hashtag URL/input, not by changing provenance semantics.
SOURCE_MARKER = "STRICT_ACTUAL_HASHTAG_V27"
_APPLIED = False


def _source_name(tag: str) -> str:
    return f"hashtag_{tag}"


def _tag_url(tag: str) -> str:
    return f"https://www.instagram.com/explore/tags/{tag}/"


def build_v27_sources():
    sources = {}
    for tag in TARGET_TAGS:
        sources[_source_name(tag)] = {
            "actor_id": SOURCE_ACTOR,
            "input": {
                "directUrls": [_tag_url(tag)],
                "resultsType": "reels",
                "resultsLimit": RESULTS_PER_TAG,
                "onlyPostsNewerThan": RECENCY,
                "addParentData": True,
            },
            "run_id": "",
            "status": "NOT_STARTED",
            "dataset_id": "",
            "status_message": "",
            "started_at": "",
            "strict_scope_marker": SOURCE_MARKER,
        }
    return sources


def is_current_v27_source_set(job) -> bool:
    sources = (job or {}).get("sources") or {}
    if set(sources) != {_source_name(tag) for tag in TARGET_TAGS}:
        return False

    for tag in TARGET_TAGS:
        source = sources.get(_source_name(tag)) or {}
        payload = source.get("input") or {}
        if str(source.get("actor_id") or "") != SOURCE_ACTOR:
            return False
        if str(source.get("strict_scope_marker") or "") != SOURCE_MARKER:
            return False
        if list(payload.get("directUrls") or []) != [_tag_url(tag)]:
            return False
        if str(payload.get("resultsType") or "").lower() != "reels":
            return False
        if int(payload.get("resultsLimit") or 0) != RESULTS_PER_TAG:
            return False
        if str(payload.get("onlyPostsNewerThan") or "") != RECENCY:
            return False
    return True


def install_v27_high_volume_discovery():
    """Patch only source construction/current-source validation after V27 composes."""
    global _APPLIED
    info = {
        "actor": SOURCE_ACTOR,
        "results_per_tag": RESULTS_PER_TAG,
        "max_raw_requested": RESULTS_PER_TAG * len(TARGET_TAGS),
        "recency": RECENCY,
        "strict_actual_hashtag_still_required": True,
    }
    if _APPLIED:
        return info

    import radar_growth_v6 as growth
    import radar_omni_veo_v21 as v21
    import radar_request_job as radar_job
    import radar_strict_scope_v27 as strict

    # Every path that creates or validates a durable source set must agree.
    radar_job._build_sources = build_v27_sources
    v21._build_sources = build_v27_sources
    growth._is_current_source_set = is_current_v27_source_set
    v21._is_current_source_set = is_current_v27_source_set

    # V27's migration helpers call these names directly.
    strict._build_sources_strict = build_v27_sources
    strict._is_current_source_set_strict = is_current_v27_source_set

    _APPLIED = True
    return info
