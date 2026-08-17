import json
import os
from datetime import datetime, timezone
from threading import Thread

from flask import Flask, jsonify, render_template, request

from apify_recovery import recover_last_successful_hashtag_run
from config import ANALYSIS_MODEL, RADAR_MODEL, RADAR_KEEP_LIMIT, RADAR_SYNC_COOLDOWN_MINUTES
from db import db_conn, init_db
from gemini_service import analyze_video
from progress import get_radar_status, set_radar_status
from prompt_target import lock_generation_target
from radar_entry import sync_radar
from reel_media import download_reel_for_analysis
from service_checks import check_all_services

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret")
init_db()


def recommendation_status(row):
    score = float(row.get("viral_score_v2") or 0)
    anomaly = float(row.get("anomaly_multiplier") or 0)
    hours = float(row.get("hours_since_publish") or 999)
    views = int(row.get("views") or 0)
    vph = float(row.get("views_per_hour") or 0)
    usual = float(row.get("creator_usual_views") or 0)

    if score >= 85 or (score >= 78 and anomaly >= 5 and hours <= 72):
        level, label = "S", "🔥 СРОЧНО БРАТЬ В РАБОТУ"
    elif score >= 72 or (score >= 65 and anomaly >= 3) or (score >= 65 and vph >= 20000):
        level, label = "A", "🟢 СИЛЬНЫЙ КАНДИДАТ"
    elif score >= 56:
        level, label = "B", "🟡 МОЖНО ТЕСТИРОВАТЬ"
    else:
        level, label = "C", "⚪ НИЗКИЙ ПРИОРИТЕТ"

    reasons = []
    if score >= 80:
        reasons.append(f"Viral Score {score:.0f}/100")
    if anomaly >= 2 and usual > 0:
        reasons.append(f"аномалия автора ×{anomaly:.1f}")
    elif usual <= 0:
        reasons.append("база автора ещё собирается")
    if vph >= 50000:
        reasons.append(f"очень высокая скорость {round(vph):,}/ч".replace(",", " "))
    elif vph >= 10000:
        reasons.append(f"сильная скорость {round(vph):,}/ч".replace(",", " "))
    if hours <= 24:
        reasons.append("опубликован меньше суток назад")
    elif hours <= 72:
        reasons.append("свежий ролик до 72 часов")
    if views >= 100000:
        reasons.append("уже перешёл 100K просмотров")

    return {
        "priority_level": level,
        "priority_label": label,
        "priority_reason": " · ".join(reasons[:4]) or "средний сигнал без сильной аномалии",
    }


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
               LIMIT ?""",
            (RADAR_KEEP_LIMIT,),
        ).fetchall()
    out = []
    for row in rows:
        x = dict(row)
        try:
            x["characters"] = json.loads(x.pop("characters_json") or "[]")
        except Exception:
            x["characters"] = []
        x.update(recommendation_status(x))
        out.append(x)
    return jsonify(out)


@app.get("/api/radar/candidates")
def radar_candidates():
    def query_rows():
        with db_conn() as conn:
            return conn.execute(
                """SELECT id,creator,post_url,preview_url,published_at,duration_sec,views,likes,comments,
                          hours_since_publish,views_per_hour,viral_score_v2,ai_checked,ai_match,reason,search_term
                   FROM radar_posts
                   WHERE datetime(published_at)>=datetime('now','-7 days')
                   ORDER BY viral_score_v2 DESC, views_per_hour DESC, views DESC
                   LIMIT 30"""
            ).fetchall()

    rows = query_rows()
    if not rows and os.environ.get("APIFY_API_TOKEN"):
        try:
            recover_last_successful_hashtag_run()
            rows = query_rows()
        except Exception:
            pass
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
    with app.app_context():
        try:
            sync_radar()
        except Exception as exc:
            release_radar_sync_after_error()
            set_radar_status("error", "Поиск остановлен", 0, None, str(exc)[:300])
            app.logger.exception("radar background sync failed")


@app.post("/api/radar/sync")
def radar_sync():
    allowed, retry_minutes = reserve_radar_sync()
    if not allowed:
        return jsonify(error=f"Повторный полный поиск будет доступен примерно через {retry_minutes} мин."), 429

    set_radar_status(
        "running",
        "Запускаю радар",
        1,
        360,
        "Поиск запущен в фоне. Первый полный проход обычно занимает примерно 4–8 минут.",
    )
    Thread(target=run_radar_background, name="radar-sync", daemon=True).start()
    return jsonify(ok=True, started=True, message="Радар запущен в фоне"), 202


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
