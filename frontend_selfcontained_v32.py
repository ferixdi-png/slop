from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from flask import Response, jsonify, request


BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = BASE_DIR / "templates" / "index.html"
STATIC_DIR = BASE_DIR / "static"
PROFILE = "frontend_v32_self_contained"
SOURCE_WATCHDOG_SECONDS = max(180, min(900, int(os.environ.get("RADAR_SOURCE_MAX_AGE_SECONDS", "480"))))

_STYLE_RE = re.compile(
    r'<link\s+[^>]*href=["\']/static/([^"\'?]+)(?:\?[^"\']*)?["\'][^>]*>',
    re.IGNORECASE,
)
_SCRIPT_RE = re.compile(
    r'<script\s+[^>]*src=["\']/static/([^"\'?]+)(?:\?[^"\']*)?["\'][^>]*>\s*</script>',
    re.IGNORECASE,
)

_INSTALLED = False
_BUNDLE = None
_META = None


def _asset_text(name: str) -> tuple[str, dict]:
    path = (STATIC_DIR / name).resolve()
    if STATIC_DIR.resolve() not in path.parents or not path.is_file():
        raise RuntimeError(f"V32 asset missing: {name}")
    raw = path.read_bytes()
    if not raw:
        raise RuntimeError(f"V32 asset is empty: {name}")
    text = raw.decode("utf-8")
    return text, {
        "name": name,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest()[:16],
    }


def _runtime_watchdog_script() -> str:
    # This script is intentionally independent from all application JS. Even if one
    # application bundle throws later, this interval keeps running and replaces a
    # silent black page with an actionable visible diagnostic panel.
    return r"""
(() => {
  const GUARD_ID = 'v32RuntimeGuard';
  const healthy = () => {
    const shell = document.querySelector('.app-shell');
    if (!shell) return false;
    const rect = shell.getBoundingClientRect();
    const style = getComputedStyle(shell);
    return rect.width > 200 && rect.height > 200 && style.display !== 'none' && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
  };
  const show = (reason) => {
    let el = document.getElementById(GUARD_ID);
    if (!el) {
      el = document.createElement('div');
      el.id = GUARD_ID;
      el.style.cssText = 'position:fixed;z-index:2147483647;left:18px;right:18px;top:18px;padding:16px 18px;border:1px solid #8b4048;border-radius:12px;background:#2a1217;color:#ffe3e6;font:700 13px/1.5 system-ui,-apple-system,Segoe UI,sans-serif;box-shadow:0 18px 60px rgba(0,0,0,.55)';
      document.body.appendChild(el);
    }
    el.innerHTML = `<b>Интерфейс восстановлен защитой V32</b><br>${String(reason || 'Страница перестала отрисовываться.')}<br><button id="v32Reload" style="margin-top:10px;padding:8px 12px;border:0;border-radius:8px;background:#b7ff33;color:#081008;font-weight:900;cursor:pointer">ПЕРЕЗАГРУЗИТЬ</button>`;
    const btn = document.getElementById('v32Reload');
    if (btn) btn.onclick = () => location.reload();
  };
  const clear = () => document.getElementById(GUARD_ID)?.remove();

  window.addEventListener('error', (event) => {
    const message = event?.message || event?.error?.message;
    if (message && !healthy()) show(`JavaScript error: ${message}`);
  });
  window.addEventListener('unhandledrejection', (event) => {
    if (!healthy()) show(`Unhandled promise: ${event?.reason?.message || event?.reason || 'unknown'}`);
  });

  const check = () => {
    if (healthy()) clear();
    else show('Основной DOM исчез или стал невидимым. Серверный HTML остаётся загруженным, поэтому пустого чёрного экрана больше быть не должно.');
  };
  setTimeout(check, 2500);
  setInterval(check, 3000);
})();
"""


def _build_bundle() -> tuple[bytes, dict]:
    if not TEMPLATE_PATH.is_file():
        raise RuntimeError("V32 template missing: templates/index.html")
    template = TEMPLATE_PATH.read_text("utf-8")
    assets: list[dict] = []

    def style_repl(match):
        name = match.group(1)
        text, meta = _asset_text(name)
        assets.append(meta)
        safe = text.replace("</style", "<\\/style")
        return f'<style data-v32-asset="{name}">\n{safe}\n</style>'

    def script_repl(match):
        name = match.group(1)
        text, meta = _asset_text(name)
        assets.append(meta)
        safe = text.replace("</script", "<\\/script")
        return f'<script data-v32-asset="{name}">\n{safe}\n</script>'

    html = _STYLE_RE.sub(style_repl, template)
    html = _SCRIPT_RE.sub(script_repl, html)

    # V31's initial boot guard is still useful, but the page no longer depends on
    # external static requests. Mark the UI honestly as V32 and append a permanent
    # independent watchdog that survives application-level JS errors.
    html = html.replace("V31 STATIC RELIABILITY", "V32 SELF-CONTAINED UI")
    html = html.replace("V31 static integrity", "V32 single-response UI")
    html = html.replace("V31-поиска", "V32-поиска")
    html = html.replace("ПУЛ КАНДИДАТОВ V31", "ПУЛ КАНДИДАТОВ V32")
    html = html.replace("V31 дополнительно контролирует целостность CSS/JS и не допускает немого чёрного экрана.", "V32 встраивает весь CSS/JS прямо в HTML и не зависит от отдельной доставки static-файлов.")
    html = html.replace("V31 отдельно защищает браузерный слой от пустых CSS/JS-ответов.", "V32 отдаёт интерфейс одним самодостаточным HTML-ответом и постоянно контролирует, что DOM остаётся видимым.")

    watchdog = _runtime_watchdog_script().replace("</script", "<\\/script")
    html = html.replace("</body>", f'<script data-v32-watchdog="1">\n{watchdog}\n</script>\n</body>')

    # If these remain, the browser can still depend on Render static delivery. Fail the
    # deployment instead of shipping another build capable of silently going black.
    lower = html.lower()
    if 'src="/static/' in lower or "src='/static/" in lower or 'href="/static/' in lower or "href='/static/" in lower:
        raise RuntimeError("V32 bundle still contains external /static dependency")

    body = html.encode("utf-8")
    if len(body) < 50_000:
        raise RuntimeError(f"V32 bundle unexpectedly small: {len(body)} bytes")

    names = [item["name"] for item in assets]
    if len(names) != len(set(names)):
        raise RuntimeError("V32 duplicated a static asset while bundling")

    meta = {
        "ok": True,
        "profile": PROFILE,
        "html_bytes": len(body),
        "html_sha256": hashlib.sha256(body).hexdigest()[:16],
        "assets": assets,
        "asset_count": len(assets),
        "external_static_dependencies": 0,
        "runtime_watchdog": True,
        "root_db_dependency": False,
        "source_watchdog_seconds": SOURCE_WATCHDOG_SECONDS,
    }
    return body, meta


def frontend_health_payload() -> dict:
    global _BUNDLE, _META
    if _BUNDLE is None or _META is None:
        _BUNDLE, _META = _build_bundle()
    return dict(_META)


def install_frontend_v32(app) -> dict:
    global _INSTALLED, _BUNDLE, _META
    if _INSTALLED:
        return frontend_health_payload()

    # Build at process startup. A broken/missing asset makes the new deploy fail before
    # Render marks it live, allowing the previous healthy deploy to keep serving.
    _BUNDLE, _META = _build_bundle()

    @app.before_request
    def _v32_self_contained_root():
        if request.method == "GET" and request.path == "/":
            response = Response(_BUNDLE, status=200, content_type="text/html; charset=utf-8")
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            response.headers["X-Frontend-Profile"] = PROFILE
            response.headers["X-Frontend-Bytes"] = str(len(_BUNDLE))
            response.headers["X-Frontend-SHA256"] = _META["html_sha256"]
            return response
        return None

    if "frontend_health_v32" not in app.view_functions:
        app.add_url_rule(
            "/api/frontend-health",
            endpoint="frontend_health_v32",
            view_func=lambda: jsonify(frontend_health_payload()),
            methods=["GET"],
        )

    # The current job already uses radar_hardening_v19._poll_sources. That function
    # reads this module global on every tick, so tightening it here also fixes an
    # already-running durable run after the next Render deploy without restarting paid
    # discovery.
    try:
        import radar_hardening_v19 as hardening
        hardening.SOURCE_MAX_AGE_SECONDS = SOURCE_WATCHDOG_SECONDS
    except Exception:
        pass

    _INSTALLED = True
    return frontend_health_payload()
