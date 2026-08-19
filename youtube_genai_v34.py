"""Current Gemini YouTube-URL enrichment for V34.

The old V28 code used the pre-May-2026 Interactions response_format schema while
the project pinned google-genai<2. V34 upgrades the SDK and uses the current
plain output_text contract. If YouTube URL enrichment still fails for a specific
video/model, the broad V34 radar keeps the candidate as AI_UNVERIFIED instead of
removing it.
"""

from __future__ import annotations

import json
import re

from google import genai

# Import-time patch transforms the proven V33 HTML globals before its installer
# runs. It still leaves exactly one browser runtime.
import frontend_broad_v34  # noqa: F401
import frontend_failopen_v33 as v33
import gemini_service
import radar_multiplatform_v28 as v28
from models import RadarAssessment
from radar_json_headroom_v38 import install_json_headroom_v38


# Flask executes after_request handlers in reverse registration order. V34 is
# registered after V28/V29/V30, so without this small ordering adapter the older
# response metadata can overwrite only the public speech_required field back to
# True even though the real V34 runtime is already broad. Wrap the final V33
# installer (which runs after V34 backend registration) and move the V34 response
# normalizer to index 0 so it executes LAST and is authoritative externally.
_ORIGINAL_V33_INSTALL = v33.install_frontend_v33


def _v33_install_with_v34_response_order(app):
    info = _ORIGINAL_V33_INSTALL(app)
    funcs = list((app.after_request_funcs or {}).get(None, []))
    broad = [fn for fn in funcs if getattr(fn, "__name__", "") == "v34_broad_response"]
    if broad:
        rest = [fn for fn in funcs if fn not in broad]
        app.after_request_funcs[None] = broad + rest
    return info


v33.install_frontend_v33 = _v33_install_with_v34_response_order


def _json_text(text: str) -> str:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        json.loads(raw)
        return raw
    except Exception:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            candidate = raw[start : end + 1]
            json.loads(candidate)
            return candidate
        raise RuntimeError("YOUTUBE_GEMINI_INVALID_JSON")


def classify_youtube_url_v34(url, caption, measured):
    measured = float(measured or 0)
    if measured < 1.0 or measured > v28.SOURCE_MAX_DURATION_SEC:
        return v28._reject(
            f"DURATION_GATE: YouTube metadata duration {measured:.2f}s is outside 1.00-{v28.SOURCE_MAX_DURATION_SEC:.2f}s"
        )

    import os
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("На сервере не задан GEMINI_API_KEY")

    schema = RadarAssessment.model_json_schema()
    prompt = (
        v28._screen_prompt(caption, measured, "YouTube Shorts")
        + "\n\nRETURN ONLY ONE VALID JSON OBJECT matching this JSON Schema. Do not use markdown fences.\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    )

    client = genai.Client(api_key=key)
    try:
        interaction = client.interactions.create(
            model=gemini_service.RADAR_MODEL,
            input=[
                {"type": "text", "text": prompt},
                {"type": "video", "uri": str(url)},
            ],
        )
        output = getattr(interaction, "output_text", "") or ""
        assessment = RadarAssessment.model_validate_json(_json_text(output))
        return v28._finalize_screening(assessment, measured)
    finally:
        try:
            client.close()
        except Exception:
            pass


def install_youtube_v34():
    # Runtime imports/calls this only after V30 has installed its fail-closed local
    # classifier wrapper. V38 therefore replaces the captured Gemini base safely
    # without bypassing V30 motion, budget or media guards.
    json_headroom_info = install_json_headroom_v38()
    v28.classify_youtube_url_v28 = classify_youtube_url_v34
    return {
        "youtube_interactions_schema": "google-genai-2.x-current",
        "youtube_url_input": True,
        "youtube_failure_policy": "keep_as_ai_unverified",
        "v34_public_response_authoritative": True,
        "local_file_json_headroom": json_headroom_info,
    }
