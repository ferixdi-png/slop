import json, os, tempfile
from datetime import datetime, timezone
from pathlib import Path
from flask import Flask, jsonify, render_template, request
from config import ANALYSIS_MODEL, RADAR_MODEL, RADAR_KEEP_LIMIT, MAX_UPLOAD_MB, AFFILIATE_NAME, AFFILIATE_URL
from db import db_conn, init_db
from gemini_service import analyze_video
from radar_service import download_temp_video, sync_radar

app=Flask(__name__)
app.secret_key=os.environ.get("SECRET_KEY","dev-secret")
app.config["MAX_CONTENT_LENGTH"]=MAX_UPLOAD_MB*1024*1024
init_db()

@app.get("/")
def index():
    with db_conn() as conn:
        rows=conn.execute("SELECT id,created_at,title,source_url,views,viral_score,model,result_json FROM analyses ORDER BY id DESC LIMIT 30").fetchall()
        creator_count=conn.execute("SELECT COUNT(*) FROM tracked_creators").fetchone()[0]
    items=[]
    for row in rows:
        data=dict(row)
        try:
            result=json.loads(data.pop("result_json")); data["duration_sec"]=result.get("source_duration_sec",0); data["confidence"]=result.get("reconstruction_confidence",0)
        except Exception: pass
        items.append(data)
    return render_template("index.html",items=items,creator_count=creator_count)

@app.get("/api/status")
def status():
    with db_conn() as conn:
        analyses=conn.execute("SELECT COUNT(*) FROM analyses").fetchone()[0]
        radar=conn.execute("SELECT COUNT(*) FROM radar_posts WHERE ai_match=1").fetchone()[0]
        creators=conn.execute("SELECT COUNT(*) FROM tracked_creators").fetchone()[0]
    return jsonify(gemini_configured=bool(os.environ.get("GEMINI_API_KEY")),apify_configured=bool(os.environ.get("APIFY_API_TOKEN")),analysis_model=ANALYSIS_MODEL,radar_model=RADAR_MODEL,analyses=analyses,radar_matches=radar,tracked_creators=creators,affiliate_configured=bool(AFFILIATE_NAME or AFFILIATE_URL))

@app.get("/api/analysis/<int:item_id>")
def get_analysis(item_id):
    with db_conn() as conn: row=conn.execute("SELECT * FROM analyses WHERE id=?",(item_id,)).fetchone()
    if not row: return jsonify(error="Разбор не найден"),404
    data=dict(row); data["result"]=json.loads(data.pop("result_json")); return jsonify(data)

def save_analysis(title,source_url,views,viral_score,result):
    with db_conn() as conn:
        cur=conn.execute("INSERT INTO analyses(created_at,title,source_url,views,viral_score,model,result_json) VALUES(?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(),title,source_url,views,viral_score,ANALYSIS_MODEL,json.dumps(result,ensure_ascii=False)))
        conn.commit(); return cur.lastrowid

@app.post("/api/analyze")
def api_analyze():
    video=request.files.get("video")
    if not video or not video.filename: return jsonify(error="Выберите видео"),400
    suffix=Path(video.filename).suffix.lower() or ".mp4"
    if suffix not in {".mp4",".mov",".webm",".mpeg",".mpg",".avi"}: return jsonify(error="Поддерживаются MP4 MOV WEBM MPEG MPG AVI"),400
    title=(request.form.get("title") or Path(video.filename).stem).strip()[:160]
    source_url=(request.form.get("source_url") or "").strip()[:1000]
    owned=request.form.get("owned_or_licensed")=="true"
    try: views=max(0,int(request.form.get("views") or 0))
    except ValueError: return jsonify(error="Просмотры должны быть числом"),400
    tmp=None
    try:
        with tempfile.NamedTemporaryFile(delete=False,suffix=suffix) as f: video.save(f); tmp=f.name
        package=analyze_video(tmp,owned); result=package.model_dump(); item_id=save_analysis(title,source_url,views,0,result)
        return jsonify(id=item_id,result=result)
    except Exception as exc:
        app.logger.exception("analysis failed"); return jsonify(error=str(exc)),500
    finally:
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass

@app.get("/api/radar")
def radar():
    with db_conn() as conn:
        rows=conn.execute("SELECT * FROM radar_posts WHERE datetime(published_at)>=datetime('now','-7 days') AND ai_match=1 ORDER BY views_per_hour DESC,views DESC LIMIT ?",(RADAR_KEEP_LIMIT,)).fetchall()
    out=[]
    for row in rows:
        x=dict(row)
        try: x["characters"]=json.loads(x.pop("characters_json") or "[]")
        except Exception: x["characters"]=[]
        out.append(x)
    return jsonify(out)

@app.post("/api/radar/sync")
def radar_sync():
    expected=os.environ.get("RADAR_SYNC_KEY",""); supplied=request.headers.get("X-Radar-Key","")
    if expected and supplied!=expected: return jsonify(error="Неверный ключ синхронизации"),401
    try: return jsonify(ok=True,**sync_radar())
    except Exception as exc: app.logger.exception("radar sync failed"); return jsonify(error=str(exc)),500

@app.post("/api/radar/<int:item_id>/analyze")
def radar_analyze(item_id):
    owned=bool((request.get_json(silent=True) or {}).get("owned_or_licensed"))
    with db_conn() as conn: row=conn.execute("SELECT * FROM radar_posts WHERE id=?",(item_id,)).fetchone()
    if not row: return jsonify(error="Ролик не найден"),404
    row=dict(row)
    if not row.get("video_url"): return jsonify(error="У результата нет прямого видеофайла. Скачайте Reel и загрузите вручную."),400
    tmp=None
    try:
        tmp=download_temp_video(row["video_url"]); package=analyze_video(tmp,owned); result=package.model_dump()
        item_id=save_analysis((f"@{row['creator']} — {row['hook'] or 'ролик из радара'}")[:160],row["post_url"],row["views"],row["views_per_hour"],result)
        return jsonify(id=item_id,result=result)
    except Exception as exc: return jsonify(error=str(exc)),500
    finally:
        if tmp:
            try: os.unlink(tmp)
            except OSError: pass

@app.get("/api/creators")
def creators():
    with db_conn() as conn: rows=conn.execute("SELECT * FROM tracked_creators ORDER BY best_views_per_hour DESC,matching_reels DESC LIMIT 200").fetchall()
    return jsonify([dict(x) for x in rows])

@app.get("/health")
def health(): return jsonify(ok=True,analysis_model=ANALYSIS_MODEL,radar_model=RADAR_MODEL)

if __name__=="__main__": app.run(host="0.0.0.0",port=int(os.environ.get("PORT","5000")),debug=True)
