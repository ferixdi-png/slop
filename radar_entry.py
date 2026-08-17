import radar_service_v2 as _pipeline
from actor_utils import run_actor_items_checked

# Patch the pipeline's Apify runner so FAILED/ABORTED/TIMED-OUT Actor runs
# become explicit source warnings instead of silent empty datasets.
_pipeline.run_actor_items = run_actor_items_checked

sync_radar = _pipeline.sync_radar_v2
