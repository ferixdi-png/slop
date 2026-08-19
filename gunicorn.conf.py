# Loaded automatically by Gunicorn from ./gunicorn.conf.py even when the Render
# service was created manually and its Start Command is still just `gunicorn app:app ...`.
# This keeps one authoritative live log stream in Render → Logs.

import os

# Make the Render contract explicit instead of relying on environment-side
# Gunicorn args. Render requires 0.0.0.0 and exposes PORT (normally 10000).
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

accesslog = "-"
errorlog = "-"
loglevel = "info"
capture_output = True

# Keep the radar request worker stable while long external API calls run in a
# separate background thread. CLI flags on Render may override these values.
timeout = 900
graceful_timeout = 30
keepalive = 5

# Avoid filesystem-backed worker heartbeat stalls on Linux/Render.
worker_tmp_dir = "/dev/shm"


def post_worker_init(worker):
    """Install V31 static reliability after the Flask app is loaded.

    The production incident showed healthy HTML/API responses while browser
    assets were logged as 200 with a zero-byte body. Replacing Flask's dynamic
    send_static_file target with an explicit byte Response makes CSS/JS payloads
    deterministic and exposes their real byte length in headers/logs.
    """
    try:
        from app import app
        from static_reliability_v31 import install_static_reliability

        health = install_static_reliability(app)
        sizes = {name: meta.get("bytes", 0) for name, meta in health.get("assets", {}).items()}
        worker.log.info("V31 STATIC RELIABILITY READY ok=%s sizes=%s", health.get("ok"), sizes)
    except Exception:
        worker.log.exception("V31 static reliability bootstrap failed")
        raise
