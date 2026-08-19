"""V34 final momentum ranking for the broad trend pool.

V28 is the authoritative five-tag / fourteen-day scorer and already maintains the
single DB-backed radar_momentum_history table. V34 must not calculate that history
a second time. This overlay guarantees one authoritative score refresh immediately
before final TOP construction, checkpoints the finished history, and makes the
broad TOP prefer measured current growth when it exists, otherwise lifetime VPH.

Architecture rules:
- V28 owns five-tag/14-day scoring and DB observations;
- V25 owns cloud/local checkpoint serialization for that same DB table;
- V34 owns final refresh timing + broad ranking only;
- persisted TOP rows must belong to the current V30 screening profile;
- V35 manual-start is installed once by the final production bootstrap.
"""

from __future__ import annotations

import radar_broad_v34 as broad
import radar_edge_v19 as edge
import radar_momentum_cloud_v25 as cloud_momentum
import radar_multiplatform_v28 as v28
import radar_quality
from db import db_conn
from radar_logs import add_radar_log

PROFILE = "momentum_v34_five_tags_14d"
_APPLIED = False
_BASE_REFRESH = None
_BASE_FINALIZE = None


def refresh_momentum_v34(conn):
    """Run the authoritative V28 scorer once, then checkpoint its finished history."""
    _BASE_REFRESH(conn)
    try:
        if not cloud_momentum.save_momentum_checkpoint(conn):
            add_radar_log(
                "V34 momentum checkpoint остался только в локальном fallback.",
                level="WARN",
                stage="momentum-cloud",
            )
    except Exception as exc:
        add_radar_log(
            f"V34 momentum checkpoint не сохранён в cloud mirror: {exc}",
            level="WARN",
            stage="momentum-cloud",
        )


def finalize_with_momentum_v34(job):
    """Refresh current velocity exactly once before the broad finalizer reads TOP."""
    with db_conn() as conn:
        refresh_momentum_v34(conn)
    return _BASE_FINALIZE(job)


def query_broad_by_current_velocity(limit=100):
    """Current-profile broad rows ranked by measured growth, then estimated VPH."""
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-14 days')
                 AND duration_sec>=1.0 AND duration_sec<=15.05
                 AND LOWER(COALESCE(search_term,'')) IN ('omni','veo','veo3','ai','ии')
                 AND COALESCE(screening_profile,'')=?
               ORDER BY
                 CASE WHEN COALESCE(measured_growth_per_hour,0)>0 THEN 1 ELSE 0 END DESC,
                 CASE WHEN COALESCE(measured_growth_per_hour,0)>0
                      THEN measured_growth_per_hour ELSE COALESCE(views_per_hour,0) END DESC,
                 viral_score_v2 DESC,
                 views DESC
               LIMIT ?""",
            (broad.SCREENING_PROFILE, max(int(limit) * 3, 300)),
        ).fetchall()
    out = []
    for raw in rows:
        x = dict(raw)
        if not broad.broad_eligible(x):
            continue
        try:
            import json
            x["characters"] = json.loads(x.get("characters_json") or "[]")
        except Exception:
            x["characters"] = []
        x.update(radar_quality.recommendation_status_for_row(x))
        x.update(broad.adaptation_fields(x))
        measured = float(x.get("measured_growth_per_hour") or 0)
        fallback = float(x.get("views_per_hour") or 0)
        x["effective_growth_per_hour"] = measured if measured > 0 else fallback
        x["growth_signal"] = "MEASURED" if measured > 0 else "ESTIMATED"
        out.append(x)
        if len(out) >= int(limit):
            break
    return out


def apply_momentum_v34():
    global _APPLIED, _BASE_REFRESH, _BASE_FINALIZE
    if _APPLIED:
        return {
            "profile": PROFILE,
            "tags": list(v28.TARGET_TAGS),
            "lookback_days": v28.LOOKBACK_DAYS,
            "ranking": "measured_current_growth_else_views_per_hour",
            "cloud_checkpoint": True,
        }

    _APPLIED = True
    _BASE_REFRESH = radar_quality.refresh_recent_scores_quality
    _BASE_FINALIZE = edge._BASE_FINALIZE
    radar_quality.refresh_recent_scores_quality = refresh_momentum_v34
    broad._query_broad_rows = query_broad_by_current_velocity
    edge._BASE_FINALIZE = finalize_with_momentum_v34

    info = {
        "profile": PROFILE,
        "tags": list(v28.TARGET_TAGS),
        "lookback_days": v28.LOOKBACK_DAYS,
        "ranking": "measured_current_growth_else_views_per_hour",
        "scoring_owner": "radar_multiplatform_v28.refresh_scores_v28",
        "history_backend": "radar_momentum_history",
        "cloud_checkpoint": True,
        "refresh_before_final_top": True,
        "current_profile_only": True,
        "duplicate_history_calculation": False,
        "manual_start_installed_here": False,
        "control_owner": "final_runtime_bootstrap",
    }
    add_radar_log(
        "V34 MOMENTUM READY: one V28 5-tag/14-day refresh runs before final TOP; current-profile ranking only.",
        stage="startup",
        details=info,
    )
    return info
