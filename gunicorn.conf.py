"""Gunicorn production contract for Render.

The live V33/V34 frontend is a single self-contained HTML response and has zero
/static dependencies. Do not install legacy V31 static middleware at worker boot:
unused CSS/JS files must never be able to make an otherwise healthy production
worker fail to start.
"""

import os

# Render requires 0.0.0.0 and exposes PORT (normally 10000).
bind = f"0.0.0.0:{os.environ.get('PORT', '10000')}"

accesslog = "-"
errorlog = "-"
loglevel = "info"
capture_output = True

# Long external calls execute within the radar workflow; keep the worker alive
# while preserving a bounded graceful shutdown window.
timeout = 900
graceful_timeout = 30
keepalive = 5

# Avoid filesystem-backed worker heartbeat stalls on Linux/Render.
worker_tmp_dir = "/dev/shm"
