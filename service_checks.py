import os
import time

import requests
from google import genai
from google.genai import types

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
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return {"ok": False, "status": "missing", "label": "Ключ не задан", "latency_ms": None}
    started = time.perf_counter()
    client = None
    try:
        client = genai.Client(api_key=key)
        response = client.models.generate_content(
            model=RADAR_MODEL,
            contents="Reply only OK",
            config=types.GenerateContentConfig(
                max_output_tokens=4,
                thinking_config=types.ThinkingConfig(thinking_level="minimal"),
            ),
        )
        latency = round((time.perf_counter() - started) * 1000)
        text = (response.text or "").strip()
        return {
            "ok": bool(text),
            "status": "connected" if text else "error",
            "label": "Ключ работает" if text else "API ответил без текста",
            "latency_ms": latency,
            "model": RADAR_MODEL,
        }
    except Exception as exc:
        return {
            "ok": False,
            "status": "invalid",
            "label": f"Gemini API: {str(exc)[:180]}",
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "model": RADAR_MODEL,
        }
    finally:
        if client:
            try:
                client.close()
            except Exception:
                pass


def check_all_services():
    return {
        "gemini": check_gemini_key(),
        "apify": check_apify_key(),
    }
