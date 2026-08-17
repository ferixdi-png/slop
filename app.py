import json
import os
from datetime import datetime, timezone
from threading import Lock, Thread

from flask import Flask, jsonify, render_template, request

from cloud_state import restore_radar_snapshot_if_empty
from config import ANALYSIS_MODEL, RADAR_MODEL, RADAR_KEEP_LIMIT, RADAR_SYNC_COOLDOWN_MINUTES
from db import db_conn, init_db
from gemini_service import analyze_video
from progress import get_radar_status, set_radar_status
from prompt_target import lock_generation_target
from radar_entry import sync_radar
from radar_quality import recommendation_status_for_row, top_eligible
from reel_media import download_reel_for_analysis
from service_checks import check_all_services

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
init_db()
try:
    restore_radar_snapshot_if_empty()
except Exception:
    # A cloud restore failure must never prevent the web app from starting.
    pass
radar_run_lock = Lock()


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
    # Fetch more than the visible TOP because weak-evidence items are intentionally
    # removed here rather than allowed to appear as false winners.
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
    # Candidates never disappear merely because Gemini is re-checking them.
    # Low-evidence items can stay here for transparency, but cannot enter TOP.
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


def reserve_radar_sync():
    now = datetime.now(timezone.utc)
    current = get_radar_status()
    with db_conn() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key='last_radar_sync_at'").fetchone()
        if row:
            try:
                previous = datetime.fromisoformat(row["value"].replace("Z", "+00:00"))
                if previous.tzinfo is None:
                    previous = previous.replace(tzinfo=timezone.utc)
                elapsed = (now - previous.astimezone(timezone.utc)).total_seconds() / 60
                if current.get("stage") == "running":
                    return False, max(1, int(RADAR_SYNC_COOLDOWN_MINUTES - elapsed))
                if current.get("stage") == "done" and elapsed < RADAR_SYNC_COOLDOWN_MINUTES:
                    return False, max(1, int(RADAR_SYNC_COOLDOWN_MINUTES - elapsed))
            except Exception:
                pass
        conn.execute(
            """INSERT INTO app_state(key,value) VALUES('last_radar_sync_at',?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
            (now.isoformat(),),
        )
        conn.commit()
    return True, 0


def release_radar_sync_after_error():
    with db_conn() as conn:
        conn.execute("DELETE FROM app_state WHERE key='last_radar_sync_at'")
        conn.commit()


def run_radar_background():
    try:
        with app.app_context():
            try:
                sync_radar()
            except Exception as exc:
                try:
                    release_radar_sync_after_error()
                except Exception:
                    pass
                try:
                    set_radar_status("error", "Поиск остановлен", 0, None, str(exc)[:300])
                except Exception:
                    pass
                app.logger.exception("radar background sync failed")
    finally:
        if radar_run_lock.locked():
            radar_run_lock.release()


@app.post("/api/radar/sync")
def radar_sync():
    if not radar_run_lock.acquire(blocking=False):
        return jsonify(error="Поиск уже выполняется. Дождись завершения текущего запуска."), 409

    try:
        allowed, retry_minutes = reserve_radar_sync()
        if not allowed:
            radar_run_lock.release()
            return jsonify(error=f"Повторный полный поиск будет доступен примерно через {retry_minutes} мин."), 429

        set_radar_status(
            "running",
            "Запускаю радар",
            1,
            360,
            "Поиск запущен в фоне. Старый стабильный TOP остаётся на экране, пока новый проход не проверит кандидатов.",
        )
        Thread(target=run_radar_background, name="radar-sync", daemon=True).start()
        return jsonify(ok=True, started=True, message="Радар запущен в фоне"), 202
    except Exception:
        if radar_run_lock.locked():
            radar_run_lock.release()
        raise


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
    owned = bool((request.get_json(silent=True) or {}).get("owned_or_licensed"))
    with db_conn() as conn:
        row = conn.execute("SELECT * FROM radar_posts WHERE id=?", (item_id,)).fetchone()
    if not row:
        return jsonify(error="Ролик не найден"), 404

    row = dict(row)
    if not bool(row.get("ai_match")) or not top_eligible(row):
        return jsonify(error="Этот ролик не прошёл финальный фильтр качества TOP."), 400

    tmp = None
    try:
        tmp, refreshed_duration = download_reel_for_analysis(row)
        source_duration = round(float(refreshed_duration or row.get("duration_sec") or 0), 2)
        if source_duration <= 0 or source_duration > 10.05:
            raise RuntimeError("Нет корректной длительности выбранного Reel до 10 секунд")

        package = lock_generation_target(analyze_video(tmp, owned, source_duration))
        result = package.model_dump()
        analysis_id = save_analysis(
            (f"@{row['creator']} — {row['hook'] or 'ролик из радара'}")[:160],
            row["post_url"],
            row["views"],
            row.get("viral_score_v2", 0),
            result,
        )
        return jsonify(
            id=analysis_id,
            model=ANALYSIS_MODEL,
            generation_target="gemini-omni-flash-preview",
            source_duration_sec=source_duration,
            result=result,
        )
    except Exception as exc:
        app.logger.exception("radar analysis failed")
        return jsonify(error=str(exc)), 500
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


@app.get("/health")
def health():
    return jsonify(ok=True, analysis_model=ANALYSIS_MODEL, radar_model=RADAR_MODEL)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
