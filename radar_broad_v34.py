"""V34 broad trend-pool overlay.

Product goal: surface 50-100 genuinely fast-growing short-video candidates and let
Gemini enrich/adapt them instead of acting as a destructive semantic gate.

Hard boundaries stay hard:
- current three platforms and five post-level hashtags;
- 14-day freshness window;
- the V29/V30 paid discovery caps (<$5 target);
- invalid/non-video duration and confirmed static/slideshow media can still be
  excluded from the primary pool.

Everything else is soft metadata. No speech -> add Russian speech. Timing mismatch
-> rewrite/compress. Informational/talking-head -> repackage the mechanic. AI/media
failure -> keep the momentum candidate for manual review instead of hiding it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import cloud_state
import radar_edge_v19 as edge
import radar_multiplatform_v28 as v28
import radar_quality
import radar_request_job as radar_job
import radar_service
from db import db_conn
from progress import set_radar_status
from radar_logs import add_radar_log

MODE_VERSION = "multiplatform_broad_v34_trendpool100"
# IMPORTANT: keep the semantic persistence identity equal to the current V30
# profile. Changing it would reset an already-paid active discovery job.
SCREENING_PROFILE = "multiplatform_speech_v30_audit10_budget5"
OUTPUT_LIMIT = 100
CANDIDATE_API_LIMIT = 120
AI_ENRICH_LIMIT = 20
TARGET_OUTPUT_MIN = 50
TARGET_OUTPUT_MAX = 100

HARD_REJECT_PREFIXES = (
    "DURATION_GATE",
    "REJECT_STATIC_IMAGE",
    "STATIC_OR_SLIDESHOW",
    "MOTION_GATE_UNAVAILABLE",
)

_APPLIED = False
_BASE_PREPARE = None
_BASE_PROCESS_AI = None


def _hard_reject_reason(reason: str) -> bool:
    text = str(reason or "").strip().upper()
    return any(text.startswith(prefix) for prefix in HARD_REJECT_PREFIXES)


def _duration_ok(row) -> bool:
    duration = float((row or {}).get("duration_sec") or 0)
    # Unknown metadata duration is useful discovery evidence and can be checked
    # manually/on-demand later. A known duration must stay in the established
    # source envelope.
    return duration <= 0 or (1.0 <= duration <= v28.SOURCE_MAX_DURATION_SEC)


def broad_eligible(row) -> bool:
    row = row or {}
    term = str(row.get("search_term") or "").strip().lower()
    platform = str(row.get("platform") or "")
    if term not in v28.TARGET_SET or platform not in v28.PLATFORM_SET:
        return False
    if not _duration_ok(row):
        return False
    if _hard_reject_reason(row.get("reason") or ""):
        return False
    return True


def broad_matches(assessment) -> bool:
    """Gemini is an annotator in V34; only objective media-invalid reasons reject."""
    if assessment is None:
        return True
    return not _hard_reject_reason(getattr(assessment, "reason", "") or "")


def adaptation_fields(row) -> dict:
    row = row or {}
    checked = bool(row.get("ai_checked"))
    reason = str(row.get("reason") or "")
    error = str(row.get("ai_error") or "")
    duration = float(row.get("duration_sec") or 0)
    upper = reason.upper()

    if error:
        status = "AI_UNVERIFIED"
        label = "⚪ AI не проверил — посмотреть вручную"
        advice = "Сильный числовой сигнал сохранён. Открой оригинал и реши механику вручную."
        manual = True
    elif not checked:
        status = "NOT_ENRICHED"
        label = "⚪ Ещё без AI-разметки"
        advice = "Ролик уже в тренд-пуле по скорости/просмотрам. Gemini-разметка не обязательна для попадания в TOP."
        manual = True
    elif upper.startswith(v28.PASS_PREFIX):
        status = "REPEAT_CORE"
        label = "🟢 Можно повторять механику"
        advice = "Речь и тайминг уже подходят; адаптируй персонажей/реплики под свой ролик."
        manual = False
    elif upper.startswith("NO_SPOKEN_DIALOGUE"):
        status = "ADD_RUSSIAN_SPEECH"
        label = "🟡 Добавить русскую речь"
        advice = "Сохрани визуальный хук и действие, поверх добавь короткую русскую реплику/реакцию."
        manual = False
    elif upper.startswith("TIMING_OR_MECHANIC_REJECT"):
        status = "REBUILD_TO_10S"
        label = "🟡 Адаптировать под 10 секунд"
        advice = "Сохрани главный хук/payoff, убери паузы и перепиши реплики под естественные 10 секунд."
        manual = False
    elif upper.startswith("TUTORIAL_OR_REVIEW") or upper.startswith("INFORMATION_TALKING_HEAD"):
        status = "REPACKAGE_MECHANIC"
        label = "🟡 Переупаковать механику"
        advice = "Используй вирусный визуальный/сюжетный паттерн, но преврати объяснение в короткую сцену или реакцию."
        manual = False
    else:
        status = "ADAPT_FREELY"
        label = "🟡 Свободная адаптация"
        advice = "Метрики сильнее семантического вердикта: сохрани хук и перестрой содержание под свой 10-секундный формат."
        manual = False

    if duration > v28.DIRECT_MAX_DURATION_SEC:
        advice += " Исходник длиннее 10 сек: оставь только ключевой setup → действие → payoff."

    return {
        "trend_pool": True,
        "ai_role": "enrichment_only",
        "speech_required_for_top": False,
        "adaptation_status": status,
        "adaptation_label": label,
        "adaptation_advice": advice,
        "manual_review_recommended": manual,
        "requires_compression": bool(duration > v28.DIRECT_MAX_DURATION_SEC),
        "target_duration_sec": v28.COMPRESSED_TARGET_SEC if duration > v28.DIRECT_MAX_DURATION_SEC else (round(duration, 2) if duration > 0 else 10.0),
    }


def _current_job_urls() -> set[str]:
    try:
        job = cloud_state.load_radar_job() or {}
    except Exception:
        return set()
    return {
        str(item.get("post_url") or "")
        for item in (job.get("candidates") or [])
        if item.get("post_url")
    }


def _promote_db_candidates(candidates) -> int:
    """Make every current numeric candidate analyzable without pretending AI checked it."""
    urls = [
        str(item.get("post_url") or "")
        for item in (candidates or [])
        if item.get("post_url") and broad_eligible(item)
    ]
    if not urls:
        return 0
    with db_conn() as conn:
        for url in urls:
            conn.execute(
                "UPDATE radar_posts SET ai_match=1,screening_profile=? WHERE post_url=?",
                (SCREENING_PROFILE, url),
            )
        conn.commit()
    return len(urls)


def _promote_active_job_at_startup() -> int:
    try:
        job = cloud_state.load_radar_job() or {}
    except Exception:
        return 0
    candidates = job.get("candidates") or []
    if not candidates:
        return 0
    changed = 0
    for item in candidates:
        if not broad_eligible(item):
            continue
        if not item.get("ai_match"):
            item["ai_match"] = True
            item["trend_pool"] = True
            changed += 1
    if changed:
        radar_job._persist(job)
    _promote_db_candidates(candidates)
    return changed


def prepare_candidates_v34(client, job):
    job = _BASE_PREPARE(client, job)
    candidates = job.get("candidates") or []
    promoted = 0
    for item in candidates:
        if broad_eligible(item):
            item["ai_match"] = True
            item["trend_pool"] = True
            item["ai_role"] = "enrichment_only"
            promoted += 1
    _promote_db_candidates(candidates)
    stats = dict(job.get("stats") or {})
    stats["trend_pool_candidates"] = promoted
    stats["target_output_min"] = TARGET_OUTPUT_MIN
    stats["target_output_max"] = TARGET_OUTPUT_MAX
    stats["ai_enrich_limit"] = AI_ENRICH_LIMIT
    job["stats"] = stats
    radar_job._persist(job)
    set_radar_status(
        "running",
        "Тренд-пул готов — Gemini добавляет метки",
        40,
        max(20, min(AI_ENRICH_LIMIT, len(candidates)) * 12),
        f"Сильных вариантов: {promoted}. Они уже доступны в TOP; Gemini проверит только до {AI_ENRICH_LIMIT} лидеров и ничего не удаляет за отсутствие речи.",
        details={
            "raw": stats.get("raw", 0),
            "numeric_candidates": stats.get("numeric_candidates", 0),
            "trend_pool": promoted,
            "ai_total": min(AI_ENRICH_LIMIT, len(candidates)),
            "ai_done": 0,
            "ai_role": "enrichment_only",
            "speech_required": False,
            "run_id": job.get("run_id"),
        },
    )
    return job


def _soft_promote_job_items(job):
    changed = False
    for item in job.get("candidates") or []:
        if not broad_eligible(item):
            continue
        if not item.get("ai_match"):
            item["ai_match"] = True
            changed = True
        if item.get("ai_error") and not item.get("ai_done"):
            # A missing/expired media URL or temporary Gemini problem is not a reason
            # to retry a strong trend candidate three times. Keep it and move on.
            item["ai_done"] = True
            item["ai_unverified"] = True
            item["trend_pool"] = True
            changed = True
    if changed:
        radar_job._persist(job)
    return job


def _finish_enrichment_if_ready(job):
    candidates = job.get("candidates") or []
    enriched = sum(1 for item in candidates if item.get("ai_done") and not item.get("ai_skipped_broad"))
    if enriched < min(AI_ENRICH_LIMIT, len(candidates)):
        return False
    for item in candidates:
        if not item.get("ai_done"):
            item["ai_done"] = True
            item["ai_match"] = bool(broad_eligible(item))
            item["ai_skipped_broad"] = True
            item["trend_pool"] = bool(item["ai_match"])
    job["phase"] = "finalizing"
    radar_job._persist(job)
    return True


def process_one_ai_v34(job):
    job = _soft_promote_job_items(job)
    if _finish_enrichment_if_ready(job):
        return job

    before = radar_job._next_ai_index(job)
    job = _BASE_PROCESS_AI(job)
    candidates = job.get("candidates") or []
    if before is not None and before < len(candidates):
        item = candidates[before]
        if broad_eligible(item):
            item["ai_match"] = True
            item["trend_pool"] = True
            if item.get("ai_error"):
                item["ai_done"] = True
                item["ai_unverified"] = True
                add_radar_log(
                    f"V34 SOFT AI ERROR: {item.get('platform','')} @{item.get('creator','')} сохранён в TOP без повторных попыток.",
                    level="WARN",
                    stage="v34-enrichment",
                    details={"post_url": item.get("post_url"), "error": str(item.get("ai_error") or "")[:300]},
                )
        radar_job._persist(job)

    _finish_enrichment_if_ready(job)
    done = sum(1 for x in candidates if x.get("ai_done") and not x.get("ai_skipped_broad"))
    available = sum(1 for x in candidates if broad_eligible(x))
    set_radar_status(
        "running" if job.get("phase") != "finalizing" else "running",
        "Gemini размечает лучшие варианты" if job.get("phase") != "finalizing" else "AI-разметка закончена",
        45 + int(35 * min(done, AI_ENRICH_LIMIT) / max(1, min(AI_ENRICH_LIMIT, len(candidates)))),
        max(10, (min(AI_ENRICH_LIMIT, len(candidates)) - done) * 12),
        f"В тренд-пуле уже {available}. Gemini-разметка {done}/{min(AI_ENRICH_LIMIT, len(candidates))}; отсутствие речи или timing reject больше не удаляет ролик.",
        details={
            "raw": (job.get("stats") or {}).get("raw", 0),
            "numeric_candidates": (job.get("stats") or {}).get("numeric_candidates", 0),
            "trend_pool": available,
            "ai_total": min(AI_ENRICH_LIMIT, len(candidates)),
            "ai_done": done,
            "matched": available,
            "speech_required": False,
            "ai_role": "enrichment_only",
            "run_id": job.get("run_id"),
        },
    )
    return job


def _query_broad_rows(limit=OUTPUT_LIMIT):
    placeholders = ",".join("?" for _ in v28.TARGET_TAGS)
    with db_conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM radar_posts
                WHERE datetime(published_at)>=datetime('now','-{v28.LOOKBACK_DAYS} days')
                  AND LOWER(COALESCE(search_term,'')) IN ({placeholders})
                ORDER BY viral_score_v2 DESC,views_per_hour DESC,views DESC
                LIMIT 400""",
            v28.TARGET_TAGS,
        ).fetchall()
    out = []
    for raw in rows:
        x = dict(raw)
        if not broad_eligible(x):
            continue
        try:
            x["characters"] = json.loads(x.get("characters_json") or "[]")
        except Exception:
            x["characters"] = []
        x.update(radar_quality.recommendation_status_for_row(x))
        x.update(adaptation_fields(x))
        out.append(x)
        if len(out) >= limit:
            break
    return out


def finalize_v34(job):
    candidates = job.get("candidates") or []
    available = sum(1 for x in candidates if broad_eligible(x))
    _promote_db_candidates(candidates)
    top_rows = _query_broad_rows(OUTPUT_LIMIT)
    result = {
        "raw": (job.get("stats") or {}).get("raw", 0),
        "after_numeric_filter": (job.get("stats") or {}).get("numeric_candidates", 0),
        "trend_pool": available,
        "ai_enriched": sum(1 for x in candidates if x.get("ai_done") and not x.get("ai_skipped_broad")),
        "ai_unverified": sum(1 for x in candidates if x.get("ai_unverified")),
        "kept": len(top_rows),
        "target_output_min": TARGET_OUTPUT_MIN,
        "target_output_max": TARGET_OUTPUT_MAX,
        "source_errors": len(job.get("source_failures") or {}),
        "lookback_days": v28.LOOKBACK_DAYS,
        "ai_role": "enrichment_only",
        "speech_required": False,
    }
    job["phase"] = "done"
    job["completed_at"] = radar_job._now_iso()
    job["result"] = result
    job["error"] = ""
    job["current_ai_index"] = None
    job["current_ai_post_url"] = ""
    radar_job._persist(job)
    try:
        radar_job.save_radar_snapshot()
    except Exception as exc:
        add_radar_log(f"V34 финальный snapshot не сохранён: {exc}", level="WARN", stage="snapshot")

    set_radar_status(
        "done",
        "Поиск завершён — широкий тренд-пул готов",
        100,
        0,
        f"Собрано {result['raw']} → сильных кандидатов {result['after_numeric_filter']} → в выдаче {len(top_rows)}. Gemini теперь только помогает адаптировать, а не отсекает.",
        details=result,
    )
    add_radar_log("V34 BROAD TREND POOL DONE.", stage="done", details=result)
    return job


def _remove_v28_api_interceptor(app):
    funcs = list((app.before_request_funcs or {}).get(None, []))
    app.before_request_funcs[None] = [
        fn for fn in funcs if getattr(fn, "__name__", "") != "v28_api_views"
    ]


def _install_broad_api(app):
    if getattr(app, "_v34_broad_api", False):
        return
    from flask import jsonify, request

    @app.before_request
    def v34_broad_api():
        if request.method != "GET":
            return None
        if request.path == "/api/radar":
            return jsonify(_query_broad_rows(OUTPUT_LIMIT))
        if request.path == "/api/radar/candidates":
            return jsonify(_query_broad_rows(CANDIDATE_API_LIMIT))
        return None

    @app.after_request
    def v34_broad_response(response):
        if not response.is_json:
            return response
        data = response.get_json(silent=True)
        if request.path in {"/api/status", "/api/radar/status", "/health"} and isinstance(data, dict):
            fields = {
                "radar_product_mode": MODE_VERSION,
                "radar_target_output_min": TARGET_OUTPUT_MIN,
                "radar_target_output_max": TARGET_OUTPUT_MAX,
                "radar_ai_enrich_limit": AI_ENRICH_LIMIT,
                "radar_ai_role": "enrichment_only",
                "radar_speech_required": False,
                "radar_no_speech_policy": "keep_and_add_russian_speech",
                "radar_timing_reject_policy": "keep_and_rewrite_to_10s",
                "radar_ai_error_policy": "keep_for_manual_review",
            }
            if request.path == "/api/radar/status":
                details = dict(data.get("details") or {})
                details.update(fields)
                data["details"] = details
            else:
                data.update(fields)
            response.set_data(app.json.dumps(data))
            response.mimetype = "application/json"
        elif request.path in {"/api/radar", "/api/radar/candidates"} and isinstance(data, list):
            # V28's older after_request guard may have injected speech_required=true.
            for item in data:
                if isinstance(item, dict):
                    item["speech_required"] = False
                    item["speech_required_for_top"] = False
                    item["ai_role"] = "enrichment_only"
            response.set_data(app.json.dumps(data))
            response.mimetype = "application/json"
        return response

    app._v34_broad_api = True


def apply_broad_v34():
    global _APPLIED, _BASE_PREPARE, _BASE_PROCESS_AI
    if _APPLIED:
        return {
            "mode": MODE_VERSION,
            "target_output_min": TARGET_OUTPUT_MIN,
            "target_output_max": TARGET_OUTPUT_MAX,
            "ai_enrich_limit": AI_ENRICH_LIMIT,
        }
    _APPLIED = True

    # Keep V30 persistence identity to resume the current paid run.
    # Change product semantics only: trend momentum decides visibility; AI annotates.
    v28.matches_v28 = broad_matches
    radar_job.matches = broad_matches
    radar_service.matches = broad_matches
    v28.top_eligible_v28 = broad_eligible
    radar_quality.top_eligible = broad_eligible
    radar_job.top_eligible = broad_eligible

    _BASE_PREPARE = radar_job._prepare_candidates
    _BASE_PROCESS_AI = radar_job._process_one_ai
    radar_job._prepare_candidates = prepare_candidates_v34
    radar_job._process_one_ai = process_one_ai_v34

    # Replace V28's strict ai_match-only finalizer with broad momentum output.
    edge._BASE_FINALIZE = finalize_v34

    app_module = sys.modules.get("app")
    if app_module is None:
        raise RuntimeError("V34 broad overlay must be applied from app startup")
    app_module.top_eligible = broad_eligible
    app_module.KEEP_LIMIT = OUTPUT_LIMIT
    _remove_v28_api_interceptor(app_module.app)
    _install_broad_api(app_module.app)

    startup_promoted = _promote_active_job_at_startup()
    info = {
        "mode": MODE_VERSION,
        "screening_profile": SCREENING_PROFILE,
        "platforms": list(v28.PLATFORMS),
        "hashtags": list(v28.TARGET_TAGS),
        "lookback_days": v28.LOOKBACK_DAYS,
        "target_output_min": TARGET_OUTPUT_MIN,
        "target_output_max": TARGET_OUTPUT_MAX,
        "keep_limit": OUTPUT_LIMIT,
        "ai_enrich_limit": AI_ENRICH_LIMIT,
        "ai_role": "enrichment_only",
        "speech_required": False,
        "no_speech_policy": "keep_and_add_russian_speech",
        "timing_reject_policy": "keep_and_rewrite_to_10s",
        "ai_error_policy": "keep_for_manual_review",
        "hard_reject_prefixes": list(HARD_REJECT_PREFIXES),
        "startup_promoted_active_candidates": startup_promoted,
    }
    add_radar_log(
        "V34 BROAD READY: momentum ranks 50-100 options; Gemini enriches instead of deleting no-speech/timing/media-error candidates.",
        stage="startup",
        details=info,
    )
    return info
