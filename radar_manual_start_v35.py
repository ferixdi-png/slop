"""V35 manual-start-only guard.

A durable radar job may survive browser reloads and Render deploys, but paid work
must never advance merely because a page was opened. Every driver session is
created only by an explicit POST /api/radar/sync and receives an in-memory token.
POST /api/radar/tick is rejected unless that token is presented.

The token is intentionally NOT persisted. Browser reload, process restart and
new deploy therefore pause the driver until the user explicitly clicks Start /
Continue again. Read-only GET endpoints never arm the driver.
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from flask import has_request_context, request

import cloud_state
import radar_request_job as radar_job
from radar_logs import add_radar_log

PROFILE = "manual_start_only_v35"
TOKEN_HEADER = "X-Radar-Driver-Token"
TOKEN_TTL_SECONDS = 12 * 60 * 60

_LOCK = threading.Lock()
_TOKENS: dict[str, tuple[str, float]] = {}
_APPLIED = False
_BASE_APP_START = None
_BASE_APP_TICK = None
_BASE_PUBLIC = radar_job.public_job


def _cleanup_tokens() -> None:
    now = time.monotonic()
    with _LOCK:
        expired = [token for token, (_, expires) in _TOKENS.items() if expires <= now]
        for token in expired:
            _TOKENS.pop(token, None)


def _issue_token(run_id: str) -> str:
    _cleanup_tokens()
    token = secrets.token_urlsafe(32)
    with _LOCK:
        _TOKENS[token] = (str(run_id or ""), time.monotonic() + TOKEN_TTL_SECONDS)
    return token


def _request_token() -> str:
    if not has_request_context():
        return ""
    return str(request.headers.get(TOKEN_HEADER, "") or "").strip()


def _token_valid(token: str, run_id: str) -> bool:
    if not token or not run_id:
        return False
    _cleanup_tokens()
    with _LOCK:
        record = _TOKENS.get(token)
    if not record:
        return False
    expected_run, expires = record
    return expires > time.monotonic() and secrets.compare_digest(expected_run, str(run_id))


def _revoke_run(run_id: str) -> None:
    if not run_id:
        return
    with _LOCK:
        doomed = [token for token, (rid, _) in _TOKENS.items() if rid == str(run_id)]
        for token in doomed:
            _TOKENS.pop(token, None)


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

    # Public UI truth is session-aware: an unfinished durable job is not "running"
    # in a tab that has never explicitly started/continued it.
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
        # _BASE_APP_START may itself have used the session-aware public_job wrapper
        # before the new token existed. Normalize the response to the truth AFTER
        # the explicit click has authorized this exact run.
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
            details={"run_id": run_id, "token_persisted": False},
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
                "Tick заблокирован: нет driver-token от явного нажатия "
                "«ЗАПУСТИТЬ/ПРОДОЛЖИТЬ ПОИСК»."
            ),
        )
        add_radar_log(
            "V35 BLOCKED AUTO-TICK: durable job существует, но ручной driver-token отсутствует.",
            level="WARN",
            stage="manual-start",
            details={"run_id": run_id, "phase": job.get("phase")},
        )
        return payload, 409

    payload, status_code = _BASE_APP_TICK()
    payload = dict(payload or {})
    if not bool(payload.get("active")):
        _revoke_run(run_id)
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
        "deploy_resume_policy": "paused_until_explicit_click",
    }
    add_radar_log(
        "V35 MANUAL-START READY: GET/reload/deploy cannot advance radar; every tick needs an in-memory token issued only by explicit Start/Continue.",
        stage="startup",
        details=info,
    )
    return info
