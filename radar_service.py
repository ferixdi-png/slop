import json, os, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import requests
from apify_client import ApifyClient
from config import *
from db import db_conn
from gemini_service import classify_radar_video

def parse_dt(value: Any):
    if value is None or value == "": return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        try:
            stamp=float(value); stamp = stamp/1000 if stamp > 10_000_000_000 else stamp
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except Exception: return None
    try:
        dt=datetime.fromisoformat(str(value).strip().replace("Z","+00:00"))
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(timezone.utc)
    except ValueError: return None

def views_per_hour(views, published):
    age=max(.25,(datetime.now(timezone.utc)-published).total_seconds()/3600)
    return round(age,2), round(views/age,2)

def download_temp_video(url):
    if not url.startswith("https://"): raise RuntimeError("Некорректная ссылка на видео")
    total=0; tmp=tempfile.NamedTemporaryFile(delete=False,suffix=".mp4")
    try:
        with requests.get(url,stream=True,timeout=(10,45),headers={"User-Agent":"Mozilla/5.0"}) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1024*1024):
                if not chunk: continue
                total += len(chunk)
                if total > 50*1024*1024: raise RuntimeError("Видео из радара больше 50 МБ")
                tmp.write(chunk)
        tmp.close(); return tmp.name
    except Exception:
        tmp.close()
        try: os.unlink(tmp.name)
        except OSError: pass
        raise

def run_actor_items(client, actor_id, run_input):
    run=client.actor(actor_id).call(run_input=run_input)
    if not run or not run.get("defaultDatasetId"): return []
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())

def normalize_reel(raw, source):
    owner=raw.get("owner") if isinstance(raw.get("owner"),dict) else {}
    url=raw.get("url") or raw.get("postUrl") or raw.get("post_url") or raw.get("inputUrl") or ""
    video_url=raw.get("videoUrl") or raw.get("video_url") or raw.get("videoSrc") or ""
    preview=raw.get("displayUrl") or raw.get("display_url") or raw.get("thumbnailUrl") or raw.get("thumbnail_src") or ""
    creator=raw.get("ownerUsername") or raw.get("owner_username") or raw.get("username") or owner.get("username") or ""
    caption=raw.get("caption") or raw.get("text") or raw.get("description") or ""
    duration=raw.get("videoDuration") or raw.get("video_duration") or raw.get("duration") or 0
    views=(raw.get("videoViewCount") or raw.get("video_view_count") or raw.get("viewCount") or raw.get("view_count") or raw.get("videoPlayCount") or raw.get("video_play_count") or raw.get("playCount") or raw.get("playsCount") or 0)
    likes=raw.get("likesCount") or raw.get("likeCount") or raw.get("like_count") or raw.get("likes") or 0
    comments=raw.get("commentsCount") or raw.get("commentCount") or raw.get("comment_count") or raw.get("comments") or 0
    published=parse_dt(raw.get("timestamp") or raw.get("takenAtTimestamp") or raw.get("taken_at_timestamp") or raw.get("takenAt") or raw.get("publishedAt"))
    if not url or not creator or not published: return None
    try: duration=float(duration or 0); views=int(views or 0); likes=int(likes or 0); comments=int(comments or 0)
    except (TypeError,ValueError): return None
    if duration<=0 or duration>10 or published < datetime.now(timezone.utc)-timedelta(days=7): return None
    hours,vph=views_per_hour(views,published)
    return dict(platform="Instagram Reels",creator=creator,post_url=url,video_url=video_url,preview_url=preview,
        published_at=published.isoformat(),duration_sec=duration,views=views,likes=likes,comments=comments,
        hours_since_publish=hours,views_per_hour=vph,search_term=raw.get("searchTerm") or raw.get("hashtag") or source,
        caption=str(caption)[:4000])

def matches(a):
    return all([a.is_russian,a.is_ai_video,a.is_comedy_scene,not a.is_tutorial_or_review,not a.is_talking_head,a.simple_situation,a.reproducible_format])

def save_post(conn,item,a):
    match=bool(a and matches(a))
    conn.execute("""INSERT INTO radar_posts(platform,creator,post_url,video_url,preview_url,published_at,duration_sec,views,likes,comments,hours_since_publish,views_per_hour,search_term,caption,ai_checked,ai_match,scene_description,characters_json,joke,hook,ending,reproducible,reason)
    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(post_url) DO UPDATE SET video_url=excluded.video_url,preview_url=excluded.preview_url,views=excluded.views,likes=excluded.likes,comments=excluded.comments,hours_since_publish=excluded.hours_since_publish,views_per_hour=excluded.views_per_hour,search_term=excluded.search_term,caption=excluded.caption,ai_checked=excluded.ai_checked,ai_match=excluded.ai_match,scene_description=excluded.scene_description,characters_json=excluded.characters_json,joke=excluded.joke,hook=excluded.hook,ending=excluded.ending,reproducible=excluded.reproducible,reason=excluded.reason""",
    (item["platform"],item["creator"],item["post_url"],item["video_url"],item["preview_url"],item["published_at"],item["duration_sec"],item["views"],item["likes"],item["comments"],item["hours_since_publish"],item["views_per_hour"],item["search_term"],item["caption"],1 if a else 0,1 if match else 0,a.scene_description if a else "",json.dumps(a.characters if a else [],ensure_ascii=False),a.joke if a else "",a.hook if a else "",a.ending if a else "",1 if a and a.reproducible_format else 0,a.reason if a else ""))
    if match:
        now=datetime.now(timezone.utc).isoformat()
        conn.execute("""INSERT INTO tracked_creators(username,first_seen_at,last_seen_at,best_views_per_hour,matching_reels) VALUES(?,?,?,?,1)
        ON CONFLICT(username) DO UPDATE SET last_seen_at=excluded.last_seen_at,best_views_per_hour=MAX(tracked_creators.best_views_per_hour,excluded.best_views_per_hour),matching_reels=tracked_creators.matching_reels+1""",
        (item["creator"],now,now,item["views_per_hour"]))

def sync_radar():
    token=os.environ.get("APIFY_API_TOKEN")
    if not token: raise RuntimeError("Не задан APIFY_API_TOKEN")
    client=ApifyClient(token); raw_items=[]; source_errors=0
    with db_conn() as conn:
        tracked=[r[0] for r in conn.execute("SELECT username FROM tracked_creators ORDER BY best_views_per_hour DESC LIMIT 100").fetchall()]
    if tracked:
        try:
            reels=run_actor_items(client,APIFY_CREATOR_ACTOR,{
                "username": tracked, "resultsLimit": 10, "onlyPostsNewerThan": "7 days",
                "skipPinnedPosts": True, "includeTranscript": False, "includeDownloadedVideo": False
            })
            raw_items.extend((x,"наблюдаемый автор") for x in reels)
        except Exception: source_errors += 1
    for term in SEARCH_TERMS:
        try:
            rows=run_actor_items(client,APIFY_SEARCH_ACTOR,{"search":term,"searchType":"popular","searchLimit":SEARCH_LIMIT})
            for x in rows:
                x.setdefault("searchTerm",term); raw_items.append((x,f"поиск: {term}"))
        except Exception: source_errors += 1
    try:
        rows=run_actor_items(client,APIFY_HASHTAG_ACTOR,{"hashtags":HASHTAGS,"resultsType":"reels","resultsLimit":HASHTAG_LIMIT})
        raw_items.extend((x,f"хештег: {x.get('hashtag') or ''}") for x in rows)
    except Exception: source_errors += 1
    unique={}
    for raw,source in raw_items:
        item=normalize_reel(raw,source)
        if item and (item["post_url"] not in unique or item["views"]>unique[item["post_url"]]["views"]): unique[item["post_url"]]=item
    candidates=sorted(unique.values(),key=lambda x:(x["views_per_hour"],x["views"]),reverse=True)[:RADAR_AI_ANALYZE_LIMIT]
    checked=matched=errors=0
    with db_conn() as conn:
        for item in candidates:
            assessment=tmp=None
            try:
                if item["video_url"]:
                    tmp=download_temp_video(item["video_url"]); assessment=classify_radar_video(tmp,item["caption"]); checked+=1
                    if matches(assessment): matched+=1
                save_post(conn,item,assessment)
            except Exception:
                save_post(conn,item,None); errors+=1
            finally:
                if tmp:
                    try: os.unlink(tmp)
                    except OSError: pass
        conn.commit()
    return {"raw":len(raw_items),"after_numeric_filter":len(unique),"ai_checked":checked,"matched":matched,"errors":errors,"source_errors":source_errors,"kept":min(matched,RADAR_KEEP_LIMIT)}
