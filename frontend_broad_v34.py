"""V34 product wording/rendering on top of the proven V33 fail-open document.

We reuse V33's single-runtime/browser resilience, but transform its static product
copy and renderer before it is ever served. No second browser runtime is added.
"""

from __future__ import annotations

import hashlib

from flask import Response, jsonify, request

import frontend_failopen_v33 as v33

PROFILE = "frontend_v34_broad_fail_open"

HTML = v33.HTML

_REPLACEMENTS = (
    (
        "frontend_v33_fail_open_single_runtime",
        PROFILE,
    ),
    (
        "Ищем последние 14 дней по #omni, #veo, #veo3, #ai и #ии. Речь и движение обязательны. До 10.05 сек сохраняем исходный тайминг; 10.05–15.05 сек допускается только естественное сжатие до 10 секунд. Discovery hard cap $2.80, общий target полного поиска меньше $5.",
        "Ищем последние 14 дней по #omni, #veo, #veo3, #ai и #ии. Главный критерий — ролик реально набирает просмотры. Речь НЕ обязательна: её можно добавить при адаптации. Gemini только размечает до 20 лидеров и не удаляет сильный тренд. Discovery hard cap $2.80, общий target полного поиска меньше $5.",
    ),
    (
        "V33 FAIL-OPEN SINGLE RUNTIME · HARD BUDGET &lt;$5",
        "V34 BROAD TREND POOL · 50–100 ВАРИАНТОВ · HARD BUDGET &lt;$5",
    ),
    (
        "Speech + timing gate · максимум 150 кандидатов",
        "Gemini enrichment · до 20 лидеров · не является фильтром TOP",
    ),
    (
        "Один frontend-runtime. Никаких legacy observer/polling слоёв.",
        "Широкий тренд-пул: сначала скорость роста, потом AI-подсказки.",
    ),
    (
        "UI всегда остаётся видимым. Динамика работает отдельно: один последовательный refresh-loop и один durable tick-driver. Перезагрузка страницы не создаёт новый поиск и не повторяет платный discovery.",
        "Цель — дать 50–100 сильнейших вариантов, чтобы ты сам выбрал механику. Отсутствие речи, другой тайминг или временная ошибка Gemini больше не скрывают ролик. Перезагрузка страницы не повторяет платный discovery.",
    ),
    (
        "Только прошедшие текущий V30 speech/timing + motion fail-closed профиль.",
        "До 100 роликов по momentum: скорость просмотров, свежесть, доказанный охват. Gemini только подсказывает способ адаптации.",
    ),
    (
        "До 900 raw · максимум 150 смысловых кандидатов · 180 automatic AI ticks hard guard.",
        "До 900 raw · до 150 числовых кандидатов · TOP до 100 · Gemini обогащает максимум 20 лидеров.",
    ),
    (
        "V33 fail-open: если JavaScript не запустится или API временно упадёт, серверный HTML и весь основной интерфейс не скрываются и не заменяются пустым экраном.",
        "V34 broad + V33 fail-open: тренд решают метрики, AI даёт подсказки; при сбое JavaScript/API основной интерфейс всё равно остаётся видимым.",
    ),
    (
        "`<span class=\"badge\">речь + тайминг OK</span>`",
        "`<span class=\"badge\">${esc(x.adaptation_label||'ТРЕНД-КАНДИДАТ')}</span>`",
    ),
    (
        "const status=x.ai_match?'РЕЧЬ + ТАЙМИНГ OK':(x.ai_checked?'ОТКЛОНЁН GEMINI':'GEMINI ПРОВЕРЯЕТ');const cls=x.ai_match?'ok':(x.ai_checked?'no':'');",
        "const status=x.adaptation_label||(x.ai_checked?'AI РАЗМЕТИЛ':'ЕЩЁ БЕЗ AI');const cls=x.adaptation_status==='REPEAT_CORE'?'ok':'';",
    ),
    (
        "stats.push([num(d.matched),'прошли'])",
        "stats.push([num(d.matched),'в тренд-пуле'])",
    ),
    (
        "Сильный TOP пока формируется.",
        "Тренд-пул пока формируется. Как только числовые кандидаты готовы, они появляются здесь без ожидания Gemini.",
    ),
    (
        "Speech + timing gate",
        "Gemini enrichment",
    ),
)

for old, new in _REPLACEMENTS:
    if old in HTML:
        HTML = HTML.replace(old, new)

# Replace the exact static third badge in radarHtml if the template form differs
# slightly from the explicit replacement above.
HTML = HTML.replace(
    "`<span class=\"badge\">речь + тайминг OK</span>`",
    "`<span class=\"badge\">${esc(x.adaptation_label||'ТРЕНД-КАНДИДАТ')}</span>`",
)
HTML = HTML.replace("V33 · fail-open single runtime · JS готов", "V34 · broad trend pool · fail-open JS готов")
HTML = HTML.replace("V33 · HTML загружен · запускаю единый JS runtime…", "V34 · широкий тренд-пул · запускаю fail-open runtime…")
HTML = HTML.replace("V33 · runtime fault зафиксирован · интерфейс сохранён", "V34 · runtime fault зафиксирован · интерфейс сохранён")
HTML = HTML.replace("V33 · связь/динамика восстанавливается · интерфейс сохранён", "V34 · связь/динамика восстанавливается · интерфейс сохранён")
HTML = HTML.replace("window.__V33_LOADED__=true", "window.__V34_LOADED__=true")

HTML_BYTES = HTML.encode("utf-8")
HTML_SHA256 = hashlib.sha256(HTML_BYTES).hexdigest()[:16]


def frontend_health_payload() -> dict:
    return {
        "ok": True,
        "profile": PROFILE,
        "html_bytes": len(HTML_BYTES),
        "html_sha256": HTML_SHA256,
        "external_static_dependencies": 0,
        "root_db_dependency": False,
        "fail_open_dom": True,
        "single_js_runtime": True,
        "legacy_client_scripts": 0,
        "mutation_observers": 0,
        "parallel_polling_layers": 0,
        "runtime_error_surface": True,
        "product_mode": "multiplatform_broad_v34_trendpool100",
        "target_output": "50-100",
        "ai_role": "enrichment_only",
    }


def install_frontend_v34(app) -> dict:
    if "/static/" in HTML:
        raise RuntimeError("V34 frontend must not depend on /static assets")
    if "MutationObserver" in HTML:
        raise RuntimeError("V34 frontend must not contain MutationObserver")
    if HTML.count("<script>") != 1 or HTML.count("</script>") != 1:
        raise RuntimeError("V34 frontend must contain exactly one application script")
    if len(HTML_BYTES) < 20_000:
        raise RuntimeError(f"V34 HTML unexpectedly small: {len(HTML_BYTES)} bytes")

    @app.before_request
    def _v34_broad_fail_open_root():
        if request.method == "GET" and request.path == "/":
            response = Response(HTML_BYTES, status=200, content_type="text/html; charset=utf-8")
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
            response.headers["X-Frontend-Profile"] = PROFILE
            response.headers["X-Frontend-Bytes"] = str(len(HTML_BYTES))
            response.headers["X-Frontend-SHA256"] = HTML_SHA256
            response.headers["X-Radar-Product"] = "multiplatform_broad_v34_trendpool100"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["Referrer-Policy"] = "same-origin"
            return response
        return None

    if "frontend_health_v34" not in app.view_functions:
        app.add_url_rule(
            "/api/frontend-health",
            endpoint="frontend_health_v34",
            view_func=lambda: jsonify(frontend_health_payload()),
            methods=["GET"],
        )

    return frontend_health_payload()
