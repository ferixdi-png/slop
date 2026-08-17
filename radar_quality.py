import math

from radar_service import calculate_viral_score, load_creator_stats, save_post as _legacy_save_post


def _clamp01(value):
    return max(0.0, min(1.0, float(value)))


def quality_adjusted_score(item):
    """Turn raw viral velocity into a confidence-weighted repeat score.

    Goal: a tiny Reel with 9 likes must never outrank a genuinely proven winner
    just because it is very fresh. Absolute views + social proof are evidence,
    while velocity/anomaly/freshness remain useful discovery signals.
    """
    base = float(item.get("viral_score_v2") or 0)
    views = int(item.get("views") or 0)
    likes = int(item.get("likes") or 0)
    comments = int(item.get("comments") or 0)
    vph = float(item.get("views_per_hour") or 0)

    views_component = _clamp01(math.log1p(max(0, views)) / math.log1p(1_000_000))
    likes_component = _clamp01(math.log1p(max(0, likes)) / math.log1p(10_000))
    comments_component = _clamp01(math.log1p(max(0, comments)) / math.log1p(1_000))
    social_component = 0.78 * likes_component + 0.22 * comments_component

    # 65% = viral dynamics, 35% = hard evidence that people actually reacted.
    score = 0.65 * base + 35.0 * (0.68 * views_component + 0.32 * social_component)

    # Absolute-evidence caps. These are deliberately conservative because the
    # product promises "what is worth repeating", not merely "what is new".
    if views < 1_000:
        score = min(score, 28)
    elif views < 5_000:
        score = min(score, 46)
    elif views < 20_000:
        score = min(score, 64)

    if 0 < likes < 10:
        score = min(score, 25)
    elif 10 <= likes < 25:
        score = min(score, 44)
    elif 25 <= likes < 50:
        score = min(score, 61)

    # Huge play count with almost no reaction is low-confidence / suspicious.
    if views >= 100_000 and 0 < likes < 50:
        score = min(score, 54)
    if views >= 50_000 and 0 < likes and likes / max(1, views) < 0.0004:
        score = min(score, 52)

    # If likes are unavailable (0 can mean hidden/missing), do not hard-reject;
    # just prevent an evidence-free Reel from becoming S-tier.
    if likes == 0:
        score = min(score, 68 if views >= 100_000 else 52)

    # Extremely fast growth can recover some confidence, but never bypass the
    # hard low-like caps above.
    if views >= 50_000 and vph >= 20_000 and likes >= 50:
        score = max(score, min(88, base))

    return round(max(0, min(100, score)), 1)


def apply_quality_score(item):
    item = dict(item)
    item["viral_score_v2"] = quality_adjusted_score(item)
    return item


def top_eligible(row):
    """Only evidence-backed Reels enter the recommendation TOP.

    Weak/noisy candidates remain visible in the Apify candidates section.
    """
    score = float(row.get("viral_score_v2") or 0)
    views = int(row.get("views") or 0)
    likes = int(row.get("likes") or 0)
    comments = int(row.get("comments") or 0)
    vph = float(row.get("views_per_hour") or 0)

    if score < 44:
        return False
    if views < 3_000 and vph < 5_000:
        return False
    if likes == 0:
        return views >= 100_000 and score >= 55
    return likes >= 20 or comments >= 8


def recommendation_status_for_row(row):
    score = float(row.get("viral_score_v2") or 0)
    anomaly = float(row.get("anomaly_multiplier") or 0)
    hours = float(row.get("hours_since_publish") or 999)
    views = int(row.get("views") or 0)
    likes = int(row.get("likes") or 0)
    comments = int(row.get("comments") or 0)
    vph = float(row.get("views_per_hour") or 0)
    usual = float(row.get("creator_usual_views") or 0)

    strong_evidence = views >= 100_000 and likes >= 100
    solid_evidence = views >= 30_000 and likes >= 50
    test_evidence = views >= 5_000 and likes >= 20

    if score >= 82 and strong_evidence and (vph >= 10_000 or anomaly >= 4):
        level, label = "S", "🔥 СРОЧНО БРАТЬ В РАБОТУ"
    elif score >= 67 and (solid_evidence or (views >= 100_000 and likes == 0)):
        level, label = "A", "🟢 СИЛЬНЫЙ КАНДИДАТ"
    elif score >= 50 and test_evidence:
        level, label = "B", "🟡 МОЖНО ТЕСТИРОВАТЬ"
    else:
        level, label = "C", "⚪ СЛАБЫЙ СИГНАЛ"

    reasons = [f"Viral Score {score:.0f}/100"]
    if views >= 100_000:
        reasons.append(f"{round(views / 1000):,}K просмотров".replace(",", " "))
    elif views > 0:
        reasons.append(f"{views:,} просмотров".replace(",", " "))
    if likes > 0:
        reasons.append(f"{likes:,} лайков".replace(",", " "))
    else:
        reasons.append("лайки не получены из Instagram")
    if anomaly >= 2 and usual > 0:
        reasons.append(f"аномалия автора ×{anomaly:.1f}")
    elif vph >= 10_000:
        reasons.append(f"{round(vph):,}/ч".replace(",", " "))
    elif hours <= 24:
        reasons.append("меньше суток")
    if comments >= 20 and len(reasons) < 4:
        reasons.append(f"{comments} комментариев")

    return {
        "priority_level": level,
        "priority_label": label,
        "priority_reason": " · ".join(reasons[:4]),
    }


def _update_metrics_only(conn, item):
    """Refresh numeric/media fields without erasing a previous Gemini verdict."""
    conn.execute(
        """UPDATE radar_posts SET
            video_url=CASE WHEN ?<>'' THEN ? ELSE video_url END,
            preview_url=CASE WHEN ?<>'' THEN ? ELSE preview_url END,
            published_at=?,duration_sec=?,views=?,likes=?,comments=?,
            hours_since_publish=?,views_per_hour=?,followers_count=?,
            creator_usual_views=?,anomaly_multiplier=?,follower_reach=?,
            like_rate=?,comment_rate=?,viral_score_v2=?,search_term=?,caption=?
           WHERE post_url=?""",
        (
            item.get("video_url", ""), item.get("video_url", ""),
            item.get("preview_url", ""), item.get("preview_url", ""),
            item.get("published_at", ""), item.get("duration_sec", 0), item.get("views", 0),
            item.get("likes", 0), item.get("comments", 0), item.get("hours_since_publish", 0),
            item.get("views_per_hour", 0), item.get("followers_count", 0),
            item.get("creator_usual_views", 0), item.get("anomaly_multiplier", 0),
            item.get("follower_reach", 0), item.get("like_rate", 0), item.get("comment_rate", 0),
            item.get("viral_score_v2", 0), item.get("search_term", ""), item.get("caption", ""),
            item.get("post_url", ""),
        ),
    )


def save_post_preserve_ai(conn, item, assessment):
    """Do not make already-approved results disappear while a new run is in progress."""
    if assessment is None:
        existing = conn.execute(
            "SELECT ai_checked FROM radar_posts WHERE post_url=?", (item.get("post_url", ""),)
        ).fetchone()
        if existing and int(existing["ai_checked"] or 0) == 1:
            _update_metrics_only(conn, item)
            return
    _legacy_save_post(conn, item, assessment)


def refresh_recent_scores_quality(conn):
    stats = load_creator_stats(conn)
    rows = conn.execute(
        """SELECT id,creator,views,likes,comments,hours_since_publish,views_per_hour,
                  followers_count,creator_usual_views,anomaly_multiplier,follower_reach,
                  like_rate,comment_rate,viral_score_v2
           FROM radar_posts WHERE datetime(published_at)>=datetime('now','-7 days')"""
    ).fetchall()

    for row in rows:
        x = dict(row)
        creator_stat = stats.get(x["creator"], {})
        followers = int(x.get("followers_count") or 0) or int(creator_stat.get("followers_count", 0))
        usual_views = float(creator_stat.get("usual_views", 0))
        base = calculate_viral_score(
            int(x.get("views") or 0),
            int(x.get("likes") or 0),
            int(x.get("comments") or 0),
            float(x.get("hours_since_publish") or 0),
            float(x.get("views_per_hour") or 0),
            followers,
            usual_views,
        )
        scored = dict(x)
        scored.update(base)
        scored["followers_count"] = followers
        scored["creator_usual_views"] = usual_views
        final_score = quality_adjusted_score(scored)
        conn.execute(
            """UPDATE radar_posts SET followers_count=?,creator_usual_views=?,
               anomaly_multiplier=?,follower_reach=?,like_rate=?,comment_rate=?,viral_score_v2=?
               WHERE id=?""",
            (
                followers, usual_views, base["anomaly_multiplier"], base["follower_reach"],
                base["like_rate"], base["comment_rate"], final_score, x["id"],
            ),
        )
    conn.commit()
