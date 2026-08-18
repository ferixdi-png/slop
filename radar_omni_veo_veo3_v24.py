"""V24: exact #omni + #veo + #veo3 scope and cross-run momentum.

Visible radar output is fresh per run, while a tiny internal observation table
survives resets. On later runs this lets ranking use actual view growth between
observations instead of only lifetime average views/hour.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timezone

import radar_budget_v10 as budget
import radar_growth_v6 as growth
import radar_omni_veo_v21 as v21
import radar_omni_veo_v22 as v22
import radar_quality
import radar_request_job as radar_job
import radar_resilient_v17 as v17
import radar_scale_v16 as scale
import radar_service
from config import RADAR_MAX_DURATION_SEC, RADAR_MIN_DURATION_SEC
from db import db_conn
from radar_logs import add_radar_log

MODE_VERSION = "omni_veo_veo3_v24_acceleration"
HASHTAGS = ("omni", "veo", "veo3")
TARGET_TERMS = frozenset(HASHTAGS)
PASS_PREFIX = "PASS_OMNI_VEO_VEO3_TAG:"
MIN_HISTORY_HOURS = 10.0 / 60.0

_APPLIED = False
_ORIGINAL_REFRESH = v21._ORIGINAL_REFRESH_SCORES


def _clamp01(value):
    return max(0.0, min(1.0, float(value or 0)))


def _parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _ensure_momentum_schema():
    with db_conn() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(radar_posts)").fetchall()}
        if "measured_growth_per_hour" not in cols:
            conn.execute("ALTER TABLE radar_posts ADD COLUMN measured_growth_per_hour REAL DEFAULT 0")
        if "growth_acceleration" not in cols:
            conn.execute("ALTER TABLE radar_posts ADD COLUMN growth_acceleration REAL DEFAULT 0")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS radar_momentum_history (
                post_url TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                views INTEGER DEFAULT 0,
                average_views_per_hour REAL DEFAULT 0,
                search_term TEXT DEFAULT ''
            )"""
        )
        conn.commit()


def _history_velocity(row, previous, now):
    average_vph = max(0.0, float(row["views_per_hour"] or 0))
    if not previous:
        return average_vph, 0.0, False

    observed = _parse_time(previous["observed_at"])
    if not observed:
        return average_vph, 0.0, False
    elapsed_hours = max(0.0, (now - observed).total_seconds() / 3600.0)
    if elapsed_hours < MIN_HISTORY_HOURS:
        return average_vph, 0.0, False

    current_views = max(0, int(row["views"] or 0))
    previous_views = max(0, int(previous["views"] or 0))
    if current_views < previous_views:
        return average_vph, 0.0, False

    measured = max(0.0, (current_views - previous_views) / max(elapsed_hours, 1e-6))
    previous_average = max(1.0, float(previous["average_views_per_hour"] or 0))
    acceleration = measured / previous_average
    return measured, acceleration, True


def _v24_score(row, measured_vph, acceleration, has_history):
    views = max(0, int(row["views"] or 0))
    hours = max(0.0, float(row["hours_since_publish"] or 0))
    base_score = max(0.0, float(row["viral_score_v2"] or 0))

    velocity = _clamp01(math.log1p(max(0.0, measured_vph)) / math.log1p(100_000))
    freshness = _clamp01(1.0 - hours / (24.0 * 7.0))
    proof = _clamp01(math.log1p(views) / math.log1p(1_000_000))
    base = _clamp01(base_score / 100.0)

    if has_history:
        acceleration_signal = _clamp01(math.log1p(max(0.0, acceleration)) / math.log1p(10.0))
        score = 100.0 * (
            0.58 * velocity
            + 0.18 * acceleration_signal
            + 0.14 * freshness
            + 0.08 * proof
            + 0.02 * base
        )
    else:
        score = 100.0 * (0.72 * velocity + 0.16 * freshness + 0.09 * proof + 0.03 * base)
    return round(_clamp01(score / 100.0) * 100.0, 1)


def refresh_momentum_scores_v24(conn):
    """Quality metrics + actual cross-run growth when an older observation exists."""
    _ORIGINAL_REFRESH(conn)
    placeholders = ",".join("?" for _ in HASHTAGS)
    rows = conn.execute(
        f"""SELECT id,post_url,search_term,views,hours_since_publish,views_per_hour,viral_score_v2
            FROM radar_posts
            WHERE datetime(published_at)>=datetime('now','-7 days')
              AND LOWER(COALESCE(search_term,'')) IN ({placeholders})""",
        HASHTAGS,
    ).fetchall()
    now = datetime.now(timezone.utc)

    for row in rows:
        previous = conn.execute(
            "SELECT observed_at,views,average_views_per_hour FROM radar_momentum_history WHERE post_url=?",
            (row["post_url"],),
        ).fetchone()
        measured_vph, acceleration, has_history = _history_velocity(row, previous, now)
        score = _v24_score(row, measured_vph, acceleration, has_history)
        conn.execute(
            """UPDATE radar_posts
               SET viral_score_v2=?, measured_growth_per_hour=?, growth_acceleration=?
               WHERE id=?""",
            (score, measured_vph if has_history else 0.0, acceleration if has_history else 0.0, row["id"]),
        )

        # Do not overwrite a useful prior observation with another sample from
        # the same just-finished run. A >=10 minute gap becomes the next baseline.
        observed = _parse_time(previous["observed_at"]) if previous else None
        elapsed_hours = (now - observed).total_seconds() / 3600.0 if observed else None
        if previous is None or (elapsed_hours is not None and elapsed_hours >= MIN_HISTORY_HOURS):
            conn.execute(
                """INSERT INTO radar_momentum_history(post_url,observed_at,views,average_views_per_hour,search_term)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(post_url) DO UPDATE SET
                     observed_at=excluded.observed_at,
                     views=excluded.views,
                     average_views_per_hour=excluded.average_views_per_hour,
                     search_term=excluded.search_term""",
                (
                    row["post_url"],
                    now.isoformat(),
                    int(row["views"] or 0),
                    float(row["views_per_hour"] or 0),
                    str(row["search_term"] or "").lower(),
                ),
            )
    conn.commit()


def top_eligible_v24(row):
    duration = float((row or {}).get("duration_sec") or 0)
    term = str((row or {}).get("search_term") or "").strip().lower()
    reason = str((row or {}).get("reason") or "")
    return bool(
        RADAR_MIN_DURATION_SEC <= duration <= RADAR_MAX_DURATION_SEC
        and term in TARGET_TERMS
        and reason.startswith(PASS_PREFIX)
    )


def _repair_current_v24_passes():
    """V22 runs first on restart; restore only rows proven by the stricter V24 marker."""
    with db_conn() as conn:
        placeholders = ",".join("?" for _ in HASHTAGS)
        params = (*HASHTAGS, f"{PASS_PREFIX}%")
        cur = conn.execute(
            f"""UPDATE radar_posts
                SET ai_checked=1, ai_match=1
                WHERE datetime(published_at)>=datetime('now','-7 days')
                  AND LOWER(COALESCE(search_term,'')) IN ({placeholders})
                  AND COALESCE(reason,'') LIKE ?""",
            params,
        )
        repaired = int(cur.rowcount or 0)
        conn.commit()
    return repaired


def _invalidate_non_v24_passes():
    with db_conn() as conn:
        placeholders = ",".join("?" for _ in HASHTAGS)
        params = (*HASHTAGS, f"{PASS_PREFIX}%")
        cur = conn.execute(
            f"""UPDATE radar_posts
                SET ai_checked=0, ai_match=0
                WHERE datetime(published_at)>=datetime('now','-7 days')
                  AND ai_match=1
                  AND (
                    LOWER(COALESCE(search_term,'')) NOT IN ({placeholders})
                    OR COALESCE(reason,'') NOT LIKE ?
                  )""",
            params,
        )
        changed = int(cur.rowcount or 0)
        conn.commit()
    return changed


def _install_response_guard(app_module):
    app = getattr(app_module, "app", None)
    if app is None:
        return

    # Remove V21's two-tag response filter; otherwise it would drop veo3 rows.
    funcs = list((app.after_request_funcs or {}).get(None, []))
    app.after_request_funcs[None] = [
        fn for fn in funcs if getattr(fn, "__name__", "") != "omni_veo_v21_response_guard"
    ]
    if getattr(app, "_omni_veo_veo3_v24_response_guard", False):
        return

    from flask import request

    @app.after_request
    def omni_veo_veo3_v24_response_guard(response):
        if not response.is_json:
            return response
        data = response.get_json(silent=True)
        changed = False

        if request.path == "/api/radar/candidates" and isinstance(data, list):
            data = [
                item for item in data
                if str((item or {}).get("search_term") or "").strip().lower() in TARGET_TERMS
            ]
            changed = True
        elif request.path in {"/api/status", "/api/radar/status", "/health"} and isinstance(data, dict):
            scope = {
                "radar_hashtags": list(HASHTAGS),
                "radar_hashtag_limit_each": v21.HASHTAG_LIMIT,
                "radar_max_raw_requested": v21.HASHTAG_LIMIT * len(HASHTAGS),
                "radar_ranking_mode": "cross_run_acceleration_then_views_per_hour",
                "radar_mode": MODE_VERSION,
            }
            if request.path == "/api/radar/status":
                details = dict(data.get("details") or {})
                details.update(scope)
                data["details"] = details
            else:
                data.update(scope)
            changed = True

        if changed:
            response.set_data(app.json.dumps(data))
            response.mimetype = "application/json"
        return response

    app._omni_veo_veo3_v24_response_guard = True


def apply_omni_veo_veo3_v24():
    global _APPLIED
    if _APPLIED:
        return {
            "mode": MODE_VERSION,
            "hashtags": list(HASHTAGS),
            "max_raw_requested": v21.HASHTAG_LIMIT * len(HASHTAGS),
        }
    _APPLIED = True

    _ensure_momentum_schema()

    # Every V21 source helper reads its module-global HASHTAGS dynamically.
    v21.HASHTAGS = HASHTAGS
    v21.PROFILE_VERSION = v22.PROFILE_VERSION
    v22.MODE_VERSION = MODE_VERSION
    v22.PASS_PREFIX = PASS_PREFIX

    budget.HASHTAGS = list(HASHTAGS)
    budget.ACTOR_CAPS_USD = {v21._source_name(tag): v21.SOURCE_CAP_USD for tag in HASHTAGS}
    growth.HASHTAGS_V7 = list(HASHTAGS)

    # Keep the existing production-safe processing limits; increase discovery
    # breadth only. 3 x 250 = 750 raw, at most 420 MP4 validations, TOP <=180.
    growth.TARGET_MATCHES = v22.TARGET_MATCHES
    scale.TARGET_MATCHES = v22.TARGET_MATCHES
    v17.TARGET_MATCHES = v22.TARGET_MATCHES
    radar_job.RADAR_AI_ANALYZE_LIMIT = v22.AI_ANALYZE_LIMIT
    radar_job.RADAR_KEEP_LIMIT = v22.KEEP_LIMIT
    radar_service.RADAR_KEEP_LIMIT = v22.KEEP_LIMIT

    radar_quality.refresh_recent_scores_quality = refresh_momentum_scores_v24
    radar_job.top_eligible = top_eligible_v24
    radar_quality.top_eligible = top_eligible_v24

    # Existing function objects reference v22.PASS_PREFIX dynamically, so the
    # stricter V24 marker automatically applies to future MP4 PASS results.
    radar_job.matches = v22.matches_omni_veo
    radar_service.matches = v22.matches_omni_veo

    repaired = _repair_current_v24_passes()
    invalidated = _invalidate_non_v24_passes()
    info = budget._assert_budget()

    app_module = sys.modules.get("app")
    if app_module is not None:
        app_module.top_eligible = top_eligible_v24
        app_module.KEEP_LIMIT = v22.KEEP_LIMIT
        app_module.BUDGET_INFO = info
        app_module.OMNI_VEO_MODE_VERSION = MODE_VERSION
        _install_response_guard(app_module)

    add_radar_log(
        "V24 READY: #omni + #veo + #veo3, fresh visible runs, cross-run acceleration ranking.",
        stage="startup",
        details={
            "mode": MODE_VERSION,
            "hashtags": list(HASHTAGS),
            "hashtag_limit_each": v21.HASHTAG_LIMIT,
            "max_raw_requested": v21.HASHTAG_LIMIT * len(HASHTAGS),
            "analyze_limit": v22.AI_ANALYZE_LIMIT,
            "keep_limit": v22.KEEP_LIMIT,
            "ranking": "actual cross-run growth when available; fallback views/hour",
            "repaired_v24_passes": repaired,
            "invalidated_non_v24_passes": invalidated,
            **info,
        },
    )
    return {
        "mode": MODE_VERSION,
        "hashtags": list(HASHTAGS),
        "hashtag_limit_each": v21.HASHTAG_LIMIT,
        "max_raw_requested": v21.HASHTAG_LIMIT * len(HASHTAGS),
        "analyze_limit": v22.AI_ANALYZE_LIMIT,
        "keep_limit": v22.KEEP_LIMIT,
        "ranking": "cross_run_acceleration_then_views_per_hour",
        "repaired_v24_passes": repaired,
        "invalidated_non_v24_passes": invalidated,
        "budget": info,
    }
