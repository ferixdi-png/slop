import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = Path(os.environ.get("DB_PATH", DATA_DIR / "slop.db"))

ANALYSIS_MODEL = os.environ.get("ANALYSIS_MODEL", "gemini-3.5-flash")
RADAR_MODEL = os.environ.get("RADAR_MODEL", "gemini-3.1-flash-lite")
APIFY_SEARCH_ACTOR = os.environ.get("APIFY_SEARCH_ACTOR", "apify/instagram-search-scraper")
APIFY_HASHTAG_ACTOR = os.environ.get("APIFY_HASHTAG_ACTOR", "apify/instagram-hashtag-scraper")
APIFY_PROFILE_ACTOR = os.environ.get("APIFY_PROFILE_ACTOR", "apify/instagram-profile-scraper")
SEARCH_LIMIT = int(os.environ.get("APIFY_SEARCH_LIMIT", "64"))
HASHTAG_LIMIT = int(os.environ.get("APIFY_HASHTAG_LIMIT", "80"))
RADAR_AI_ANALYZE_LIMIT = int(os.environ.get("RADAR_AI_ANALYZE_LIMIT", "60"))
RADAR_KEEP_LIMIT = int(os.environ.get("RADAR_KEEP_LIMIT", "30"))
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "120"))

SEARCH_TERMS = [
    "нейроюмор", "ии юмор", "нейросеть прикол", "нейросеть юмор", "AI прикол",
    "AI юмор", "AI бабушка", "нейросеть бабушка", "AI деревня", "нейросеть деревня",
    "AI семья", "AI муж жена", "AI дед", "AI животные юмор", "AI скетч",
]
HASHTAGS = [
    "нейроюмор", "ииюмор", "аиюмор", "нейросеть", "нейросети",
    "aivideo", "бабушка", "деревня", "приколы", "юмор",
]

REALISM_LOCK = """
GLOBAL REALISM LOCK. This rule overrides the rendering style of the source while preserving observable content and staging.
The result must always look like authentic casual footage from a modern flagship smartphone from 2024–2026, never like an AI render, animation, 3D scene, beauty-filter clip, studio commercial, or cinematic production.

STRICT PRESERVATION: preserve observable character count, approximate age, body proportions, hairstyle, clothing category and colors, expression, pose, gaze direction, object placement, background layout, lighting direction, camera angle, composition, action order, dialogue order, pacing, reaction timing and comedic beat. Do not beautify, redesign, glamorize or cinematize the scene.

REALISM RESTORATION: remove AI plastic skin, over-smoothed textures, synthetic sharpness, fake HDR, unnatural clarity, beauty-filter skin, waxy faces, painted hair, fake fabric, impossible reflections and overly perfect symmetry. Restore natural skin pores, microtexture, tiny imperfections, age detail, subtle skin color variation, individual hair strands, authentic fabric weave and folds, real material response, plausible shadows and real-world imperfections.

SKIN: keep pores, fine detail, wrinkles, blemishes, under-eye detail, beard texture, freckles and tonal variation when present. Smoothness stays low. Never create glossy porcelain skin.

SMARTPHONE CAMERA: ordinary computational photography, believable autofocus, automatic exposure, slight handheld micro-shake, tiny framing drift, realistic motion behavior, natural smartphone perspective and slightly softer edges than center. No fake cinematic depth of field or exaggerated bokeh.

COLOR AND LIGHT: natural slightly imperfect white balance, realistic smartphone contrast and saturation, preserved highlights, believable lifted shadows, source-consistent light direction, no theatrical relighting or cinematic grading.

FINAL FEELING: a high-quality everyday phone video from a real person's camera roll. The source controls WHAT happens, WHO does it, WHAT is said, WHEN reactions happen and WHERE the camera is. This lock controls HOW physically real everything looks.
""".strip()
