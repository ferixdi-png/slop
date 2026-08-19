import hashlib
import mimetypes
from pathlib import Path

from flask import Response, abort, jsonify


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = (BASE_DIR / "static").resolve()
CRITICAL_ASSETS = (
    "style.css",
    "upgrade.css",
    "app.js",
    "v17_ui.js",
    "overlay_v15.js",
    "runtime.js",
    "fresh_run_v23.js",
    "scope_v24.js",
)


def _safe_static_path(filename):
    name = str(filename or "").lstrip("/")
    candidate = (STATIC_DIR / name).resolve()
    if candidate == STATIC_DIR or STATIC_DIR not in candidate.parents:
        abort(404)
    if not candidate.is_file():
        abort(404)
    return candidate


def _asset_meta(path):
    data = path.read_bytes()
    return data, {
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "content_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    }


def reliable_send_static_file(filename):
    """Serve static files as ordinary byte responses instead of a file-wrapper.

    On Render/Gunicorn this makes the actual payload length observable and avoids
    a class of proxy/browser issues where the HTML is healthy while CSS/JS may
    appear as a 200 response with no body. Empty assets fail loudly with 503.
    """
    path = _safe_static_path(filename)
    data, meta = _asset_meta(path)
    if not data:
        response = Response(
            f"critical static asset is empty: {path.name}\n",
            status=503,
            mimetype="text/plain",
        )
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Static-Bytes"] = "0"
        return response

    response = Response(data, status=200, content_type=meta["content_type"])
    response.content_length = meta["bytes"]
    response.set_etag(meta["sha256"])
    response.headers["Cache-Control"] = "public, max-age=300, must-revalidate"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Static-Bytes"] = str(meta["bytes"])
    response.headers["X-Static-SHA256"] = meta["sha256"][:16]
    return response


def static_health_payload():
    assets = {}
    ok = True
    for name in CRITICAL_ASSETS:
        path = STATIC_DIR / name
        if not path.is_file():
            assets[name] = {"ok": False, "bytes": 0, "error": "missing"}
            ok = False
            continue
        try:
            data, meta = _asset_meta(path)
            item_ok = bool(data)
            assets[name] = {
                "ok": item_ok,
                "bytes": meta["bytes"],
                "sha256": meta["sha256"][:16],
                "content_type": meta["content_type"],
            }
            ok = ok and item_ok
        except Exception as exc:
            assets[name] = {"ok": False, "bytes": 0, "error": str(exc)[:200]}
            ok = False
    return {"ok": ok, "profile": "static_reliability_v31", "assets": assets}


def install_static_reliability(app):
    # Flask's built-in /static rule resolves app.send_static_file dynamically,
    # so replacing this instance method keeps the existing URL contract intact.
    app.send_static_file = reliable_send_static_file

    endpoint = "static_health_v31"
    if endpoint not in app.view_functions:
        app.add_url_rule(
            "/api/static-health",
            endpoint,
            lambda: jsonify(static_health_payload()),
            methods=["GET"],
        )

    return static_health_payload()
