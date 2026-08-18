"""Final resilient dialogue-trend radar profile.

This layer intentionally does not replace the durable request state-machine.
It fixes the remaining production problems around that stable core:
- actually activates v16 motion/static filtering and 180-row scale;
- replaces the oversized RadarAssessment generation schema with a compact
  screening schema so short model outputs do not get truncated;
- retries one compact structured call inside the same uploaded-file session;
- resets any active pre-v17 queue so a deploy does not keep processing stale
  candidates under old rules;
- exposes reject breakdowns without making metrics a second hard filter.
"""

from __future__ import annotations

import sys
from typing import Literal

from google.genai import types
from pydantic import BaseModel, Field

import gemini_service
import radar_budget_v10 as budget
import radar_growth_v6 as growth
import radar_quality
import radar_request_job as radar_job
import radar_scale_v16 as scale
import radar_service
from config import RADAR_MAX_DURATION_SEC, RADAR_MIN_DURATION_SEC
from media_duration import measure_video_duration
from models import RadarAssessment
from progress import get_radar_status, set_radar_status
from radar_logs import add_radar_log
from static_video_gate import inspect_visual_motion

PROFILE_VERSION = "dialogue_trends_v17_resilient_budget5"
KEEP_LIMIT = 180
TARGET_MATCHES = 180
GEMINI_ANALYZE_LIMIT = 420
RADAR_CLASSIFICATION_FPS = 1.0
MAX_CLASSIFICATION_OUTPUT_TOKENS = 320


class RadarScreeningV17(BaseModel):
    """Small schema on purpose: full RadarAssessment was large enough to truncate."""

    has_spoken_dialogue: bool
    dialogue_is_comedic: bool
    is_tutorial_or_review: bool
    is_information_talking_head: bool
    is_static_or_slideshow: bool
    is_reusable_scene: bool
    dialogue_confidence: int = Field(ge=0, le=100)
    comedy_confidence: int = Field(ge=0, le=100)
    detected_language: str = ""
    dialogue_summary: str = ""
    scene_summary: str = ""
    decision_code: Literal[
        "PASS_DIALOGUE",
        "NO_SPOKEN_DIALOGUE",
        "DIALOGUE_NOT_COMEDIC",
        "TUTORIAL_OR_REVIEW",
        "STATIC_OR_SLIDESHOW",
        "OTHER_REJECT",
    ]


def _reject_assessment(reason: str) -> RadarAssessment:
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


def _assessment_from_screening(s: RadarScreeningV17) -> RadarAssessment:
    static_reject = bool(s.is_static_or_slideshow)
    tutorial_reject = bool(s.is_tutorial_or_review)
    dialogue_hit = bool(s.has_spoken_dialogue and (s.dialogue_is_comedic or s.comedy_confidence >= 55))
    passed = bool(dialogue_hit and not tutorial_reject and not static_reject)

    if static_reject:
        reason = "STATIC_OR_SLIDESHOW: visually static card/slideshow rather than a real moving scene"
    elif tutorial_reject:
        reason = "TUTORIAL_OR_REVIEW: informational/tutorial content"
    elif not s.has_spoken_dialogue:
        reason = "NO_SPOKEN_DIALOGUE: no audible spoken comedic line"
    elif not dialogue_hit:
        reason = "DIALOGUE_NOT_COMEDIC: speech exists but does not carry a clear joke/reaction"
    elif passed:
        reason = "PASS_DIALOGUE: audible short comedic dialogue/reply suitable for recreation"
    else:
        reason = f"OTHER_REJECT: {s.decision_code}"

    summary = (s.dialogue_summary or "").strip()[:220]
    scene = (s.scene_summary or summary or "Короткая разговорная сценка").strip()[:320]
    return RadarAssessment(
        is_russian=(str(s.detected_language or "").strip().lower() in {"russian", "русский", "ru"}),
        is_ai_video=False,  # origin is deliberately irrelevant to acceptance
        is_comedy_scene=bool(dialogue_hit),
        is_tutorial_or_review=tutorial_reject,
        is_talking_head=bool(s.is_information_talking_head),
        simple_situation=bool(s.is_reusable_scene),
        strong_first_frame=False,
        one_clear_joke_or_twist=bool(dialogue_hit),
        characters_count=0,
        scene_description=scene,
        characters=[],
        joke=summary if dialogue_hit else "",
        hook=summary if dialogue_hit else scene,
        ending=summary if dialogue_hit else "",
        reproducible_format=bool(s.is_reusable_scene),
        reason=reason,
        has_spoken_dialogue=bool(s.has_spoken_dialogue),
        dialogue_is_comedic=bool(dialogue_hit),
        dialogue_summary=summary,
        detected_language=str(s.detected_language or "")[:80],
    )


def matches_v17(a: RadarAssessment) -> bool:
    """High-recall semantic gate. Metrics only rank after this."""
    if a.is_tutorial_or_review:
        return False
    if str(a.reason or "").startswith(("REJECT_STATIC_IMAGE", "STATIC_OR_SLIDESHOW")):
        return False
    return bool(a.has_spoken_dialogue and a.dialogue_is_comedic)


def classify_v17(file_path: str, caption: str = "") -> RadarAssessment:
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < RADAR_MIN_DURATION_SEC or measured > RADAR_MAX_DURATION_SEC:
        return _reject_assessment(
            f"DURATION_GATE: actual MP4 duration {measured:.2f}s is outside "
            f"{RADAR_MIN_DURATION_SEC:.1f}-{RADAR_MAX_DURATION_SEC:.2f}s"
        )

    motion = inspect_visual_motion(file_path)
    if motion.checked and motion.is_static_image_video:
        add_radar_log(
            "STATIC IMAGE GATE: карточка/картинка отклонена локально до смысловой проверки.",
            level="WARN",
            stage="static-gate",
            details={
                "duration_sec": motion.duration_sec,
                "expected_samples": motion.expected_samples,
                "retained_motion_frames": motion.retained_motion_frames,
                "retained_ratio": motion.retained_ratio,
            },
        )
        return _reject_assessment(f"REJECT_STATIC_IMAGE: {motion.reason}")

    prompt = f"""Ты быстрый high-recall screening коротких Instagram Reels до 10 секунд.
Наша цель НЕ определить AI происхождение. Наша цель найти как можно больше реально полезных СМЕШНЫХ РАЗГОВОРНЫХ СЦЕНОК для повторения.

PASS_DIALOGUE если в самом видео реально слышна короткая реплика, вопрос-ответ, перепалка, панчлайн или словесная реакция и именно речь создаёт заметный юмористический эффект. Один человек с одной смешной фразой подходит. Два и более спикера не обязательны. Исходный язык любой: позже речь адаптируется на русский.

Не требуй идеальной постановки, вирусности, AI, нескольких персонажей или сложного сюжета. Если реплика смешная и сцену можно разумно повторить, ставь высокий comedy_confidence и PASS_DIALOGUE.

ЖЁСТКИЙ REJECT:
- одна картинка, карточка, цитата, постер, скриншот, слайд-шоу или почти неподвижный Reel;
- только музыка/эстетический монтаж без слышимой юморной речи;
- tutorial, обзор, объяснение, новость, информационный монолог;
- речь есть, но она не несёт шутку/панчлайн/смешную реакцию.

is_information_talking_head=true только для информационного монолога. Короткая шутка прямо в камеру НЕ является информационным talking head.
is_reusable_scene=true если можно повторить ту же структуру с другими героями/русской адаптацией; но даже простая одна реплика может быть reusable.
dialogue_summary и scene_summary максимум по одной короткой фразе. Никаких длинных транскрипций.
Верни только компактную JSON-схему.
Caption только вторичный сигнал: {str(caption or '')[:700]}""".strip()

    def run(client, uploaded):
        parts = types.Content(parts=[
            gemini_service.video_part(uploaded, RADAR_CLASSIFICATION_FPS),
            types.Part(text=prompt),
        ])
        last_error = None
        for attempt in (1, 2):
            try:
                response = client.models.generate_content(
                    model=gemini_service.RADAR_MODEL,
                    contents=parts,
                    config=types.GenerateContentConfig(
                        thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                        temperature=0,
                        response_mime_type="application/json",
                        response_schema=RadarScreeningV17,
                        max_output_tokens=MAX_CLASSIFICATION_OUTPUT_TOKENS,
                    ),
                )
                screening = gemini_service.parse_response(response, RadarScreeningV17)
                if attempt > 1:
                    add_radar_log(
                        "Structured screening восстановлен повторной попыткой.",
                        stage="semantic-check",
                    )
                return _assessment_from_screening(screening)
            except Exception as exc:
                last_error = exc
                if attempt == 1:
                    add_radar_log(
                        f"Structured screening оборвался; повторяю один раз: {str(exc)[:240]}",
                        level="WARN",
                        stage="semantic-check",
                    )
                    continue
                raise RuntimeError(f"SEMANTIC_SCREENING_JSON_FAILED: {exc}") from exc
        raise RuntimeError(f"SEMANTIC_SCREENING_JSON_FAILED: {last_error}")

    return gemini_service.with_uploaded_file(file_path, run)


def _reject_breakdown(candidates) -> dict:
    out = {
        "static_rejected": 0,
        "no_dialogue_rejected": 0,
        "non_comedic_rejected": 0,
        "tutorial_rejected": 0,
        "semantic_errors": 0,
        "matched": 0,
    }
    for item in candidates or []:
        if item.get("ai_error"):
            out["semantic_errors"] += 1
        if item.get("ai_done") and item.get("ai_match"):
            out["matched"] += 1
        reason = str(((item.get("assessment") or {}).get("reason") or ""))
        if reason.startswith(("REJECT_STATIC_IMAGE", "STATIC_OR_SLIDESHOW")):
            out["static_rejected"] += 1
        elif reason.startswith("NO_SPOKEN_DIALOGUE"):
            out["no_dialogue_rejected"] += 1
        elif reason.startswith("DIALOGUE_NOT_COMEDIC"):
            out["non_comedic_rejected"] += 1
        elif reason.startswith("TUTORIAL_OR_REVIEW"):
            out["tutorial_rejected"] += 1
    return out


_ORIGINAL_PROCESS_ONE = radar_job._process_one_ai


def _process_one_v17(job):
    # A deploy during an old v15/v16 AI queue must not keep wasting requests on
    # stale candidates. Reset once and rebuild with current comedy/dialogue sources.
    if str(job.get("profile") or "") != PROFILE_VERSION:
        job = growth._reset_stale_job(job, "migration-v17")
        add_radar_log(
            "V17 MIGRATION: старая очередь сброшена; запускаю новый dialogue-first discovery.",
            stage="migration",
            details={"profile": PROFILE_VERSION},
        )
        return job

    job = _ORIGINAL_PROCESS_ONE(job)
    breakdown = _reject_breakdown(job.get("candidates") or [])
    stats = dict(job.get("stats") or {})
    stats.update(breakdown)
    job["stats"] = stats
    radar_job._persist(job)

    # Keep the stable progress state but enrich it with actionable diagnostics.
    try:
        current = get_radar_status()
        details = dict(current.get("details") or {})
        details.update(breakdown)
        set_radar_status(
            current.get("stage") or "running",
            current.get("label") or "Проверяю диалоги",
            int(current.get("progress") or 0),
            current.get("eta_seconds"),
            current.get("message") or "",
            warning=current.get("warning") or "",
            details=details,
        )
    except Exception:
        pass
    return job


def apply_resilient_v17_overrides():
    # First activate the already-tested v16 motion gate, source mix, 180-row TOP
    # and unchanged <$5 actor caps. Then replace only the fragile semantic layer.
    info = scale.apply_scale_v16_overrides()

    budget.PROFILE_VERSION = PROFILE_VERSION
    budget.MAX_GEMINI_OUTPUT_TOKENS = MAX_CLASSIFICATION_OUTPUT_TOKENS
    growth.PROFILE_VERSION = PROFILE_VERSION
    growth.TARGET_MATCHES = TARGET_MATCHES
    growth.MIN_AI_CHECKS_BEFORE_EARLY_STOP = TARGET_MATCHES
    growth.AI_ANALYZE_LIMIT = GEMINI_ANALYZE_LIMIT
    growth.KEEP_LIMIT = KEEP_LIMIT

    radar_job.RADAR_AI_ANALYZE_LIMIT = GEMINI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = KEEP_LIMIT
    radar_service.RADAR_KEEP_LIMIT = KEEP_LIMIT

    gemini_service.classify_radar_video = classify_v17
    radar_job.matches = matches_v17
    radar_service.matches = matches_v17
    radar_quality.top_eligible = scale.dialogue.top_eligible_dialogue
    radar_job.top_eligible = scale.dialogue.top_eligible_dialogue
    radar_job._process_one_ai = _process_one_v17

    info = budget._assert_budget()

    app_module = sys.modules.get("app")
    if app_module is not None:
        app_module.PROFILE_VERSION = PROFILE_VERSION
        app_module.KEEP_LIMIT = KEEP_LIMIT
        app_module.BUDGET_INFO = info
        app_module.top_eligible = scale.dialogue.top_eligible_dialogue

    add_radar_log(
        "V17 READY: compact semantic schema, one structured retry, static gate, dialogue-first search, TOP up to 180, hard budget <$5.",
        stage="startup",
        details={
            "profile": PROFILE_VERSION,
            "keep_limit": KEEP_LIMIT,
            "target_matches": TARGET_MATCHES,
            "gemini_analyze_limit": GEMINI_ANALYZE_LIMIT,
            "radar_video_fps": RADAR_CLASSIFICATION_FPS,
            "max_output_tokens": MAX_CLASSIFICATION_OUTPUT_TOKENS,
            **info,
        },
    )
    return info
