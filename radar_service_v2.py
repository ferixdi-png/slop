import json
import os

from apify_client import ApifyClient

from config import (
    APIFY_CREATOR_ACTOR,
    APIFY_HASHTAG_ACTOR,
    APIFY_SEARCH_ACTOR,
    HASHTAGS,
    HASHTAG_LIMIT,
    RADAR_AI_ANALYZE_LIMIT,
    RADAR_KEEP_LIMIT,
    SEARCH_LIMIT,
    SEARCH_TERMS,
)
from db import db_conn
from gemini_service import classify_radar_video
from progress import set_radar_status
from radar_normalize import normalize_reel
from radar_service import (
    download_temp_video,
    load_creator_stats,
    matches,
    refresh_recent_scores,
    run_actor_items,
    save_meta_report,
    save_post,
    update_creator_baselines,
)


def sync_radar_v2():
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        raise RuntimeError("Не задан APIFY_API_TOKEN")

    client = ApifyClient(token)
    raw_items = []
    source_errors = 0
    warnings = []

    set_radar_status(
        "running", "Подготавливаю поиск", 3, 360,
        "Проверяю источники и базу уже найденных авторов.",
    )

    with db_conn() as conn:
        tracked = [
            r[0] for r in conn.execute(
                "SELECT username FROM tracked_creators ORDER BY best_views_per_hour DESC LIMIT 100"
            ).fetchall()
        ]

    creator_rows = []
    if tracked:
        set_radar_status(
            "running", "Проверяю сильных авторов", 8, 330,
            f"Смотрю свежие Reels у {len(tracked)} уже найденных авторов.",
            details={"tracked_creators": len(tracked)},
        )
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
            warnings.append("Мониторинг авторов временно недоступен")

    set_radar_status(
        "running", "Ищу Popular Reels", 15, 300,
        "Один общий поиск по всем ключевым фразам. Если Instagram блокирует этот источник, радар продолжит по хештегам.",
    )
    try:
        term = SEARCH_TERMS[0] if SEARCH_TERMS else ""
        rows = run_actor_items(
            client,
            APIFY_SEARCH_ACTOR,
            {"search": term, "searchType": "popular", "searchLimit": SEARCH_LIMIT},
        )
        for x in rows:
            x.setdefault("searchTerm", x.get("searchTerm") or "ключевой поиск")
            raw_items.append((x, "Popular Reels"))
    except Exception:
        source_errors += 1
        warnings.append("Popular Reels заблокирован Instagram — продолжаю по хештегам")
        set_radar_status(
            "running", "Popular Reels недоступен — продолжаю", 22, 280,
            "Instagram заблокировал поисковый источник. Это не ошибка API-ключа. Перехожу к хештегам.",
            warning=warnings[-1],
        )

    set_radar_status(
        "running", "Собираю Reels по хештегам", 27, 250,
        f"Проверяю {len(HASHTAGS)} хештегов и собираю сырые кандидаты.",
        warning=" · ".join(warnings[-2:]),
    )
    try:
        rows = run_actor_items(
            client,
            APIFY_HASHTAG_ACTOR,
            {"hashtags": HASHTAGS, "resultsType": "reels", "resultsLimit": HASHTAG_LIMIT},
        )
        raw_items.extend((x, f"хештег: {x.get('hashtag') or x.get('searchTerm') or ''}") for x in rows)
    except Exception:
        source_errors += 1
        warnings.append("Hashtag Scraper временно недоступен")

    set_radar_status(
        "running", "Фильтрую кандидатов", 36, 220,
        f"Получено {len(raw_items)} сырых записей. Оставляю только последние 7 дней и длительность до 10 секунд.",
        warning=" · ".join(warnings[-2:]),
        details={"raw": len(raw_items)},
    )

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

    total = len(candidates)
    set_radar_status(
        "running", "Начинаю AI-проверку видео", 42, max(60, total * 6 + 60),
        f"После числового фильтра осталось {len(unique)}. Детально посмотрю до {total} самых сильных роликов.",
        warning=" · ".join(warnings[-2:]),
        details={"raw": len(raw_items), "numeric_candidates": len(unique), "ai_total": total, "ai_done": 0},
    )

    checked = matched = errors = 0
    with db_conn() as conn:
        for index, item in enumerate(candidates, start=1):
            progress = 42 + int(40 * (index - 1) / max(1, total))
            remaining = max(0, total - index + 1)
            set_radar_status(
                "running",
                f"AI-проверка роликов {index}/{total}",
                progress,
                max(35, remaining * 6 + 45),
                "Проверяю русский язык, AI-природу, комедийную сценку, простой сюжет и возможность повторения.",
                warning=" · ".join(warnings[-2:]),
                details={
                    "raw": len(raw_items),
                    "numeric_candidates": len(unique),
                    "ai_total": total,
                    "ai_done": index - 1,
                    "matched": matched,
                },
            )
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
        conn.commit()

    set_radar_status(
        "running", "Считаю аномалии авторов", 86, 80,
        f"AI-проверка завершена: просмотрено {checked}, подошло {matched}. Собираю базовый уровень просмотров авторов.",
        warning=" · ".join(warnings[-2:]),
        details={"ai_done": checked, "matched": matched, "errors": errors},
    )

    with db_conn() as conn:
        need_baseline = [
            r[0] for r in conn.execute(
                """SELECT username FROM tracked_creators
                   WHERE sample_size=0
                   ORDER BY best_views_per_hour DESC LIMIT 50"""
            ).fetchall()
        ]

    if need_baseline:
        try:
            baseline_rows = run_actor_items(
                client,
                APIFY_CREATOR_ACTOR,
                {
                    "username": need_baseline,
                    "resultsLimit": 10,
                    "onlyPostsNewerThan": "30 days",
                    "skipPinnedPosts": True,
                    "includeTranscript": False,
                    "includeDownloadedVideo": False,
                },
            )
            with db_conn() as conn:
                update_creator_baselines(conn, baseline_rows)
                refresh_recent_scores(conn)
        except Exception:
            source_errors += 1
            warnings.append("Не для всех авторов удалось посчитать медиану")

    set_radar_status(
        "running", "Собираю мету недели", 95, 35,
        "Формирую TOP-30 и общие паттерны: персонажи, локации, хуки и комедийные механики.",
        warning=" · ".join(warnings[-2:]),
    )

    with db_conn() as conn:
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

    result = {
        "raw": len(raw_items),
        "after_numeric_filter": len(unique),
        "ai_checked": checked,
        "matched": matched,
        "errors": errors,
        "source_errors": source_errors,
        "kept": min(len(top_rows), RADAR_KEEP_LIMIT),
    }
    set_radar_status(
        "done", "Поиск завершён", 100, 0,
        f"Собрано {len(raw_items)} → после фильтра {len(unique)} → AI проверил {checked} → в TOP {len(top_rows)}.",
        warning=" · ".join(warnings[-2:]),
        details=result,
    )
    return result
