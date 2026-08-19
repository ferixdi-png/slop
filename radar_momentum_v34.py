"""V34 full-scope momentum ranking.

Legacy V24 measured cross-run growth only for omni/veo/veo3 in a seven-day SQL
window. The product is now five tags and fourteen days. This overlay preserves the
existing history table/cloud checkpoint, extends measurement to the missing scope,
and makes the broad TOP prefer effective current velocity (measured when available,
lifetime views/hour otherwise).

Important architecture rule: this module owns MOMENTUM ONLY. V35 manual-start
control is installed exactly once by the final production bootstrap after every
legacy/budget/momentum wrapper. Keeping those responsibilities separate prevents
frontend patches and driver guards from being applied twice during import.
"""

from __future__ import annotations

from datetime import datetime, timezone

import radar_broad_v34 as broad
import radar_momentum_cloud_v25 as cloud_momentum
import radar_multiplatform_v28 as v28
import radar_omni_veo_veo3_v24 as v24
import radar_quality
from db import db_conn
from radar_logs import add_radar_log

PROFILE = "momentum_v34_five_tags_14d"
_APPLIED = False
_BASE_REFRESH = None


def _is_legacy_already_covered(row, now):
    term = str(row["search_term"] or "").lower()
    published = v24._parse_time(row["published_at"])
    age_days = ((now - published).total_seconds() / 86400.0) if published else 999.0
    return term in {"omni", "veo", "veo3"} and age_days <= 7.0


def refresh_momentum_v34(conn):
    # First run every proven quality/V24/V25 step. Then fill only the part that the
    # legacy 3-tag/7-day momentum implementation did not cover.
    _BASE_REFRESH(conn)
    placeholders = ",".join("?" for _ in v28.TARGET_TAGS)
    rows = conn.execute(
        f"""SELECT id,post_url,search_term,published_at,views,hours_since_publish,
                   views_per_hour,viral_score_v2,measured_growth_per_hour,growth_acceleration
            FROM radar_posts
            WHERE datetime(published_at)>=datetime('now','-{v28.LOOKBACK_DAYS} days')
              AND LOWER(COALESCE(search_term,'')) IN ({placeholders})""",
        tuple(v28.TARGET_TAGS),
    ).fetchall()
    now = datetime.now(timezone.utc)
    history = cloud_momentum.load_momentum_history()
    changed = False

    for raw in rows:
        row = dict(raw)
        if _is_legacy_already_covered(row, now):
            continue
        url = str(row.get("post_url") or "")
        if not url:
            continue
        current_views = int(row.get("views") or 0)
        previous = history.get(url) if isinstance(history, dict) else None
        measured = 0.0
        acceleration = 0.0
        if isinstance(previous, dict):
            try:
                old_views = int(previous.get("views") or 0)
                old_at = v24._parse_time(previous.get("observed_at"))
                if old_at and current_views >= old_views:
                    hours = max(0.05, (now - old_at).total_seconds() / 3600.0)
                    measured = max(0.0, (current_views - old_views) / hours)
                    old_rate = float(previous.get("effective_growth_per_hour") or previous.get("views_per_hour") or 0)
                    acceleration = measured - old_rate
            except Exception:
                measured = 0.0
                acceleration = 0.0
        if measured > 0:
            conn.execute(
                "UPDATE radar_posts SET measured_growth_per_hour=?,growth_acceleration=? WHERE id=?",
                (round(measured, 2), round(acceleration, 2), row["id"]),
            )
        history[url] = {
            "views": current_views,
            "observed_at": now.isoformat(),
            "views_per_hour": float(row.get("views_per_hour") or 0),
            "effective_growth_per_hour": measured if measured > 0 else float(row.get("views_per_hour") or 0),
            "search_term": row.get("search_term"),
        }
        changed = True

    conn.commit()
    if changed:
        cloud_momentum.save_momentum_history(history)


def query_broad_by_current_velocity(limit=100):
    """Broad V34 rows ranked by measured growth first, then estimated velocity."""
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-14 days')
                 AND duration_sec>=1.0 AND duration_sec<=15.05
                 AND LOWER(COALESCE(search_term,'')) IN ('omni','veo','veo3','ai','ии')
               ORDER BY
                 CASE WHEN COALESCE(measured_growth_per_hour,0)>0 THEN 1 ELSE 0 END DESC,
                 COALESCE(measured_growth_per_hour,views_per_hour,0) DESC,
                 viral_score_v2 DESC,
                 views DESC
               LIMIT ?""",
            (max(int(limit) * 3, 300),),
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
    global _APPLIED, _BASE_REFRESH
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
    radar_quality.refresh_recent_scores_quality = refresh_momentum_v34
    broad._query_broad_rows = query_broad_by_current_velocity

    info = {
        "profile": PROFILE,
        "tags": list(v28.TARGET_TAGS),
        "lookback_days": v28.LOOKBACK_DAYS,
        "ranking": "measured_current_growth_else_views_per_hour",
        "cloud_checkpoint": True,
        "manual_start_installed_here": False,
        "control_owner": "final_runtime_bootstrap",
    }
    add_radar_log(
        "V34 MOMENTUM READY: all 5 tags / 14 days; measured growth ranking; control layer delegated to final bootstrap.",
        stage="startup",
        details=info,
    )
    return info
