"""V36 production-prompt media reliability.

The radar broad pool intentionally keeps strong candidates even when Gemini did
not mark them as a semantic PASS. Manual production analysis must therefore be
more reliable than automatic enrichment.

This layer fixes a concrete production failure seen on Render: manual YouTube
refresh succeeded, but the Actor returned a private Apify Key-Value Store record
URL. The generic hardened downloader fetched it anonymously and received HTTP
403, so /api/radar/<id>/analyze returned 500 and no prompts were shown.

V36 rules:
- keep all V30 SSRF / redirect / MIME / size protections;
- send APIFY_API_TOKEN only as a Bearer header and only to api.apify.com storage
  endpoints; never put the secret in URLs, DB rows, browser responses or logs;
- never forward the Bearer token to a redirect target on another host;
- if authenticated KVS access still returns 401/403, generate a signed public URL
  for exactly that one KVS record through the Apify client and retry once;
- preserve the original private media URL in the DB (no signed secret URL leak);
- expose non-secret diagnostics for CI and /api/status runtime metadata.
"""

from __future__ import annotations

import os
import re
import tempfile
from urllib.parse import unquote, urljoin, urlparse

import requests
from apify_client import ApifyClient

import radar_audit_v30 as audit
import radar_service
import reel_media
from radar_logs import add_radar_log

PROFILE = "production_prompts_v36_authenticated_media"
_APPLIED = False
_BASE_DOWNLOAD = None

_APIFY_KVS_RE = re.compile(r"^/v2/key-value-stores/([^/]+)/records/(.+)$")
_APIFY_RUN_KVS_RE = re.compile(r"^/v2/actor-runs/([^/]+)/key-value-store/records/(.+)$")


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
    """Return a signed URL for one private KVS record, or an empty string."""
    token = _apify_token()
    if not token:
        return ""
    parsed = urlparse(str(url or ""))
    if (parsed.hostname or "").lower().rstrip(".") != "api.apify.com":
        return ""
    match = _APIFY_KVS_RE.match(parsed.path or "")
    if not match:
        return ""
    store_id = unquote(match.group(1))
    key = unquote(match.group(2))
    if not store_id or not key:
        return ""
    try:
        signed = ApifyClient(token).key_value_store(store_id).get_record_public_url(key)
        return str(signed or "").strip()
    except Exception as exc:
        add_radar_log(
            f"V36 не удалось получить signed KVS URL: {type(exc).__name__}",
            level="WARN",
            stage="prompts-media",
            details={"store_id": store_id, "record_key": key[:120]},
        )
        return ""


def download_temp_video_v36(url: str):
    """V30 hardened downloader + scoped Apify authentication."""
    original = str(url or "").strip()
    current = audit._validate_public_https_url(original)
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

            # A private Actor KVS record is expected to require auth. Bearer is the
            # primary path. If permissions/signature semantics still return 401/403,
            # request a signed URL for exactly this record and retry without exposing
            # the account token anywhere outside the server process.
            if response.status_code in {401, 403} and _is_apify_api_media(current) and not signed_retry_used:
                response.close()
                response = None
                signed = _signed_record_url(current)
                if signed:
                    current = audit._validate_public_https_url(signed)
                    signed_retry_used = True
                    add_radar_log(
                        "V36 private Apify media: Bearer path rejected, retrying via signed single-record URL.",
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
                # _request_headers() is recomputed for every hop. Therefore a Bearer
                # token used on api.apify.com can never leak to CDN/storage redirects.
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


def install_prompt_reliability_v36() -> dict:
    global _APPLIED, _BASE_DOWNLOAD
    if _APPLIED:
        return diagnostics()

    _BASE_DOWNLOAD = radar_service.download_temp_video
    radar_service.download_temp_video = download_temp_video_v36
    reel_media.download_temp_video = download_temp_video_v36
    _APPLIED = True

    info = diagnostics()
    add_radar_log(
        "V36 PROMPTS READY: private Apify KVS media uses scoped Bearer auth + signed-record fallback; V30 media safety remains active.",
        stage="startup",
        details=info,
    )
    return info


def diagnostics() -> dict:
    return {
        "profile": PROFILE,
        "apify_private_media_auth": "bearer_header_only",
        "apify_signed_record_fallback": True,
        "token_in_url": False,
        "token_in_browser": False,
        "redirect_token_forwarding": False,
        "v30_ssrf_guard_preserved": True,
        "max_video_bytes": int(audit.MAX_VIDEO_BYTES),
    }
