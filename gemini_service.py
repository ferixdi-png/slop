import json
import os
import re
import time

from google import genai
from google.genai import types

from config import (
    ANALYSIS_MODEL,
    RADAR_MODEL,
    REALISM_LOCK,
    FORENSIC_VIDEO_FPS,
    RADAR_VIDEO_FPS,
)
from models import (
    ProductionPackage,
    ForensicSourceAnalysis,
    ReconstructionAudit,
    RadarAssessment,
    RadarMetaReport,
)


def strip_speech(text):
    text = re.sub(r"[^0-9A-Za-zА-Яа-яЁё\s\u0300-\u036f]", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def duration_rule(expected_duration=None):
    if expected_duration and expected_duration > 0:
        exact = float(expected_duration)
        return (
            f"Measured source duration is exactly {exact:.2f} seconds. "
            f"The reconstruction must remain exactly {exact:.2f} seconds. "
            "Never round to 10 seconds. Never add a silent tail. Never shorten the ending. "
            "Fit every action line pause reaction and punchline inside the measured duration."
        )
    return "Measure and preserve the actual source duration without intentional extension or shortening."


def rights_rule(owned):
    if owned:
        return "The user confirmed rights to the source. Preserve the Russian wording literally as the audio supports."
    return (
        "For ordinary short dialogue in this under-10-second source, transcribe the spoken Russian words as heard so the timing map is factual. "
        "Do not imitate a recognizable real person's identity or voiceprint. If a source contains lyrics or a long distinctive quotation, preserve timing, speaker order and meaning without reproducing restricted wording."
    )


def video_part(uploaded, fps):
    return types.Part(
        file_data=types.FileData(
            file_uri=uploaded.uri,
            mime_type=uploaded.mime_type,
        ),
        video_metadata=types.VideoMetadata(fps=float(fps)),
    )


def normalize_package(package, expected_duration=None, audit_score=None):
    if expected_duration and expected_duration > 0:
        exact = round(float(expected_duration), 2)
        package.source_duration_sec = exact
        package.block_3_video.exact_duration_sec = exact
        package.block_3_video.duration = f"{exact:.2f} seconds"
        package.block_3_video.duration_lock = (
            f"ABSOLUTE SOURCE DURATION LOCK: final video must last exactly {exact:.2f} seconds. "
            "No extension shortening padding empty tail speed change or reinterpretation of the total duration. "
            "All source actions speech pauses reactions and the final beat must fit inside this exact measured duration."
        )

    if audit_score is not None:
        package.reconstruction_confidence = min(
            int(package.reconstruction_confidence or 0), int(audit_score)
        )

    for line in package.block_3_video.dialogue:
        line.russian_text = strip_speech(line.russian_text)
        binding = (line.visual_speaker_binding or "the visible speaker").strip()
        line.dialogue_instruction = f'{binding} says in Russian "{line.russian_text}"'

    for line in package.block_4_audio.dialogue:
        line.text = strip_speech(line.text)

    package.block_1_frame0_prompt = (
        package.block_1_frame0_prompt.rstrip()
        + "\n\nMANDATORY ABSOLUTE SMARTPHONE REALISM OVERRIDE:\n"
        + REALISM_LOCK
    )
    package.block_3_video.realism_lock = REALISM_LOCK

    mandatory_hard_rules = [
        "Preserve the exact measured source duration with zero intentional padding or trimming",
        "If the source contains speech the target speech is Russian and follows the source turn order",
        "Only the visually bound active speaker may move lips during that exact line",
        "Every visible non speaker keeps the mouth closed and lips completely motionless during another characters line",
        "No anticipatory lip movement before a characters line and no residual lip movement after it ends",
        "Never overlap swap duplicate or leak dialogue between characters",
        "Never duplicate teleport disappear or spontaneously transfer props characters hands or objects",
        "Every important action happens once only and in the original causal order",
        "Preserve screen side relative distance gaze direction body orientation and pose progression",
        "Preserve camera side height distance framing direction and movement logic",
        "Preserve the source setup escalation reaction punchline and final beat",
        "Do not inherit cartoon CGI 3D illustration or synthetic rendering from the source",
        "Maintain authentic casual handheld smartphone realism throughout the entire clip",
    ]
    existing = list(package.block_3_video.hard_rules or [])
    for rule in mandatory_hard_rules:
        if rule not in existing:
            existing.append(rule)
    package.block_3_video.hard_rules = existing
    return package


def forensic_system_prompt(owned, expected_duration=None):
    return f"""
You are a forensic reconstruction analyst for ultra-short vertical videos. This pass is OBSERVATION ONLY. Do not design, improve, simplify or rewrite the source. Build a factual reconstruction blueprint from the uploaded video.

SOURCE DURATION
{duration_rule(expected_duration)}
All timestamps must stay inside that measured duration. Use sub-second boundaries whenever the evidence supports them. The video is sampled at a higher frame rate specifically so you can catch fast actions, quick reactions and speaker changes.

MANDATORY TEMPORAL SEGMENTATION
Create a new timeline event whenever ANY of these materially changes:
active speaker
mouth state
hand position or gesture
object holder or position
body pose
head direction
gaze
facial expression
screen position
camera framing or movement
shot continuity
causal beat
reaction
For a 6 to 10 second comedy clip this will often produce many short events. Do not compress several visibly different micro-actions into one vague event.

CHARACTERS
For each visible person create one stable_visual_binding using persistent physical traits only: approximate age, gender presentation when visually clear, hair, clothing colors, build and stable spatial role. Never use Speaker A, Character 1 or arbitrary names.
Record initial screen box in approximate percentages: left/top/right/bottom or center coordinates and approximate size. Record foreground/middle/background, distance to camera, facing direction, body rotation, head angle, gaze and expression.

FRAME ZERO
The literal first visible frame is a blueprint for image generation. Record:
camera height and side
camera distance
lens/perspective
vertical crop and headroom
background geometry
exact relative location of every person
body pose
head angle
gaze
expression
mouth state
each hand state
every visible prop and its location
lighting direction and quality
first motion readiness
first speaker readiness for the first phoneme
Nothing in Frame Zero may be described vaguely.

RUSSIAN SPEECH AND LIP SYNC FORENSICS
Listen to the audio and identify the actual visible speaker for every turn.
Transcribe Russian speech as accurately as the audio supports.
For each dialogue turn record start_sec and end_sec, speaker binding, exact Russian text, voice quality, emotion, speaker mouth behavior and listener mouth states.
Explicitly state the mouth state of every other visible character during that line.
Track pauses, breaths, laughter, reaction sounds and off-screen speech.
If the visual speaker assignment is uncertain, say so in uncertainties instead of guessing.

OBJECT CONTINUITY
Treat each visible prop as a persistent physical object. Record initial holder/location/orientation, each contact, transfer, release, drop or repositioning and its final state. No invisible teleportation and no duplicate objects unless the source visibly contains multiples.

ACTIONS AND CAUSALITY
Every important action occurs exactly once in the source map. Record what causes the next reaction. Preserve micro-reactions that create the joke: glance, pause, delayed look, hand stop, head turn, eyebrow movement, body recoil, etc.

CAMERA FORENSICS
For every timeline event distinguish subject movement from camera movement. Record camera side, approximate height, distance, framing, pan/tilt/push/pull, handheld drift, focus shift and cuts. If it is one continuous take, preserve that fact.

AUDIO
Record dialogue, silence, laughter, breaths, environmental sounds and any sound cue that changes comedic timing.

STYLE
Describe the source honestly in this forensic pass even if it is cartoon, CGI or visibly AI. The production pass will later convert visual rendering to realistic smartphone live action while preserving staging.

RIGHTS
{rights_rule(owned)}

Return only the ForensicSourceAnalysis schema. Factual correspondence is more important than concise writing.
""".strip()


def production_system_prompt(owned, expected_duration=None):
    return f"""
You are a production prompt engineer reconstructing an ultra-short Russian comedy video from BOTH the uploaded source and a detailed forensic source map.
Return exactly the six-block ProductionPackage schema.

AUTHORITY ORDER
1 factual forensic source map for timing, speaker ownership, spatial position, actions, objects and Frame Zero
2 uploaded video for resolving uncertainty
3 realism override for rendering style only
Never invent a new gag, new action, new prop, new line, new camera move or extra ending.

ABSOLUTE CONTENT RECONSTRUCTION LOCK
Preserve as closely as allowed:
character count and roles
screen side and relative distances
body orientation and pose progression
head angle gaze expression progression
hand actions and object contacts
object identity holder location orientation and transfer order
camera side height distance framing and movement logic
speech turn order relative timing pauses reactions and laughter
setup escalation punchline reaction and ending
exact measured duration

DURATION
{duration_rule(expected_duration)}
Block 3 duration and exact_duration_sec must equal the measured source duration exactly.

FRAME 0 PHOTO PROMPT
Block 1 is a standalone ultra-detailed English prompt for the literal starting frame.
It must specify every visible character with exact spatial relationship, clothing, pose, head angle, gaze, mouth state, hand state and object contact.
Specify background geometry, camera side/height/distance, lens behavior, crop, headroom, lighting, shadow behavior, texture and physical readiness for the first motion and first phoneme.
The user must be able to generate the start image from Block 1 without seeing the original.
If the source is cartoon/CGI/stylized, translate the same staging into believable live-action people/objects without inheriting artificial rendering.

RUSSIAN DIALOGUE
If source audio contains speech, target speech is Russian.
Use Literal Speaker Binding: each speaker is identified by a physical visual description.
Each spoken line appears once in dialogue.
russian_text contains only the spoken words without punctuation.
dialogue_instruction must be exactly: visual binding + says in Russian + quoted Russian text.
Preserve source wording for ordinary short dialogue as the audio supports, source turn order and relative timing.

SPEAKER AND LIP-SYNC LOCK
Only the visually bound active speaker moves lips for a line.
Every other visible person keeps mouth closed and lips completely still during that line.
No anticipatory lip movement before the persons own line.
No residual pseudo-speech after the line.
No jaw chewing or lip motion synchronized to somebody else's voice.
Off-screen speech must not animate any visible mouth.
Laughter/reaction mouth movement happens only for the character audibly producing it.

NARRATIVE TIMELINE
Do not expose numeric seconds in narrative_timeline.
Translate the forensic timed map into an ultra-specific chronological causal sequence using First, Then, Then, Finally.
Although numeric timestamps are hidden in the public prompt, preserve their relative timing internally.
Mention exact screen position, movement, gaze, hand/object state, listener behavior, camera behavior and mouth state whenever they matter.
Every source action occurs once only.

BLOCK 3 MUST BE SELF-SUFFICIENT AND ULTRA-DETAILED
Fill every field concretely:
model
aspect_ratio
duration
exact_duration_sec
duration_lock
camera
scene_description
visual_style
camera_realism
character_continuity_lock
object_lock
speaker_lock
lip_sync_lock
body_behavior
hand_behavior
environment_behavior
motion_consistency
forbidden_visual_traits
dialogue
narrative_timeline
realism_lock
hard_rules
Never write only vague phrases like same as source, identical to reference or preserve everything. Spell out the actual source-specific conditions.

REALISM OVERRIDE
The source determines WHAT happens, but artificial rendering never carries over.
Even if the winning Reel is a cartoon, CGI or plastic AI clip, recreate the same staging as a believable spontaneous real-world vertical smartphone video.
Human skin has pores, microtexture and natural tonal variation. Hair has separate strands and flyaways. Hands are anatomically correct. Clothes have real fabric thickness, seams, folds and gravity. Objects have real material response and contact shadows. Lighting comes from the environment. Camera behavior includes believable autofocus, auto exposure, slight handheld drift and realistic motion softness. Avoid cinematic grading, fake HDR, beauty filters and artificial bokeh.

BLOCK 4
Russian dialogue, pronunciation hints and intonation/laughter map. Spoken text contains no punctuation and no dash symbols.

BLOCK 5
Short publication copy only. No invented brands/URLs and no promise that the video will go viral.

SAFETY
Do not describe bypassing safety filters. Make only the smallest necessary safe substitution if a source element cannot be reproduced.

RIGHTS
{rights_rule(owned)}

{REALISM_LOCK}

Return only ProductionPackage.
""".strip()


def audit_system_prompt(expected_duration=None):
    return f"""
You are an adversarial reconstruction QA auditor. Compare the forensic source map against the generated ProductionPackage. Do not reward eloquence. Fail anything that materially changes the source mechanics.

CHECK INDEPENDENTLY
1 exact total duration equals {f'{float(expected_duration):.2f} seconds' if expected_duration else 'the measured source duration'}
2 Frame Zero spatial arrangement, pose, hands, props, gaze, mouth state, camera and first-action readiness
3 Russian dialogue text and turn order as supported by the forensic map
4 correct visible speaker binding for every line
5 non-speaker lip stillness and no voice overlap/swap
6 screen side, relative distance, body orientation, gaze and movement progression
7 object singularity, holder/location and transfer continuity
8 every important action once only and in source causal order
9 camera side, height, distance, framing and movement logic
10 setup, escalation, punchline, reaction and ending
11 conversion to realistic smartphone live action without changing staging
12 no invented action, prop, speaker line, reaction or extra ending

Any serious wrong speaker, wrong side, wrong object, wrong action order, missing beat, invented beat, wrong duration or materially wrong Frame Zero makes the relevant boolean false.
critical_issues must be concrete.
repair_instructions must be actionable and source-specific.
Return only ReconstructionAudit.
""".strip()


def wait_active(client, uploaded):
    deadline = time.time() + 240
    while getattr(uploaded, "state", None) and getattr(uploaded.state, "name", "") != "ACTIVE":
        if getattr(uploaded.state, "name", "") == "FAILED":
            raise RuntimeError("Gemini не смог обработать видеофайл")
        if time.time() > deadline:
            raise RuntimeError("Gemini слишком долго обрабатывает видео")
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    return uploaded


def with_uploaded_file(file_path, fn):
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("На сервере не задан GEMINI_API_KEY")
    client = genai.Client(api_key=key)
    uploaded = None
    try:
        uploaded = wait_active(client, client.files.upload(file=file_path))
        return fn(client, uploaded)
    finally:
        if uploaded and getattr(uploaded, "name", None):
            try:
                client.files.delete(name=uploaded.name)
            except Exception:
                pass
        try:
            client.close()
        except Exception:
            pass


def parse_response(response, schema):
    if getattr(response, "parsed", None):
        return response.parsed
    if not getattr(response, "text", None):
        raise RuntimeError("Gemini вернул пустой structured response")
    return schema.model_validate_json(response.text)


def high_thinking_config(**kwargs):
    return types.GenerateContentConfig(
        thinking_config=types.ThinkingConfig(thinking_level="high"),
        **kwargs,
    )


def build_forensic_map(client, uploaded, owned=False, expected_duration=None):
    response = client.models.generate_content(
        model=ANALYSIS_MODEL,
        contents=types.Content(parts=[
            video_part(uploaded, FORENSIC_VIDEO_FPS),
            types.Part(text=(
                "Perform the forensic source analysis now. Inspect the entire video and audio. "
                "Produce the factual timed reconstruction map only."
            )),
        ]),
        config=high_thinking_config(
            system_instruction=forensic_system_prompt(owned, expected_duration),
            response_mime_type="application/json",
            response_schema=ForensicSourceAnalysis,
        ),
    )
    forensic = parse_response(response, ForensicSourceAnalysis)
    if expected_duration and expected_duration > 0:
        forensic.measured_duration_sec = round(float(expected_duration), 2)
    return forensic


def build_production_package(client, uploaded, forensic, owned=False, expected_duration=None, repair=None):
    repair_text = ""
    if repair:
        repair_text = (
            "\nPREVIOUS PACKAGE FAILED QA. Correct every listed issue. Do not preserve any previous mistake.\n"
            + json.dumps(repair, ensure_ascii=False)
        )
    source_map = json.dumps(forensic.model_dump(), ensure_ascii=False)
    response = client.models.generate_content(
        model=ANALYSIS_MODEL,
        contents=types.Content(parts=[
            video_part(uploaded, FORENSIC_VIDEO_FPS),
            types.Part(text=(
                "FORENSIC SOURCE MAP:\n" + source_map + repair_text
                + "\nBuild the final six-block production package from this map and re-check the uploaded source video."
            )),
        ]),
        config=high_thinking_config(
            system_instruction=production_system_prompt(owned, expected_duration),
            response_mime_type="application/json",
            response_schema=ProductionPackage,
        ),
    )
    return normalize_package(parse_response(response, ProductionPackage), expected_duration)


def audit_package(client, forensic, package, expected_duration=None):
    response = client.models.generate_content(
        model=ANALYSIS_MODEL,
        contents=(
            "FORENSIC SOURCE MAP:\n"
            + json.dumps(forensic.model_dump(), ensure_ascii=False)
            + "\n\nPRODUCTION PACKAGE:\n"
            + json.dumps(package.model_dump(), ensure_ascii=False)
        ),
        config=high_thinking_config(
            system_instruction=audit_system_prompt(expected_duration),
            response_mime_type="application/json",
            response_schema=ReconstructionAudit,
        ),
    )
    return parse_response(response, ReconstructionAudit)


def audit_passes(audit):
    checks = [
        audit.exact_duration_ok,
        audit.frame_zero_match_ok,
        audit.dialogue_text_and_order_ok,
        audit.speaker_binding_ok,
        audit.lip_sync_lock_ok,
        audit.spatial_positions_ok,
        audit.object_continuity_ok,
        audit.action_order_and_single_occurrence_ok,
        audit.camera_logic_ok,
        audit.source_story_mechanics_ok,
        audit.realism_override_ok,
    ]
    return all(checks) and audit.overall_match_score >= 96 and not audit.critical_issues


def analyze_video(file_path, owned=False, expected_duration=None):
    def run(client, uploaded):
        forensic = build_forensic_map(client, uploaded, owned, expected_duration)
        package = build_production_package(client, uploaded, forensic, owned, expected_duration)
        audit = audit_package(client, forensic, package, expected_duration)

        if not audit_passes(audit):
            package = build_production_package(
                client,
                uploaded,
                forensic,
                owned,
                expected_duration,
                repair={
                    "overall_match_score": audit.overall_match_score,
                    "critical_issues": audit.critical_issues,
                    "repair_instructions": audit.repair_instructions,
                },
            )
            audit = audit_package(client, forensic, package, expected_duration)

        return normalize_package(package, expected_duration, audit.overall_match_score)

    return with_uploaded_file(file_path, run)


def classify_radar_video(file_path, caption=""):
    def run(client, uploaded):
        prompt = f"""
Оцени Instagram Reel для радара охватных AI-комедийных сценок.
Подходит только если одновременно выполняется основное:
русский язык речи ИЛИ явный российский бытовой контекст
ролик явно создан или существенно сделан с помощью AI
это комедийная сценка бытовой прикол или понятный абсурд
не обучалка про нейросети
не обзор AI сервиса
не talking head
не обычный мем с текстом
простая ситуация понятна без длинного объяснения
формат реально можно воспроизвести
предпочтительно 1–3 персонажа

Оцени САМО ВИДЕО И АУДИО, а подпись используй только как вторичный сигнал.
Если речь есть, отдельно убедись что она русская.
Сильный первый кадр и одна ясная шутка/поворот повышают качество кандидата, но не заменяют обязательные критерии.

Подпись:
{caption[:2000]}
""".strip()
        response = client.models.generate_content(
            model=RADAR_MODEL,
            contents=types.Content(parts=[
                video_part(uploaded, RADAR_VIDEO_FPS),
                types.Part(text=prompt),
            ]),
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                response_mime_type="application/json",
                response_schema=RadarAssessment,
            ),
        )
        return parse_response(response, RadarAssessment)

    return with_uploaded_file(file_path, run)


def summarize_radar_meta(items):
    if not items:
        return None
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None

    compact = []
    for i, item in enumerate(items[:30], start=1):
        compact.append({
            "rank": i,
            "creator": item.get("creator", ""),
            "duration_sec": item.get("duration_sec", 0),
            "views": item.get("views", 0),
            "viral_score_v2": item.get("viral_score_v2", 0),
            "characters": item.get("characters", []),
            "scene_description": item.get("scene_description", ""),
            "hook": item.get("hook", ""),
            "joke": item.get("joke", ""),
            "ending": item.get("ending", ""),
        })

    prompt = f"""
Перед тобой TOP коротких российских AI-комедийных Reels текущего радара.
Найди повторяющиеся паттерны только в этих данных.
Сгруппируй по типам персонажей, локациям, конфликтам/ситуациям, типам первого хука и комедийным механикам.
reels_count должен соответствовать реальному числу записей из входа.
question_hook_count это число роликов где hook явно начинается с вопроса или вопросительной реплики.
examples должны ссылаться на конкретные наблюдения вида @автор описание.
key_takeaways дай 3–6 коротких практических выводов о текущей мете.

ДАННЫЕ:
{json.dumps(compact, ensure_ascii=False)}
""".strip()

    client = genai.Client(api_key=key)
    try:
        response = client.models.generate_content(
            model=RADAR_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
                response_mime_type="application/json",
                response_schema=RadarMetaReport,
            ),
        )
        return parse_response(response, RadarMetaReport)
    finally:
        try:
            client.close()
        except Exception:
            pass
