"""V34 full-scope momentum ranking.

Legacy V24 measured cross-run growth only for omni/veo/veo3 in a seven-day SQL
window. The product is now five tags and fourteen days. This overlay preserves the
existing history table/cloud checkpoint, extends measurement to the missing scope,
and makes the broad TOP prefer effective current velocity (measured when available,
lifetime views/hour otherwise).
"""

from __future__ import annotations

import sys
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
        v28.TARGET_TAGS,
    ).fetchall()
    now = datetime.now(timezone.utc)
    extended = 0

    for row in rows:
        if _is_legacy_already_covered(row, now):
            continue
        previous = conn.execute(
            "SELECT observed_at,views,average_views_per_hour FROM radar_momentum_history WHERE post_url=?",
            (row["post_url"],),
        ).fetchone()
        measured_vph, acceleration, has_history = v24._history_velocity(row, previous, now)
        score = v28._momentum_score(
            int(row["views"] or 0),
            float(row["hours_since_publish"] or 0),
            float(measured_vph or row["views_per_hour"] or 0),
            float(acceleration or 0),
            bool(has_history),
        )
        conn.execute(
            """UPDATE radar_posts
               SET viral_score_v2=?, measured_growth_per_hour=?, growth_acceleration=?
               WHERE id=?""",
            (
                score,
                measured_vph if has_history else 0.0,
                acceleration if has_history else 0.0,
                row["id"],
            ),
        )
        observed = v24._parse_time(previous["observed_at"]) if previous else None
        elapsed_hours = (now - observed).total_seconds() / 3600.0 if observed else None
        if previous is None or (elapsed_hours is not None and elapsed_hours >= v24.MIN_HISTORY_HOURS):
            conn.execute(
                """INSERT INTO radar_momentum_history(post_url,observed_at,views,average_views_per_hour,search_term)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(post_url) DO UPDATE SET
                     observed_at=excluded.observed_at,
                     views=excluded.views,
                     average_views_per_hour=excluded.average_views_per_hour,
                     search_term=excluded.search_term""",
                (
                    row["post_url"], now.isoformat(), int(row["views"] or 0),
                    float(row["views_per_hour"] or 0), str(row["search_term"] or "").lower(),
                ),
            )
        extended += 1
    conn.commit()

    # V25's wrapper checkpointed before our extension. Save once more so AI/ИИ and
    # days 8-14 also survive Render instance replacement.
    try:
        cloud_momentum.save_momentum_checkpoint(conn)
    except Exception as exc:
        add_radar_log(f"V34 momentum checkpoint warning: {exc}", level="WARN", stage="v34-momentum")
    return extended


def query_broad_by_current_velocity(limit=broad.OUTPUT_LIMIT):
    placeholders = ",".join("?" for _ in v28.TARGET_TAGS)
    with db_conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM radar_posts
                WHERE datetime(published_at)>=datetime('now','-{v28.LOOKBACK_DAYS} days')
                  AND LOWER(COALESCE(search_term,'')) IN ({placeholders})
                ORDER BY
                  CASE WHEN COALESCE(measured_growth_per_hour,0)>0
                       THEN measured_growth_per_hour ELSE COALESCE(views_per_hour,0) END DESC,
                  COALESCE(growth_acceleration,0) DESC,
                  COALESCE(viral_score_v2,0) DESC,
                  COALESCE(views,0) DESC
                LIMIT 400""",
            v28.TARGET_TAGS,
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
        return {"profile": PROFILE, "tags": list(v28.TARGET_TAGS), "lookback_days": v28.LOOKBACK_DAYS}
    _APPLIED = True
    _BASE_REFRESH = radar_quality.refresh_recent_scores_quality
    radar_quality.refresh_recent_scores_quality = refresh_momentum_v34
    broad._query_broad_rows = query_broad_by_current_velocity

    # FINAL SAFETY LAYER. It must be installed after every legacy/budget/hardening
    # wrapper and before the browser document is installed. Durable state may
    # survive, but browser GET/reload/deploy cannot advance it without a fresh
    # in-memory token issued by an explicit Start/Continue click.
    app_module = sys.modules.get("app")
    if app_module is None:
        raise RuntimeError("V35 manual-start guard must be installed from app startup")
    from radar_manual_start_v35 import install_manual_start_v35
    manual_info = install_manual_start_v35(app_module)
    from frontend_manual_start_v35 import patch_frontend_v35
    frontend_manual_info = patch_frontend_v35()

    info = {
        "profile": PROFILE,
        "tags": list(v28.TARGET_TAGS),
        "lookback_days": v28.LOOKBACK_DAYS,
        "ranking": "measured_current_growth_else_views_per_hour",
        "cloud_checkpoint": True,
        "manual_start_profile": manual_info["profile"],
        "manual_start_only": True,
        "auto_resume_on_page_load": False,
        "tick_requires_driver_token": True,
        "driver_token_persisted": False,
        "frontend_manual_start": frontend_manual_info["manual_start_only"],
    }
    add_radar_log(
        "V34 MOMENTUM + V35 MANUAL START READY: all 5 tags / 14 days; measured growth ranking; page load/reload/deploy cannot advance paid work.",
        stage="startup",
        details=info,
    )
    return info
