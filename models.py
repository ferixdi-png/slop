from pydantic import BaseModel, Field, model_validator


class DirectorBlock(BaseModel):
    joke_mechanics: str
    exact_copy_strategy: str
    safety_adjustments: str
    visual_bindings: list[str]
    location: str
    laughter_budget: str
    realism_decision: str


class ComplianceCard(BaseModel):
    characters: list[str]
    poses_and_positions: list[str]
    hands_and_objects: list[str]
    no_text_check: str
    first_phoneme_readiness: str
    realism_check: list[str]


class DialogueLine(BaseModel):
    line_number: int
    visual_speaker_binding: str
    russian_text: str
    voice_description: str
    dialogue_instruction: str = ""


class VideoPromptBlock(BaseModel):
    model: str
    aspect_ratio: str
    duration: str
    exact_duration_sec: float
    duration_lock: str
    camera: str
    scene_description: str
    visual_style: str
    camera_realism: str
    character_continuity_lock: list[str]
    object_lock: list[str]
    speaker_lock: list[str]
    lip_sync_lock: list[str]
    body_behavior: list[str]
    hand_behavior: list[str]
    environment_behavior: list[str]
    motion_consistency: list[str]
    forbidden_visual_traits: list[str]
    dialogue: list[DialogueLine]
    narrative_timeline: list[str]
    realism_lock: str
    hard_rules: list[str]

    @model_validator(mode="after")
    def enforce_lip_sync_rules(self):
        required_speaker = [
            "Only the visually described active speaker may produce speech for the current line",
            "Never swap voices or dialogue lines between visible characters",
            "If a voice is off screen all visible characters remain silent with lips motionless",
        ]
        required_lip = [
            "The active speaker begins visible lip movement only when that speaker line begins",
            "The active speaker stops speech driven lip movement immediately when that line ends",
            "Every non speaking visible character keeps the mouth closed and lips completely motionless during another characters line",
            "No anticipatory lip movement before a characters own line",
            "No residual pseudo speech lip movement after a characters own line",
            "No silent chewing jaw flapping or lip motion synchronized to another characters voice",
            "Laughter or vocal reactions move the mouth only when that exact character audibly produces that reaction",
        ]
        for rule in required_speaker:
            if rule not in self.speaker_lock:
                self.speaker_lock.append(rule)
        for rule in required_lip:
            if rule not in self.lip_sync_lock:
                self.lip_sync_lock.append(rule)
        return self


class AudioLine(BaseModel):
    line_number: int
    visual_speaker_binding: str
    text: str


class AudioBlock(BaseModel):
    dialogue: list[AudioLine]
    pronunciation_hints: list[str]
    intonation_and_laughter_map: list[str]


class PublicationBlock(BaseModel):
    short_post: str
    shorts_title: str
    retention_phrase: str
    hashtags: list[str]


class ProductionPackage(BaseModel):
    source_title: str
    source_duration_sec: float
    source_language: str
    reconstruction_confidence: int = Field(ge=0, le=100)
    block_0_director: DirectorBlock
    block_1_frame0_prompt: str
    block_2_compliance: ComplianceCard
    block_3_video: VideoPromptBlock
    block_4_audio: AudioBlock
    block_5_publication: PublicationBlock


# ─────────────────────────────────────────────────────────────
# INTERNAL FORENSIC SOURCE MAP
# Used before the public 0–5 package is created.
# ─────────────────────────────────────────────────────────────


class ForensicCharacter(BaseModel):
    stable_visual_binding: str
    full_visual_description: str
    clothing_and_colors: str
    initial_screen_position: str
    initial_screen_box_percent: str
    initial_body_pose: str
    initial_head_and_gaze: str
    role_in_scene: str
    identity_continuity_notes: list[str]


class ForensicObject(BaseModel):
    object_description: str
    initial_location_or_holder: str
    initial_screen_position_percent: str
    initial_orientation_state: str
    continuity_rule: str


class ForensicFrameZero(BaseModel):
    camera_position_and_height: str
    framing_and_composition: str
    lens_and_perspective: str
    normalized_screen_coordinates: list[str]
    character_states: list[str]
    hand_states: list[str]
    object_states: list[str]
    gaze_and_expression_states: list[str]
    mouth_states: list[str]
    lighting_and_environment: str
    first_speaker_binding: str
    first_phoneme_readiness: str


class ForensicDialogueTurn(BaseModel):
    line_number: int
    start_sec: float
    end_sec: float
    speaker_binding: str
    exact_russian_text: str
    voice_description: str
    emotion_and_intonation: str
    speaker_mouth_behavior: str
    listener_mouth_states: list[str]


class ForensicTimelineEvent(BaseModel):
    start_sec: float
    end_sec: float
    event_index: int
    shot_or_segment: str
    normalized_screen_coordinates: list[str]
    character_positions: list[str]
    body_actions: list[str]
    hand_actions: list[str]
    gaze_and_expressions: list[str]
    object_states: list[str]
    camera_and_framing: str
    audio_and_speech: str
    mouth_states: list[str]
    causal_link_to_next: str


class ForensicSourceAnalysis(BaseModel):
    measured_duration_sec: float
    detected_language: str
    shot_count: int = Field(ge=1)
    is_single_continuous_take: bool
    frame_zero: ForensicFrameZero
    characters: list[ForensicCharacter]
    objects: list[ForensicObject]
    dialogue_turns: list[ForensicDialogueTurn]
    timeline_events: list[ForensicTimelineEvent]
    camera_path_summary: list[str]
    lighting_continuity: list[str]
    final_frame_state: list[str]
    exact_story_mechanics: str
    uncertainties: list[str]


class ReconstructionAudit(BaseModel):
    exact_duration_ok: bool
    frame_zero_match_ok: bool
    dialogue_text_and_order_ok: bool
    speaker_binding_ok: bool
    lip_sync_lock_ok: bool
    spatial_positions_ok: bool
    object_continuity_ok: bool
    action_order_and_single_occurrence_ok: bool
    camera_logic_ok: bool
    source_story_mechanics_ok: bool
    realism_override_ok: bool
    overall_match_score: int = Field(ge=0, le=100)
    critical_issues: list[str]
    repair_instructions: list[str]


class RadarAssessment(BaseModel):
    is_russian: bool
    is_ai_video: bool
    is_comedy_scene: bool
    is_tutorial_or_review: bool
    is_talking_head: bool
    simple_situation: bool
    strong_first_frame: bool
    one_clear_joke_or_twist: bool
    characters_count: int = Field(ge=0, le=20)
    scene_description: str
    characters: list[str]
    joke: str
    hook: str
    ending: str
    reproducible_format: bool
    reason: str


class TrendCluster(BaseModel):
    label: str
    reels_count: int = Field(ge=1)
    description: str
    examples: list[str]


class RadarMetaReport(BaseModel):
    summary: str
    clusters: list[TrendCluster]
    recurring_characters: list[str]
    recurring_settings: list[str]
    recurring_hooks: list[str]
    recurring_conflicts: list[str]
    question_hook_count: int = Field(ge=0)
    key_takeaways: list[str]
