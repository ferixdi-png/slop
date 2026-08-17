import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from apify_client import ApifyClient

from config import (
    APIFY_CREATOR_ACTOR,
    APIFY_HASHTAG_ACTOR,
    APIFY_SEARCH_ACTOR,
    HASHTAGS,
    HASHTAG_LIMIT,
    RADAR_AI_ANALYZE_LIMIT,
    RADAR_KEEP_LIMIT,
    RADAR_MODEL,
    SEARCH_LIMIT,
    SEARCH_TERMS,
)
from db import db_conn
from gemini_service import classify_radar_video
from progress import set_radar_status
from radar_logs import (
    add_radar_log,
    get_radar_run_id,
    reset_radar_run_id,
    set_radar_run_id,
)
from radar_normalize import normalize_reel
from radar_service import (
    load_creator_stats,
    matches,
    refresh_recent_scores,
    run_actor_items,
    save_meta_report,
    save_post,
    update_creator_baselines,
)
from reel_media import download_reel_for_analysis


def _save_one(item, assessment=None):
    """One tiny SQLite write transaction. Never keep DB open during network/API work."""
    with db_conn() as conn:
        save_post(conn, item, assessment)
        conn.commit()


def _assessment_details(assessment):
    if assessment is None:
        return {}
    return {
        "is_russian": bool(getattr(assessment, "is_russian", False)),
        "is_ai_video": bool(getattr(assessment, "is_ai_video", False)),
        "is_comedy_scene": bool(getattr(assessment, "is_comedy_scene", False)),
        "is_tutorial_or_review": bool(getattr(assessment, "is_tutorial_or_review", False)),
        "is_talking_head": bool(getattr(assessment, "is_talking_head", False)),
        "simple_situation": bool(getattr(assessment, "simple_situation", False)),
        "reproducible_format": bool(getattr(assessment, "reproducible_format", False)),
        "characters_count": int(getattr(assessment, "characters_count", 0) or 0),
        "reason": str(getattr(assessment, "reason", "") or "")[:500],
    }


def _run_source(token, run_id, actor_id, run_input):
    """Run one Apify source in its own thread/client while preserving Render runId logs."""
    context_token = set_radar_run_id(run_id)
    try:
        client = ApifyClient(token)
        return run_actor_items(client, actor_id, run_input)
    finally:
        reset_radar_run_id(context_token)


def sync_radar_v2():
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        raise RuntimeError("Не задан APIFY_API_TOKEN")

    raw_items = []
    source_errors = 0
    warnings = []
    run_id = get_radar_run_id()

    add_radar_log(
        "Старт полного радара.",
        stage="pipeline",
        details={
            "search_actor": APIFY_SEARCH_ACTOR,
            "hashtag_actor": APIFY_HASHTAG_ACTOR,
            "creator_actor": APIFY_CREATOR_ACTOR,
            "hashtags": len(HASHTAGS),
            "ai_limit": RADAR_AI_ANALYZE_LIMIT,
            "top_limit": RADAR_KEEP_LIMIT,
            "source_mode": "parallel",
        },
    )

    set_radar_status(
        "running", "Подготавливаю поиск", 3, 600,
        "Читаю базу авторов и готовлю параллельный сбор Apify.",
    )

    with db_conn() as conn:
        tracked = [
            r[0] for r in conn.execute(
                "SELECT username FROM tracked_creators ORDER BY best_views_per_hour DESC LIMIT 100"
            ).fetchall()
        ]
    add_radar_log(f"В базе наблюдения {len(tracked)} авторов.", stage="creators")

    term = SEARCH_TERMS[0] if SEARCH_TERMS else ""
    source_specs = {
        "popular": (
            APIFY_SEARCH_ACTOR,
            {"search": term, "searchType": "popular", "searchLimit": SEARCH_LIMIT},
        ),
        "hashtags": (
            APIFY_HASHTAG_ACTOR,
            {"hashtags": HASHTAGS, "resultsType": "reels", "resultsLimit": HASHTAG_LIMIT},
        ),
    }
    if tracked:
        source_specs["creators"] = (
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

    set_radar_status(
        "running", "Собираю источники параллельно", 10, 260,
        f"Одновременно запускаю {len(source_specs)} источника: Popular Reels, хештеги"
        + (" и наблюдаемые авторы." if tracked else "."),
        details={"sources_total": len(source_specs), "sources_done": 0, "tracked_creators": len(tracked)},
    )
    add_radar_log(
        "Запускаю Apify-источники параллельно.",
        stage="sources",
        details={"sources": list(source_specs.keys())},
    )

    source_results = {}
    completed_sources = 0
    with ThreadPoolExecutor(max_workers=len(source_specs), thread_name_prefix="apify-source") as executor:
        future_map = {
            executor.submit(_run_source, token, run_id, actor_id, run_input): name
            for name, (actor_id, run_input) in source_specs.items()
        }
        for future in as_completed(future_map):
            name = future_map[future]
            completed_sources += 1
            try:
                rows = future.result() or []
                source_results[name] = rows
                add_radar_log(
                    f"Источник {name} завершён: {len(rows)} элементов.",
                    stage="sources",
                )
            except Exception as exc:
                source_errors += 1
                source_results[name] = []
                if name == "popular":
                    warning = f"Popular Reels недоступен — продолжаю по другим источникам: {str(exc)[:220]}"
                elif name == "hashtags":
                    warning = f"Hashtag Scraper временно недоступен: {str(exc)[:220]}"
                else:
                    warning = f"Мониторинг авторов временно недоступен: {str(exc)[:220]}"
                warnings.append(warning)
                add_radar_log(warning, level="WARN", stage="sources")

            progress = 10 + int(20 * completed_sources / max(1, len(source_specs)))
            set_radar_status(
                "running", "Собираю источники параллельно", progress, max(30, 260 - completed_sources * 60),
                f"Готово источников: {completed_sources}/{len(source_specs)}.",
                warning=" · ".join(warnings[-2:]),
                details={"sources_total": len(source_specs), "sources_done": completed_sources},
            )

    creator_rows = source_results.get("creators", [])
    if creator_rows:
        try:
            with db_conn() as conn:
                update_creator_baselines(conn, creator_rows)
            add_radar_log(
                f"История наблюдаемых авторов обновлена по {len(creator_rows)} строкам.",
                stage="creators",
            )
        except Exception as exc:
            warning = f"Не удалось обновить baseline наблюдаемых авторов: {str(exc)[:180]}"
            warnings.append(warning)
            add_radar_log(warning, level="WARN", stage="creators")

    for x in source_results.get("popular", []):
        x.setdefault("searchTerm", x.get("searchTerm") or "ключевой поиск")
        raw_items.append((x, "Popular Reels"))
    for x in source_results.get("hashtags", []):
        raw_items.append((x, f"хештег: {x.get('hashtag') or x.get('searchTerm') or ''}"))
    raw_items.extend((x, "наблюдаемый автор") for x in creator_rows)

    set_radar_status(
        "running", "Фильтрую кандидатов", 34, 420,
        f"Получено {len(raw_items)} сырых записей. Оставляю последние 7 дней, длительность ≤10 секунд и минимальный сигнал охватов.",
        warning=" · ".join(warnings[-2:]),
        details={"raw": len(raw_items)},
    )

    with db_conn() as conn:
        creator_stats = load_creator_stats(conn)

    unique = {}
    rejected_numeric = 0
    for raw, source in raw_items:
        item = normalize_reel(raw, source, creator_stats)
        if not item:
            rejected_numeric += 1
            continue
        if (
            item["post_url"] not in unique
            or item["viral_score_v2"] > unique[item["post_url"]]["viral_score_v2"]
        ):
            unique[item["post_url"]] = item

    candidates = sorted(
        unique.values(),
        key=lambda x: (x["viral_score_v2"], x["views_per_hour"], x["views"]),
        reverse=True,
    )[:RADAR_AI_ANALYZE_LIMIT]

    add_radar_log(
        "Числовой фильтр завершён.",
        stage="filter",
        details={
            "raw": len(raw_items),
            "rejected_or_invalid": rejected_numeric,
            "unique_under_10s_last_7d": len(unique),
            "sent_to_ai": len(candidates),
        },
    )

    if candidates:
        with db_conn() as conn:
            for item in candidates:
                save_post(conn, item, None)
            conn.commit()
        add_radar_log(
            f"{len(candidates)} кандидатов записаны в базу до AI-проверки.",
            stage="database",
        )

    total = len(candidates)
    set_radar_status(
        "running", "Начинаю AI-проверку видео", 40, max(90, total * 10 + 60),
        f"После числового фильтра осталось {len(unique)}. Gemini проверит только {total} сильнейших кандидатов.",
        warning=" · ".join(warnings[-2:]),
        details={"raw": len(raw_items), "numeric_candidates": len(unique), "ai_total": total, "ai_done": 0},
    )

    checked = matched = errors = 0
    for index, item in enumerate(candidates, start=1):
        progress = 40 + int(44 * (index - 1) / max(1, total))
        remaining = max(0, total - index + 1)
        set_radar_status(
            "running",
            f"AI-проверка роликов {index}/{total}",
            progress,
            max(40, remaining * 10 + 45),
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

        assessment = None
        tmp = None
        creator = item.get("creator", "")
        add_radar_log(
            f"AI {index}/{total}: начинаю проверку @{creator}.",
            stage="gemini-radar",
            details={
                "views": item.get("views"),
                "likes": item.get("likes"),
                "comments": item.get("comments"),
                "duration_sec": item.get("duration_sec"),
                "viral_score": item.get("viral_score_v2"),
                "post_url": item.get("post_url"),
            },
        )
        try:
            tmp, refreshed_duration = download_reel_for_analysis(item)
            if refreshed_duration and 0 < float(refreshed_duration) <= 10.05:
                item["duration_sec"] = float(refreshed_duration)
            add_radar_log(
                f"AI {index}/{total}: MP4 готов, отправляю @{creator} в {RADAR_MODEL}.",
                stage="gemini-radar",
                details={"duration_sec": item.get("duration_sec")},
            )
            assessment = classify_radar_video(tmp, item["caption"])
            checked += 1
            passed = matches(assessment)
            if passed:
                matched += 1
            add_radar_log(
                f"AI {index}/{total}: {'PASS' if passed else 'REJECT'} @{creator} — {str(getattr(assessment, 'reason', '') or '')[:260]}",
                level="INFO" if passed else "WARN",
                stage="gemini-radar",
                details=_assessment_details(assessment),
            )
            _save_one(item, assessment)
        except Exception as exc:
            errors += 1
            add_radar_log(
                f"AI {index}/{total}: ERROR @{creator}: {exc}",
                level="ERROR",
                stage="gemini-radar",
            )
            try:
                _save_one(item, None)
            except Exception as db_exc:
                warning = f"Не удалось сохранить @{creator}: {str(db_exc)[:120]}"
                warnings.append(warning)
                add_radar_log(warning, level="ERROR", stage="database")
            warnings.append(f"AI-проверка @{creator} не завершена: {str(exc)[:100]}")
        finally:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    set_radar_status(
        "running", "Обновляю рейтинги и аномалии", 87, 55,
        f"AI-проверка завершена: просмотрено {checked}, подошло {matched}. Пересчитываю рейтинги по уже накопленной истории авторов.",
        warning=" · ".join(warnings[-2:]),
        details={"ai_done": checked, "matched": matched, "errors": errors},
    )
    add_radar_log(
        "AI-фильтр завершён.",
        stage="gemini-radar",
        details={"checked": checked, "matched": matched, "errors": errors},
    )

    # Do not launch another potentially long creator Actor here. Existing tracked
    # creators were refreshed in parallel at the start; newly discovered creators
    # receive a stable baseline on the next radar run. This keeps one run bounded.
    try:
        with db_conn() as conn:
            refresh_recent_scores(conn)
        add_radar_log("Рейтинги пересчитаны по доступной истории авторов.", stage="baselines")
    except Exception as exc:
        warning = f"Часть аномалий автора будет доступна на следующем запуске: {str(exc)[:180]}"
        warnings.append(warning)
        add_radar_log(warning, level="WARN", stage="baselines")

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
    top_rows = [dict(row) for row in top_rows]

    try:
        if top_rows:
            add_radar_log(
                f"Строю мету недели по {len(top_rows)} AI-подтверждённым роликам.",
                stage="meta",
            )
            with db_conn() as conn:
                save_meta_report(conn, top_rows)
                conn.commit()
            add_radar_log("Мета недели сохранена.", stage="meta")
        else:
            add_radar_log("Мета недели пропущена: AI-подтверждённый TOP пуст.", level="WARN", stage="meta")
    except Exception as exc:
        errors += 1
        warning = f"Мета недели не собрана: {str(exc)[:180]}"
        warnings.append(warning)
        add_radar_log(warning, level="ERROR", stage="meta")

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
    add_radar_log("Radar pipeline DONE.", stage="done", details=result)
    return result
