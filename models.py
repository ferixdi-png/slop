from pydantic import BaseModel, Field

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
    camera: str
    scene_description: str
    dialogue: list[DialogueLine]
    narrative_timeline: list[str]
    realism_lock: str
    hard_rules: list[str]

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
