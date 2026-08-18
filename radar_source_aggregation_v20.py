"""Source-agnostic candidate aggregation for the durable radar.

V20 fixes a compatibility regression introduced when discovery source IDs became
more descriptive. The original v5 preparer only consumed three literal keys
(`popular`, `hashtags`, `creators`), so successful newer datasets could contain
hundreds of rows while the candidate queue incorrectly became empty.

This layer deliberately does not change discovery budgets, semantic screening,
ranking or production prompts. It only makes dataset -> raw candidate aggregation
independent of source names and adds invariants so a non-empty discovery cannot
silently finish as a successful zero-result run.
"""

from __future__ import annotations

from typing import Any

import radar_growth_v6 as growth
import radar_request_job as radar_job
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log

SOURCE_AGGREGATION_PROFILE = "source_aggregation_v20"

_APPLIED = False


def _source_kind(name: str) -> str:
    value = str(name or "").strip().lower()
    if "creator" in value or "author" in value or "account" in value:
        return "creator"
    if "popular" in value or "search" in value:
        return "popular"
    if "hashtag" in value or "tag" in value:
        return "hashtag"
    if "keyword" in value or "query" in value:
        return "keyword"
    return "generic"


def _source_label(name: str, kind: str, row: dict[str, Any]) -> str:
    if kind == "creator":
        return "tracked creator"
    if kind == "popular":
        return "popular discovery"
    if kind == "hashtag":
        return f"tag discovery: {row.get('hashtag') or row.get('hashtagName') or row.get('searchTerm') or ''}".strip()
    if kind == "keyword":
        return f"keyword discovery: {row.get('searchTerm') or row.get('keyword') or ''}".strip()
    return f"discovery source: {name}"


def flatten_source_results(source_results: dict[str, Any]):
    """Return every dictionary row regardless of the source key naming scheme."""
    raw_items: list[tuple[dict[str, Any], str]] = []
    creator_rows: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    non_mapping_rows = 0

    for name, rows in (source_results or {}).items():
        kind = _source_kind(name)
        count = 0
        for raw in rows or []:
            if not isinstance(raw, dict):
                non_mapping_rows += 1
                continue
            row = dict(raw)
            count += 1
            if kind == "creator":
                creator_rows.append(row)
            if kind == "popular" and not row.get("searchTerm"):
                row["searchTerm"] = "popular discovery"
            raw_items.append((row, _source_label(name, kind, row)))
        source_counts[str(name)] = count

    return raw_items, creator_rows, source_counts, non_mapping_rows


def _fail_aggregation(job, message: str, details: dict[str, Any]):
    job["phase"] = "failed"
    job["error"] = message
    job.setdefault("stats", {}).update(details)
    radar_job._persist(job)
    set_radar_status(
        "error",
        "Ошибка сборки очереди",
        31,
        0,
        message,
        warning="Discovery вернул данные, но они не дошли до очереди. Run остановлен вместо ложного успешного нуля.",
        details={"run_id": job.get("run_id"), **details},
    )
    add_radar_log(message, level="ERROR", stage="source-aggregation", details=details)
    return job


def prepare_source_agnostic(client, job):
    """Drop-in replacement for the original v5 prepare function."""
    source_results = radar_job._collect_source_rows(client, job)
    raw_items, creator_rows, source_counts, non_mapping_rows = flatten_source_results(source_results)

    claimed_dataset_rows = sum(
        int((source or {}).get("dataset_items_loaded") or 0)
        for source in (job.get("sources") or {}).values()
    )
    flattened_rows = len(raw_items)

    # This is the exact regression that produced 852 downloaded rows -> raw=0.
    # Never allow it to be represented as a successful empty search again.
    if claimed_dataset_rows > 0 and flattened_rows == 0:
        return _fail_aggregation(
            job,
            "SOURCE_AGGREGATION_INVARIANT: datasets содержат элементы, но агрегатор не получил ни одной записи.",
            {
                "dataset_rows_claimed": claimed_dataset_rows,
                "flattened_rows": flattened_rows,
                "source_counts": source_counts,
                "non_mapping_rows": non_mapping_rows,
                "source_aggregation_profile": SOURCE_AGGREGATION_PROFILE,
            },
        )

    if creator_rows:
        try:
            with db_conn() as conn:
                radar_job.update_creator_baselines(conn, creator_rows)
        except Exception as exc:
            job.setdefault("warnings", []).append(f"baseline: {str(exc)[:180]}")

    with db_conn() as conn:
        creator_stats = radar_job.load_creator_stats(conn)

    unique: dict[str, dict[str, Any]] = {}
    rejected = 0
    normalize_errors = 0
    first_normalize_error = ""

    for raw, source in raw_items:
        try:
            item = radar_job.normalize_reel(raw, source, creator_stats)
        except Exception as exc:
            rejected += 1
            normalize_errors += 1
            if not first_normalize_error:
                first_normalize_error = str(exc)[:240]
            continue
        if not item:
            rejected += 1
            continue
        current = unique.get(item["post_url"])
        if current is None or item["viral_score_v2"] > current["viral_score_v2"]:
            unique[item["post_url"]] = item

    # A schema-level normalization crash across the whole dataset is a software
    # failure, not a legitimate zero trend result.
    if raw_items and normalize_errors == len(raw_items):
        return _fail_aggregation(
            job,
            "NORMALIZATION_INVARIANT: каждая найденная запись завершилась ошибкой нормализации.",
            {
                "raw": len(raw_items),
                "normalize_errors": normalize_errors,
                "first_normalize_error": first_normalize_error,
                "source_counts": source_counts,
                "source_aggregation_profile": SOURCE_AGGREGATION_PROFILE,
            },
        )

    candidates = sorted(
        unique.values(),
        key=lambda x: (x["viral_score_v2"], x["views_per_hour"], x["views"]),
        reverse=True,
    )[: radar_job.RADAR_AI_ANALYZE_LIMIT]

    with db_conn() as conn:
        for item in candidates:
            previous = conn.execute(
                "SELECT ai_checked,ai_match FROM radar_posts WHERE post_url=?",
                (item["post_url"],),
            ).fetchone()
            item["ai_done"] = bool(previous and int(previous["ai_checked"] or 0) == 1)
            item["ai_match"] = bool(previous and int(previous["ai_match"] or 0) == 1)
            item["ai_attempts"] = 0
            item["ai_error"] = ""
            radar_job.save_post_preserve_ai(conn, item, None)
        conn.commit()

    job["candidates"] = candidates
    job["stats"] = {
        **dict(job.get("stats") or {}),
        "raw": len(raw_items),
        "dataset_rows_claimed": claimed_dataset_rows,
        "source_counts": source_counts,
        "non_mapping_rows": non_mapping_rows,
        "rejected_or_invalid": rejected,
        "normalize_errors": normalize_errors,
        "numeric_candidates": len(unique),
        "ai_total": len(candidates),
        "source_aggregation_profile": SOURCE_AGGREGATION_PROFILE,
    }
    job["phase"] = "ai" if candidates else "finalizing"
    radar_job._persist(job)

    try:
        radar_job.save_radar_snapshot()
    except Exception as exc:
        add_radar_log(
            f"Snapshot кандидатов не сохранён: {exc}",
            level="WARN",
            stage="snapshot",
        )

    cached = sum(1 for item in candidates if item.get("ai_done"))
    add_radar_log(
        "SOURCE AGGREGATION v20: все успешные discovery datasets объединены в единую очередь.",
        stage="filter",
        details={
            "dataset_rows_claimed": claimed_dataset_rows,
            "raw": len(raw_items),
            "unique": len(unique),
            "rejected_or_invalid": rejected,
            "normalize_errors": normalize_errors,
            "ai_total": len(candidates),
            "already_checked": cached,
            "source_counts": source_counts,
            "profile": SOURCE_AGGREGATION_PROFILE,
        },
    )
    set_radar_status(
        "running",
        "Кандидаты готовы",
        36,
        max(60, max(0, len(candidates) - cached) * 14),
        f"Получено {len(raw_items)} записей → {len(unique)} валидных коротких кандидатов → смысловая очередь {len(candidates)}. Уже проверено ранее: {cached}.",
        warning=" · ".join(job.get("warnings") or []),
        details={
            "raw": len(raw_items),
            "dataset_rows_claimed": claimed_dataset_rows,
            "source_counts": source_counts,
            "rejected_or_invalid": rejected,
            "normalize_errors": normalize_errors,
            "numeric_candidates": len(unique),
            "ai_total": len(candidates),
            "ai_done": cached,
            "run_id": job.get("run_id"),
            "source_aggregation_profile": SOURCE_AGGREGATION_PROFILE,
        },
    )
    return job


def apply_source_aggregation_v20():
    global _APPLIED
    if _APPLIED:
        return {"source_aggregation_profile": SOURCE_AGGREGATION_PROFILE}
    _APPLIED = True

    # The wrapper chain is hardening -> growth._prepare_candidates_v6 ->
    # growth._ORIGINAL_PREPARE. Replacing this final base keeps all existing v19
    # cache invalidation, retry guards and profile migration intact.
    growth._ORIGINAL_PREPARE = prepare_source_agnostic

    add_radar_log(
        "SOURCE AGGREGATION v20 READY: preparer больше не зависит от literal source IDs; non-empty datasets не могут тихо превратиться в raw=0.",
        stage="startup",
        details={"source_aggregation_profile": SOURCE_AGGREGATION_PROFILE},
    )
    return {"source_aggregation_profile": SOURCE_AGGREGATION_PROFILE}
