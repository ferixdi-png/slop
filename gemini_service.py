import json, os, re, time
from google import genai
from google.genai import types

from config import ANALYSIS_MODEL, RADAR_MODEL, REALISM_LOCK
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


def normalize_package(package, expected_duration=None, audit_score=None):
    if expected_duration and expected_duration > 0:
        exact = round(float(expected_duration), 2)
        package.source_duration_sec = exact
        package.block_3_video.exact_duration_sec = exact
        package.block_3_video.duration = f"{exact:.2f} seconds"
        package.block_3_video.duration_lock = (
            f"ABSOLUTE SOURCE DURATION LOCK: the generated video must last exactly {exact:.2f} seconds. "
            "Do not extend shorten pad slow down speed up or reinterpret the total duration. "
            "All actions speech pauses reactions and the final beat must fit inside the exact source duration."
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
        line.visual_speaker_binding = strip_speech(line.visual_speaker_binding)

    package.block_4_audio.pronunciation_hints = [
        strip_speech(x) for x in package.block_4_audio.pronunciation_hints
    ]
    package.block_4_audio.intonation_and_laughter_map = [
        strip_speech(x) for x in package.block_4_audio.intonation_and_laughter_map
    ]

    package.block_1_frame0_prompt += (
        "\n\nMANDATORY ABSOLUTE SMARTPHONE REALISM OVERRIDE:\n" + REALISM_LOCK
    )
    package.block_3_video.realism_lock = REALISM_LOCK

    mandatory_hard_rules = [
        "Preserve the exact measured source duration with zero intentional padding or trimming",
        "Russian speech only whenever the source contains dialogue",
        "Preserve original dialogue turn order and relative timing",
        "Only the visually bound speaking character may move lips during that line",
        "All non speaking characters keep lips completely closed and motionless",
        "Never overlap or swap dialogue between characters",
        "Never duplicate teleport disappear or spontaneously transfer props characters hands or objects",
        "Every important action happens once only and in the original causal order",
        "Preserve screen side relative distance gaze direction and body orientation unless the source visibly changes them",
        "Preserve camera height distance framing direction and movement logic from the source",
        "Do not inherit cartoon CGI 3D or synthetic rendering from the source",
        "Maintain authentic casual handheld smartphone realism throughout the entire clip",
    ]
    existing = list(package.block_3_video.hard_rules or [])
    for rule in mandatory_hard_rules:
        if rule not in existing:
            existing.append(rule)
    package.block_3_video.hard_rules = existing
    return package


def duration_rule(expected_duration=None):
    if expected_duration and expected_duration > 0:
        exact = float(expected_duration)
        return (
            f"Measured source duration is exactly {exact:.2f} seconds. "
            f"The reconstruction must remain exactly {exact:.2f} seconds. "
            "Never round to 10 seconds and never add a silent tail."
        )
    return "Measure and preserve the actual source duration without intentional extension or shortening."


def rights_rule(owned):
    if owned:
        return (
            "The user confirmed rights to the source. Preserve the spoken Russian wording as literally as the audio supports."
        )
    return (
        "If the source contains a recognizable real person or long distinctive protected wording do not imitate that person's identity "
        "or reproduce long unique wording verbatim. Preserve role order meaning rhythm and comedic mechanics instead."
    )


def forensic_system_prompt(owned, expected_duration=None):
    return f"""
You are a forensic video reconstruction analyst. Your ONLY job in this pass is to observe the uploaded source with maximum precision and build a factual source map. Do not design the new video yet. Do not improve the joke. Do not invent missing actions.

DURATION
{duration_rule(expected_duration)}
All start_sec and end_sec values must stay inside that measured duration. Use the finest defensible temporal segmentation. Create a new timeline event whenever speaker action hand state gaze pose object state camera framing or causal beat materially changes. For fast events use sub-second boundaries when visually or audibly supportable. Do not fabricate frame-level precision when the source is ambiguous; list ambiguity in uncertainties.

CHARACTER IDENTITY AND SPATIAL MAP
Create one stable_visual_binding for every visible character based only on persistent visible traits such as age range clothing colors hairstyle body build and position. Never use Character 1 Speaker A or arbitrary names unless a name is visibly established by the source.
For every event record where each character is on screen: left center right foreground middle background seated standing leaning walking facing left facing right facing camera profile or back. Preserve relative distance between characters and camera.
Record body pose head angle gaze facial expression and every meaningful hand state separately.

FRAME ZERO
Treat the literal first visible frame as a reconstruction blueprint. Record camera height distance lens perspective composition headroom crop background geometry exact character placement hand placement object placement gaze expression mouth state lighting and the readiness for the first motion or first phoneme.
The later Frame 0 prompt must be able to reproduce this state before animation begins.

DIALOGUE AND LIP SYNC FORENSICS
If there is speech identify the exact visual speaker for every turn.
Transcribe Russian speech as accurately as the audio supports. Preserve line order. Record approximate start_sec and end_sec for each turn.
For every dialogue turn explicitly describe the speaker mouth behavior and the mouth state of every visible non speaker. Non speakers must be identified as mouth closed lips still unless the source visibly shows otherwise.
Record pauses laughter inhalations reactions and whether any overlap actually occurs. Never infer speaker identity from audio alone when the visual source contradicts it.

ACTIONS OBJECTS AND CAUSALITY
Every important action must be represented once and only once in timeline_events.
Track each prop as one persistent physical object unless the source visibly contains multiples. Record initial holder or location orientation transfers contacts releases drops and final state. No invisible teleportation in the source map.
Describe the causal relation between consecutive events: what physically or verbally causes the next reaction.

CAMERA
For each event record shot continuity camera side approximate height distance framing camera orientation movement direction handheld behavior zoom or push if visible focus changes and cuts if present.
Do not confuse subject movement with camera movement.

AUDIO
Record dialogue environmental sound reaction sounds laughter silence and timing relevant to the comedy.

VISUAL STYLE
In this forensic pass describe what is actually visible even if it is cartoon CGI or AI generated. The realism conversion happens only in the production pass.

RIGHTS
{rights_rule(owned)}

Return only the structured forensic schema. Accuracy is more important than brevity.
""".strip()


def production_system_prompt(owned, expected_duration=None):
    return f"""
You are a professional production system reconstructing a short Russian AI comedy video from a forensic source map plus the original uploaded video.
Your output is exactly the public 6 block package 0 to 5.
The forensic source map is the authority for timing spatial placement dialogue ownership action order object continuity camera logic and Frame 0. Recheck the uploaded video whenever the forensic map contains uncertainty. Never invent a new beat merely because it would be more cinematic or funnier.

ABSOLUTE ONE TO ONE CONTENT LOCK
Preserve from the source as closely as rights allow:
character count and roles
screen side and relative positions
body orientation and pose progression
head angle gaze and expression progression
hand actions and object contacts
object identity holder location and transfer order
camera side height distance framing and movement logic
speech turn order relative timing pauses and reactions
story causality setup escalation punchline and ending
exact measured total duration

DURATION
{duration_rule(expected_duration)}
The public Block 3 duration and exact_duration_sec must equal the measured source duration. All speech actions reactions and ending must fit naturally inside it. Never round upward to 10 seconds.

RUSSIAN SPEECH AND LIP SYNC
If the source contains dialogue the target contains Russian dialogue.
Use Literal Speaker Binding only: visible physical description of the speaker.
No CHARACTER_01 Speaker A abstract labels or unbound names.
Each Russian line appears once in dialogue. dialogue_instruction must equal visual speaker binding plus says in Russian plus the Russian line in quotes.
Inside russian_text use no punctuation.
Only the bound speaker moves lips during that line. Every other visible character is silent with mouth closed and lips motionless. Do not overlap swap duplicate or leak lines.
Preserve the forensic dialogue order and relative turn timing.

NARRATIVE TIMELINE
Do not expose numeric timestamps inside narrative_timeline because this target prompt format is more stable without them.
Translate the forensic timed map into a chronological causal sequence using First Then Then Finally.
The internal source map controls the relative timing even though the public narrative contains no timestamps.
Every important action happens once only.

FRAME 0
Block 1 must be an ultra detailed English image prompt derived from forensic frame_zero.
It must specify exact character placement body pose head angle gaze expression mouth state hands objects clothing background geometry camera height distance lens perspective crop lighting and the physical readiness for the first motion and first phoneme.
It must be sufficient to generate the starting image without seeing the source.

ABSOLUTE SMARTPHONE REALISM OVERRIDE
The source controls WHAT happens but not synthetic rendering style.
Even if the source is cartoon CGI 3D stylized or obviously AI generated transform it into physically plausible live action while preserving source staging.
The result must feel like ordinary spontaneous vertical footage from a modern flagship smartphone 2024 to 2026.
Natural pores micro skin texture tonal variation facial asymmetry individual hair strands flyaways anatomically normal hands real fabric thickness folds seams tension real materials contact shadows real environmental lighting realistic autofocus automatic exposure slight handheld micro shake framing drift plausible motion blur and natural smartphone depth are mandatory.
No beauty filter porcelain skin CGI surfaces fake HDR studio glamour light cinematic grading or artificial DSLR bokeh.

BLOCK 3 MUST BE SELF SUFFICIENT
Fill every field with concrete source specific instructions:
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
Do not use vague phrases such as same as source or preserve everything without also spelling out what must be preserved.

Block 4 contains dialogue pronunciation hints intonation and laughter map. No dash characters and no punctuation in the spoken text.
Block 5 contains short post Shorts title retention phrase and hashtags. Do not invent brands or URLs and do not guarantee reach.

SAFETY
Do not describe bypassing filters. If a problematic element requires modification make the smallest safe change while preserving pacing and comedic mechanics.

RIGHTS
{rights_rule(owned)}

{REALISM_LOCK}

Return only the ProductionPackage schema.
""".strip()


def audit_system_prompt(expected_duration=None):
    return f"""
You are a strict reconstruction quality auditor. Compare the forensic source map against the generated public package. Do not reward writing quality. Judge factual correspondence.

Verify all of the following independently:
1 exact total duration equals the measured source duration {f'{float(expected_duration):.2f}s' if expected_duration else ''}
2 Frame 0 preserves source spatial arrangement pose hands objects gaze mouth state camera and first action readiness
3 dialogue text and turn order correspond to the forensic map within rights constraints
4 each line is bound to the correct visible speaker
5 lip sync rules explicitly silence every non speaker and prevent voice overlap or swaps
6 screen side relative distance body orientation gaze progression and movement order remain consistent
7 every tracked object remains singular and continuous with correct holder location and transfers
8 every important action occurs once in the original causal order
9 camera side height distance framing and movement logic correspond to source
10 setup escalation punchline and ending mechanics correspond to source
11 synthetic source styling is converted to live action smartphone realism without altering source staging

A single serious speaker swap object teleport missing action invented action wrong side of frame wrong dialogue order wrong duration or wrong Frame 0 should make the relevant boolean false.
critical_issues must be concrete mismatches only.
repair_instructions must say exactly what the production pass must change.
Return only ReconstructionAudit.
""".strip()


def wait_active(client, uploaded):
    deadline = time.time() + 240
    while getattr(uploaded, "state", None) and getattr(uploaded.state, "name", "") != "ACTIVE":
        if getattr(uploaded.state, "name", "") == "FAILED":
            raise RuntimeError("Сервис анализа не смог обработать видео")
        if time.time() > deadline:
            raise RuntimeError("Видео слишком долго обрабатывается")
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    return uploaded


def with_uploaded_file(file_path, fn):
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("На сервере не задан GEMINI_API_KEY")
    client, uploaded = genai.Client(api_key=key), None
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
    return schema.model_validate_json(response.text)


def build_forensic_map(client, uploaded, owned=False, expected_duration=None):
    response = client.models.generate_content(
        model=ANALYSIS_MODEL,
        contents=[
            uploaded,
            "Perform the forensic source analysis now. Observe the full video and produce the factual timed source map only."
        ],
        config=types.GenerateContentConfig(
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
    forensic_json = json.dumps(forensic.model_dump(), ensure_ascii=False)
    repair_text = ""
    if repair:
        repair_text = (
            "\nA previous package failed audit. Apply every repair instruction exactly and do not preserve the previous mistake.\n"
            + json.dumps(repair, ensure_ascii=False)
        )
    response = client.models.generate_content(
        model=ANALYSIS_MODEL,
        contents=[
            uploaded,
            "FORENSIC SOURCE MAP:\n" + forensic_json + repair_text,
            "Build the final six block production package from this source map and the uploaded video."
        ],
        config=types.GenerateContentConfig(
            system_instruction=production_system_prompt(owned, expected_duration),
            response_mime_type="application/json",
            response_schema=ProductionPackage,
        ),
    )
    return normalize_package(parse_response(response, ProductionPackage), expected_duration)


def audit_package(client, forensic, package, expected_duration=None):
    response = client.models.generate_content(
        model=ANALYSIS_MODEL,
        contents=[
            "FORENSIC SOURCE MAP:\n" + json.dumps(forensic.model_dump(), ensure_ascii=False),
            "PRODUCTION PACKAGE:\n" + json.dumps(package.model_dump(), ensure_ascii=False),
        ],
        config=types.GenerateContentConfig(
            system_instruction=audit_system_prompt(expected_duration),
            response_mime_type="application/json",
            response_schema=ReconstructionAudit,
        ),
    )
    return parse_response(response, ReconstructionAudit)


def audit_passes(audit):
    core_checks = [
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
    return all(core_checks) and audit.overall_match_score >= 96 and not audit.critical_issues


def analyze_video(file_path, owned=False, expected_duration=None):
    def run(client, uploaded):
        # Pass 1: forensic timed observation map.
        forensic = build_forensic_map(client, uploaded, owned, expected_duration)

        # Pass 2: production package generated from the forensic map.
        package = build_production_package(
            client, uploaded, forensic, owned, expected_duration
        )

        # Pass 3: strict text-only audit. Repair once if anything material differs.
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

        return normalize_package(
            package, expected_duration, audit_score=audit.overall_match_score
        )

    return with_uploaded_file(file_path, run)


def classify_radar_video(file_path, caption=""):
    def run(client, uploaded):
        prompt = f"""Оцени Instagram Reel для радара.
Подходит только если: русский язык или российский контекст; явно AI-generated; комедийная бытовая сценка, прикол или абсурд; не обучалка, не обзор сервиса, не talking head, не обычный мем с текстом; простая понятная ситуация; формат можно воспроизвести с другими персонажами. Желательно 1–3 персонажа.
Если есть речь, она должна быть русской или естественно относиться к российскому контексту.
Оцени само видео, не только подпись.
Подпись: {caption[:2000]}"""
        response = client.models.generate_content(
            model=RADAR_MODEL,
            contents=[uploaded, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RadarAssessment,
            ),
        )
        return parse_response(response, RadarAssessment)

    return with_uploaded_file(file_path, run)


def summarize_radar_meta(items):
    if not items:
        return None
    key = os.environ.get("GEMINI_API_KEY")
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

    prompt = f"""Перед тобой TOP коротких российских AI-комедийных Reels текущего радара.
Найди повторяющиеся паттерны именно в этих данных, ничего не выдумывай сверх входа.
Сгруппируй ролики в понятные кластеры: тип персонажей, локация, конфликт или ситуация, тип первого хука, комедийная механика.
Один ролик может поддерживать несколько наблюдений, но reels_count в конкретном кластере должен отражать реальное количество подходящих записей из входа.
question_hook_count — сколько роликов по описанию hook явно начинаются с вопроса или вопросительной реплики.
examples — короткие ссылки на конкретные наблюдаемые примеры вида @автор: описание.
key_takeaways — 3–6 коротких выводов о текущей мете, только по этому TOP.

ДАННЫЕ:
{json.dumps(compact, ensure_ascii=False)}
"""
    client = genai.Client(api_key=key)
    try:
        response = client.models.generate_content(
            model=RADAR_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
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
