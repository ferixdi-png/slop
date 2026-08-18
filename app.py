import json
import os
import sys
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from cloud_state import cloud_state_diagnostics
from config import ANALYSIS_MODEL, RADAR_MODEL, RADAR_MIN_DURATION_SEC, RADAR_MAX_DURATION_SEC
from db import db_conn, init_db
from progress import get_radar_status, set_radar_status
from prompt_target import lock_generation_target
from radar_logs import add_radar_log, reset_radar_run_id, set_radar_run_id
from radar_quality import recommendation_status_for_row, top_eligible
from radar_source_compat import apply_source_alias_compat

apply_source_alias_compat()

from radar_request_job import (
    RUNTIME as RADAR_RUNTIME,
    create_or_resume_job,
    public_job,
    tick_job,
)
from reel_media import download_reel_for_analysis
from service_checks import check_all_services

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
init_db()

from radar_growth_v6 import apply_growth_overrides, top_eligible_v6
apply_growth_overrides()
top_eligible = top_eligible_v6

from radar_budget_v10 import (
    KEEP_LIMIT,
    apply_budget_overrides,
    budget_breakdown,
    wrap_tick_job,
)
BUDGET_INFO = apply_budget_overrides()

from radar_highfreq_v12 import apply_highfreq_overrides
BUDGET_INFO = apply_highfreq_overrides()

# Dialogue-first target: ordinary real Reels and generated Reels are treated the
# same. The useful thing is the short funny spoken mechanic, not its origin.
from radar_dialogue_v14 import (
    PROFILE_VERSION,
    apply_dialogue_first_overrides,
    top_eligible_dialogue,
)
BUDGET_INFO = apply_dialogue_first_overrides()
top_eligible = top_eligible_dialogue

# Production target: generate only the underlying camera footage. Editorial
# photos/screenshots/PIP layers are reconstructed separately as a CapCut plan.
from overlay_cleanplate_v15 import (
    PRODUCTION_PROFILE_VERSION,
    apply_overlay_cleanplate_overrides,
)
PRODUCTION_INFO = apply_overlay_cleanplate_overrides()

# Compact semantic screening + static gate + broad dialogue discovery.
from radar_resilient_v17 import apply_resilient_v17_overrides
BUDGET_INFO = apply_resilient_v17_overrides()

# Explicit user stop. V19 upgrades this to a durable out-of-band marker so the
# endpoint never waits minutes behind an in-flight Gemini/Apify request.
from radar_cancel_v18 import cancel_active_job

# Preserve the monthly-quota budget wrapper first, then put the final v19 safety
# layer around that already-stable request-driven state machine.
tick_job = wrap_tick_job(tick_job)
from radar_hardening_v19 import apply_hardening_v19
BUDGET_INFO = apply_hardening_v19()


@app.get("/")
def index():
    with db_conn() as conn:
        creator_count = conn.execute("SELECT COUNT(*) FROM tracked_creators").fetchone()[0]
    return render_template("index.html", creator_count=creator_count)


@app.get("/api/status")
def status():
    with db_conn() as conn:
        radar = conn.execute(
            """SELECT COUNT(*) FROM radar_posts
               WHERE ai_match=1 AND duration_sec>=? AND duration_sec<=?""",
            (RADAR_MIN_DURATION_SEC, RADAR_MAX_DURATION_SEC),
        ).fetchone()[0]
        creators = conn.execute("SELECT COUNT(*) FROM tracked_creators").fetchone()[0]
    return jsonify(
        gemini_configured=bool(os.environ.get("GEMINI_API_KEY")),
        apify_configured=bool(os.environ.get("APIFY_API_TOKEN")),
        analysis_model=ANALYSIS_MODEL,
        radar_model=RADAR_MODEL,
        radar_matches=radar,
        tracked_creators=creators,
        render_commit=str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12],
        render_instance=os.environ.get("RENDER_INSTANCE_ID", ""),
        render_cpu_count=os.environ.get("RENDER_CPU_COUNT", ""),
        radar_runtime=RADAR_RUNTIME,
        radar_profile=PROFILE_VERSION,
        production_profile=PRODUCTION_PROFILE_VERSION,
        radar_keep_limit=KEEP_LIMIT,
        radar_duration_min=RADAR_MIN_DURATION_SEC,
        radar_duration_max=RADAR_MAX_DURATION_SEC,
        radar_budget=BUDGET_INFO,
        cloud_state=cloud_state_diagnostics(),
    )


@app.get("/api/diagnostics")
def diagnostics():
    payload = check_all_services()
    if isinstance(payload, dict):
        payload["cloud_state"] = cloud_state_diagnostics()
        payload["radar_profile"] = PROFILE_VERSION
    return jsonify(payload)


@app.get("/api/radar/status")
def radar_status():
    payload = get_radar_status()
    details = dict(payload.get("details") or {})
    details.update(
        runtime=RADAR_RUNTIME,
        radar_profile=PROFILE_VERSION,
        production_profile=PRODUCTION_PROFILE_VERSION,
        radar_keep_limit=KEEP_LIMIT,
        radar_duration_min=RADAR_MIN_DURATION_SEC,
        radar_duration_max=RADAR_MAX_DURATION_SEC,
        radar_budget=budget_breakdown(),
        cloud_state=cloud_state_diagnostics(),
        render_commit=str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12],
        render_instance=os.environ.get("RENDER_INSTANCE_ID", ""),
        server_pid=os.getpid(),
    )
    payload["details"] = details
    return jsonify(payload)


@app.get("/api/radar/job")
def radar_job_truth():
    """Read-only durable job truth. Opening/reloading a page must not advance work."""
    return jsonify(public_job())


@app.get("/api/radar")
def radar():
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-7 days')
                 AND ai_match=1
                 AND duration_sec>=? AND duration_sec<=?
               ORDER BY viral_score_v2 DESC, views_per_hour DESC, views DESC
               LIMIT 300""",
            (RADAR_MIN_DURATION_SEC, RADAR_MAX_DURATION_SEC),
        ).fetchall()

    out = []
    for row in rows:
        x = dict(row)
        if not top_eligible(x):
            continue
        try:
            x["characters"] = json.loads(x.pop("characters_json") or "[]")
        except Exception:
            x["characters"] = []
        x.update(recommendation_status_for_row(x))
        out.append(x)
        if len(out) >= KEEP_LIMIT:
            break
    return jsonify(out)


@app.get("/api/radar/candidates")
def radar_candidates():
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT id,creator,post_url,preview_url,published_at,duration_sec,views,likes,comments,
                      hours_since_publish,views_per_hour,viral_score_v2,ai_checked,ai_match,reason,search_term,
                      screening_profile
               FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-7 days')
                 AND (duration_sec IS NULL OR duration_sec=0 OR (duration_sec>=? AND duration_sec<=?))
               ORDER BY ai_match DESC, viral_score_v2 DESC, views_per_hour DESC, views DESC
               LIMIT 500""",
            (RADAR_MIN_DURATION_SEC, RADAR_MAX_DURATION_SEC),
        ).fetchall()
    return jsonify([dict(x) for x in rows])


@app.get("/api/radar/meta")
def radar_meta():
    with db_conn() as conn:
        row = conn.execute(
            "SELECT created_at,source_count,average_duration_sec,report_json "
            "FROM radar_meta ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if not row:
        return jsonify(None)
    data = dict(row)
    raw_report = data.pop("report_json")
    try:
        data["report"] = json.loads(raw_report)
        data["report_parse_error"] = False
    except Exception as exc:
        # A single corrupted old meta row must never break the entire dashboard.
        data["report"] = {}
        data["report_parse_error"] = True
        data["warning"] = f"Старая meta-запись повреждена: {str(exc)[:180]}"
        add_radar_log(data["warning"], level="WARN", stage="meta")
    return jsonify(data)


@app.post("/api/radar/sync")
def radar_sync():
    try:
        payload, status_code = create_or_resume_job()
        return jsonify(payload), status_code
    except Exception as exc:
        add_radar_log(
            f"Не удалось создать/возобновить durable radar job: {exc}",
            level="ERROR",
            stage="launch",
        )
        try:
            set_radar_status(
                "error",
                "Не удалось запустить поиск",
                0,
                None,
                str(exc)[:300],
                details={
                    "runtime": RADAR_RUNTIME,
                    "radar_profile": PROFILE_VERSION,
                    "production_profile": PRODUCTION_PROFILE_VERSION,
                    "radar_budget": budget_breakdown(),
                    "render_commit": str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12],
                },
            )
        except Exception:
            pass
        return jsonify(error=str(exc)), 500


@app.post("/api/radar/tick")
def radar_tick():
    payload, status_code = tick_job()
    return jsonify(payload), status_code


@app.post("/api/radar/stop")
def radar_stop():
    try:
        payload, status_code = cancel_active_job()
        return jsonify(payload), status_code
    except Exception as exc:
        add_radar_log(
            f"FORCE STOP ERROR: {exc}",
            level="ERROR",
            stage="stop",
        )
        return jsonify(error=f"Не удалось остановить поиск: {exc}"), 500


def save_analysis(title, source_url, views, viral_score, result):
    with db_conn() as conn:
        cur = conn.execute(
            "INSERT INTO analyses(created_at,title,source_url,views,viral_score,model,result_json) VALUES(?,?,?,?,?,?,?)",
            (
                datetime.now(timezone.utc).isoformat(),
                title,
                source_url,
                views,
                viral_score,
                ANALYSIS_MODEL,
                json.dumps(result, ensure_ascii=False),
            ),
        )
        conn.commit()
        return cur.lastrowid


@app.post("/api/radar/<int:item_id>/analyze")
def radar_analyze(item_id):
    analysis_run_id = f"prompt-{item_id}-{uuid.uuid4().hex[:6]}"
    context_token = set_radar_run_id(analysis_run_id)
    try:
        owned = bool((request.get_json(silent=True) or {}).get("owned_or_licensed"))
        with db_conn() as conn:
            row = conn.execute("SELECT * FROM radar_posts WHERE id=?", (item_id,)).fetchone()
        if not row:
            return jsonify(error="Ролик не найден"), 404

        row = dict(row)
        if not bool(row.get("ai_match")) or not top_eligible(row):
            return jsonify(error="Этот ролик не прошёл финальный фильтр короткой повторяемой сценки."), 400

        tmp = None
        add_radar_log(
            f"Запущен детальный анализ Reel #{item_id} @{row.get('creator','')}",
            stage="prompts",
            details={"views": row.get("views"), "duration_sec": row.get("duration_sec")},
        )
        try:
            add_radar_log("Скачиваю исходный MP4 для production-анализа.", stage="prompts")
            tmp, refreshed_duration = download_reel_for_analysis(row)
            source_duration = round(float(refreshed_duration or row.get("duration_sec") or 0), 2)
            if source_duration < RADAR_MIN_DURATION_SEC or source_duration > RADAR_MAX_DURATION_SEC:
                raise RuntimeError(
                    f"Фактическая длительность Reel {source_duration:.2f} сек; нужен диапазон "
                    f"{RADAR_MIN_DURATION_SEC:.1f}–{RADAR_MAX_DURATION_SEC:.2f} сек"
                )
            add_radar_log(
                f"MP4 готов. Фактическая длительность {source_duration:.2f} сек. Загружаю production runtime.",
                stage="prompts",
            )

            from gemini_pipeline_logged import analyze_video_logged

            package = lock_generation_target(analyze_video_logged(tmp, owned, source_duration))
            result = package.model_dump()
            analysis_id = save_analysis(
                (f"@{row['creator']} — {row['hook'] or 'ролик из радара'}")[:160],
                row["post_url"],
                row["views"],
                row.get("viral_score_v2", 0),
                result,
            )
            add_radar_log(
                f"Ультра-промпты для @{row.get('creator','')} готовы.",
                stage="prompts",
                details={
                    "analysis_id": analysis_id,
                    "qa": result.get("reconstruction_confidence"),
                    "production_profile": PRODUCTION_PROFILE_VERSION,
                    "capcut_overlays": len((result.get("capcut_overlay_plan") or {}).get("steps") or []),
                },
            )
            return jsonify(
                id=analysis_id,
                model=ANALYSIS_MODEL,
                generation_target="gemini-omni-flash-preview",
                production_profile=PRODUCTION_PROFILE_VERSION,
                source_duration_sec=source_duration,
                result=result,
            )
        except Exception as exc:
            add_radar_log(
                f"Ошибка промптов @{row.get('creator','')}: {exc}",
                level="ERROR",
                stage="prompts",
            )
            app.logger.exception("radar analysis failed")
            return jsonify(error=str(exc)), 500
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
    finally:
        reset_radar_run_id(context_token)


@app.get("/health")
def health():
    return jsonify(
        ok=True,
        analysis_model=ANALYSIS_MODEL,
        radar_model=RADAR_MODEL,
        radar_runtime=RADAR_RUNTIME,
        radar_profile=PROFILE_VERSION,
        production_profile=PRODUCTION_PROFILE_VERSION,
        radar_keep_limit=KEEP_LIMIT,
        radar_budget=BUDGET_INFO,
        cloud_state=cloud_state_diagnostics(),
        server_pid=os.getpid(),
        render_commit=str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12],
    )


add_radar_log(
    f"Сервис запущен. Radar runtime: {RADAR_RUNTIME}. Profile: {PROFILE_VERSION}. Production: {PRODUCTION_PROFILE_VERSION}. Startup без внешних API-вызовов.",
    stage="startup",
    details={
        "python": sys.version.split()[0],
        "analysis_model": ANALYSIS_MODEL,
        "radar_model": RADAR_MODEL,
        "radar_profile": PROFILE_VERSION,
        "production_profile": PRODUCTION_PROFILE_VERSION,
        "radar_keep_limit": KEEP_LIMIT,
        "radar_duration_min": RADAR_MIN_DURATION_SEC,
        "radar_duration_max": RADAR_MAX_DURATION_SEC,
        "radar_budget": BUDGET_INFO,
        "cloud_state": cloud_state_diagnostics(),
        "render_cpu_count": os.environ.get("RENDER_CPU_COUNT", ""),
    },
)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
