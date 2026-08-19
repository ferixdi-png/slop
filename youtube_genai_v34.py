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

import gemini_service
import radar_multiplatform_v28 as v28
from models import RadarAssessment


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
    v28.classify_youtube_url_v28 = classify_youtube_url_v34
    return {
        "youtube_interactions_schema": "google-genai-2.x-current",
        "youtube_url_input": True,
        "youtube_failure_policy": "keep_as_ai_unverified",
    }
