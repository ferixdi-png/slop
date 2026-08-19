"""V36 production-prompt reliability.

Concrete production failure fixed here:
- a manual YouTube refresh succeeded;
- the Actor returned a private Apify Key-Value Store MP4 URL;
- the generic hardened downloader fetched it anonymously and got HTTP 403;
- /api/radar/<id>/analyze returned 500, so the UI had no PHOTO/VIDEO prompts.

V34 also intentionally keeps strong trend candidates even when automatic Gemini
screening did not mark them as ai_match=1. The old app route still required that
legacy bit, which made prompt generation inconsistent with the broad-pool product.

V36 rules:
1) preserve all V30 SSRF / redirect / MIME / size protections;
2) authenticate private Apify media only with a server-side Bearer header scoped
   to api.apify.com storage endpoints; never put the account token in URLs/DB/UI/logs;
3) never forward that Bearer header to another redirect host;
4) if Bearer still gets 401/403, request a signed URL for only that one KVS record;
5) keep signed URLs ephemeral in server memory and preserve the original URL in DB;
6) allow manual production prompts for every current broad-eligible trend candidate,
   even if its automatic enrichment result was NO_SPEECH / timing reject / unverified;
7) preserve V30 single-flight and analysis-cache behavior for the broad candidates.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from urllib.parse import unquote, urljoin, urlparse

import requests
from apify_client import ApifyClient
from flask import jsonify, request

import radar_audit_v30 as audit
import radar_service
import reel_media
from db import db_conn
from radar_logs import add_radar_log, reset_radar_run_id, set_radar_run_id

PROFILE = "production_prompts_v36_authenticated_media"
_APPLIED = False
_ENDPOINT_INSTALLED = False
_BASE_DOWNLOAD = None

_APIFY_KVS_RE = re.compile(r"^/v2/key-value-stores/([^/]+)/records/(.+)$")
_APIFY_RUN_KVS_RE = re.compile(r"^/v2/actor-runs/([^/]+)/key-value-store/records/(.+)$")
_ANALYZE_RE = re.compile(r"^/api/radar/(\d+)/analyze$")


def _apify_token() -> str:
    return str(os.environ.get("APIFY_API_TOKEN", "") or "").strip()


def _is_apify_api_media(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    if (parsed.hostname or "").lower().rstrip(".") != "api.apify.com":
        return False
    path = parsed.path or ""
    return bool(_APIFY_KVS_RE.match(path) or _APIFY_RUN_KVS_RE.match(path))


def _request_headers(url: str) -> dict[str, str]:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "video/*,application/octet-stream;q=0.9,*/*;q=0.1",
    }
    token = _apify_token()
    if token and _is_apify_api_media(url):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _signed_record_url(url: str) -> str:
    """Return an ephemeral signed URL for one private KVS/run record, or empty string."""
    token = _apify_token()
    if not token:
        return ""
    parsed = urlparse(str(url or ""))
    if (parsed.hostname or "").lower().rstrip(".") != "api.apify.com":
        return ""

    path = parsed.path or ""
    direct_match = _APIFY_KVS_RE.match(path)
    run_match = _APIFY_RUN_KVS_RE.match(path)
    store_id = ""
    run_id = ""
    key = ""

    if direct_match:
        store_id = unquote(direct_match.group(1))
        key = unquote(direct_match.group(2))
    elif run_match:
        run_id = unquote(run_match.group(1))
        key = unquote(run_match.group(2))
    else:
        return ""

    if not key or (not store_id and not run_id):
        return ""

    try:
        client = ApifyClient(token)
        if not store_id:
            run = client.run(run_id).get() or {}
            if isinstance(run, dict):
                store_id = str(
                    run.get("defaultKeyValueStoreId")
                    or run.get("default_key_value_store_id")
                    or ""
                )
            else:
                store_id = str(
                    getattr(run, "default_key_value_store_id", "")
                    or getattr(run, "defaultKeyValueStoreId", "")
                    or ""
                )
        if not store_id:
            return ""
        signed = client.key_value_store(store_id).get_record_public_url(key)
        return str(signed or "").strip()
    except Exception as exc:
        add_radar_log(
            f"V36 signed KVS fallback unavailable: {type(exc).__name__}",
            level="WARN",
            stage="prompts-media",
            details={
                "store_id": store_id,
                "actor_run_id": run_id,
                "record_key": key[:120],
            },
        )
        return ""


def download_temp_video_v36(url: str):
    """V30 hardened downloader + scoped Apify authentication."""
    current = audit._validate_public_https_url(str(url or "").strip())
    response = None
    signed_retry_used = False

    try:
        redirects = 0
        while True:
            response = requests.get(
                current,
                stream=True,
                timeout=(10, 45),
                allow_redirects=False,
                headers=_request_headers(current),
            )

            if response.status_code in {401, 403} and _is_apify_api_media(current) and not signed_retry_used:
                response.close()
                response = None
                signed = _signed_record_url(current)
                if signed:
                    current = audit._validate_public_https_url(signed)
                    signed_retry_used = True
                    add_radar_log(
                        "V36 private Apify media: Bearer rejected, retrying via signed single-record URL.",
                        stage="prompts-media",
                    )
                    continue

            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location") or ""
                response.close()
                response = None
                if not location:
                    raise RuntimeError("MEDIA_REDIRECT_REJECTED: empty redirect")
                redirects += 1
                if redirects > audit.MAX_REDIRECTS:
                    raise RuntimeError("MEDIA_REDIRECT_REJECTED: too many redirects")
                # Headers are recomputed on every hop; the secret is only attached to
                # the exact Apify API storage host and cannot leak to a CDN redirect.
                current = audit._validate_public_https_url(urljoin(current, location))
                continue
            break

        if response is None:
            raise RuntimeError("MEDIA_DOWNLOAD_FAILED: no response")
        response.raise_for_status()

        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type in {"text/html", "text/plain", "application/json", "application/xml", "text/xml"}:
            raise RuntimeError(f"MEDIA_CONTENT_REJECTED: {content_type}")
        try:
            declared = int(response.headers.get("Content-Length") or 0)
        except Exception:
            declared = 0
        if declared > audit.MAX_VIDEO_BYTES:
            raise RuntimeError("Видео из радара больше 50 МБ")

        total = 0
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        try:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > audit.MAX_VIDEO_BYTES:
                    raise RuntimeError("Видео из радара больше 50 МБ")
                tmp.write(chunk)
            tmp.close()
            if total < 1024:
                raise RuntimeError("MEDIA_CONTENT_REJECTED: payload is too small to be a video")
            return tmp.name
        except Exception:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
    finally:
        if response is not None:
            response.close()


def _cached_broad_analysis(app_module, row: dict, owned: bool):
    fingerprint = audit._analysis_fingerprint(
        str(row.get("post_url") or ""), float(row.get("duration_sec") or 0), owned
    )
    with db_conn() as conn:
        cached = conn.execute(
            """SELECT id,model,result_json,source_duration_sec,production_profile
               FROM analyses
               WHERE source_url=? AND model=? AND production_profile=?
                 AND owned_or_licensed=? AND source_fingerprint=?
               ORDER BY id DESC LIMIT 1""",
            (
                row.get("post_url"),
                audit.ANALYSIS_MODEL,
                audit.PRODUCTION_PROFILE_VERSION,
                1 if owned else 0,
                fingerprint,
            ),
        ).fetchone()
    if not cached:
        return None
    try:
        result = json.loads(cached["result_json"] or "{}")
    except Exception:
        return None
    add_radar_log(
        f"V36 ANALYSIS CACHE HIT: broad candidate #{row.get('id')}; Gemini production-chain skipped.",
        stage="analysis-cache",
        details={"analysis_id": cached["id"], "ai_match": bool(row.get("ai_match"))},
    )
    return jsonify(
        id=cached["id"],
        model=cached["model"],
        generation_target="gemini-omni-flash-preview",
        production_profile=audit.PRODUCTION_PROFILE_VERSION,
        source_duration_sec=float(cached["source_duration_sec"] or row.get("duration_sec") or 0),
        cached=True,
        broad_candidate=True,
        result=result,
    )


def _analyze_broad_candidate(app_module, item_id: int, row: dict):
    """Production analysis for V34 candidates that are visible but ai_match != 1."""
    owned = bool((request.get_json(silent=True) or {}).get("owned_or_licensed"))
    cached = _cached_broad_analysis(app_module, row, owned)
    if cached is not None and not bool((request.get_json(silent=True) or {}).get("force")):
        return cached

    run_id = f"prompt-{item_id}-v36-{uuid.uuid4().hex[:6]}"
    context_token = set_radar_run_id(run_id)
    tmp = None
    try:
        add_radar_log(
            f"V36 production-анализ broad candidate #{item_id} @{row.get('creator','')}",
            stage="prompts",
            details={
                "views": row.get("views"),
                "duration_sec": row.get("duration_sec"),
                "ai_match": bool(row.get("ai_match")),
                "reason": str(row.get("reason") or "")[:180],
            },
        )
        tmp, refreshed_duration = app_module.download_reel_for_analysis(row)
        source_duration = round(float(refreshed_duration or row.get("duration_sec") or 0), 2)
        if source_duration < app_module.RADAR_MIN_DURATION_SEC or source_duration > app_module.RADAR_MAX_DURATION_SEC:
            raise RuntimeError(
                f"Фактическая длительность {source_duration:.2f} сек вне production-диапазона "
                f"{app_module.RADAR_MIN_DURATION_SEC:.1f}–{app_module.RADAR_MAX_DURATION_SEC:.2f} сек"
            )

        from gemini_pipeline_logged import analyze_video_logged

        package = app_module.lock_generation_target(analyze_video_logged(tmp, owned, source_duration))
        result = package.model_dump()
        analysis_id = app_module.save_analysis(
            (f"@{row.get('creator','')} — {row.get('hook') or 'тренд-кандидат'}")[:160],
            row.get("post_url"),
            row.get("views", 0),
            row.get("viral_score_v2", 0),
            result,
        )
        add_radar_log(
            f"V36 ультра-промпты для broad candidate @{row.get('creator','')} готовы.",
            stage="prompts",
            details={"analysis_id": analysis_id, "qa": result.get("reconstruction_confidence")},
        )
        return jsonify(
            id=analysis_id,
            model=app_module.ANALYSIS_MODEL,
            generation_target="gemini-omni-flash-preview",
            production_profile=app_module.PRODUCTION_PROFILE_VERSION,
            source_duration_sec=source_duration,
            broad_candidate=True,
            result=result,
        )
    except Exception as exc:
        add_radar_log(
            f"V36 ошибка production-промптов @{row.get('creator','')}: {exc}",
            level="ERROR",
            stage="prompts",
        )
        return jsonify(
            error=str(exc),
            code="PRODUCTION_PROMPTS_FAILED",
            broad_candidate=True,
            retryable=("403" in str(exc) or "429" in str(exc) or "timeout" in str(exc).lower()),
        ), 502
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
        reset_radar_run_id(context_token)


def _install_broad_prompt_endpoint(app_module) -> None:
    global _ENDPOINT_INSTALLED
    if _ENDPOINT_INSTALLED:
        return

    app = app_module.app

    @app.before_request
    def v36_broad_prompt_access():
        match = _ANALYZE_RE.match(request.path) if request.method == "POST" else None
        if not match:
            return None
        item_id = int(match.group(1))
        with db_conn() as conn:
            record = conn.execute("SELECT * FROM radar_posts WHERE id=?", (item_id,)).fetchone()
        if not record:
            return None
        row = dict(record)

        # Normal historical PASS candidates continue through the original app route;
        # they still benefit from the V36 authenticated downloader below.
        if bool(row.get("ai_match")):
            return None

        # V34 broad-pool candidates are intentionally visible despite a semantic
        # soft-reject/unverified state. Prompt access follows current product
        # eligibility, not the obsolete ai_match gate.
        if str(row.get("screening_profile") or "") != str(app_module.PROFILE_VERSION or ""):
            return None
        if not app_module.top_eligible(row):
            return None
        return _analyze_broad_candidate(app_module, item_id, row)

    _ENDPOINT_INSTALLED = True


def install_prompt_reliability_v36(app_module=None) -> dict:
    global _APPLIED, _BASE_DOWNLOAD
    if _APPLIED:
        if app_module is not None:
            _install_broad_prompt_endpoint(app_module)
        return diagnostics()

    _BASE_DOWNLOAD = radar_service.download_temp_video
    radar_service.download_temp_video = download_temp_video_v36
    reel_media.download_temp_video = download_temp_video_v36
    if app_module is not None:
        _install_broad_prompt_endpoint(app_module)
    _APPLIED = True

    info = diagnostics()
    add_radar_log(
        "V36 PROMPTS READY: private Apify KVS media auth fixed; broad V34 candidates can always request production prompts.",
        stage="startup",
        details=info,
    )
    return info


def diagnostics() -> dict:
    return {
        "profile": PROFILE,
        "apify_private_media_auth": "bearer_header_only",
        "apify_signed_record_fallback": True,
        "apify_run_record_signed_fallback": True,
        "token_in_url": False,
        "token_in_browser": False,
        "redirect_token_forwarding": False,
        "v30_ssrf_guard_preserved": True,
        "broad_candidate_prompt_access": True,
        "legacy_ai_match_gate_removed_for_broad_candidates": True,
        "analysis_cache_for_broad_candidates": True,
        "max_video_bytes": int(audit.MAX_VIDEO_BYTES),
    }
