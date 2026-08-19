"""V38 structured-output headroom + V39 passive MVP recovery.

Production log 2026-08-19 showed an Instagram enrichment response ending inside a
JSON string (Pydantic `EOF while parsing a string`). The V28 local file classifier
asked Gemini to populate the full RadarAssessment schema while allowing only 420
output tokens. That ceiling is too tight for ~20 structured fields and can truncate
an otherwise valid response.

V38 deliberately changes only response reliability:
- keep the V30 motion/SSRF/budget wrappers authoritative;
- keep one Gemini call per enrichment attempt (no automatic retry added);
- raise the structured-output ceiling to 1024 tokens;
- explicitly keep descriptive fields compact so normal responses remain cheap;
- preserve the same RadarAssessment schema and V28 finalization semantics.

V39 additionally performs one passive durable snapshot recovery during startup so
a fresh Render instance can display the previous TOP without requiring /sync or
/tick. It does not start discovery, advance a job, or call Gemini.
"""

from __future__ import annotations

from google.genai import types

import gemini_service
import radar_audit_v30 as v30
import radar_multiplatform_v28 as v28
from media_duration import measure_video_duration
from models import RadarAssessment
from mvp_passive_recovery_v39 import diagnostics as recovery_diagnostics
from mvp_passive_recovery_v39 import install_passive_recovery_v39
from radar_logs import add_radar_log

PROFILE = "gemini_radar_json_headroom_v38"
MAX_OUTPUT_TOKENS = 1024
_APPLIED = False
_PREVIOUS_BASE_CLASSIFIER = None


def _compact_screen_prompt(caption, measured, platform):
    return v28._screen_prompt(caption, measured, platform) + """

V38 STRUCTURED OUTPUT RELIABILITY — ABSOLUTE:
Return one complete JSON object matching RadarAssessment. The schema is already
provided by the API; do not add markdown, commentary, duplicate keys or prose
outside the object.

Keep text concise so the object always finishes:
- scene_description: max 160 characters;
- characters: max 4 short items, max 80 characters each;
- joke, hook, ending, reason, dialogue_summary: max 140 characters each;
- detected_language: short language name/code only;
- never reproduce the full caption or transcript;
- prefer short factual phrases over explanations.

Completing every required field and closing every quote/bracket/brace is more
important than descriptive detail.
""".strip()


def classify_file_v38_base(file_path, caption="", platform=""):
    """V28-equivalent local classifier with enough JSON output headroom.

    In production this is called from V30's `classify_file_v30`, so V30 still owns
    the fail-closed motion gate before this function reaches Gemini.
    """
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < 1.0 or measured > v28.SOURCE_MAX_DURATION_SEC:
        return v28._reject(
            f"DURATION_GATE: actual video duration {measured:.2f}s is outside "
            f"1.00-{v28.SOURCE_MAX_DURATION_SEC:.2f}s"
        )

    prompt = _compact_screen_prompt(caption, measured, platform or "short video")

    def run(client, uploaded):
        response = client.models.generate_content(
            model=gemini_service.RADAR_MODEL,
            contents=types.Content(
                parts=[
                    gemini_service.video_part(uploaded, 1.0),
                    types.Part(text=prompt),
                ]
            ),
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                temperature=0,
                response_mime_type="application/json",
                response_schema=RadarAssessment,
                max_output_tokens=MAX_OUTPUT_TOKENS,
            ),
        )
        return gemini_service.parse_response(response, RadarAssessment)

    assessment = gemini_service.with_uploaded_file(file_path, run)
    return v28._finalize_screening(assessment, measured)


def install_json_headroom_v38():
    """Replace only V30's captured local-file base classifier and restore saved TOP."""
    global _APPLIED, _PREVIOUS_BASE_CLASSIFIER
    if _APPLIED:
        return diagnostics()

    if v30._BASE_CLASSIFY_FILE is None:
        raise RuntimeError("V38 must be installed after V30 audit captures its base classifier")

    _PREVIOUS_BASE_CLASSIFIER = v30._BASE_CLASSIFY_FILE
    v30._BASE_CLASSIFY_FILE = classify_file_v38_base
    _APPLIED = True

    # This executes after V30 has replaced the snapshot implementation and after
    # V23 has installed its latest-run freshness guard. It is therefore the safe
    # point to hydrate a fresh Render SQLite cache without advancing the radar job.
    recovery_info = install_passive_recovery_v39()

    info = diagnostics()
    add_radar_log(
        "V38+V39 READY: RadarAssessment cap 420 -> 1024; passive TOP recovery enabled; no auto-search or extra Gemini retry.",
        stage="startup",
        details={**info, "passive_recovery": recovery_info},
    )
    return info


def diagnostics():
    return {
        "profile": PROFILE,
        "structured_output_max_tokens": MAX_OUTPUT_TOKENS,
        "automatic_retry_added": False,
        "schema": "RadarAssessment",
        "v30_motion_gate_preserved": True,
        "compact_output_contract": True,
        "passive_recovery": recovery_diagnostics(),
    }
