"""V27: strict hashtag provenance + compressible 10-15 second source clips.

Product contract:
- discovery sources are exactly #omni, #veo and #veo3;
- a dataset row is accepted only when the post itself explicitly contains at
  least one of those hashtags in caption hashtags / caption text;
- source clips may be up to 15.05s;
- <=10.05s clips use the cheap local duration + motion gate;
- >10.05s clips are admitted only when Gemini judges that the complete core
  mechanic can be compressed naturally to exactly 10.00s;
- production analysis reads the full source but, for long accepted clips,
  generates a 10.00s package using semantic compression rather than speed-up.
"""

from __future__ import annotations

import contextvars
import json
import re
import sys
from datetime import datetime, timezone

from google.genai import types

import cloud_state
import gemini_pipeline_logged
import gemini_service
import radar_dialogue_v14 as dialogue
import radar_growth_v6 as growth
import radar_normalize
import radar_omni_veo_v21 as v21
import radar_omni_veo_v22 as v22
import radar_omni_veo_veo3_v24 as v24
import radar_quality
import radar_request_job as radar_job
import radar_resilient_v17 as v17
import radar_runtime_polish_v26 as v26
import radar_scale_v16 as scale
import radar_service
from db import db_conn
from media_duration import measure_video_duration
from models import ProductionPackage, RadarAssessment, ReconstructionAudit
from progress import set_radar_status
from radar_logs import add_radar_log
from static_video_gate import inspect_visual_motion

MODE_VERSION = "omni_veo_veo3_v27_strict_compress10"
STRICT_SOURCE_MARKER = "STRICT_ACTUAL_HASHTAG_V27"
PASS_PREFIX = "PASS_STRICT_TAG_V27:"
DIRECT_MAX_DURATION_SEC = 10.05
SOURCE_MAX_DURATION_SEC = 15.05
COMPRESSED_TARGET_SEC = 10.00
TARGET_TAGS = ("omni", "veo", "veo3")
TARGET_SET = frozenset(TARGET_TAGS)

_APPLIED = False
_BASE_BUILD_SOURCES = None
_BASE_IS_CURRENT_SOURCE_SET = None
_BASE_ANALYZE_LOGGED = None
_BASE_PRODUCTION_PROMPT = None
_BASE_AUDIT_PROMPT = None
_BASE_NORMALIZE_PACKAGE = None
_BASE_DOWNLOAD = None
_SOURCE_DURATION_CONTEXT = contextvars.ContextVar("v27_source_duration", default=0.0)

_HASHTAG_RE = re.compile(r"(?<![0-9A-Za-z_])#([0-9A-Za-z_]+)", re.IGNORECASE)


def _assessment(passed: bool, reason: str, scene: str = "") -> RadarAssessment:
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
        scene_description=scene if passed else "",
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


def _clean_tag(value) -> str:
    text = str(value or "").strip().lower().lstrip("#")
    return text if re.fullmatch(r"[0-9a-z_]+", text) else ""


def _actual_hashtags(raw) -> set[str]:
    """Extract only hashtags proven to exist on the post; never trust source/searchTerm."""
    found: set[str] = set()
    for key in ("hashtags", "captionHashtags", "caption_hashtags", "captionTags", "caption_tags"):
        value = (raw or {}).get(key)
        values = value if isinstance(value, (list, tuple, set)) else [value] if value else []
        for entry in values:
            if isinstance(entry, dict):
                entry = entry.get("name") or entry.get("tag") or entry.get("hashtag") or ""
            if isinstance(entry, str):
                # Some providers return a whitespace/comma-separated string.
                explicit = _HASHTAG_RE.findall(entry)
                if explicit:
                    found.update(_clean_tag(tag) for tag in explicit if _clean_tag(tag))
                else:
                    for part in re.split(r"[\s,;]+", entry):
                        tag = _clean_tag(part)
                        if tag:
                            found.add(tag)

    caption = str(
        (raw or {}).get("caption")
        or (raw or {}).get("text")
        or (raw or {}).get("description")
        or ""
    )
    found.update(_clean_tag(tag) for tag in _HASHTAG_RE.findall(caption) if _clean_tag(tag))
    return found


def _verified_target_tag(raw, source: str = "") -> str:
    actual = _actual_hashtags(raw)
    matches = actual.intersection(TARGET_SET)
    if not matches:
        return ""
    source_tag = v21._tag_from_source(source)
    if source_tag in matches:
        return source_tag
    for tag in TARGET_TAGS:
        if tag in matches:
            return tag
    return ""


def normalize_strict_candidate(raw, source, creator_stats=None):
    """Hard provenance gate: source feed alone can never admit an unrelated Reel."""
    tag = _verified_target_tag(raw, source)
    if not tag:
        return None

    enriched = dict(raw or {})
    enriched["searchTerm"] = tag
    item = v21._ORIGINAL_DIALOGUE_NORMALIZE(enriched, f"hashtag: {tag}", creator_stats)
    if not item:
        return None

    item["search_term"] = tag
    item["viral_score_v2"] = v21._momentum_score(
        int(item.get("views") or 0),
        float(item.get("views_per_hour") or 0),
        float(item.get("hours_since_publish") or 0),
        float(item.get("viral_score_v2") or 0),
    )
    item["strict_hashtag_verified"] = True
    item["verified_hashtag"] = tag
    return item


def _build_sources_strict():
    sources = _BASE_BUILD_SOURCES()
    for source in sources.values():
        source["strict_scope_marker"] = STRICT_SOURCE_MARKER
    return sources


def _is_current_source_set_strict(job) -> bool:
    if not _BASE_IS_CURRENT_SOURCE_SET(job):
        return False
    sources = (job or {}).get("sources") or {}
    return bool(sources) and all(
        str((source or {}).get("strict_scope_marker") or "") == STRICT_SOURCE_MARKER
        for source in sources.values()
    )


def _compression_verdict(file_path: str, caption: str, measured: float) -> tuple[bool, str]:
    """One small Gemini call only for >10s clips: decide whether 10s is natural."""
    def run(client, uploaded):
        prompt = f"""You are a strict editor deciding whether a {measured:.2f}-second Instagram Reel can be rebuilt as EXACTLY 10.00 seconds without losing its core mechanic.

PASS ONLY when ALL are true:
1. The setup, essential action/dialogue, punchline or key payoff, and necessary final reaction can all remain.
2. Compression can come from removing dead air, redundant establishing time, repeated gestures, repeated wording, or overlong reaction holds.
3. Speaker ownership and dialogue order can remain unchanged.
4. Speech can stay natural; it does not require obvious chipmunk speed-up, rushed pronunciation, or deleting a necessary line.
5. The causal order and the reason the scene works remain understandable at 10 seconds.

REJECT if fitting 10 seconds requires deleting a critical setup/payoff/reaction, merging distinct necessary beats, changing the joke, changing speakers, or making speech unnaturally fast.

Use the RadarAssessment schema as a compact verdict carrier:
- reproducible_format=true ONLY if natural semantic compression to exactly 10.00s is feasible.
- simple_situation=true ONLY if the essential mechanics remain clear at 10.00s.
- is_tutorial_or_review=true if this is mainly a tutorial/review rather than a reusable scene.
- reason: one concise explanation naming what would be removed/tightened or why compression fails.
Do not judge whether the video is AI, funny, Russian, or visually beautiful. Only judge 10-second compressibility.
Caption is secondary: {str(caption or '')[:1000]}""".strip()
        response = client.models.generate_content(
            model=gemini_service.RADAR_MODEL,
            contents=types.Content(parts=[
                gemini_service.video_part(uploaded, 1.0),
                types.Part(text=prompt),
            ]),
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                response_mime_type="application/json",
                response_schema=RadarAssessment,
                max_output_tokens=320,
            ),
        )
        return gemini_service.parse_response(response, RadarAssessment)

    verdict = gemini_service.with_uploaded_file(file_path, run)
    passed = bool(
        verdict.reproducible_format
        and verdict.simple_situation
        and not verdict.is_tutorial_or_review
    )
    return passed, str(verdict.reason or "").strip()[:500]


def classify_strict_reel(file_path: str, caption: str = "") -> RadarAssessment:
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < 1.0 or measured > SOURCE_MAX_DURATION_SEC:
        return _assessment(
            False,
            f"DURATION_GATE: actual MP4 duration {measured:.2f}s is outside 1.00-{SOURCE_MAX_DURATION_SEC:.2f}s",
        )

    motion = inspect_visual_motion(file_path)
    if motion.checked and motion.is_static_image_video:
        return _assessment(False, f"REJECT_STATIC_IMAGE: {motion.reason}")

    if measured <= DIRECT_MAX_DURATION_SEC:
        return _assessment(
            True,
            f"{PASS_PREFIX}DIRECT: verified moving source {measured:.2f}s; no compression required",
            "Reel с подтверждённым #omni / #veo / #veo3",
        )

    compressible, explanation = _compression_verdict(file_path, caption, measured)
    if not compressible:
        return _assessment(
            False,
            f"REJECT_NOT_COMPRESSIBLE_TO_10S: source {measured:.2f}s; {explanation or 'core mechanics do not fit naturally'}",
        )
    return _assessment(
        True,
        f"{PASS_PREFIX}COMPRESSIBLE_TO_10S: source {measured:.2f}s -> target {COMPRESSED_TARGET_SEC:.2f}s; {explanation}",
        "Reel с подтверждённым #omni / #veo / #veo3; механику можно естественно ужать до 10 секунд",
    )


def _download_with_15s_refresh(row):
    tmp, duration = _BASE_DOWNLOAD(row)
    measured = float(duration or 0)
    if 0 < measured <= SOURCE_MAX_DURATION_SEC and isinstance(row, dict):
        row["duration_sec"] = measured
    return tmp, duration


def _compression_override(source_duration: float, target_duration: float) -> str:
    return f"""
V27 SEMANTIC COMPRESSION OVERRIDE — HIGHER PRIORITY THAN ANY EARLIER SOURCE-DURATION PRESERVATION WORDING.
The uploaded SOURCE lasts {source_duration:.2f} seconds. The FINAL GENERATED VIDEO must last EXACTLY {target_duration:.2f} seconds.
This shorter target is intentional and approved only because the source passed a dedicated compressibility gate.
Preserve the complete CORE MECHANIC: setup, necessary speaker order, essential action/object causality, punchline or key payoff, and the minimum reaction needed for the beat to land.
Compress only nonessential time: dead air, redundant establishing hold, repeated gestures, repeated wording, unnecessarily long pauses, and overlong reaction holds.
Do NOT simply speed the whole source up. Do NOT make Russian speech unnaturally fast. Do NOT delete a required line, change speaker ownership, merge distinct necessary actions, change the joke, invent a new shortcut, or remove the payoff.
Re-time the surviving actions naturally across {target_duration:.2f} seconds. The target timeline starts at 0.00 and ends at {target_duration:.2f} seconds.
When an earlier instruction says to preserve the exact measured SOURCE duration, this V27 rule overrides duration only; all content/identity/spatial/object/causal locks remain in force.
""".strip()


def production_prompt_v27(owned, expected_duration=None):
    base = _BASE_PRODUCTION_PROMPT(owned, expected_duration)
    source = float(_SOURCE_DURATION_CONTEXT.get() or 0)
    target = float(expected_duration or 0)
    if source > DIRECT_MAX_DURATION_SEC and target > 0 and target < source:
        return base + "\n\n" + _compression_override(source, target)
    return base


def audit_prompt_v27(expected_duration=None):
    base = _BASE_AUDIT_PROMPT(expected_duration)
    source = float(_SOURCE_DURATION_CONTEXT.get() or 0)
    target = float(expected_duration or 0)
    if source > DIRECT_MAX_DURATION_SEC and target > 0 and target < source:
        return base + "\n\n" + _compression_override(source, target) + "\nQA PASS requires that compression removes only nonessential time while the complete core mechanic still lands naturally."
    return base


def normalize_package_v27(package, expected_duration=None, audit_score=None):
    package = _BASE_NORMALIZE_PACKAGE(package, expected_duration, audit_score)
    source = float(_SOURCE_DURATION_CONTEXT.get() or 0)
    target = float(expected_duration or 0)
    if source > DIRECT_MAX_DURATION_SEC and target > 0 and target < source:
        package.source_duration_sec = round(source, 2)
        package.block_3_video.exact_duration_sec = round(target, 2)
        package.block_3_video.duration = f"{target:.2f} seconds"
        package.block_3_video.duration_lock = (
            f"V27 COMPRESSION LOCK: source is {source:.2f}s; final generated video is exactly {target:.2f}s. "
            "Remove only dead air/redundancy/overlong holds; preserve setup, essential dialogue/action, payoff and necessary reaction. "
            "No global speed-up and no unnaturally rushed speech."
        )
        rule = (
            f"Source {source:.2f}s is intentionally compressed to exactly {target:.2f}s by removing only nonessential time; "
            "core mechanics, speaker order, causality and payoff remain intact"
        )
        if rule not in package.block_3_video.hard_rules:
            package.block_3_video.hard_rules.append(rule)
    return package


def analyze_video_logged_v27(file_path, owned=False, expected_duration=None):
    source_duration = round(float(expected_duration or 0), 2)
    if source_duration <= DIRECT_MAX_DURATION_SEC:
        return _BASE_ANALYZE_LOGGED(file_path, owned, source_duration)
    if source_duration > SOURCE_MAX_DURATION_SEC:
        raise RuntimeError(
            f"Фактическая длительность Reel {source_duration:.2f} сек; максимум исходника {SOURCE_MAX_DURATION_SEC:.2f} сек"
        )

    target_duration = COMPRESSED_TARGET_SEC
    token = _SOURCE_DURATION_CONTEXT.set(source_duration)
    add_radar_log(
        f"V27 production: источник {source_duration:.2f}s прошёл compressibility gate; строю ровно {target_duration:.2f}s.",
        stage="compression",
        details={"source_duration_sec": source_duration, "target_duration_sec": target_duration},
    )

    def run(client, uploaded):
        add_radar_log("V27 PASS 1/3: forensic читает полный исходник без обрезки.", stage="compression")
        forensic = gemini_service.build_forensic_map(client, uploaded, owned, source_duration)

        add_radar_log("V27 PASS 2/3: собираю семантически сжатый 10-секундный production package.", stage="compression")
        package = gemini_service.build_production_package(
            client, uploaded, forensic, owned, target_duration
        )

        add_radar_log("V27 PASS 3/3: QA проверяет и 10.00s, и сохранность механики.", stage="compression")
        audit = gemini_service.audit_package(client, forensic, package, target_duration)
        if not gemini_service.audit_passes(audit):
            package = gemini_service.build_production_package(
                client,
                uploaded,
                forensic,
                owned,
                target_duration,
                repair={
                    "overall_match_score": audit.overall_match_score,
                    "critical_issues": audit.critical_issues,
                    "repair_instructions": audit.repair_instructions,
                    "v27_source_duration_sec": source_duration,
                    "v27_target_duration_sec": target_duration,
                },
            )
            audit = gemini_service.audit_package(client, forensic, package, target_duration)

        result = gemini_service.normalize_package(
            package, target_duration, audit.overall_match_score
        )
        add_radar_log(
            f"V27 compression pipeline готов: {source_duration:.2f}s -> {target_duration:.2f}s; QA {int(audit.overall_match_score or 0)}/100.",
            stage="compression",
        )
        return result

    try:
        return gemini_service.with_uploaded_file(file_path, run)
    finally:
        _SOURCE_DURATION_CONTEXT.reset(token)


def _install_response_guard(app_module):
    app = getattr(app_module, "app", None)
    if app is None or getattr(app, "_strict_scope_v27_response_guard", False):
        return

    from flask import request

    @app.after_request
    def strict_scope_v27_response_guard(response):
        if not response.is_json:
            return response
        data = response.get_json(silent=True)
        changed = False

        if request.path in {"/api/radar", "/api/radar/candidates"} and isinstance(data, list):
            cleaned = []
            for raw in data:
                item = dict(raw or {})
                term = str(item.get("search_term") or "").strip().lower()
                if term not in TARGET_SET:
                    continue
                duration = float(item.get("duration_sec") or 0)
                item["requires_compression"] = bool(duration > DIRECT_MAX_DURATION_SEC)
                item["target_duration_sec"] = (
                    COMPRESSED_TARGET_SEC if duration > DIRECT_MAX_DURATION_SEC else round(duration, 2)
                )
                reason = str(item.get("reason") or "")
                if reason.startswith(PASS_PREFIX + "COMPRESSIBLE_TO_10S"):
                    item["compression_status"] = "compressible_to_10s"
                elif duration > DIRECT_MAX_DURATION_SEC and item.get("ai_checked"):
                    item["compression_status"] = "rejected_not_compressible"
                elif duration > DIRECT_MAX_DURATION_SEC:
                    item["compression_status"] = "waiting_compressibility_check"
                else:
                    item["compression_status"] = "not_required"
                cleaned.append(item)
            data = cleaned
            changed = True
        elif request.path in {"/api/status", "/api/radar/status", "/health"} and isinstance(data, dict):
            scope = {
                "radar_mode": MODE_VERSION,
                "radar_hashtags": list(TARGET_TAGS),
                "radar_strict_actual_hashtag": True,
                "radar_source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
                "radar_direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
                "radar_compressed_target_sec": COMPRESSED_TARGET_SEC,
                "radar_long_clip_rule": "10.05-15.05s only when naturally compressible to exactly 10.00s",
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

    app._strict_scope_v27_response_guard = True


def _migrate_pre_v27_job_and_output():
    """Discard a live old broad run exactly when it lacks the V27 source marker."""
    job = cloud_state.load_radar_job() or {}
    sources = job.get("sources") or {}
    already_strict = bool(sources) and all(
        str((source or {}).get("strict_scope_marker") or "") == STRICT_SOURCE_MARKER
        for source in sources.values()
    )
    if already_strict:
        return {"pre_v27_job_reset": False}

    cleared_posts = 0
    cleared_meta = 0
    with db_conn() as conn:
        cleared_posts = int(conn.execute("SELECT COUNT(*) FROM radar_posts").fetchone()[0] or 0)
        cleared_meta = int(conn.execute("SELECT COUNT(*) FROM radar_meta").fetchone()[0] or 0)
        conn.execute("DELETE FROM radar_posts")
        conn.execute("DELETE FROM radar_meta")
        conn.commit()

    if job:
        job["phase"] = "queued"
        job["sources"] = _build_sources_strict()
        job["candidates"] = []
        job["warnings"] = []
        job["source_failures"] = {}
        job["stats"] = {"migrated_to_v27": True}
        job["result"] = {}
        job["error"] = ""
        job["last_error"] = ""
        job["current_source"] = ""
        job["current_ai_index"] = None
        job["current_ai_post_url"] = ""
        radar_job._persist(job)

    try:
        cloud_state.save_radar_snapshot()
    except Exception:
        pass

    if cleared_posts or cleared_meta or job:
        add_radar_log(
            "V27 MIGRATION: старый broad/non-strict run и его видимая выдача удалены до следующего paid step.",
            stage="v27-migration",
            details={
                "cleared_radar_posts": cleared_posts,
                "cleared_radar_meta": cleared_meta,
                "had_durable_job": bool(job),
            },
        )
    return {
        "pre_v27_job_reset": bool(job),
        "cleared_radar_posts": cleared_posts,
        "cleared_radar_meta": cleared_meta,
    }


def apply_strict_scope_v27():
    global _APPLIED
    global _BASE_BUILD_SOURCES, _BASE_IS_CURRENT_SOURCE_SET, _BASE_ANALYZE_LOGGED
    global _BASE_PRODUCTION_PROMPT, _BASE_AUDIT_PROMPT, _BASE_NORMALIZE_PACKAGE, _BASE_DOWNLOAD

    if _APPLIED:
        return {"strict_scope_version": 27, "mode": MODE_VERSION}
    _APPLIED = True

    scope_info = v26.apply_runtime_polish_v26()

    # Expand only source intake. A >10.05s source still needs a positive semantic
    # compression verdict and its generated target remains exactly 10.00s.
    for module in (radar_normalize, dialogue, growth, v17, scale, v22, v24):
        if hasattr(module, "RADAR_MAX_DURATION_SEC"):
            module.RADAR_MAX_DURATION_SEC = SOURCE_MAX_DURATION_SEC

    app_module = sys.modules.get("app")
    if app_module is not None:
        app_module.RADAR_MAX_DURATION_SEC = SOURCE_MAX_DURATION_SEC

    _BASE_BUILD_SOURCES = v21._build_sources
    _BASE_IS_CURRENT_SOURCE_SET = v21._is_current_source_set
    _BASE_ANALYZE_LOGGED = gemini_pipeline_logged.analyze_video_logged
    _BASE_PRODUCTION_PROMPT = gemini_service.production_system_prompt
    _BASE_AUDIT_PROMPT = gemini_service.audit_system_prompt
    _BASE_NORMALIZE_PACKAGE = gemini_service.normalize_package
    _BASE_DOWNLOAD = radar_job.download_reel_for_analysis

    # Exact source set + explicit marker makes any pre-V27 active queue stale.
    v21._build_sources = _build_sources_strict
    radar_job._build_sources = _build_sources_strict
    v21._is_current_source_set = _is_current_source_set_strict
    growth._is_current_source_set = _is_current_source_set_strict

    # Provenance gate happens before DB insert/candidate display.
    radar_job.normalize_reel = normalize_strict_candidate

    # Mass screen: local for <=10.05, one semantic compression check for 10.05-15.05.
    v22.PASS_PREFIX = PASS_PREFIX
    v24.PASS_PREFIX = PASS_PREFIX
    gemini_service.classify_radar_video = classify_strict_reel
    radar_job.matches = v22.matches_omni_veo
    radar_service.matches = v22.matches_omni_veo

    # Preserve measured >10s duration in the candidate dict despite old v5's
    # legacy <=10.05 assignment guard.
    radar_job.download_reel_for_analysis = _download_with_15s_refresh

    # Production pipeline: full-source forensic + exactly 10s semantic compression.
    gemini_service.production_system_prompt = production_prompt_v27
    gemini_service.audit_system_prompt = audit_prompt_v27
    gemini_service.normalize_package = normalize_package_v27
    gemini_pipeline_logged.analyze_video_logged = analyze_video_logged_v27

    # Final eligibility keeps exact three-tag scope and now accepts strict direct
    # or strict-compressible PASS rows up to 15.05s.
    radar_job.top_eligible = v24.top_eligible_v24
    radar_quality.top_eligible = v24.top_eligible_v24
    if app_module is not None:
        app_module.top_eligible = v24.top_eligible_v24
        _install_response_guard(app_module)

    migration = _migrate_pre_v27_job_and_output()

    add_radar_log(
        "V27 READY: only posts explicitly containing #omni/#veo/#veo3; sources up to 15.05s, long clips must compress naturally to 10.00s.",
        stage="startup",
        details={
            "mode": MODE_VERSION,
            "hashtags": list(TARGET_TAGS),
            "strict_actual_hashtag": True,
            "direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
            "source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
            "compressed_target_sec": COMPRESSED_TARGET_SEC,
            **migration,
            **scope_info,
        },
    )
    return {
        "strict_scope_version": 27,
        "mode": MODE_VERSION,
        "hashtags": list(TARGET_TAGS),
        "strict_actual_hashtag": True,
        "direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
        "source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
        "compressed_target_sec": COMPRESSED_TARGET_SEC,
        **migration,
        **scope_info,
    }
