import os

from apify_client import ApifyClient

from actor_utils import run_actor_group_checked
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
from progress import set_radar_status
from radar_logs import add_radar_log
from radar_normalize import normalize_reel
from radar_service import (
    load_creator_stats,
    matches,
    refresh_recent_scores,
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


def sync_radar_v2():
    token = os.environ.get("APIFY_API_TOKEN")
    if not token:
        raise RuntimeError("Не задан APIFY_API_TOKEN")

    client = ApifyClient(token)
    raw_items = []
    source_errors = 0
    warnings = []

    add_radar_log(
        "Старт полного радара в low-memory режиме Starter 512 MB.",
        stage="pipeline",
        details={
            "search_actor": APIFY_SEARCH_ACTOR,
            "hashtag_actor": APIFY_HASHTAG_ACTOR,
            "creator_actor": APIFY_CREATOR_ACTOR,
            "hashtags": len(HASHTAGS),
            "ai_limit": RADAR_AI_ANALYZE_LIMIT,
            "top_limit": RADAR_KEEP_LIMIT,
            "source_mode": "remote_parallel_single_local_poll",
        },
    )

    set_radar_status(
        "running", "Подготавливаю поиск", 3, 600,
        "Читаю базу авторов и готовлю Apify. Actor-ы работают на стороне Apify; Render только опрашивает их одним лёгким циклом.",
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
        "running", "Запускаю источники Apify", 10, 260,
        f"Запускаю {len(source_specs)} Actor на стороне Apify без локальных Python worker-потоков.",
        details={"sources_total": len(source_specs), "sources_done": 0, "tracked_creators": len(tracked)},
    )
    add_radar_log(
        "Стартую Actor-ы удалённо; локальная параллельность отключена для экономии RAM.",
        stage="sources",
        details={"sources": list(source_specs.keys())},
    )

    source_results, source_failures = run_actor_group_checked(client, source_specs)
    source_errors = len(source_failures)

    for name, error_text in source_failures.items():
        if name == "popular":
            warning = f"Popular Reels недоступен — продолжаю по другим источникам: {str(error_text)[:220]}"
        elif name == "hashtags":
            warning = f"Hashtag Scraper временно недоступен: {str(error_text)[:220]}"
        else:
            warning = f"Мониторинг авторов временно недоступен: {str(error_text)[:220]}"
        warnings.append(warning)
        add_radar_log(warning, level="WARN", stage="sources")

    for name, rows in source_results.items():
        if name not in source_failures:
            add_radar_log(
                f"Источник {name} завершён: {len(rows or [])} элементов.",
                stage="sources",
            )

    set_radar_status(
        "running", "Источники Apify собраны", 30, 45,
        f"Завершено источников: {len(source_results)}/{len(source_specs)}. Перехожу к фильтрации.",
        warning=" · ".join(warnings[-2:]),
        details={
            "sources_total": len(source_specs),
            "sources_done": len(source_results),
            "source_errors": source_errors,
        },
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

    raw_count = len(raw_items)
    numeric_count = len(unique)
    add_radar_log(
        "Числовой фильтр завершён.",
        stage="filter",
        details={
            "raw": raw_count,
            "rejected_or_invalid": rejected_numeric,
            "unique_under_10s_last_7d": numeric_count,
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

    # Free the bulky Apify payload before importing the Gemini SDK. On a Starter
    # instance this keeps peak RSS well below the 512 MB limit.
    del raw_items
    del source_results
    del creator_rows
    del creator_stats
    del unique

    total = len(candidates)
    set_radar_status(
        "running", "Начинаю AI-проверку видео", 40, max(90, total * 10 + 60),
        f"После числового фильтра осталось {numeric_count}. Gemini проверит только {total} сильнейших кандидатов.",
        warning=" · ".join(warnings[-2:]),
        details={"raw": raw_count, "numeric_candidates": numeric_count, "ai_total": total, "ai_done": 0},
    )

    classify_radar_video = None
    if total:
        add_radar_log(
            f"Загружаю Gemini runtime только сейчас, после освобождения Apify payload. Модель: {RADAR_MODEL}.",
            stage="gemini-radar",
        )
        from gemini_service import classify_radar_video as _classify_radar_video
        classify_radar_video = _classify_radar_video

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
                "raw": raw_count,
                "numeric_candidates": numeric_count,
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
        "raw": raw_count,
        "after_numeric_filter": numeric_count,
        "ai_checked": checked,
        "matched": matched,
        "errors": errors,
        "source_errors": source_errors,
        "kept": min(len(top_rows), RADAR_KEEP_LIMIT),
    }
    set_radar_status(
        "done", "Поиск завершён", 100, 0,
        f"Собрано {raw_count} → после фильтра {numeric_count} → AI проверил {checked} → в TOP {len(top_rows)}.",
        warning=" · ".join(warnings[-2:]),
        details=result,
    )
    add_radar_log("Radar pipeline DONE.", stage="done", details=result)
    return result
