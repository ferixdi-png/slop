import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "slop.db"))

# ABSOLUTE MODEL LOCK.
# Radar classification, forensic video analysis, production prompts, QA and repair
# always use the same low-cost model. Environment variables cannot override it.
ANALYSIS_MODEL = "gemini-3.1-flash-lite"
RADAR_MODEL = "gemini-3.1-flash-lite"
APIFY_SEARCH_ACTOR = os.environ.get("APIFY_SEARCH_ACTOR", "apify/instagram-search-scraper")
APIFY_HASHTAG_ACTOR = os.environ.get("APIFY_HASHTAG_ACTOR", "apify/instagram-hashtag-scraper")
APIFY_CREATOR_ACTOR = os.environ.get("APIFY_CREATOR_ACTOR", "apify/instagram-reel-scraper")
SEARCH_LIMIT = int(os.environ.get("APIFY_SEARCH_LIMIT", "64"))
HASHTAG_LIMIT = int(os.environ.get("APIFY_HASHTAG_LIMIT", "80"))
# The radar only needs the strongest discovery pool. Detailed prompts are generated
# later on demand for the chosen winners, so checking more than 30 videos wastes time/tokens.
RADAR_AI_ANALYZE_LIMIT = min(30, int(os.environ.get("RADAR_AI_ANALYZE_LIMIT", "30")))
RADAR_KEEP_LIMIT = int(os.environ.get("RADAR_KEEP_LIMIT", "30"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "120"))

# Fine-grained video sampling for clips up to 10 seconds.
FORENSIC_VIDEO_FPS = float(os.environ.get("FORENSIC_VIDEO_FPS", "5"))
RADAR_VIDEO_FPS = float(os.environ.get("RADAR_VIDEO_FPS", "2"))

# Kept only for backwards compatibility with old deployments; launch concurrency is
# now controlled by an in-process request lock rather than a time-based cooldown.
RADAR_SYNC_COOLDOWN_MINUTES = 0

# Instagram Search Scraper accepts multiple search phrases in one comma-separated query.
SEARCH_TERMS = [
    "нейроюмор, ии юмор, нейросеть прикол, нейросеть юмор, AI прикол, AI юмор, "
    "AI бабушка, нейросеть бабушка, AI деревня, нейросеть деревня, AI семья, "
    "AI муж жена, AI дед, AI животные юмор, AI скетч"
]
HASHTAGS = [
    "нейроюмор", "ииюмор", "аиюмор", "нейросеть", "нейросети",
    "aivideo", "бабушка", "деревня", "приколы", "юмор",
]

REALISM_LOCK = """
GLOBAL SMARTPHONE REALISM OVERRIDE — ABSOLUTE PRIORITY.

The source controls CONTENT, not rendering style.
Preserve what happens: character count and roles, observable age archetypes, body proportions, hairstyle logic, clothing category and colors, pose, gaze direction, object placement, background layout, camera position, composition, action order, dialogue order, pacing, reaction timing, comedic beat and ending.
If the source is cartoon, animation, CGI, stylized 3D, plastic AI imagery or otherwise artificial, DO NOT preserve that artificial rendering style. Reconstruct the same scene as believable live-action reality.

FINAL TARGET:
The result must look like authentic casual vertical footage captured on a modern flagship smartphone from 2024–2026 and found in a real person's camera roll. It must never feel like an animation, render, studio commercial, beauty-filter clip or cinematic production.

CANDID HUMAN BEHAVIOR:
Natural imperfect posture, uneven weight distribution, small asymmetry, believable hand placement, tiny involuntary movements, spontaneous micro-expressions, realistic blinking, natural eye focus, subtle timing imperfections, hair movement and clothing movement appropriate to the action. Avoid influencer posing, catalog stiffness and perfect symmetry.

SKIN REALISM:
Natural human pores, microtexture, tiny imperfections, subtle tonal variation, real under-eye detail, wrinkles or age detail when appropriate, beard texture or freckles when present. Smoothness stays low. No porcelain complexion, glossy plastic skin, waxy faces, beauty-filter geometry or excessive retouching.

HAIR REALISM:
Individual strands, small flyaways, realistic roots, natural highlights, plausible density, real interaction with shoulders, wind and movement. No painted hair, helmet hair or CGI strands.

HAND AND ANATOMY REALISM:
Correct human anatomy, plausible joints, natural grip around objects, no duplicated fingers or limbs, no melted hands, no body morphing. Permanent body proportions remain physically consistent through the scene.

FABRIC AND MATERIAL REALISM:
Real fabric thickness, weave, seams, folds, gravity, tension points and contact with the body. Objects must have plausible materials, reflections, contact shadows and wear. No melted fabric, fake surfaces or impossible reflections.

SMARTPHONE CAPTURE SIMULATION:
Handheld modern flagship smartphone camera, ordinary computational photography, realistic wide-camera perspective, small physically plausible lens distortion, slightly softer edges than center, believable autofocus, subtle focus breathing, automatic exposure, automatic white balance, slight handheld micro-shake, tiny framing drift and occasional tiny motion softness when appropriate.
No artificial DSLR-style cinematic bokeh, no impossible gimbal perfection unless the source is clearly static, no fake depth of field.

LIGHTING:
All light must plausibly originate from the real environment. Preserve the source lighting direction where possible, but convert stylized light into physically believable daylight or ambient indoor light. Subject and background share the same light. Allow minor highlight clipping and imperfect shadow lift typical of phones. No invisible studio lights, theatrical relighting, neon fantasy glow or cinematic grading.

IMAGE PROCESSING:
Natural optical sharpness, moderate noise reduction while preserving texture, realistic smartphone HDR only, authentic highlight rolloff, subtle sensor grain, slightly imperfect white balance and realistic saturation. Remove synthetic sharpening, fake HDR, unnatural clarity, over-denoising and overedited Instagram look.

REAL-WORLD IMPERFECTIONS:
Allow minor framing errors, small horizon deviation, slightly inconsistent headroom, background clutter appropriate to real life, tiny autofocus uncertainty, subtle exposure adaptation, hair crossing the face, clothing wrinkles and micro motion blur.

FRAME 0:
Frame 0 must already look like a real photograph from a smartphone camera roll, not concept art. Every visible face, hand, fabric, object, shadow and background surface must pass the same realism rules before animation starts.

FINAL FEELING:
A premium but ordinary smartphone video of a real event that genuinely happened. The viewer should read it as live-action phone footage first, not as AI-generated content.
""".strip()
