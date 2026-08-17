import radar_service_v2 as _pipeline
from actor_utils import run_actor_items_checked
from radar_normalize import normalize_reel
from radar_quality import refresh_recent_scores_quality, save_post_preserve_ai

# Live pipeline patches:
# 1) failed Apify Actor runs become explicit warnings instead of silent empty datasets.
# 2) Instagram output from different Actors is normalized through the tolerant 2026 field mapper.
# 3) a fresh candidate pass never erases a previous Gemini approval while re-checking.
# 4) creator-baseline refresh preserves the evidence-weighted ranking formula.
_pipeline.run_actor_items = run_actor_items_checked
_pipeline.normalize_reel = normalize_reel
_pipeline.save_post = save_post_preserve_ai
_pipeline.refresh_recent_scores = refresh_recent_scores_quality

sync_radar = _pipeline.sync_radar_v2
