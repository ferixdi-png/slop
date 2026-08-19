"""V34 full-scope momentum ranking.

Legacy V24 measures cross-run growth for omni/veo/veo3 in its seven-day window.
The final product is five tags and fourteen days. This overlay extends that exact
DB-backed momentum mechanism to the missing scope and ranks the broad TOP by
current velocity without creating a second history store.

Architecture rules:
- radar_momentum_history is the single source of cross-run observation truth;
- V25 remains the cloud/local checkpoint for that table;
- current V30 screening_profile is mandatory for persisted TOP rows;
- this module owns MOMENTUM ONLY. V35 manual-start is installed by the final
  production bootstrap after all discovery/budget/momentum wrappers.
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


def _history_row(conn, post_url):
    return conn.execute(
        """SELECT observed_at,views,average_views_per_hour,search_term
           FROM radar_momentum_history WHERE post_url=?""",
        (post_url,),
    ).fetchone()


def _observation_due(previous, now):
    if previous is None:
        return True
    observed = v24._parse_time(previous["observed_at"])
    if observed is None:
        return True
    return (now - observed).total_seconds() / 3600.0 >= v24.MIN_HISTORY_HOURS


def _upsert_observation(conn, row, now):
    conn.execute(
        """INSERT INTO radar_momentum_history(post_url,observed_at,views,average_views_per_hour,search_term)
           VALUES(?,?,?,?,?)
           ON CONFLICT(post_url) DO UPDATE SET
             observed_at=excluded.observed_at,
             views=excluded.views,
             average_views_per_hour=excluded.average_views_per_hour,
             search_term=excluded.search_term""",
        (
            str(row["post_url"]),
            now.isoformat(),
            int(row["views"] or 0),
            float(row["views_per_hour"] or 0),
            str(row["search_term"] or "").lower(),
        ),
    )


def refresh_momentum_v34(conn):
    """Refresh all five tags using one DB history + one V25 checkpoint format."""
    # Preserve every proven quality/V24/V25 step first. V24 handles its original
    # 3-tag/7-day scope; below we fill only rows it intentionally does not cover.
    _BASE_REFRESH(conn)

    placeholders = ",".join("?" for _ in v28.TARGET_TAGS)
    params = (*v28.TARGET_TAGS, broad.SCREENING_PROFILE)
    rows = conn.execute(
        f"""SELECT id,post_url,search_term,published_at,views,hours_since_publish,
                   views_per_hour,viral_score_v2,measured_growth_per_hour,growth_acceleration
            FROM radar_posts
            WHERE datetime(published_at)>=datetime('now','-{v28.LOOKBACK_DAYS} days')
              AND LOWER(COALESCE(search_term,'')) IN ({placeholders})
              AND COALESCE(screening_profile,'')=?""",
        params,
    ).fetchall()
    now = datetime.now(timezone.utc)
    extended = 0

    for row in rows:
        if _is_legacy_already_covered(row, now):
            continue
        post_url = str(row["post_url"] or "")
        if not post_url:
            continue

        previous = _history_row(conn, post_url)
        measured_vph, acceleration, has_history = v24._history_velocity(row, previous, now)
        score = v28._momentum_score(
            int(row["views"] or 0),
            measured_vph,
            float(row["hours_since_publish"] or 0),
            float(row["viral_score_v2"] or 0),
            acceleration,
            has_history,
        )
        conn.execute(
            """UPDATE radar_posts
               SET viral_score_v2=?, measured_growth_per_hour=?, growth_acceleration=?
               WHERE id=?""",
            (
                score,
                round(measured_vph, 2) if has_history else 0.0,
                round(acceleration, 4) if has_history else 0.0,
                row["id"],
            ),
        )
        if _observation_due(previous, now):
            _upsert_observation(conn, row, now)
        extended += 1

    conn.commit()

    # V25 may have checkpointed the legacy rows before this extension ran. Save
    # once more after the five-tag update so a Render replacement sees the same
    # history that the current process used. This is KVS state I/O, not a paid Actor.
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

    add_radar_log(
        "V34 MOMENTUM REFRESH: five-tag/14-day DB history refreshed without a parallel history store.",
        stage="momentum",
        details={"extended_rows": extended, "profile": broad.SCREENING_PROFILE},
    )


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
        "history_backend": "radar_momentum_history_v24_v25",
        "current_profile_only": True,
        "manual_start_installed_here": False,
        "control_owner": "final_runtime_bootstrap",
    }
    add_radar_log(
        "V34 MOMENTUM READY: all 5 tags / 14 days; one DB/cloud history; current-profile ranking only.",
        stage="startup",
        details=info,
    )
    return info
