"""V26 user-facing runtime polish for the final three-tag radar."""

from __future__ import annotations

import radar_omni_veo_veo3_v24 as v24
import radar_omni_veo_v22 as v22
import radar_request_job as radar_job
from models import RadarAssessment
from progress import set_radar_status

_APPLIED = False
_BASE_START = None


def _assessment_v26(passed: bool, reason: str) -> RadarAssessment:
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
        scene_description="Короткий Reel из #omni / #veo / #veo3" if passed else "",
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


def _start_one_source_v26(client, job):
    result = _BASE_START(client, job)
    sources = (result or {}).get("sources") or {}
    started = sum(1 for source in sources.values() if source.get("run_id"))
    total = max(1, len(sources))
    set_radar_status(
        "running",
        "Собираю #omni + #veo + #veo3",
        4 + int(8 * started / total),
        300,
        f"Запущено {started}/{total} хештегов. До {v24.v21.HASHTAG_LIMIT} Reels на каждый; общий raw-пул до {v24.v21.HASHTAG_LIMIT * len(v24.HASHTAGS)}.",
        details={
            "run_id": (result or {}).get("run_id"),
            "sources_started": started,
            "sources_total": len(sources),
            "hashtags": list(v24.HASHTAGS),
            "hashtag_limit_each": v24.v21.HASHTAG_LIMIT,
            "max_raw_requested": v24.v21.HASHTAG_LIMIT * len(v24.HASHTAGS),
        },
    )
    return result


def apply_runtime_polish_v26():
    global _APPLIED, _BASE_START
    if _APPLIED:
        return {"runtime_polish_version": 26}
    _APPLIED = True

    scope_info = v24.apply_omni_veo_veo3_v24()
    v22._assessment = _assessment_v26

    _BASE_START = radar_job._start_one_source
    radar_job._start_one_source = _start_one_source_v26

    return {"runtime_polish_version": 26, **scope_info}
