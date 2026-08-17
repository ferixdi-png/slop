import radar_service_v2 as _pipeline
from actor_utils import run_actor_items_checked
from radar_normalize import normalize_reel

# Live pipeline patches:
# 1) failed Apify Actor runs become explicit warnings instead of silent empty datasets.
# 2) Instagram output from different Actors is normalized through the tolerant 2026 field mapper.
_pipeline.run_actor_items = run_actor_items_checked
_pipeline.normalize_reel = normalize_reel

sync_radar = _pipeline.sync_radar_v2
