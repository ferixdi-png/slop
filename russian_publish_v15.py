"""Russian-only publication localization for the production package."""

import gemini_service
from radar_logs import add_radar_log

PRODUCTION_PROFILE_VERSION = "clean_plate_capcut_ru_v15"

_ORIGINAL_PRODUCTION_PROMPT = gemini_service.production_system_prompt
_ORIGINAL_AUDIT_PROMPT = gemini_service.audit_system_prompt
_ORIGINAL_AUDIT_PASSES = gemini_service.audit_passes
_ORIGINAL_AUDIT_SCHEMA = gemini_service.ReconstructionAudit
_APPLIED = False


class ReconstructionAuditRuV15(_ORIGINAL_AUDIT_SCHEMA):
    publication_russian_ok: bool


RUSSIAN_PUBLICATION_RULES = r"""
RUSSIAN PUBLICATION LOCALIZATION — ABSOLUTE
Block 5 is ALWAYS created for a Russian-speaking audience regardless of source language.

short_post must be natural Russian written from scratch for the recreated clip, not an English caption and not a literal machine translation.
shorts_title must be a concise natural Russian title that preserves the source joke/hook without copying foreign phrasing.
retention_phrase must be natural Russian and fit the same comedic setup or curiosity gap.
hashtags must be adapted for Russian publication. Prefer short relevant Russian-language tags and broadly understood topical tags only when genuinely useful. Do not copy a foreign source hashtag dump.

If the source is not Russian:
translate/adapt the joke, premise, speaker intent and punchline into idiomatic Russian;
preserve the same character roles, order of information and comedic meaning;
do not preserve foreign-language captions, subtitles or publication copy merely because they appear in the source.

Block 5 must contain no unexplained English sentences. Brand/model names may remain in Latin script only when they are proper names and materially relevant.
""".strip()


RUSSIAN_PUBLICATION_AUDIT = r"""
PUBLICATION LANGUAGE QA — REQUIRED
publication_russian_ok=true only if Block 5 short_post, shorts_title and retention_phrase are natural Russian for a Russian-speaking audience and the hashtags are publication-ready for that audience.
For a foreign-language source, fail publication_russian_ok if Block 5 simply copies or literally preserves the foreign caption, title, hook wording or hashtag dump.
Proper brand/model names in Latin script are allowed when relevant, but the surrounding publication copy must remain Russian.
""".strip()


def production_system_prompt_ru_v15(owned, expected_duration=None):
    return _ORIGINAL_PRODUCTION_PROMPT(owned, expected_duration) + "\n\n" + RUSSIAN_PUBLICATION_RULES


def audit_system_prompt_ru_v15(expected_duration=None):
    return _ORIGINAL_AUDIT_PROMPT(expected_duration) + "\n\n" + RUSSIAN_PUBLICATION_AUDIT


def audit_passes_ru_v15(audit):
    return bool(_ORIGINAL_AUDIT_PASSES(audit) and getattr(audit, "publication_russian_ok", False))


def apply_russian_publication_overrides():
    global _APPLIED
    if _APPLIED:
        return {"production_profile": PRODUCTION_PROFILE_VERSION, "applied": True}

    gemini_service.ReconstructionAudit = ReconstructionAuditRuV15
    gemini_service.production_system_prompt = production_system_prompt_ru_v15
    gemini_service.audit_system_prompt = audit_system_prompt_ru_v15
    gemini_service.audit_passes = audit_passes_ru_v15

    # This is intentionally activated last in the radar override chain. It keeps
    # all production v15 locks intact while replacing only discovery/classification
    # policy with the static-image gate + scaled dialogue output profile.
    from radar_scale_v16 import apply_scale_v16_overrides
    scale_info = apply_scale_v16_overrides()

    _APPLIED = True
    add_radar_log(
        "Production v15 RU + Radar v16: русский Block 5 сохранён; static-image gate и выдача до 180 активированы.",
        stage="startup",
        details={"production_profile": PRODUCTION_PROFILE_VERSION, **(scale_info or {})},
    )
    return {"production_profile": PRODUCTION_PROFILE_VERSION, "applied": True}
