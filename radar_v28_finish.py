"""Final 14-day DB/API wiring for the V28 product overlay."""

from __future__ import annotations

import json

import radar_edge_v19 as edge
import radar_quality
import radar_request_job as radar_job
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log
from radar_multiplatform_v28 import (
    KEEP_LIMIT,
    LOOKBACK_DAYS,
    MODE_VERSION,
    SCREENING_PROFILE,
    TARGET_TAGS,
    TARGET_SET,
    PLATFORM_SET,
    refresh_scores_v28,
    top_eligible_v28,
)

_APPLIED = False


def finalize_v28_base(job):
    """Equivalent to the proven base finalizer, but with the real V28 14-day window."""
    candidates = job.get("candidates") or []
    set_radar_status(
        "running",
        "Формирую V28 TOP за 14 дней",
        92,
        45,
        "Пересчитываю скорость, ускорение и мету по Instagram + TikTok + YouTube.",
        details={
            "ai_total": len(candidates),
            "ai_done": sum(1 for x in candidates if x.get("ai_done")),
            "run_id": job.get("run_id"),
            "lookback_days": LOOKBACK_DAYS,
        },
    )

    with db_conn() as conn:
        refresh_scores_v28(conn)
        rows = conn.execute(
            f"""SELECT * FROM radar_posts
                WHERE datetime(published_at)>=datetime('now','-{LOOKBACK_DAYS} days')
                  AND ai_match=1
                  AND COALESCE(screening_profile,'')=?
                ORDER BY viral_score_v2 DESC,views_per_hour DESC,views DESC
                LIMIT 500""",
            (SCREENING_PROFILE,),
        ).fetchall()
    top_rows = [dict(row) for row in rows if top_eligible_v28(dict(row))][:KEEP_LIMIT]

    meta_error = ""
    if top_rows:
        try:
            with db_conn() as conn:
                radar_quality.save_meta_report_quality(conn, top_rows)
                conn.commit()
        except Exception as exc:
            meta_error = str(exc)[:300]
            add_radar_log(f"V28 meta не собрана: {exc}", level="WARN", stage="meta")

    try:
        radar_job.save_radar_snapshot()
    except Exception as exc:
        add_radar_log(f"V28 финальный snapshot не сохранён: {exc}", level="WARN", stage="snapshot")

    done = sum(1 for item in candidates if item.get("ai_done"))
    matched = sum(1 for item in candidates if item.get("ai_done") and item.get("ai_match"))
    errors = sum(1 for item in candidates if item.get("ai_error"))
    stats = job.get("stats") or {}
    result = {
        "raw": stats.get("raw", 0),
        "after_numeric_filter": stats.get("numeric_candidates", 0),
        "ai_checked": done,
        "matched": matched,
        "errors": errors,
        "source_errors": len(job.get("source_failures") or {}),
        "static_rejected": stats.get("static_rejected", 0),
        "no_dialogue_rejected": stats.get("no_dialogue_rejected", 0),
        "kept": len(top_rows),
        "lookback_days": LOOKBACK_DAYS,
        "meta_error": meta_error,
    }
    job["phase"] = "done"
    job["completed_at"] = radar_job._now_iso()
    job["result"] = result
    job["error"] = ""
    job["current_ai_index"] = None
    job["current_ai_post_url"] = ""
    radar_job._persist(job)

    set_radar_status(
        "done",
        "Поиск завершён",
        100,
        0,
        f"Собрано {result['raw']} → кандидатов {result['after_numeric_filter']} → Gemini проверил речь {done} → в TOP {len(top_rows)}.",
        warning=(f"Мета: {meta_error}" if meta_error else ""),
        details=result,
    )
    add_radar_log("V28 MULTIPLATFORM SPEECH DONE.", stage="done", details=result)
    return job


def _install_api_views(app_module):
    app = getattr(app_module, "app", None)
    if app is None or getattr(app, "_v28_api_views", False):
        return
    from flask import jsonify, request

    @app.before_request
    def v28_api_views():
        if request.method != "GET":
            return None

        if request.path == "/api/radar":
            with db_conn() as conn:
                rows = conn.execute(
                    f"""SELECT * FROM radar_posts
                        WHERE datetime(published_at)>=datetime('now','-{LOOKBACK_DAYS} days')
                          AND ai_match=1
                          AND COALESCE(screening_profile,'')=?
                        ORDER BY viral_score_v2 DESC,views_per_hour DESC,views DESC
                        LIMIT 500""",
                    (SCREENING_PROFILE,),
                ).fetchall()
            out = []
            for row in rows:
                x = dict(row)
                if not top_eligible_v28(x):
                    continue
                try:
                    x["characters"] = json.loads(x.get("characters_json") or "[]")
                except Exception:
                    x["characters"] = []
                x.update(radar_quality.recommendation_status_for_row(x))
                out.append(x)
                if len(out) >= KEEP_LIMIT:
                    break
            return jsonify(out)

        if request.path == "/api/radar/candidates":
            placeholders = ",".join("?" for _ in TARGET_TAGS)
            with db_conn() as conn:
                rows = conn.execute(
                    f"""SELECT id,platform,creator,post_url,preview_url,published_at,duration_sec,
                               views,likes,comments,hours_since_publish,views_per_hour,followers_count,
                               creator_usual_views,viral_score_v2,search_term,ai_checked,ai_match,
                               screening_profile,reason
                        FROM radar_posts
                        WHERE datetime(published_at)>=datetime('now','-{LOOKBACK_DAYS} days')
                          AND LOWER(COALESCE(search_term,'')) IN ({placeholders})
                        ORDER BY viral_score_v2 DESC,views_per_hour DESC,views DESC
                        LIMIT 500""",
                    TARGET_TAGS,
                ).fetchall()
            out = []
            for row in rows:
                x = dict(row)
                if str(x.get("platform") or "") not in PLATFORM_SET:
                    continue
                if str(x.get("search_term") or "").strip().lower() not in TARGET_SET:
                    continue
                out.append(x)
            return jsonify(out)
        return None

    app._v28_api_views = True


def apply_v28_finish():
    global _APPLIED
    if _APPLIED:
        return {"v28_14day_api": True}
    _APPLIED = True

    # Preserve edge finalization wrapper (stale-cache cleanup) but replace the
    # deepest seven-day finalizer it calls.
    edge._BASE_FINALIZE = finalize_v28_base

    app_module = __import__("sys").modules.get("app")
    if app_module is not None:
        _install_api_views(app_module)

    add_radar_log(
        "V28 DB/API READY: TOP, candidates and finalizer all use the real 14-day window.",
        stage="startup",
        details={"mode": MODE_VERSION, "lookback_days": LOOKBACK_DAYS},
    )
    return {"v28_14day_api": True, "lookback_days": LOOKBACK_DAYS}
