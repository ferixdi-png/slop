import os
import time

import requests

from config import RADAR_MODEL


def check_apify_key():
    token = os.environ.get("APIFY_API_TOKEN", "").strip()
    if not token:
        return {"ok": False, "status": "missing", "label": "Ключ не задан", "latency_ms": None}
    started = time.perf_counter()
    try:
        r = requests.get(
            "https://api.apify.com/v2/users/me",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=12,
        )
        latency = round((time.perf_counter() - started) * 1000)
        if r.status_code != 200:
            return {
                "ok": False,
                "status": "invalid",
                "label": f"Apify отклонил ключ HTTP {r.status_code}",
                "latency_ms": latency,
            }
        data = (r.json() or {}).get("data") or {}
        return {
            "ok": True,
            "status": "connected",
            "label": "Ключ работает",
            "latency_ms": latency,
            "account": data.get("username") or "Apify account",
            "plan": data.get("plan") or "",
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "label": f"Не удалось связаться с Apify: {str(exc)[:120]}",
            "latency_ms": None,
        }


def check_gemini_key():
    """Cheap deterministic credential check: no text generation and no output-token spend."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return {
            "ok": False,
            "status": "missing",
            "label": "Ключ не задан",
            "latency_ms": None,
            "model": RADAR_MODEL,
        }

    started = time.perf_counter()
    try:
        # Official Gemini Models API. A 200 response proves that the API key is
        # accepted and that this exact locked model is visible to the project.
        r = requests.get(
            f"https://generativelanguage.googleapis.com/v1beta/models/{RADAR_MODEL}",
            params={"key": key},
            headers={"Accept": "application/json"},
            timeout=15,
        )
        latency = round((time.perf_counter() - started) * 1000)

        if r.status_code == 200:
            data = r.json() or {}
            returned_name = str(data.get("name") or "")
            return {
                "ok": True,
                "status": "connected",
                "label": "Ключ работает",
                "latency_ms": latency,
                "model": RADAR_MODEL,
                "model_name": returned_name,
            }

        try:
            payload = r.json() or {}
            message = ((payload.get("error") or {}).get("message") or "").strip()
        except Exception:
            message = ""

        if r.status_code in (400, 404):
            label = f"Ключ принят, но модель {RADAR_MODEL} недоступна"
        elif r.status_code in (401, 403):
            label = "Google отклонил API ключ"
        elif r.status_code == 429:
            label = "Gemini API доступен, но сейчас ограничена квота"
        else:
            label = f"Gemini API HTTP {r.status_code}"

        if message:
            label += f": {message[:140]}"

        return {
            "ok": False,
            "status": "invalid" if r.status_code in (401, 403) else "error",
            "label": label,
            "latency_ms": latency,
            "model": RADAR_MODEL,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "error",
            "label": f"Не удалось связаться с Gemini API: {str(exc)[:160]}",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "model": RADAR_MODEL,
        }


def check_all_services():
    return {
        "gemini": check_gemini_key(),
        "apify": check_apify_key(),
    }
