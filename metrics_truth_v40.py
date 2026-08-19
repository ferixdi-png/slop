from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

from apify_client import ApifyClient
from flask import jsonify, request

import cloud_state
import frontend_broad_v34 as broad_frontend
import frontend_failopen_v33 as v33_frontend
import radar_audit_v30 as v30
import radar_momentum_v34 as momentum
import radar_multiplatform_v28 as v28
import radar_omni_veo_veo3_v24 as v24
import radar_quality
import radar_request_job as radar_job
import radar_service
from db import db_conn, ensure_column
from radar_logs import add_radar_log

PROFILE = "metrics_truth_v40_public_counts"
MANUAL_METRICS_CAP_USD = float(v30.MANUAL_REFRESH_CAP_USD)
_APPLIED = False
_BASE_NORMALIZE = None
_BASE_SAVE_POST = None
_BASE_UPDATE_METRICS = None
_BASE_MOMENTUM_REFRESH = None

_METRIC_FIELDS = (
    "views", "likes", "comments", "hours_since_publish", "views_per_hour",
    "followers_count", "creator_usual_views", "anomaly_multiplier", "follower_reach",
    "like_rate", "comment_rate", "viral_score_v2", "measured_growth_per_hour",
    "growth_acceleration", "metrics_updated_at", "views_metric_source", "views_metric_quality",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _present_int(raw: dict, key: str):
    if key not in raw or raw.get(key) in (None, ""):
        return None
    try:
        return max(0, int(float(raw.get(key))))
    except Exception:
        return None


def _metric_timestamp(raw: dict) -> str:
    now = _now()
    for key in ("scrapedAt", "scraped_at", "fetchedAt", "fetched_at"):
        dt = v28._parse_dt((raw or {}).get(key))
        if dt and dt <= now + timedelta(minutes=5):
            return dt.isoformat()
    return now.isoformat()


def extract_views_metric(raw: dict, platform: str, fallback_views=0):
    """Return the platform's canonical public view counter and provenance.

    Instagram's public Reel counter is videoPlayCount. videoViewCount is retained
    only as an explicitly marked deprecated fallback so it can never masquerade as
    the same metric in cross-run momentum history.
    """
    raw = dict(raw or {})
    if platform == "Instagram Reels":
        for key in ("videoPlayCount", "playCount"):
            value = _present_int(raw, key)
            if value is not None:
                return value, f"instagram.{key}", "public_exact"
        for key in ("videoViewCount", "viewCount", "viewsCount", "views"):
            value = _present_int(raw, key)
            if value is not None:
                quality = "deprecated_fallback" if key in {"videoViewCount", "viewCount"} else "generic_fallback"
                return value, f"instagram.{key}", quality
    elif platform == "TikTok":
        value = _present_int(raw, "playCount")
        if value is not None:
            return value, "tiktok.playCount", "public_exact"
        value = _present_int(raw, "views")
        if value is not None:
            return value, "tiktok.views", "generic_fallback"
    elif platform == "YouTube Shorts":
        value = _present_int(raw, "viewCount")
        if value is not None:
            return value, "youtube.viewCount", "public_exact"
        value = _present_int(raw, "views")
        if value is not None:
            return value, "youtube.views", "generic_fallback"
    return max(0, int(fallback_views or 0)), "legacy.unknown", "unknown"


def _likes_comments(raw: dict, platform: str, fallback_likes=0, fallback_comments=0):
    raw = dict(raw or {})
    if platform == "Instagram Reels":
        likes = v28._safe_int(raw.get("likesCount"), raw.get("likeCount"), raw.get("likes"))
        comments = v28._safe_int(raw.get("commentsCount"), raw.get("commentCount"), raw.get("comments"))
    elif platform == "TikTok":
        likes = v28._safe_int(raw.get("diggCount"), raw.get("likeCount"), raw.get("likes"))
        comments = v28._safe_int(raw.get("commentCount"), raw.get("comments"))
    else:
        likes = v28._safe_int(raw.get("likeCount"), raw.get("likes"))
        comments = v28._safe_int(raw.get("commentCount"), raw.get("comments"))
    return int(likes or fallback_likes or 0), int(comments or fallback_comments or 0)


def _recompute_item(item: dict) -> dict:
    item = dict(item or {})
    published = v28._parse_dt(item.get("published_at"))
    if published:
        age_hours = max(0.25, (_now() - published).total_seconds() / 3600.0)
        item["hours_since_publish"] = round(age_hours, 2)
        item["views_per_hour"] = round(int(item.get("views") or 0) / age_hours, 2)
    score = radar_service.calculate_viral_score(
        int(item.get("views") or 0),
        int(item.get("likes") or 0),
        int(item.get("comments") or 0),
        float(item.get("hours_since_publish") or 0),
        float(item.get("views_per_hour") or 0),
        int(item.get("followers_count") or 0),
        float(item.get("creator_usual_views") or 0),
    )
    item.update(score)
    item = radar_quality.apply_quality_score(item)
    item["viral_score_v2"] = v28._momentum_score(
        int(item.get("views") or 0),
        float(item.get("views_per_hour") or 0),
        float(item.get("hours_since_publish") or 0),
        float(item.get("viral_score_v2") or 0),
    )
    return item


def normalize_candidate_v40(raw, source, creator_stats=None):
    item = _BASE_NORMALIZE(raw, source, creator_stats)
    if not item:
        return None
    views, metric_source, metric_quality = extract_views_metric(raw, item.get("platform", ""), item.get("views", 0))
    likes, comments = _likes_comments(raw, item.get("platform", ""), item.get("likes", 0), item.get("comments", 0))
    item["views"] = views
    item["likes"] = likes
    item["comments"] = comments
    item["metrics_updated_at"] = _metric_timestamp(dict(raw or {}))
    item["views_metric_source"] = metric_source
    item["views_metric_quality"] = metric_quality
    return _recompute_item(item)


def _ensure_schema() -> None:
    v24._ensure_momentum_schema()
    with db_conn() as conn:
        ensure_column(conn, "radar_posts", "metrics_updated_at", "TEXT DEFAULT ''")
        ensure_column(conn, "radar_posts", "views_metric_source", "TEXT DEFAULT ''")
        ensure_column(conn, "radar_posts", "views_metric_quality", "TEXT DEFAULT ''")
        ensure_column(conn, "radar_momentum_history", "views_metric_source", "TEXT DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_radar_metrics_freshness ON radar_posts(metrics_updated_at)"
        )
        conn.commit()


def _incoming_is_older(existing, item) -> bool:
    old_dt = _parse_dt(existing["metrics_updated_at"] if existing else "")
    new_dt = _parse_dt((item or {}).get("metrics_updated_at"))
    if old_dt and not new_dt:
        return True
    return bool(old_dt and new_dt and new_dt < old_dt)


def _preserve_newer_metrics(conn, item):
    adjusted = dict(item or {})
    existing = conn.execute(
        """SELECT views,likes,comments,hours_since_publish,views_per_hour,followers_count,
                  creator_usual_views,anomaly_multiplier,follower_reach,like_rate,comment_rate,
                  viral_score_v2,measured_growth_per_hour,growth_acceleration,
                  metrics_updated_at,views_metric_source,views_metric_quality
           FROM radar_posts WHERE post_url=?""",
        (adjusted.get("post_url", ""),),
    ).fetchone()
    if existing and _incoming_is_older(existing, adjusted):
        for key in _METRIC_FIELDS:
            if key in existing.keys():
                adjusted[key] = existing[key]
    return adjusted


def _write_metric_meta(conn, item):
    conn.execute(
        """UPDATE radar_posts SET metrics_updated_at=?,views_metric_source=?,views_metric_quality=?
           WHERE post_url=?""",
        (
            str(item.get("metrics_updated_at") or ""),
            str(item.get("views_metric_source") or ""),
            str(item.get("views_metric_quality") or ""),
            str(item.get("post_url") or ""),
        ),
    )


def save_post_v40(conn, item, assessment):
    adjusted = _preserve_newer_metrics(conn, item)
    _BASE_SAVE_POST(conn, adjusted, assessment)
    _write_metric_meta(conn, adjusted)


def update_metrics_only_v40(conn, item):
    adjusted = _preserve_newer_metrics(conn, item)
    _BASE_UPDATE_METRICS(conn, adjusted)
    _write_metric_meta(conn, adjusted)


def refresh_metric_aware_momentum_v40(conn):
    """Never calculate fake acceleration across two different metric definitions."""
    _ensure_schema()
    rows = conn.execute(
        """SELECT post_url,views_metric_source FROM radar_posts
           WHERE datetime(published_at)>=datetime('now','-14 days')"""
    ).fetchall()
    for row in rows:
        current_source = str(row["views_metric_source"] or "")
        if not current_source:
            continue
        previous = conn.execute(
            "SELECT views_metric_source FROM radar_momentum_history WHERE post_url=?",
            (row["post_url"],),
        ).fetchone()
        if previous and str(previous["views_metric_source"] or "") != current_source:
            conn.execute("DELETE FROM radar_momentum_history WHERE post_url=?", (row["post_url"],))
            conn.execute(
                "UPDATE radar_posts SET measured_growth_per_hour=0,growth_acceleration=0 WHERE post_url=?",
                (row["post_url"],),
            )
    conn.commit()

    _BASE_MOMENTUM_REFRESH(conn)

    conn.execute(
        """UPDATE radar_momentum_history
           SET views_metric_source=COALESCE((
               SELECT r.views_metric_source FROM radar_posts r
               WHERE r.post_url=radar_momentum_history.post_url
               ORDER BY datetime(COALESCE(r.metrics_updated_at,r.published_at)) DESC LIMIT 1
           ),'')"""
    )
    conn.commit()


def _decorate_metric_item(item: dict) -> dict:
    item = dict(item or {})
    quality = str(item.get("views_metric_quality") or "")
    source = str(item.get("views_metric_source") or "")
    observed = _parse_dt(item.get("metrics_updated_at"))
    age_minutes = None
    if observed:
        age_minutes = max(0, int((_now() - observed).total_seconds() // 60))
    item["metrics_age_minutes"] = age_minutes
    item["metrics_stale"] = bool(age_minutes is None or age_minutes > 60)
    item["views_are_public_platform_count"] = quality == "public_exact"
    if quality == "public_exact":
        item["views_metric_label"] = "публичных просмотров"
    elif quality in {"deprecated_fallback", "generic_fallback"}:
        item["views_metric_label"] = "просмотров · fallback"
    else:
        item["views_metric_label"] = "просмотров · источник неизвестен"
    item["views_metric_provenance"] = source
    return item


def _manual_refresh_raw(row: dict):
    token = str(os.environ.get("APIFY_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("Не задан APIFY_API_TOKEN")
    client = ApifyClient(token)
    platform = str(row.get("platform") or "")
    url = str(row.get("post_url") or "")
    if platform == "Instagram Reels":
        actor = v28.INSTAGRAM_ACTOR
        run_input = {
            "directUrls": [url],
            "resultsType": "posts",
            "resultsLimit": 1,
            "addParentData": True,
        }
    elif platform == "TikTok":
        actor = v28.TIKTOK_ACTOR
        run_input = {
            "postURLs": [url],
            "resultsPerPage": 1,
            "shouldDownloadVideos": False,
            "shouldDownloadCovers": False,
            "scrapeRelatedVideos": False,
        }
    elif platform == "YouTube Shorts":
        actor = v28.YOUTUBE_ACTOR
        run_input = {
            "startUrls": [url],
            "maxResults": 1,
            "includeComments": False,
            "downloadVideos": False,
        }
    else:
        raise RuntimeError(f"Неизвестная платформа: {platform}")
    rows = v30.capped_refresh_actor_items_v30(client, actor, run_input)
    if not rows:
        raise RuntimeError("Платформа не вернула свежие метрики")
    return dict(rows[0]), actor


def _refresh_one_metric(item_id: int):
    with db_conn() as conn:
        record = conn.execute("SELECT * FROM radar_posts WHERE id=?", (item_id,)).fetchone()
    if not record:
        return jsonify(error="Ролик не найден"), 404
    row = dict(record)
    raw, actor = _manual_refresh_raw(row)
    views, source, quality = extract_views_metric(raw, row.get("platform", ""), row.get("views", 0))
    likes, comments = _likes_comments(raw, row.get("platform", ""), row.get("likes", 0), row.get("comments", 0))
    updated = dict(row)
    updated.update(
        views=views,
        likes=likes,
        comments=comments,
        metrics_updated_at=_metric_timestamp(raw),
        views_metric_source=source,
        views_metric_quality=quality,
    )
    updated = _recompute_item(updated)
    with db_conn() as conn:
        conn.execute(
            """UPDATE radar_posts SET views=?,likes=?,comments=?,hours_since_publish=?,views_per_hour=?,
               anomaly_multiplier=?,follower_reach=?,like_rate=?,comment_rate=?,viral_score_v2=?,
               metrics_updated_at=?,views_metric_source=?,views_metric_quality=? WHERE id=?""",
            (
                updated["views"], updated["likes"], updated["comments"],
                updated["hours_since_publish"], updated["views_per_hour"],
                updated.get("anomaly_multiplier", 0), updated.get("follower_reach", 0),
                updated.get("like_rate", 0), updated.get("comment_rate", 0), updated.get("viral_score_v2", 0),
                updated["metrics_updated_at"], source, quality, item_id,
            ),
        )
        radar_quality.refresh_recent_scores_quality(conn)
        fresh = conn.execute("SELECT * FROM radar_posts WHERE id=?", (item_id,)).fetchone()
        conn.commit()
    try:
        radar_job.save_radar_snapshot()
    except Exception as exc:
        add_radar_log(f"V40 metric refresh snapshot warning: {exc}", level="WARN", stage="metrics-refresh")
    payload = _decorate_metric_item(dict(fresh or updated))
    payload.update(
        ok=True,
        manual_refresh=True,
        actor=actor,
        hard_cap_usd=MANUAL_METRICS_CAP_USD,
    )
    add_radar_log(
        f"V40 METRICS REFRESH: {row.get('platform')} @{row.get('creator')} -> {payload.get('views')} views.",
        stage="metrics-refresh",
        details={
            "item_id": item_id,
            "views": payload.get("views"),
            "views_metric_source": source,
            "metrics_updated_at": payload.get("metrics_updated_at"),
            "hard_cap_usd": MANUAL_METRICS_CAP_USD,
        },
    )
    return jsonify(payload), 200


def _patch_frontend() -> dict:
    html = broad_frontend.HTML
    helper_old = "function nowLabel(){return new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit',second:'2-digit'});}"
    helper_new = helper_old + "\nfunction metricAge(v){if(!v)return 'метрики старого формата';const d=new Date(v);if(Number.isNaN(d.getTime()))return 'время метрик неизвестно';const m=Math.max(0,Math.round((Date.now()-d.getTime())/60000));if(m<2)return 'снято сейчас';if(m<60)return `снято ${m} мин назад`;const h=Math.round(m/60);return `снято ${h} ч назад`;}"
    if helper_old in html and "function metricAge(" not in html:
        html = html.replace(helper_old, helper_new, 1)

    metric_old = '<div class="metric"><b>${num(x.views)}</b><small>просмотров</small></div>'
    metric_new = '<div class="metric"><b>${num(x.views)}</b><small>${esc(x.views_metric_label||\'публичных просмотров\')}</small><small>${esc(metricAge(x.metrics_updated_at))}</small></div>'
    if metric_old in html:
        html = html.replace(metric_old, metric_new, 1)

    candidate_old = '${num(x.likes)} лайков · ${num(x.views_per_hour)}/ч</span>'
    candidate_new = '${num(x.likes)} лайков · ${num(x.views_per_hour)}/ч · ${esc(metricAge(x.metrics_updated_at))}</span>'
    if candidate_old in html:
        html = html.replace(candidate_old, candidate_new, 1)

    action_old = '<a class="btn ghost" href="${esc(x.post_url||\'#\')}" target="_blank" rel="noopener noreferrer">ОРИГИНАЛ</a><button class="btn" data-analyze="${Number(x.id||0)}">УЛЬТРА-ПРОМПТЫ</button>'
    action_new = '<a class="btn ghost" href="${esc(x.post_url||\'#\')}" target="_blank" rel="noopener noreferrer">ОРИГИНАЛ</a><button class="btn ghost" data-metrics="${Number(x.id||0)}">ОБНОВИТЬ ЦИФРЫ</button><button class="btn" data-analyze="${Number(x.id||0)}">УЛЬТРА-ПРОМПТЫ</button>'
    if action_old in html:
        html = html.replace(action_old, action_new, 1)

    analyze_marker = "async function analyze(id,button){"
    if analyze_marker in html and "async function refreshMetrics(" not in html:
        refresh_fn = "async function refreshMetrics(id,button){if(!id)return;const old=button.textContent;button.disabled=true;button.textContent='ОБНОВЛЯЮ…';try{const d=await api(`/api/radar/${id}/metrics-refresh`,{method:'POST'},190000);await refreshLists(true);clearRuntimeError();}catch(e){showRuntimeError(`Метрики: ${e.message}`);}finally{button.disabled=false;button.textContent=old;}}\n"
        html = html.replace(analyze_marker, refresh_fn + analyze_marker, 1)

    click_old = "document.addEventListener('click',async e=>{const a=e.target.closest('[data-analyze]');if(a){await analyze(Number(a.dataset.analyze),a);return;}const c=e.target.closest('[data-copy]');"
    click_new = "document.addEventListener('click',async e=>{const m=e.target.closest('[data-metrics]');if(m){await refreshMetrics(Number(m.dataset.metrics),m);return;}const a=e.target.closest('[data-analyze]');if(a){await analyze(Number(a.dataset.analyze),a);return;}const c=e.target.closest('[data-copy]');"
    if click_old in html:
        html = html.replace(click_old, click_new, 1)

    copy_old = "До 100 роликов по momentum: скорость просмотров, свежесть, доказанный охват. Gemini только подсказывает способ адаптации."
    copy_new = "До 100 роликов по momentum. Просмотры — публичный счётчик платформы на момент последнего сбора; свежесть метрик показана в каждой карточке. «Обновить цифры» делает только ручной refresh одного ролика."
    if copy_old in html:
        html = html.replace(copy_old, copy_new, 1)

    html_bytes = html.encode("utf-8")
    html_sha = hashlib.sha256(html_bytes).hexdigest()[:16]
    broad_frontend.HTML = html
    broad_frontend.HTML_BYTES = html_bytes
    broad_frontend.HTML_SHA256 = html_sha
    v33_frontend.HTML = html
    v33_frontend.HTML_BYTES = html_bytes
    v33_frontend.HTML_SHA256 = html_sha
    return {
        "frontend_metrics_freshness": True,
        "manual_metric_refresh_button": True,
        "html_sha256": html_sha,
        "html_bytes": len(html_bytes),
    }


def _install_api(app) -> None:
    if getattr(app, "_metrics_truth_v40_api", False):
        return

    @app.after_request
    def metrics_truth_v40_response(response):
        if not response.is_json:
            return response
        if request.path not in {"/api/radar", "/api/radar/candidates"}:
            return response
        data = response.get_json(silent=True)
        if not isinstance(data, list):
            return response
        data = [_decorate_metric_item(x) if isinstance(x, dict) else x for x in data]
        response.set_data(app.json.dumps(data))
        response.mimetype = "application/json"
        return response

    if "metrics_refresh_v40" not in app.view_functions:
        app.add_url_rule(
            "/api/radar/<int:item_id>/metrics-refresh",
            endpoint="metrics_refresh_v40",
            view_func=_refresh_one_metric,
            methods=["POST"],
        )
    app._metrics_truth_v40_api = True


def install_metrics_truth_v40(app=None) -> dict:
    global _APPLIED, _BASE_NORMALIZE, _BASE_SAVE_POST, _BASE_UPDATE_METRICS, _BASE_MOMENTUM_REFRESH
    if _APPLIED:
        if app is not None:
            _install_api(app)
        return diagnostics()

    _ensure_schema()
    _BASE_NORMALIZE = v28.normalize_multiplatform_candidate
    _BASE_SAVE_POST = radar_quality._legacy_save_post
    _BASE_UPDATE_METRICS = radar_quality._update_metrics_only
    _BASE_MOMENTUM_REFRESH = momentum._BASE_REFRESH

    v28.normalize_multiplatform_candidate = normalize_candidate_v40
    radar_job.normalize_reel = normalize_candidate_v40
    radar_quality._legacy_save_post = save_post_v40
    radar_quality._update_metrics_only = update_metrics_only_v40
    radar_service.save_post = save_post_v40
    momentum._BASE_REFRESH = refresh_metric_aware_momentum_v40

    frontend_info = _patch_frontend()
    if app is not None:
        _install_api(app)
    _APPLIED = True
    info = {**diagnostics(), **frontend_info}
    add_radar_log(
        "V40 METRICS TRUTH READY: Instagram videoPlayCount / TikTok playCount / YouTube viewCount; metric timestamp + provenance; source-safe momentum; manual one-item refresh.",
        stage="startup",
        details=info,
    )
    return info


def diagnostics() -> dict:
    return {
        "profile": PROFILE,
        "instagram_views": "videoPlayCount",
        "instagram_deprecated_fallback": "videoViewCount",
        "tiktok_views": "playCount",
        "youtube_views": "viewCount",
        "metrics_timestamped": True,
        "stale_metric_overwrite_blocked": True,
        "momentum_metric_source_guard": True,
        "manual_metric_refresh": True,
        "manual_metric_refresh_hard_cap_usd": MANUAL_METRICS_CAP_USD,
        "automatic_metric_refresh": False,
    }
