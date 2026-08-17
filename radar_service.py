import json, math, os, statistics, tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

import requests
from apify_client import ApifyClient

from config import *
from db import db_conn
from gemini_service import classify_radar_video, summarize_radar_meta


def parse_dt(value: Any):
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        try:
            stamp = float(value)
            stamp = stamp / 1000 if stamp > 10_000_000_000 else stamp
            return datetime.fromtimestamp(stamp, tz=timezone.utc)
        except Exception:
            return None
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        return (dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt).astimezone(timezone.utc)
    except ValueError:
        return None


def safe_int(*values):
    for value in values:
        if value is None or value == "":
            continue
        try:
            return max(0, int(float(value)))
        except (TypeError, ValueError):
            continue
    return 0


def safe_float(*values):
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def raw_owner(raw):
    return raw.get("owner") if isinstance(raw.get("owner"), dict) else {}


def raw_creator(raw):
    owner = raw_owner(raw)
    return str(
        raw.get("ownerUsername")
        or raw.get("owner_username")
        or raw.get("username")
        or owner.get("username")
        or ""
    ).strip().lstrip("@")


def raw_views(raw):
    return safe_int(
        raw.get("videoViewCount"), raw.get("video_view_count"),
        raw.get("viewCount"), raw.get("view_count"),
        raw.get("videoPlayCount"), raw.get("video_play_count"),
        raw.get("playCount"), raw.get("playsCount"),
    )


def raw_followers(raw):
    owner = raw_owner(raw)
    return safe_int(
        raw.get("ownerFollowersCount"), raw.get("owner_followers_count"),
        raw.get("followersCount"), raw.get("followers_count"),
        raw.get("followers"), owner.get("followersCount"),
        owner.get("followers_count"),
    )


def views_per_hour(views, published):
    age = max(.25, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    return round(age, 2), round(views / age, 2)


def clamp01(value):
    return max(0.0, min(1.0, float(value)))


def calculate_viral_score(views, likes, comments, hours, views_per_hour_value, followers, usual_views):
    anomaly = (views / usual_views) if usual_views > 0 else 0.0
    follower_reach = (views / followers) if followers > 0 else 0.0
    like_rate = (likes / views) if views > 0 else 0.0
    comment_rate = (comments / views) if views > 0 else 0.0

    velocity_component = clamp01(math.log1p(max(0, views_per_hour_value)) / math.log1p(100_000))
    anomaly_component = clamp01(math.log1p(max(0, anomaly)) / math.log(21)) if usual_views > 0 else 0.25
    reach_component = clamp01(math.log1p(max(0, follower_reach)) / math.log(6)) if followers > 0 else 0.25

    like_component = clamp01(like_rate / 0.08)
    comment_component = clamp01(comment_rate / 0.01)
    engagement_component = 0.7 * like_component + 0.3 * comment_component

    freshness_component = clamp01(1 - (hours / (24 * 7)))

    score = 100 * (
        0.35 * velocity_component
        + 0.25 * anomaly_component
        + 0.15 * reach_component
        + 0.15 * engagement_component
        + 0.10 * freshness_component
    )
    return {
        "viral_score_v2": round(score, 1),
        "anomaly_multiplier": round(anomaly, 2) if usual_views > 0 else 0.0,
        "follower_reach": round(follower_reach, 3) if followers > 0 else 0.0,
        "like_rate": round(like_rate, 5),
        "comment_rate": round(comment_rate, 5),
    }


def download_temp_video(url):
    if not url.startswith("https://"):
        raise RuntimeError("Некорректная ссылка на видео")
    total = 0
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
    try:
        with requests.get(url, stream=True, timeout=(10, 45), headers={"User-Agent": "Mozilla/5.0"}) as r:
            r.raise_for_status()
            for chunk in r.iter_content(1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > 50 * 1024 * 1024:
                    raise RuntimeError("Видео из радара больше 50 МБ")
                tmp.write(chunk)
        tmp.close()
        return tmp.name
    except Exception:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def run_actor_items(client, actor_id, run_input):
    run = client.actor(actor_id).call(run_input=run_input)
    if not run or not run.get("defaultDatasetId"):
        return []
    return list(client.dataset(run["defaultDatasetId"]).iterate_items())


def update_creator_baselines(conn, rows):
    grouped = {}
    followers = {}
    for raw in rows:
        creator = raw_creator(raw)
        views = raw_views(raw)
        if not creator:
            continue
        if views > 0:
            grouped.setdefault(creator, []).append(views)
        f = raw_followers(raw)
        if f > 0:
            followers[creator] = max(followers.get(creator, 0), f)

    now = datetime.now(timezone.utc).isoformat()
    for creator, values in grouped.items():
        usual = float(statistics.median(values)) if values else 0.0
        conn.execute(
            """INSERT INTO tracked_creators(
                username,first_seen_at,last_seen_at,best_views_per_hour,matching_reels,
                followers_count,usual_views,sample_size
            ) VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(username) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                followers_count=CASE WHEN excluded.followers_count>0 THEN excluded.followers_count ELSE tracked_creators.followers_count END,
                usual_views=CASE WHEN excluded.usual_views>0 THEN excluded.usual_views ELSE tracked_creators.usual_views END,
                sample_size=CASE WHEN excluded.sample_size>0 THEN excluded.sample_size ELSE tracked_creators.sample_size END""",
            (creator, now, now, 0, 0, followers.get(creator, 0), usual, len(values)),
        )
    conn.commit()


def load_creator_stats(conn):
    rows = conn.execute(
        "SELECT username,followers_count,usual_views,sample_size FROM tracked_creators"
    ).fetchall()
    return {
        row["username"]: {
            "followers_count": int(row["followers_count"] or 0),
            "usual_views": float(row["usual_views"] or 0),
            "sample_size": int(row["sample_size"] or 0),
        }
        for row in rows
    }


def normalize_reel(raw, source, creator_stats=None):
    url = raw.get("url") or raw.get("postUrl") or raw.get("post_url") or raw.get("inputUrl") or ""
    video_url = raw.get("videoUrl") or raw.get("video_url") or raw.get("videoSrc") or ""
    preview = raw.get("displayUrl") or raw.get("display_url") or raw.get("thumbnailUrl") or raw.get("thumbnail_src") or ""
    creator = raw_creator(raw)
    caption = raw.get("caption") or raw.get("text") or raw.get("description") or ""
    duration = safe_float(raw.get("videoDuration"), raw.get("video_duration"), raw.get("duration"))
    views = raw_views(raw)
    likes = safe_int(raw.get("likesCount"), raw.get("likeCount"), raw.get("like_count"), raw.get("likes"))
    comments = safe_int(raw.get("commentsCount"), raw.get("commentCount"), raw.get("comment_count"), raw.get("comments"))
    published = parse_dt(
        raw.get("timestamp") or raw.get("takenAtTimestamp") or raw.get("taken_at_timestamp")
        or raw.get("takenAt") or raw.get("publishedAt")
    )
    if not url or not creator or not published:
        return None
    if duration <= 0 or duration > 10 or published < datetime.now(timezone.utc) - timedelta(days=7):
        return None

    stats = (creator_stats or {}).get(creator, {})
    followers = raw_followers(raw) or int(stats.get("followers_count", 0))
    usual_views = float(stats.get("usual_views", 0))
    hours, vph = views_per_hour(views, published)
    score = calculate_viral_score(views, likes, comments, hours, vph, followers, usual_views)

    return dict(
        platform="Instagram Reels",
        creator=creator,
        post_url=url,
        video_url=video_url,
        preview_url=preview,
        published_at=published.isoformat(),
        duration_sec=duration,
        views=views,
        likes=likes,
        comments=comments,
        hours_since_publish=hours,
        views_per_hour=vph,
        followers_count=followers,
        creator_usual_views=round(usual_views, 1),
        search_term=raw.get("searchTerm") or raw.get("hashtag") or source,
        caption=str(caption)[:4000],
        **score,
    )


def matches(a):
    return all([
        a.is_russian,
        a.is_ai_video,
        a.is_comedy_scene,
        not a.is_tutorial_or_review,
        not a.is_talking_head,
        a.simple_situation,
        a.reproducible_format,
    ])


def save_post(conn, item, a):
    match = bool(a and matches(a))
    conn.execute(
        """INSERT INTO radar_posts(
            platform,creator,post_url,video_url,preview_url,published_at,duration_sec,
            views,likes,comments,hours_since_publish,views_per_hour,followers_count,
            creator_usual_views,anomaly_multiplier,follower_reach,like_rate,comment_rate,
            viral_score_v2,search_term,caption,ai_checked,ai_match,scene_description,
            characters_json,joke,hook,ending,reproducible,reason
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(post_url) DO UPDATE SET
            video_url=excluded.video_url,preview_url=excluded.preview_url,
            views=excluded.views,likes=excluded.likes,comments=excluded.comments,
            hours_since_publish=excluded.hours_since_publish,
            views_per_hour=excluded.views_per_hour,
            followers_count=excluded.followers_count,
            creator_usual_views=excluded.creator_usual_views,
            anomaly_multiplier=excluded.anomaly_multiplier,
            follower_reach=excluded.follower_reach,
            like_rate=excluded.like_rate,comment_rate=excluded.comment_rate,
            viral_score_v2=excluded.viral_score_v2,
            search_term=excluded.search_term,caption=excluded.caption,
            ai_checked=excluded.ai_checked,ai_match=excluded.ai_match,
            scene_description=excluded.scene_description,
            characters_json=excluded.characters_json,joke=excluded.joke,
            hook=excluded.hook,ending=excluded.ending,
            reproducible=excluded.reproducible,reason=excluded.reason""",
        (
            item["platform"], item["creator"], item["post_url"], item["video_url"], item["preview_url"],
            item["published_at"], item["duration_sec"], item["views"], item["likes"], item["comments"],
            item["hours_since_publish"], item["views_per_hour"], item["followers_count"],
            item["creator_usual_views"], item["anomaly_multiplier"], item["follower_reach"],
            item["like_rate"], item["comment_rate"], item["viral_score_v2"],
            item["search_term"], item["caption"],
            1 if a else 0, 1 if match else 0,
            a.scene_description if a else "",
            json.dumps(a.characters if a else [], ensure_ascii=False),
            a.joke if a else "", a.hook if a else "", a.ending if a else "",
            1 if a and a.reproducible_format else 0,
            a.reason if a else "",
        ),
    )

    if match:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO tracked_creators(
                username,first_seen_at,last_seen_at,best_views_per_hour,matching_reels,
                followers_count,usual_views,sample_size
            ) VALUES(?,?,?,?,1,?,?,0)
            ON CONFLICT(username) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,
                best_views_per_hour=MAX(tracked_creators.best_views_per_hour,excluded.best_views_per_hour),
                matching_reels=tracked_creators.matching_reels+1,
                followers_count=CASE WHEN excluded.followers_count>0 THEN excluded.followers_count ELSE tracked_creators.followers_count END""",
            (
                item["creator"], now, now, item["views_per_hour"],
                item["followers_count"], item["creator_usual_views"],
            ),
        )


def save_meta_report(conn, rows):
    if not rows:
        return None
    payload = []
    for row in rows[:RADAR_KEEP_LIMIT]:
        x = dict(row)
        try:
            x["characters"] = json.loads(x.get("characters_json") or "[]")
        except Exception:
            x["characters"] = []
        payload.append(x)

    report = summarize_radar_meta(payload)
    if not report:
        return None

    avg_duration = round(sum(float(x.get("duration_sec") or 0) for x in payload) / max(1, len(payload)), 2)
    conn.execute(
        "INSERT INTO radar_meta(created_at,source_count,average_duration_sec,report_json) VALUES(?,?,?,?)",
        (
            datetime.now(timezone.utc).isoformat(),
            len(payload),
            avg_duration,
            json.dumps(report.model_dump(), ensure_ascii=False),
        ),
    )
    return report


def sync_radar():
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        raise RuntimeError("Не задан APIFY_API_TOKEN")

    client = ApifyClient(token)
    raw_items = []
    source_errors = 0

    with db_conn() as conn:
        tracked = [
            r[0] for r in conn.execute(
                "SELECT username FROM tracked_creators ORDER BY best_views_per_hour DESC LIMIT 100"
            ).fetchall()
        ]

    creator_rows = []
    if tracked:
        try:
            creator_rows = run_actor_items(
                client,
                APIFY_CREATOR_ACTOR,
                {
                    "username": tracked,
                    "resultsLimit": 10,
                    "onlyPostsNewerThan": "7 days",
                    "skipPinnedPosts": True,
                    "includeTranscript": False,
                    "includeDownloadedVideo": False,
                },
            )
            raw_items.extend((x, "наблюдаемый автор") for x in creator_rows)
            with db_conn() as conn:
                update_creator_baselines(conn, creator_rows)
        except Exception:
            source_errors += 1

    for term in SEARCH_TERMS:
        try:
            rows = run_actor_items(
                client,
                APIFY_SEARCH_ACTOR,
                {"search": term, "searchType": "popular", "searchLimit": SEARCH_LIMIT},
            )
            for x in rows:
                x.setdefault("searchTerm", term)
                raw_items.append((x, f"поиск: {term}"))
        except Exception:
            source_errors += 1

    try:
        rows = run_actor_items(
            client,
            APIFY_HASHTAG_ACTOR,
            {"hashtags": HASHTAGS, "resultsType": "reels", "resultsLimit": HASHTAG_LIMIT},
        )
        raw_items.extend((x, f"хештег: {x.get('hashtag') or ''}") for x in rows)
    except Exception:
        source_errors += 1

    with db_conn() as conn:
        creator_stats = load_creator_stats(conn)

    unique = {}
    for raw, source in raw_items:
        item = normalize_reel(raw, source, creator_stats)
        if item and (
            item["post_url"] not in unique
            or item["viral_score_v2"] > unique[item["post_url"]]["viral_score_v2"]
        ):
            unique[item["post_url"]] = item

    candidates = sorted(
        unique.values(),
        key=lambda x: (x["viral_score_v2"], x["views_per_hour"], x["views"]),
        reverse=True,
    )[:RADAR_AI_ANALYZE_LIMIT]

    checked = matched = errors = 0
    with db_conn() as conn:
        for item in candidates:
            assessment = tmp = None
            try:
                if item["video_url"]:
                    tmp = download_temp_video(item["video_url"])
                    assessment = classify_radar_video(tmp, item["caption"])
                    checked += 1
                    if matches(assessment):
                        matched += 1
                save_post(conn, item, assessment)
            except Exception:
                save_post(conn, item, None)
                errors += 1
            finally:
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

        top_rows = conn.execute(
            """SELECT * FROM radar_posts
               WHERE datetime(published_at)>=datetime('now','-7 days') AND ai_match=1
               ORDER BY viral_score_v2 DESC, views_per_hour DESC, views DESC
               LIMIT ?""",
            (RADAR_KEEP_LIMIT,),
        ).fetchall()
        try:
            save_meta_report(conn, top_rows)
        except Exception:
            errors += 1
        conn.commit()

    return {
        "raw": len(raw_items),
        "after_numeric_filter": len(unique),
        "ai_checked": checked,
        "matched": matched,
        "errors": errors,
        "source_errors": source_errors,
        "kept": min(matched, RADAR_KEEP_LIMIT),
    }
