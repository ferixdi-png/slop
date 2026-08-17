import json
import os
import uuid
from datetime import datetime, timezone
from threading import Lock, Thread

from flask import Flask, jsonify, render_template, request

from cloud_state import restore_radar_snapshot_if_empty
from config import ANALYSIS_MODEL, RADAR_MODEL, RADAR_KEEP_LIMIT
from db import db_conn, init_db
from gemini_pipeline_logged import analyze_video_logged
from progress import get_radar_status, set_radar_status
from prompt_target import lock_generation_target
from radar_entry import sync_radar
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
radar_run_lock = Lock()
add_radar_log("Сервис запущен и готов принимать команды радара.", stage="startup")


def new_run_id():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:6]}"


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
    )


@app.get("/api/diagnostics")
def diagnostics():
    return jsonify(check_all_services())


@app.get("/api/radar/status")
def radar_status():
    return jsonify(get_radar_status())


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


def run_radar_background(run_id):
    context_token = set_radar_run_id(run_id)
    add_radar_log("Фоновый поток радара начал работу.", stage="background")
    try:
        with app.app_context():
            try:
                result = sync_radar()
                add_radar_log("Фоновый поиск завершён успешно.", stage="done", details=result)
            except Exception as exc:
                add_radar_log(str(exc), level="ERROR", stage="background")
                try:
                    set_radar_status(
                        "error",
                        "Поиск остановлен",
                        0,
                        None,
                        str(exc)[:300],
                        details={"run_id": run_id},
                    )
                except Exception:
                    pass
                app.logger.exception("radar background sync failed")
    finally:
        if radar_run_lock.locked():
            radar_run_lock.release()
        add_radar_log("Блокировка запуска радара освобождена.", stage="background")
        reset_radar_run_id(context_token)


@app.post("/api/radar/sync")
def radar_sync():
    run_id = new_run_id()
    context_token = set_radar_run_id(run_id)
    try:
        add_radar_log("Получен POST /api/radar/sync — пользователь нажал запуск.", stage="launch")
        if not radar_run_lock.acquire(blocking=False):
            add_radar_log("Новый запуск отклонён: поиск уже выполняется.", level="WARN", stage="launch")
            return jsonify(error="Поиск уже выполняется. Дождись завершения текущего запуска."), 409

        try:
            set_radar_status(
                "running",
                "Запускаю радар",
                1,
                360,
                "Команда принята сервером. Начинаю сбор источников.",
                details={"run_id": run_id},
            )
            worker = Thread(target=run_radar_background, args=(run_id,), name=f"radar-sync-{run_id}", daemon=True)
            worker.start()
            add_radar_log("Команда запуска принята, фоновый поток создан.", stage="launch")
            return jsonify(ok=True, started=True, run_id=run_id, message="Команда принята. Радар запущен в фоне."), 202
        except Exception as exc:
            if radar_run_lock.locked():
                radar_run_lock.release()
            add_radar_log(f"Ошибка запуска радара: {exc}", level="ERROR", stage="launch")
            app.logger.exception("radar launch failed")
            return jsonify(error=f"Не удалось запустить радар: {exc}"), 500
    finally:
        reset_radar_run_id(context_token)


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
                f"MP4 готов. Фактическая длительность {source_duration:.2f} сек.",
                stage="prompts",
            )

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
            add_radar_log(f"Ошибка промптов @{row.get('creator','')}: {exc}", level="ERROR", stage="prompts")
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
    return jsonify(ok=True, analysis_model=ANALYSIS_MODEL, radar_model=RADAR_MODEL)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
