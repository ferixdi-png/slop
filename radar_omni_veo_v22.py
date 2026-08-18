"""Mass Omni/Veo technical screening override.

V21 narrows discovery and introduces momentum ranking. V22 keeps that scope but
removes the old dialogue/comedy semantic rejection from mass radar screening so
the user receives a genuinely large #omni/#veo trend pool.
"""

from __future__ import annotations

import sys

import gemini_service
import radar_budget_v10 as budget
import radar_growth_v6 as growth
import radar_hardening_v19 as hardening
import radar_omni_veo_v21 as v21
import radar_request_job as radar_job
import radar_resilient_v17 as v17
import radar_scale_v16 as scale
import radar_service
from media_duration import measure_video_duration
from models import RadarAssessment
from radar_logs import add_radar_log
from static_video_gate import inspect_visual_motion

PROFILE_VERSION = "omni_veo_v22_mass_momentum"
AI_ANALYZE_LIMIT = 500
KEEP_LIMIT = 300
TARGET_MATCHES = AI_ANALYZE_LIMIT

_APPLIED = False


def _assessment(passed: bool, reason: str) -> RadarAssessment:
    return RadarAssessment(
        is_russian=False,
        is_ai_video=False,
        is_comedy_scene=False,
        is_tutorial_or_review=False,
        is_talking_head=False,
        simple_situation=bool(passed),
        strong_first_frame=False,
        one_clear_joke_or_twist=False,
        characters_count=0,
        scene_description="Короткий Reel из #omni/#veo" if passed else "",
        characters=[],
        joke="",
        hook="",
        ending="",
        reproducible_format=bool(passed),
        reason=reason,
        has_spoken_dialogue=False,
        dialogue_is_comedic=False,
        dialogue_summary="",
        detected_language="",
    )


def classify_omni_veo_reel(file_path: str, caption: str = "") -> RadarAssessment:
    """No Gemini call: verify actual duration and reject static image-video only."""
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < 1.0 or measured > 10.05:
        return _assessment(
            False,
            f"DURATION_GATE: actual MP4 duration {measured:.2f}s is outside 1.0-10.05s",
        )

    motion = inspect_visual_motion(file_path)
    if motion.checked and motion.is_static_image_video:
        return _assessment(False, f"REJECT_STATIC_IMAGE: {motion.reason}")

    return _assessment(
        True,
        f"PASS_OMNI_VEO_TAG: moving Reel, actual duration {measured:.2f}s; ranked by momentum",
    )


def matches_omni_veo(a: RadarAssessment) -> bool:
    return str(getattr(a, "reason", "") or "").startswith("PASS_OMNI_VEO_TAG")


def apply_omni_veo_v22():
    global _APPLIED
    if _APPLIED:
        return {
            "profile": PROFILE_VERSION,
            "screening": "local_duration_motion",
            "keep_limit": KEEP_LIMIT,
        }
    _APPLIED = True

    # V21 owns exact hashtag-only sources and momentum normalization.
    v21.apply_omni_veo_v21()
    v21.PROFILE_VERSION = PROFILE_VERSION
    v21.AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    v21.KEEP_LIMIT = KEEP_LIMIT
    v21.FINAL_QUERY_LIMIT = max(500, KEEP_LIMIT * 2)
    v21.TARGET_MATCHES = TARGET_MATCHES

    budget.PROFILE_VERSION = PROFILE_VERSION
    growth.PROFILE_VERSION = PROFILE_VERSION
    hardening.PROFILE_VERSION = PROFILE_VERSION
    v17.PROFILE_VERSION = PROFILE_VERSION

    budget.AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    budget.KEEP_LIMIT = KEEP_LIMIT
    growth.TARGET_MATCHES = TARGET_MATCHES
    growth.MIN_AI_CHECKS_BEFORE_EARLY_STOP = TARGET_MATCHES
    growth.AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    growth.KEEP_LIMIT = KEEP_LIMIT
    scale.TARGET_MATCHES = TARGET_MATCHES
    scale.KEEP_LIMIT = KEEP_LIMIT
    scale.GEMINI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    scale.FINAL_QUERY_LIMIT = max(500, KEEP_LIMIT * 2)
    v17.TARGET_MATCHES = TARGET_MATCHES
    v17.KEEP_LIMIT = KEEP_LIMIT
    v17.GEMINI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    radar_job.RADAR_AI_ANALYZE_LIMIT = AI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = KEEP_LIMIT
    radar_service.RADAR_KEEP_LIMIT = KEEP_LIMIT

    # Keep the durable state machine and downloader, but remove semantic rejection.
    gemini_service.classify_radar_video = classify_omni_veo_reel
    radar_job.matches = matches_omni_veo
    radar_service.matches = matches_omni_veo

    # Re-invalidate recent PASS rows under the final profile contract.
    invalidated = v21._invalidate_noncurrent_passes()
    info = budget._assert_budget()

    app_module = sys.modules.get("app")
    if app_module is not None:
        app_module.PROFILE_VERSION = PROFILE_VERSION
        app_module.KEEP_LIMIT = KEEP_LIMIT
        app_module.BUDGET_INFO = info

    add_radar_log(
        "OMNI/VEO V22 READY: mass pool, no dialogue/comedy gate, local <=10s + motion verification, TOP up to 300.",
        stage="startup",
        details={
            "profile": PROFILE_VERSION,
            "hashtags": list(v21.HASHTAGS),
            "hashtag_limit_each": v21.HASHTAG_LIMIT,
            "max_raw_requested": v21.HASHTAG_LIMIT * len(v21.HASHTAGS),
            "screening": "local duration + motion; zero Gemini semantic calls",
            "ranking": "momentum-first / views-per-hour",
            "ai_analyze_limit": AI_ANALYZE_LIMIT,
            "keep_limit": KEEP_LIMIT,
            "stale_passes_invalidated": invalidated,
            **info,
        },
    )
    return {
        "profile": PROFILE_VERSION,
        "hashtags": list(v21.HASHTAGS),
        "hashtag_limit_each": v21.HASHTAG_LIMIT,
        "max_raw_requested": v21.HASHTAG_LIMIT * len(v21.HASHTAGS),
        "screening": "local_duration_motion",
        "gemini_semantic_screening": False,
        "ai_analyze_limit": AI_ANALYZE_LIMIT,
        "keep_limit": KEEP_LIMIT,
        "stale_passes_invalidated": invalidated,
        "budget": info,
    }
