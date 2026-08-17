import json
import os
import sys
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

from cloud_state import restore_radar_snapshot_if_empty
from config import ANALYSIS_MODEL, RADAR_MODEL, RADAR_KEEP_LIMIT
from db import db_conn, init_db
from persistent_radar import resume_if_needed, runtime_state, start_or_resume as start_or_resume_radar
from progress import get_radar_status, set_radar_status
from prompt_target import lock_generation_target
from radar_logs import add_radar_log, reset_radar_run_id, set_radar_run_id
from radar_quality import recommendation_status_for_row, top_eligible
from reel_media import download_reel_for_analysis
from service_checks import check_all_services

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
init_db()

try:
    restore_radar_snapshot_if_empty()
except Exception as exc:
    add_radar_log(f"Облачное восстановление пропущено: {exc}", level="WARN", stage="startup")


@app.get("/")
def index():
    with db_conn() as conn:
        creator_count = conn.execute("SELECT COUNT(*) FROM tracked_creators").fetchone()[0]
    return render_template("index.html", creator_count=creator_count)


@app.get("/api/status")
def status():
    with db_conn() as conn:
        radar = conn.execute("SELECT COUNT(*) FROM radar_posts WHERE ai_match=1").fetchone()[0]
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
        radar_runtime="persistent_apify_job_v4",
    )


@app.get("/api/diagnostics")
def diagnostics():
    return jsonify(check_all_services())


@app.get("/api/radar/status")
def radar_status():
    # The durable Apify KVS job is authoritative. Never rely on local SQLite to
    # decide whether a restart interrupted an active search. If this process has
    # no worker, probe KVS and resume any active job regardless of local stage.
    active = runtime_state()
    job = None
    if not (active.get("worker_active") or active.get("worker_pending")):
        try:
            job = resume_if_needed()
        except Exception as exc:
            add_radar_log(
                f"Status poll не смог проверить/восстановить persistent job: {exc}",
                level="WARN",
                stage="status",
            )

    payload = get_radar_status()
    active = runtime_state()

    details = dict(payload.get("details") or {})
    details.update(active)
    details["render_commit"] = str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12]
    details["persistent_job"] = True
    if isinstance(job, dict):
        details["persistent_phase"] = job.get("phase")
        details["persistent_run_id"] = job.get("run_id")
    payload["details"] = details

    if active.get("worker_active") or active.get("worker_pending"):
        if payload.get("stage") != "running":
            payload.update(
                stage="running",
                label="Поиск выполняется",
                progress=max(1, int(payload.get("progress") or 1)),
                eta_seconds=payload.get("eta_seconds") or 360,
                message=(
                    f"Persistent worker выполняет радар. Run ID: {active.get('run_id')}. "
                    "При рестарте Render поиск автоматически продолжится по сохранённым Apify runId."
                ),
            )
    elif payload.get("stage") == "running" and not (job and job.get("phase") in {"queued", "starting_sources", "discovering", "processing"}):
        payload.update(
            stage="error",
            label="Поиск остановлен",
            eta_seconds=None,
            message="Локальный статус был running, но активного persistent job в Apify KVS нет. Можно запустить поиск заново.",
        )
    return jsonify(payload)


@app.get("/api/radar")
def radar():
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-7 days') AND ai_match=1
               ORDER BY viral_score_v2 DESC, views_per_hour DESC, views DESC
               LIMIT 120"""
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
        if len(out) >= RADAR_KEEP_LIMIT:
            break
    return jsonify(out)


@app.get("/api/radar/candidates")
def radar_candidates():
    with db_conn() as conn:
        rows = conn.execute(
            """SELECT id,creator,post_url,preview_url,published_at,duration_sec,views,likes,comments,
                      hours_since_publish,views_per_hour,viral_score_v2,ai_checked,ai_match,reason,search_term
               FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-7 days')
               ORDER BY viral_score_v2 DESC, views_per_hour DESC, views DESC
               LIMIT 50"""
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
    data["report"] = json.loads(data.pop("report_json"))
    return jsonify(data)


@app.post("/api/radar/sync")
def radar_sync():
    # This request only creates/resumes a durable KVS job and returns 202 quickly.
    # The long-running work is independent from the browser request and is rebuilt
    # automatically after a Render process replacement.
    try:
        payload, status_code = start_or_resume_radar()
        return jsonify(payload), status_code
    except Exception as exc:
        add_radar_log(
            f"Не удалось создать/возобновить persistent radar job: {exc}",
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
                    "persistent_job": True,
                    "render_commit": str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12],
                },
            )
        except Exception:
            pass
        return jsonify(error=str(exc)), 500


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
            return jsonify(error="Этот ролик не прошёл финальный фильтр качества TOP."), 400

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
            if source_duration <= 0 or source_duration > 10.05:
                raise RuntimeError("Нет корректной длительности выбранного Reel до 10 секунд")
            add_radar_log(
                f"MP4 готов. Фактическая длительность {source_duration:.2f} сек. Загружаю Gemini runtime.",
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
                details={"analysis_id": analysis_id, "qa": result.get("reconstruction_confidence")},
            )
            return jsonify(
                id=analysis_id,
                model=ANALYSIS_MODEL,
                generation_target="gemini-omni-flash-preview",
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
        radar_runtime="persistent_apify_job_v4",
        radar_worker=runtime_state(),
        render_commit=str(os.environ.get("RENDER_GIT_COMMIT", ""))[:12],
    )


add_radar_log(
    "Сервис запущен. Radar runtime: persistent_apify_job_v4.",
    stage="startup",
    details={
        "python": sys.version.split()[0],
        "analysis_model": ANALYSIS_MODEL,
        "radar_model": RADAR_MODEL,
        "render_cpu_count": os.environ.get("RENDER_CPU_COUNT", ""),
    },
)

# Always probe the durable KVS job at process boot. This is intentionally
# independent from local SQLite status: instance replacement can lose local state.
try:
    resume_if_needed()
except Exception as exc:
    add_radar_log(
        f"Автовосстановление radar job не запустилось: {exc}",
        level="WARN",
        stage="startup",
    )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
