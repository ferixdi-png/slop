"""Clean-plate generation + manual CapCut overlay separation + strict Frame 0/duration locks."""

from pydantic import BaseModel, Field

import gemini_service
from radar_logs import add_radar_log

PRODUCTION_PROFILE_VERSION = "frame0_cleanplate_capcut_ru_v15"


class ForensicEditorialOverlay(BaseModel):
    layer_id: str = ""
    overlay_kind: str = ""
    start_sec: float = 0.0
    end_sec: float = 0.0
    asset_content_description: str = ""
    screen_box_percent: str = ""
    anchor_and_position: str = ""
    crop_and_aspect_ratio: str = ""
    opacity_and_blend: str = ""
    border_corner_radius_shadow: str = ""
    animation_in: str = ""
    animation_out: str = ""
    motion_or_tracking: str = ""
    occlusion_notes: str = ""
    source_asset_reuse_note: str = ""


class ForensicSourceAnalysisV15(gemini_service.ForensicSourceAnalysis):
    editorial_overlays: list[ForensicEditorialOverlay] = Field(default_factory=list)
    overlay_separation_notes: list[str] = Field(default_factory=list)


class CapCutOverlayStep(BaseModel):
    layer_id: str = ""
    overlay_kind: str = ""
    start_sec: float = 0.0
    end_sec: float = 0.0
    asset_to_use: str = ""
    asset_preparation: str = ""
    screen_box_percent: str = ""
    anchor_and_position: str = ""
    crop_and_aspect_ratio: str = ""
    opacity_and_blend: str = ""
    border_corner_radius_shadow: str = ""
    animation_in: str = ""
    animation_out: str = ""
    motion_or_tracking: str = ""
    layering_and_occlusion: str = ""
    capcut_action: str = ""


class CapCutOverlayPlan(BaseModel):
    has_editorial_overlays: bool = False
    generation_mode: str = "CLEAN PLATE ONLY — overlays are added manually after generation"
    clean_plate_rule: str = (
        "Generate only the underlying camera-captured scene. Do not render editorial photos, screenshots, "
        "picture-in-picture cards, captions, stickers, UI cards, logos or other post-production layers."
    )
    steps: list[CapCutOverlayStep] = Field(default_factory=list)
    manual_finish_order: list[str] = Field(default_factory=list)


class ProductionPackageV15(gemini_service.ProductionPackage):
    capcut_overlay_plan: CapCutOverlayPlan = Field(default_factory=CapCutOverlayPlan)


class ReconstructionAuditV15(gemini_service.ReconstructionAudit):
    overlay_separation_ok: bool
    capcut_overlay_plan_ok: bool
    frame_zero_literal_match_ok: bool
    explicit_duration_instruction_ok: bool


_ORIGINAL_FORENSIC_PROMPT = gemini_service.forensic_system_prompt
_ORIGINAL_PRODUCTION_PROMPT = gemini_service.production_system_prompt
_ORIGINAL_AUDIT_PROMPT = gemini_service.audit_system_prompt
_ORIGINAL_NORMALIZE = gemini_service.normalize_package
_ORIGINAL_AUDIT_PASSES = gemini_service.audit_passes
_APPLIED = False


FORENSIC_OVERLAY_RULES = r"""
FRAME ZERO FORENSICS — ABSOLUTE
Treat timestamp 0.000 seconds as the authoritative visual source for Block 1. Record the exact camera position, framing, perspective, subject positions, poses, hands, gaze, mouth state, object states, lighting and visible background from that first source frame. Never substitute a later, cleaner or more aesthetically useful frame.

EDITORIAL OVERLAY FORENSICS — ABSOLUTE SEPARATION
Detect every NON-DIEGETIC layer composited over the camera footage after capture: pasted photo, screenshot, meme image, product creative, picture-in-picture image/video, floating UI card, subtitle/caption card, sticker, logo, social graphic or other screen-space layer.
Do NOT confuse these with in-world physical objects. A real phone, TV, monitor, framed photograph, paper print or physical sign that exists in 3D perspective remains part of the base scene.
For every actual editorial overlay, populate editorial_overlays with exact source-relative timing, visible asset content, screen box percentages, anchor/position, crop/aspect, opacity/blend, border/radius/shadow, animation in/out, motion/tracking, occlusion and reuse notes.
If an overlay covers part of Frame Zero, record the overlay separately and reconstruct only the underlying base scene from surrounding evidence. The overlay itself is never a physical Frame Zero object.
""".strip()


PRODUCTION_OVERLAY_RULES = r"""
FRAME 0 PHOTO PROMPT — ABSOLUTE LITERAL LOCK
Block 1 must recreate SOURCE TIMESTAMP 0.000s, not a representative frame and not a later frame. Match forensic.frame_zero as literally as possible: camera height and position, framing, crop, subject scale, normalized screen positions, body pose, hands, head angle, gaze, expression, mouth state, object positions, background layout, light direction and exposure. Do not improve the pose, recompose the scene, center subjects, change camera distance or invent a more attractive starting image.
The ONLY permitted difference from the visible source Frame 0 is removal of NON-DIEGETIC editorial overlays listed in forensic.editorial_overlays. Under those overlays generate the natural clean base scene. Real in-world screens/photos/signs remain.

EXACT VIDEO DURATION — ABSOLUTE LOCK
Block 3 must explicitly state the exact source MP4 duration in seconds. The JSON fields duration, exact_duration_sec and duration_lock must all agree. The user must be able to see immediately what duration to set in the video generator. Never round to a generic 5/8/10 seconds when the measured source length differs.

CLEAN-PLATE GENERATION + CAPCUT OVERLAY WORKFLOW — ABSOLUTE
Generate ONLY the underlying camera-captured footage. Every forensic.editorial_overlays item is POST-PRODUCTION and must be excluded from Block 1 and Block 3. Never bake pasted photos, screenshots, meme images, product creatives, PIP cards, floating UI, subtitle graphics, stickers, logos or social-media graphics into the generated image/video.
If a person points at, looks toward or reacts to an overlay, preserve that physical action toward the reserved screen-space area; add the overlay later in CapCut.

CAPCUT OVERLAY PLAN
Always fill capcut_overlay_plan. If no editorial overlays exist, has_editorial_overlays=false and steps=[]. If overlays exist, create one step per overlay preserving timing, geometry, crop, opacity, border/shadow, entry/exit, tracking and layering. asset_to_use should instruct the editor to reuse the same licensed/user-supplied asset when available rather than regenerate it inside the video model.
""".strip()


AUDIT_OVERLAY_RULES = r"""
FRAME ZERO + DURATION + OVERLAY QA — REQUIRED
frame_zero_literal_match_ok=true only when Block 1 describes the literal source frame at 0.000s with the same composition and subject/object states, except that editorial overlays are intentionally absent as clean-plate post-production layers.
explicit_duration_instruction_ok=true only when Block 3 visibly and unambiguously tells the user the exact source duration to set and duration/exact_duration_sec/duration_lock agree.
overlay_separation_ok=true only when every editorial overlay is absent from Block 1/3 as a rendered scene element while physical in-world screens/photos/signs remain preserved.
capcut_overlay_plan_ok=true only when every forensic overlay has a corresponding CapCut step with correct source-relative timing and approximate screen geometry/compositing behavior.
Fail QA if Block 1 uses a later frame, changes composition for aesthetics, bakes an editorial overlay into the generated scene, or Block 3 omits/changes the exact source duration.
""".strip()


FRAME_ZERO_PROMPT_SUFFIX = r"""

ABSOLUTE FRAME ZERO LOCK:
This PHOTO PROMPT represents source timestamp 0.000 seconds exactly. Preserve the original first-frame camera position, framing, crop, perspective, subject scale, normalized screen positions, pose, hands, head angle, gaze, expression, mouth state, physical objects, background geometry, lighting and exposure. Do not substitute any later frame and do not improve or recompose the shot.
""".rstrip()

CLEAN_PLATE_PROMPT_SUFFIX = r"""

CLEAN PLATE / POST-PRODUCTION SEPARATION — ABSOLUTE:
Generate only the underlying real camera scene. Do not render editorial photo overlays, screenshots, pasted creatives, PIP cards, caption graphics, stickers, logos, UI panels or other screen-space post-production layers. Leave those regions as the natural unobstructed scene because the overlays will be added manually in CapCut. Preserve physical in-world screens, phones, framed photos, paper and signs when they genuinely exist inside the 3D scene.
""".rstrip()

CLEAN_PLATE_HARD_RULE = (
    "Generate CLEAN BASE FOOTAGE ONLY: never bake forensic editorial overlays into the image or video; "
    "all pasted photos screenshots PIP cards captions stickers logos and UI layers are added later in CapCut"
)
FRAME_ZERO_HARD_RULE = "Frame 0 must be the literal source image at timestamp 0.000s with no recomposition or later-frame substitution"


def forensic_system_prompt_v15(owned, expected_duration=None):
    return _ORIGINAL_FORENSIC_PROMPT(owned, expected_duration) + "\n\n" + FORENSIC_OVERLAY_RULES


def production_system_prompt_v15(owned, expected_duration=None):
    prompt = _ORIGINAL_PRODUCTION_PROMPT(owned, expected_duration) + "\n\n" + PRODUCTION_OVERLAY_RULES
    if expected_duration is not None:
        prompt += f"\n\nMANDATORY DURATION VALUE FOR THIS PACKAGE: EXACT DURATION = {float(expected_duration):.2f} seconds. Put this exact number explicitly into Block 3."
    return prompt


def audit_system_prompt_v15(expected_duration=None):
    prompt = _ORIGINAL_AUDIT_PROMPT(expected_duration) + "\n\n" + AUDIT_OVERLAY_RULES
    if expected_duration is not None:
        prompt += f"\nExpected exact source duration for audit: {float(expected_duration):.2f} seconds."
    return prompt


def normalize_package_v15(package, expected_duration=None, audit_score=None):
    package = _ORIGINAL_NORMALIZE(package, expected_duration, audit_score)

    suffix = FRAME_ZERO_PROMPT_SUFFIX + CLEAN_PLATE_PROMPT_SUFFIX
    if "ABSOLUTE FRAME ZERO LOCK:" not in (package.block_1_frame0_prompt or ""):
        package.block_1_frame0_prompt = (package.block_1_frame0_prompt or "").rstrip() + suffix

    duration = float(expected_duration or package.source_duration_sec or package.block_3_video.exact_duration_sec or 0)
    if duration > 0:
        duration = round(duration, 2)
        package.source_duration_sec = duration
        package.block_3_video.exact_duration_sec = duration
        package.block_3_video.duration = f"EXACT DURATION: {duration:.2f} seconds"
        package.block_3_video.duration_lock = (
            f"SET THE VIDEO GENERATOR DURATION TO EXACTLY {duration:.2f} SECONDS. "
            "Do not shorten, extend, round to a preset length, add freeze frames, or change dialogue timing."
        )

    rules = list(package.block_3_video.hard_rules or [])
    for rule in (FRAME_ZERO_HARD_RULE, CLEAN_PLATE_HARD_RULE):
        if rule not in rules:
            rules.append(rule)
    if duration > 0:
        duration_rule = f"EXACT OUTPUT DURATION IS {duration:.2f} SECONDS — this exact value must be selected in the video generator"
        if duration_rule not in rules:
            rules.append(duration_rule)
    package.block_3_video.hard_rules = rules

    object_rules = list(package.block_3_video.object_lock or [])
    clean_object_rule = "Editorial screen-space overlays are not scene objects and must not be rendered; preserve only physical in-world objects"
    if clean_object_rule not in object_rules:
        object_rules.append(clean_object_rule)
    package.block_3_video.object_lock = object_rules

    plan = getattr(package, "capcut_overlay_plan", None)
    if plan is None:
        package.capcut_overlay_plan = CapCutOverlayPlan()
    elif not plan.has_editorial_overlays:
        plan.steps = []
    return package


def audit_passes_v15(audit):
    return bool(
        _ORIGINAL_AUDIT_PASSES(audit)
        and getattr(audit, "frame_zero_literal_match_ok", False)
        and getattr(audit, "explicit_duration_instruction_ok", False)
        and getattr(audit, "overlay_separation_ok", False)
        and getattr(audit, "capcut_overlay_plan_ok", False)
    )


def apply_overlay_cleanplate_overrides():
    global _APPLIED
    if _APPLIED:
        return {"production_profile": PRODUCTION_PROFILE_VERSION, "applied": True}

    gemini_service.ForensicSourceAnalysis = ForensicSourceAnalysisV15
    gemini_service.ProductionPackage = ProductionPackageV15
    gemini_service.ReconstructionAudit = ReconstructionAuditV15
    gemini_service.forensic_system_prompt = forensic_system_prompt_v15
    gemini_service.production_system_prompt = production_system_prompt_v15
    gemini_service.audit_system_prompt = audit_system_prompt_v15
    gemini_service.normalize_package = normalize_package_v15
    gemini_service.audit_passes = audit_passes_v15

    from russian_publish_v15 import apply_russian_publication_overrides
    apply_russian_publication_overrides()

    _APPLIED = True
    add_radar_log(
        "Production v15: literal Frame 0 + exact duration + clean plate + CapCut overlays + Russian publication включены.",
        stage="startup",
        details={"production_profile": PRODUCTION_PROFILE_VERSION},
    )
    return {"production_profile": PRODUCTION_PROFILE_VERSION, "applied": True}
