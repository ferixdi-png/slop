from __future__ import annotations

import time
import re

from flask import jsonify, request

import gemini_service
from radar_logs import add_radar_log

PROFILE = "gemini_overload_v40"
TRANSIENT_CODES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (3.0, 8.0)  # two bounded retries, no infinite loop
_APPLIED = False
_BASES = {}
_ANALYZE_RE = re.compile(r"^/api/radar/\d+/analyze$")


def _status_code(exc) -> int:
    for attr in ("status_code", "code"):
        try:
            value = int(getattr(exc, attr, 0) or 0)
            if value:
                return value
        except Exception:
            pass
    response = getattr(exc, "response", None)
    try:
        value = int(getattr(response, "status_code", 0) or 0)
        if value:
            return value
    except Exception:
        pass
    text = str(exc or "").upper()
    for code in (429, 500, 502, 503, 504):
        if f"{code} " in text or f"{code}:" in text:
            return code
    return 0


def is_transient_gemini_error(exc) -> bool:
    code = _status_code(exc)
    if code in TRANSIENT_CODES:
        return True
    text = str(exc or "").lower()
    markers = (
        "high demand",
        "temporarily unavailable",
        "service unavailable",
        "resource_exhausted",
        "unavailable",
        "rate limit",
        "too many requests",
    )
    return any(marker in text for marker in markers)


def _wrap_stage(stage: str, fn):
    def wrapped(*args, **kwargs):
        for attempt in range(len(RETRY_DELAYS) + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if not is_transient_gemini_error(exc) or attempt >= len(RETRY_DELAYS):
                    raise
                delay = RETRY_DELAYS[attempt]
                add_radar_log(
                    f"Gemini временно перегружен на этапе {stage}; повтор {attempt + 1}/{len(RETRY_DELAYS)} через {delay:.0f} сек.",
                    level="WARN",
                    stage="gemini-overload",
                    details={
                        "gemini_stage": stage,
                        "attempt": attempt + 1,
                        "status_code": _status_code(exc),
                        "delay_seconds": delay,
                    },
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")

    wrapped.__name__ = getattr(fn, "__name__", f"v40_{stage}")
    wrapped.__doc__ = getattr(fn, "__doc__", None)
    return wrapped


def _install_http_surface(app) -> None:
    if getattr(app, "_gemini_overload_v40_http", False):
        return

    @app.after_request
    def gemini_overload_v40_http(response):
        if request.method != "POST" or not _ANALYZE_RE.match(request.path) or not response.is_json:
            return response
        data = response.get_json(silent=True)
        if not isinstance(data, dict):
            return response
        error_text = str(data.get("error") or "")
        code = int(data.get("status_code") or 0) if str(data.get("status_code") or "").isdigit() else 0
        transient = code in TRANSIENT_CODES or any(
            marker in error_text.lower()
            for marker in ("503 unavailable", "high demand", "temporarily unavailable", "resource_exhausted", "rate limit")
        )
        if not transient:
            return response

        payload = dict(data)
        payload.update(
            error="Gemini временно перегружен. Видео и найденный тренд не потеряны — повтори получение промптов через несколько секунд.",
            code="GEMINI_TEMPORARILY_UNAVAILABLE",
            retryable=True,
            retry_after_sec=15,
            upstream_status=code or 503,
        )
        response.status_code = 503
        response.set_data(app.json.dumps(payload))
        response.mimetype = "application/json"
        response.headers["Retry-After"] = "15"
        return response

    app._gemini_overload_v40_http = True


def install_gemini_overload_v40(app=None) -> dict:
    global _APPLIED
    if _APPLIED:
        if app is not None:
            _install_http_surface(app)
        return diagnostics()

    for name in ("build_forensic_map", "build_production_package", "audit_package"):
        base = getattr(gemini_service, name)
        _BASES[name] = base
        setattr(gemini_service, name, _wrap_stage(name, base))

    if app is not None:
        _install_http_surface(app)
    _APPLIED = True
    info = diagnostics()
    add_radar_log(
        "V40 GEMINI OVERLOAD READY: only transient 429/5xx/high-demand failures get two bounded retries; final overload is HTTP 503, not 500.",
        stage="startup",
        details=info,
    )
    return info


def diagnostics() -> dict:
    return {
        "profile": PROFILE,
        "transient_codes": sorted(TRANSIENT_CODES),
        "retry_delays_seconds": list(RETRY_DELAYS),
        "max_extra_attempts": len(RETRY_DELAYS),
        "validation_errors_retried": False,
        "final_http_status": 503,
        "retry_after_sec": 15,
    }
