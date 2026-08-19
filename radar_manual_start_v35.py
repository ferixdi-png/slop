"""V35 manual-start-only guard.

A durable radar job may survive browser reloads and Render deploys, but paid work
must never advance merely because a page was opened. Every driver session is
created only by an explicit POST /api/radar/sync and receives a signed token.
POST /api/radar/tick is rejected unless that token is presented.

The browser keeps the token only in JavaScript memory. The server token is
stateless (safe across Gunicorn workers) and is cryptographically bound to a
per-container boot nonce, so a Render restart/deploy invalidates old tabs too.
Read-only GET endpoints never arm the driver.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any

from flask import has_request_context, request

import cloud_state
import radar_request_job as radar_job
from radar_logs import add_radar_log

PROFILE = "manual_start_only_v35"
TOKEN_HEADER = "X-Radar-Driver-Token"
TOKEN_TTL_SECONDS = 12 * 60 * 60
BOOT_NONCE_PATH = os.environ.get("RADAR_V35_BOOT_NONCE_PATH", "/tmp/radar-v35-boot-nonce").strip()

_APPLIED = False
_BASE_APP_START = None
_BASE_APP_TICK = None
_BASE_PUBLIC = radar_job.public_job
_SIGNING_KEY = b""
_BOOT_NONCE = ""
_INSTANCE_ID = ""


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _load_boot_nonce() -> str:
    """One nonce per running container, shared by all Gunicorn workers."""
    path = BOOT_NONCE_PATH or "/tmp/radar-v35-boot-nonce"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        fd = None
    except OSError:
        # Local/read-only fallback. Render normally has writable /tmp.
        return secrets.token_urlsafe(32)

    if fd is not None:
        nonce = secrets.token_urlsafe(32)
        try:
            os.write(fd, nonce.encode("ascii"))
        finally:
            os.close(fd)
        return nonce

    for _ in range(20):
        try:
            with open(path, "r", encoding="ascii") as fh:
                nonce = fh.read().strip()
            if len(nonce) >= 24:
                return nonce
        except OSError:
            pass
        time.sleep(0.01)
    raise RuntimeError("V35 boot nonce could not be shared between workers")


def _configure_signing(app_module) -> None:
    global _SIGNING_KEY, _BOOT_NONCE, _INSTANCE_ID
    _BOOT_NONCE = _load_boot_nonce()
    _INSTANCE_ID = str(os.environ.get("RENDER_INSTANCE_ID") or "local-instance")
    secret_material = str(
        os.environ.get("SECRET_KEY")
        or os.environ.get("APIFY_API_TOKEN")
        or getattr(app_module.app, "secret_key", "")
        or "local-dev-secret"
    )
    commit = str(os.environ.get("RENDER_GIT_COMMIT") or "local-commit")
    material = f"{secret_material}|{_INSTANCE_ID}|{commit}|{_BOOT_NONCE}".encode("utf-8")
    _SIGNING_KEY = hashlib.sha256(material).digest()


def _issue_token(run_id: str) -> str:
    if not _SIGNING_KEY:
        raise RuntimeError("V35 driver signing key is not configured")
    now = int(time.time())
    body = {
        "r": str(run_id or ""),
        "iat": now,
        "exp": now + TOKEN_TTL_SECONDS,
        "i": _INSTANCE_ID,
        "b": hashlib.sha256(_BOOT_NONCE.encode("ascii")).hexdigest()[:16],
        "n": secrets.token_hex(8),
    }
    encoded = _b64e(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signature = _b64e(hmac.new(_SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def _request_token() -> str:
    if not has_request_context():
        return ""
    return str(request.headers.get(TOKEN_HEADER, "") or "").strip()


def _token_valid(token: str, run_id: str) -> bool:
    if not token or not run_id or not _SIGNING_KEY:
        return False
    try:
        encoded, supplied_sig = token.split(".", 1)
        expected_sig = _b64e(hmac.new(_SIGNING_KEY, encoded.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_sig, expected_sig):
            return False
        body = json.loads(_b64d(encoded).decode("utf-8"))
        now = int(time.time())
        expected_boot = hashlib.sha256(_BOOT_NONCE.encode("ascii")).hexdigest()[:16]
        return bool(
            hmac.compare_digest(str(body.get("r") or ""), str(run_id))
            and hmac.compare_digest(str(body.get("i") or ""), _INSTANCE_ID)
            and hmac.compare_digest(str(body.get("b") or ""), expected_boot)
            and int(body.get("iat") or 0) <= now + 60
            and int(body.get("exp") or 0) >= now
        )
    except Exception:
        return False


def _revoke_run(run_id: str) -> None:
    # Tokens are stateless and bound to run_id. A cancelled/done run is no longer
    # active, and a subsequent search receives a new run_id, so no server-side
    # token registry/revocation store is required.
    return None


def public_job_v35(job=None, busy: bool = False) -> dict[str, Any]:
    payload = dict(_BASE_PUBLIC(job, busy=busy) or {})
    raw_active = bool(payload.get("active"))
    run_id = str(payload.get("run_id") or "")
    authorized = raw_active and _token_valid(_request_token(), run_id)

    payload["durable_active"] = raw_active
    payload["manual_start_only"] = True
    payload["auto_resume_on_page_load"] = False
    payload["tick_requires_driver_token"] = True
    payload["manual_session_authorized"] = bool(authorized)
    payload["paused"] = bool(raw_active and not authorized)
    payload["manual_start_required"] = bool(raw_active and not authorized)
    payload["resume_available"] = bool(raw_active)

    if raw_active and not authorized:
        payload["active"] = False
        payload["message"] = (
            "Незавершённый поиск сохранён, но приостановлен. "
            "Открытие страницы и перезагрузка никогда его не продолжают. "
            "Нажмите «ПРОДОЛЖИТЬ ПОИСК» вручную."
        )
    return payload


def create_or_resume_job_v35():
    payload, status_code = _BASE_APP_START()
    payload = dict(payload or {})
    run_id = str(payload.get("run_id") or "")
    if bool(payload.get("accepted")) and run_id:
        token = _issue_token(run_id)
        # Base wrappers may have rendered the job as paused before this explicit
        # click received its token. Normalize response to post-click truth.
        payload["active"] = True
        payload["durable_active"] = True
        payload["resume_available"] = True
        payload["driver_token"] = token
        payload["driver_token_header"] = TOKEN_HEADER
        payload["manual_start_only"] = True
        payload["manual_session_authorized"] = True
        payload["manual_start_required"] = False
        payload["paused"] = False
        payload["auto_resume_on_page_load"] = False
        payload["tick_requires_driver_token"] = True
        add_radar_log(
            "V35 MANUAL START: browser driver explicitly authorized by Start/Continue click.",
            stage="manual-start",
            details={
                "run_id": run_id,
                "token_persisted": False,
                "worker_safe": True,
                "restart_bound": True,
            },
        )
    return payload, status_code


def tick_job_v35():
    job = cloud_state.load_radar_job() or {}
    base_truth = dict(_BASE_PUBLIC(job) or {})
    run_id = str(base_truth.get("run_id") or "")
    raw_active = bool(base_truth.get("active"))
    token = _request_token()

    if raw_active and not _token_valid(token, run_id):
        payload = public_job_v35(job)
        payload.update(
            blocked=True,
            manual_start_required=True,
            message=(
                "Tick заблокирован: нет действующего driver-token от явного нажатия "
                "«ЗАПУСТИТЬ/ПРОДОЛЖИТЬ ПОИСК»."
            ),
        )
        add_radar_log(
            "V35 BLOCKED AUTO-TICK: durable job существует, но ручной driver-token отсутствует/устарел.",
            level="WARN",
            stage="manual-start",
            details={"run_id": run_id, "phase": job.get("phase")},
        )
        return payload, 409

    payload, status_code = _BASE_APP_TICK()
    payload = dict(payload or {})
    payload["manual_start_only"] = True
    payload["tick_requires_driver_token"] = True
    return payload, status_code


def install_manual_start_v35(app_module) -> dict[str, Any]:
    global _APPLIED, _BASE_APP_START, _BASE_APP_TICK
    if _APPLIED:
        return {
            "profile": PROFILE,
            "manual_start_only": True,
            "auto_resume_on_page_load": False,
            "tick_requires_driver_token": True,
        }
    _APPLIED = True
    _configure_signing(app_module)

    _BASE_APP_START = app_module.create_or_resume_job
    _BASE_APP_TICK = app_module.tick_job

    # Flask endpoint functions resolve these app.py globals at request time.
    app_module.create_or_resume_job = create_or_resume_job_v35
    app_module.tick_job = tick_job_v35
    app_module.public_job = public_job_v35

    # Keep direct module consumers session-aware too, without replacing the base
    # tick implementation that the app-level wrappers already captured.
    radar_job.public_job = public_job_v35

    app = app_module.app
    if not getattr(app, "_v35_manual_start_metadata", False):
        @app.after_request
        def _v35_manual_start_metadata(response):
            if not response.is_json:
                return response
            data = response.get_json(silent=True)
            if isinstance(data, dict) and request.path in {
                "/api/status", "/api/radar/status", "/health", "/api/radar/job"
            }:
                fields = {
                    "radar_manual_start_only": True,
                    "radar_auto_resume_on_page_load": False,
                    "radar_tick_requires_driver_token": True,
                    "radar_driver_token_persisted": False,
                    "radar_driver_token_worker_safe": True,
                    "radar_driver_token_restart_bound": True,
                    "radar_deploy_resume_policy": "paused_until_explicit_click",
                }
                if request.path == "/api/radar/status":
                    details = dict(data.get("details") or {})
                    details.update(fields)
                    data["details"] = details
                else:
                    data.update(fields)
                response.set_data(app.json.dumps(data))
                response.mimetype = "application/json"
            return response

        app._v35_manual_start_metadata = True

    info = {
        "profile": PROFILE,
        "manual_start_only": True,
        "auto_resume_on_page_load": False,
        "tick_requires_driver_token": True,
        "driver_token_persisted": False,
        "driver_token_worker_safe": True,
        "driver_token_restart_bound": True,
        "deploy_resume_policy": "paused_until_explicit_click",
    }
    add_radar_log(
        "V35 MANUAL-START READY: GET/reload/restart/deploy cannot advance radar; every tick needs a signed restart-bound token issued only by explicit Start/Continue.",
        stage="startup",
        details=info,
    )
    return info
