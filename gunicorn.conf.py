# Loaded automatically by Gunicorn from ./gunicorn.conf.py even when the Render
# service was created manually and its Start Command is still just `gunicorn app:app ...`.
# This keeps one authoritative live log stream in Render → Logs.

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
