"""Core high-frequency hashtag profile layered on top of the <$5 budget guard.

The goal is to spend the same bounded Apify budget on fewer, much larger feeds.
Low-frequency long-tail tags are intentionally excluded.
"""

import radar_budget_v10 as budget
import radar_growth_v6 as growth
from radar_logs import add_radar_log

PROFILE_VERSION = "mass_global_ai_v12_core5"
HASHTAG_LIMIT = 32

# Broad, high-frequency AI/video tags only.
# Explicitly excluded: sora, gemini, geminiomni, googleflowai and other long tails.
HASHTAGS = [
    "ai",
    "ии",
    "нейросеть",
    "нейросети",
    "aivideo",
    "aivideos",
    "grok",
    "grokai",
    "veo",
    "veo3",
    "omni",
    "omniai",
    "chatgpt",
    "openai",
    "klingai",
    "seedance",
]


def apply_highfreq_overrides():
    # Keep v10's real platform-side dollar caps, classifier bounds and quota fallback.
    budget.PROFILE_VERSION = PROFILE_VERSION
    budget.HASHTAG_LIMIT = HASHTAG_LIMIT
    budget.HASHTAGS = list(HASHTAGS)

    # The source builder reads the growth-module globals dynamically.
    growth.PROFILE_VERSION = PROFILE_VERSION
    growth.HASHTAG_LIMIT = HASHTAG_LIMIT
    growth.HASHTAGS_V7 = list(HASHTAGS)

    info = budget._assert_budget()
    add_radar_log(
        "HIGH-FREQ AI v12: только крупные AI/video хештеги; длинный хвост удалён; hard budget <$5 сохранён.",
        stage="startup",
        details={
            "profile": PROFILE_VERSION,
            "hashtags": list(HASHTAGS),
            "hashtag_limit_each": HASHTAG_LIMIT,
            **info,
        },
    )
    return info
