import radar_service_v2 as _pipeline
from actor_utils import run_actor_items_checked
from cloud_state import save_radar_snapshot
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log
from radar_normalize import normalize_reel
from radar_quality import (
    refresh_recent_scores_quality,
    save_meta_report_quality,
    save_post_preserve_ai,
    top_eligible,
)

# Live pipeline patches:
# 1) failed Apify Actor runs become explicit warnings instead of silent empty datasets.
# 2) Instagram output from different Actors is normalized through the tolerant 2026 field mapper.
# 3) a fresh candidate pass never erases a previous Gemini approval while re-checking.
# 4) creator-baseline refresh preserves the evidence-weighted ranking formula.
# 5) weekly meta is built only from quality-gated TOP winners.
_pipeline.run_actor_items = run_actor_items_checked
_pipeline.normalize_reel = normalize_reel
_pipeline.save_post = save_post_preserve_ai
_pipeline.refresh_recent_scores = refresh_recent_scores_quality
_pipeline.save_meta_report = save_meta_report_quality


def sync_radar():
    result = _pipeline.sync_radar_v2()

    with db_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-7 days') AND ai_match=1
               ORDER BY viral_score_v2 DESC,views_per_hour DESC,views DESC
               LIMIT 120"""
        ).fetchall()
    visible_top = [dict(row) for row in rows if top_eligible(dict(row))]
    result["kept"] = min(len(visible_top), 30)

    add_radar_log(
        f"Quality gate сформировал итоговый TOP: {result['kept']} роликов.",
        stage="quality",
        details={
            "ai_matches_before_quality": len(rows),
            "quality_top": result["kept"],
        },
    )

    set_radar_status(
        "done",
        "Поиск завершён",
        100,
        0,
        (
            f"Собрано {result.get('raw', 0)} → после фильтра {result.get('after_numeric_filter', 0)} "
            f"→ Gemini проверил {result.get('ai_checked', 0)} → достойных повторения в TOP {result['kept']}."
        ),
        details=result,
    )

    try:
        saved = save_radar_snapshot()
        add_radar_log(
            "Стабильный TOP сохранён в облачный Apify snapshot." if saved else "Облачный snapshot пропущен: хранилище недоступно.",
            level="INFO" if saved else "WARN",
            stage="snapshot",
        )
    except Exception as exc:
        # Cloud backup must never turn an otherwise successful radar into an error.
        add_radar_log(f"Не удалось сохранить облачный snapshot: {exc}", level="WARN", stage="snapshot")
    return result
