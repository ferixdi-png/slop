"""Clean-plate generation + manual CapCut overlay separation.

Editorial overlays belong to post-production, not to image/video generation.
The AI package recreates the underlying camera-captured scene only, while a
separate CapCut plan preserves overlay timing, placement and compositing.
"""

from pydantic import BaseModel, Field

import gemini_service
from radar_logs import add_radar_log

PRODUCTION_PROFILE_VERSION = "clean_plate_capcut_v15"


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
    overlay_separation_ok: bool = True
    capcut_overlay_plan_ok: bool = True


_ORIGINAL_FORENSIC_PROMPT = gemini_service.forensic_system_prompt
_ORIGINAL_PRODUCTION_PROMPT = gemini_service.production_system_prompt
_ORIGINAL_AUDIT_PROMPT = gemini_service.audit_system_prompt
_ORIGINAL_NORMALIZE = gemini_service.normalize_package
_ORIGINAL_AUDIT_PASSES = gemini_service.audit_passes
_APPLIED = False


FORENSIC_OVERLAY_RULES = r"""
EDITORIAL OVERLAY FORENSICS — ABSOLUTE SEPARATION
Detect every NON-DIEGETIC visual layer that was composited on top of the camera footage after capture. Examples include a pasted photo, screenshot, meme image, product creative, picture-in-picture image/video, floating UI card, subtitle card, sticker, logo, social-media graphic, screen-space caption or any other editorial layer that is attached to the final frame rather than physically present in the filmed scene.

Do NOT confuse an editorial overlay with an in-world physical object. A real phone held by a person, a television in the room, a framed photograph on a wall, a paper print, a physical sign or a monitor that exists in perspective inside the scene is NOT an editorial overlay and remains part of the generated base scene.

For each actual editorial overlay, populate editorial_overlays with:
layer_id
overlay_kind
precise start_sec and end_sec
what the asset visibly contains
screen-space bounding box in percentages
anchor/position
crop/aspect ratio
opacity/blend
border/corner radius/shadow
entry animation
exit animation
movement/tracking
what underlying scene area it occludes
how the editor should source/reuse the asset

If an overlay is present on Frame Zero, record it in editorial_overlays but do NOT treat it as a physical Frame Zero object. The underlying camera-captured scene must still be described as far as visible evidence allows.
If there are no editorial overlays, return an empty editorial_overlays list and explicitly note that the source is clean footage.
""".strip()


PRODUCTION_OVERLAY_RULES = r"""
CLEAN-PLATE GENERATION + CAPCUT OVERLAY WORKFLOW — ABSOLUTE PRIORITY
The image/video generator must create ONLY the underlying camera-captured base footage. Every item listed in forensic.editorial_overlays is POST-PRODUCTION and must be EXCLUDED from Block 1 Frame 0 and Block 3 Video Prompt.

NEVER ask the image/video model to generate or bake in:
pasted photos
screenshots
meme images
product creatives
picture-in-picture cards
floating UI panels
captions/subtitle graphics
stickers
logos
social-media interface graphics
or any other editorial overlay identified by the forensic map

This exclusion applies even when the overlay is visible at the first frame or covers a large part of the source. Generate a clean plate underneath it. Reconstruct the obscured base scene conservatively from surrounding evidence and continuity. Do not invent a new prop where the overlay used to be.

If a person points at, looks toward or reacts to an overlay, PRESERVE that physical action in the base video, but describe it as pointing/looking toward the reserved screen-space area. The overlay itself is added later in CapCut.

Do NOT remove real in-world objects such as a physical TV, real phone screen, framed photo, paper, sign or monitor that exists inside the photographed 3D scene. Only screen-space editorial/composited layers are excluded.

CAPCUT OVERLAY PLAN
Always fill capcut_overlay_plan.
If forensic.editorial_overlays is empty, set has_editorial_overlays=false and steps=[] exactly.
If overlays exist, set has_editorial_overlays=true and create one CapCutOverlayStep per forensic overlay. Preserve source timing and screen geometry as closely as the evidence supports.
For each step specify:
asset_to_use — use the same user-supplied/licensed source asset when available, otherwise a manually supplied equivalent; do not ask the video model to recreate it
asset_preparation — crop/trim/mask needed before placement
start_sec and end_sec
screen_box_percent
anchor_and_position
crop_and_aspect_ratio
opacity_and_blend
border_corner_radius_shadow
animation_in
animation_out
motion_or_tracking
layering_and_occlusion
capcut_action — a concise practical editing instruction

The generated PHOTO PROMPT and VIDEO PROMPT must remain clean-plate prompts and must make sense even before overlays are added.
""".strip()


AUDIT_OVERLAY_RULES = r"""
OVERLAY QA — REQUIRED
Treat absence of editorial overlays from the generated base footage as CORRECT, not as a Frame Zero mismatch.
overlay_separation_ok=true only when every forensic editorial overlay is absent from Block 1 and Block 3 as a rendered scene element, while physical in-world screens/photos/signs remain preserved.
capcut_overlay_plan_ok=true only when the CapCut plan contains the same editorial overlay layers with correct source-relative timing, approximate screen geometry, crop/compositing behavior and entry/exit logic.
If a source overlay is baked into the PHOTO/VIDEO prompt, set overlay_separation_ok=false.
If an overlay exists in forensic data but is missing or materially mis-timed/mis-positioned in the CapCut plan, set capcut_overlay_plan_ok=false.
If no editorial overlays exist, both fields should be true when the plan clearly says clean footage and has no steps.
""".strip()


CLEAN_PLATE_PROMPT_SUFFIX = r"""

CLEAN PLATE / POST-PRODUCTION SEPARATION — ABSOLUTE:
Generate only the underlying real camera scene. Do not render any editorial photo overlay, screenshot, pasted creative, picture-in-picture card, caption graphic, sticker, logo, UI panel or other screen-space post-production layer from the source. Leave those regions as the natural unobstructed scene because overlays will be added manually in CapCut afterward. Preserve physical in-world screens, phones, framed photos, paper and signs when they genuinely exist inside the 3D scene.
""".rstrip()


CLEAN_PLATE_HARD_RULE = (
    "Generate CLEAN BASE FOOTAGE ONLY: never bake forensic editorial overlays into the image or video; "
    "all pasted photos screenshots PIP cards captions stickers logos and UI layers are added later in CapCut"
)


def forensic_system_prompt_v15(owned, expected_duration=None):
    return _ORIGINAL_FORENSIC_PROMPT(owned, expected_duration) + "\n\n" + FORENSIC_OVERLAY_RULES


def production_system_prompt_v15(owned, expected_duration=None):
    return _ORIGINAL_PRODUCTION_PROMPT(owned, expected_duration) + "\n\n" + PRODUCTION_OVERLAY_RULES


def audit_system_prompt_v15(expected_duration=None):
    return _ORIGINAL_AUDIT_PROMPT(expected_duration) + "\n\n" + AUDIT_OVERLAY_RULES


def normalize_package_v15(package, expected_duration=None, audit_score=None):
    package = _ORIGINAL_NORMALIZE(package, expected_duration, audit_score)
    if CLEAN_PLATE_PROMPT_SUFFIX.strip() not in (package.block_1_frame0_prompt or ""):
        package.block_1_frame0_prompt = (package.block_1_frame0_prompt or "").rstrip() + CLEAN_PLATE_PROMPT_SUFFIX

    rules = list(package.block_3_video.hard_rules or [])
    if CLEAN_PLATE_HARD_RULE not in rules:
        rules.append(CLEAN_PLATE_HARD_RULE)
    package.block_3_video.hard_rules = rules

    object_rules = list(package.block_3_video.object_lock or [])
    clean_object_rule = (
        "Editorial screen-space overlays are not scene objects and must not be rendered; preserve only physical in-world objects"
    )
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
        and getattr(audit, "overlay_separation_ok", True)
        and getattr(audit, "capcut_overlay_plan_ok", True)
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

    _APPLIED = True
    add_radar_log(
        "Production v15: clean-plate генерация включена; монтажные фото/скрины/PIP исключаются из AI-промптов и выносятся в CAPCUT OVERLAY PLAN.",
        stage="startup",
        details={"production_profile": PRODUCTION_PROFILE_VERSION},
    )
    return {"production_profile": PRODUCTION_PROFILE_VERSION, "applied": True}
