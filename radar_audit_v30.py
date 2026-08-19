"""V30 adversarial audit overlay.

Closes the ten highest-risk gaps left after V29 without widening product scope or
raising the <$5 run budget:
1) refuse paid discovery when the durable Apify KVS mirror is unavailable;
2) quarantine ambiguous Actor-start transport failures instead of blindly paying twice;
3) cap every on-demand/manual media refresh Actor to one item and a tiny dollar limit;
4) cache identical production analyses so repeated clicks do not repay Gemini;
5) serialize concurrent analysis of the same source inside the one-worker Render process;
6) persist a hard automatic screening-tick budget including retry allowance;
7) make radar snapshots cover the real 14-day/900-item V29 surface and merge-recover partial DBs;
8) validate remote media URLs/redirects/DNS/size before downloading;
9) fail closed when local motion verification is unavailable and hard-reject static/slideshow video in direct YouTube screening;
10) reject cross-site mutation requests and debounce accidental immediate full reruns.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urljoin, urlparse

import requests
from flask import g, jsonify, request

import actor_utils
import cloud_state
import radar_budget_v10 as legacy_budget
import radar_budget_v29 as v29
import radar_fresh_run_v23 as fresh
import radar_growth_v6 as growth
import radar_hardening_v19 as hardening
import radar_multiplatform_v28 as v28
import radar_request_job as radar_job
import radar_resilient_v17 as v17
import radar_service
import reel_media
from config import ANALYSIS_MODEL
from db import db_conn
from media_duration import measure_video_duration
from overlay_cleanplate_v15 import PRODUCTION_PROFILE_VERSION
from progress import set_radar_status
from radar_logs import add_radar_log
from static_video_gate import inspect_visual_motion

MODE_VERSION = "multiplatform_speech_v30_audit10_budget5"
SCREENING_PROFILE = MODE_VERSION
SOURCE_MARKER = "STRICT_MULTIPLATFORM_AUDIT_V30"
SNAPSHOT_LOOKBACK_DAYS = 14
SNAPSHOT_POST_LIMIT = 1000
SNAPSHOT_CREATOR_LIMIT = 500
MAX_AUTOMATIC_AI_TICKS = 180  # 150 candidates + 30 conservative retry ticks.
MANUAL_REFRESH_CAP_USD = Decimal("0.12")
MANUAL_REFRESH_MAX_ITEMS = 1
SOURCE_START_UNCERTAIN_SECONDS = 960  # longer than the 15-minute source watchdog.
NEW_RUN_DEBOUNCE_SECONDS = 60
MAX_VIDEO_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 4

_APPLIED = False
_BASE_START_SOURCE = None
_BASE_ADVANCE = None
_BASE_PROCESS_AI = None
_BASE_CREATE_JOB = None
_BASE_SCREEN_PROMPT = None
_BASE_CLASSIFY_FILE = None
_ANALYSIS_ACTIVE: set[int] = set()
_ANALYSIS_LOCK = threading.Lock()
_ANALYZE_RE = re.compile(r"^/api/radar/(\d+)/analyze$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _ensure_analysis_schema():
    with db_conn() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(analyses)").fetchall()}
        additions = {
            "production_profile": "TEXT DEFAULT ''",
            "source_duration_sec": "REAL DEFAULT 0",
            "owned_or_licensed": "INTEGER DEFAULT 0",
            "source_fingerprint": "TEXT DEFAULT ''",
        }
        for name, definition in additions.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE analyses ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_analyses_v30_cache "
            "ON analyses(source_url,model,production_profile,owned_or_licensed,created_at)"
        )
        conn.commit()


# ---------------------------------------------------------------------------
# 7) Snapshot truth: real 14-day surface, enough rows, merge recovery.
# ---------------------------------------------------------------------------

def save_radar_snapshot_v30():
    with db_conn() as conn:
        posts = cloud_state._table_rows(
            conn,
            "radar_posts",
            f"datetime(published_at)>=datetime('now','-{SNAPSHOT_LOOKBACK_DAYS} days')",
            limit=SNAPSHOT_POST_LIMIT,
        )
        creators = cloud_state._table_rows(conn, "tracked_creators", limit=SNAPSHOT_CREATOR_LIMIT)
        meta = [
            dict(row)
            for row in conn.execute("SELECT * FROM radar_meta ORDER BY id DESC LIMIT 5").fetchall()
        ]
    payload = {
        "version": 30,
        "profile": SCREENING_PROFILE,
        "saved_at": _now_iso(),
        "lookback_days": SNAPSHOT_LOOKBACK_DAYS,
        "post_limit": SNAPSHOT_POST_LIMIT,
        "posts": posts,
        "tracked_creators": creators,
        "radar_meta": meta,
    }
    return cloud_state.save_cloud_record(cloud_state.RECORD_KEY, payload)


def restore_radar_snapshot_merge_v30():
    """Merge a current snapshot into an empty OR partially reconstructed SQLite DB."""
    payload = cloud_state.load_cloud_record(cloud_state.RECORD_KEY)
    if not isinstance(payload, dict):
        return False
    restored = 0
    with db_conn() as conn:
        restored += cloud_state._restore_rows(conn, "radar_posts", payload.get("posts") or [])
        restored += cloud_state._restore_rows(conn, "tracked_creators", payload.get("tracked_creators") or [])
        restored += cloud_state._restore_rows(conn, "radar_meta", payload.get("radar_meta") or [])
        conn.commit()
    return bool(restored)


# ---------------------------------------------------------------------------
# 8) Remote-media download hardening against SSRF/redirect/html payloads.
# ---------------------------------------------------------------------------

def _validate_public_https_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise RuntimeError("MEDIA_URL_REJECTED: only public HTTPS video URLs are allowed")
    if parsed.username or parsed.password:
        raise RuntimeError("MEDIA_URL_REJECTED: credentials in media URL are forbidden")
    if parsed.port not in (None, 443):
        raise RuntimeError("MEDIA_URL_REJECTED: non-standard HTTPS ports are forbidden")
    host = parsed.hostname.rstrip(".")
    try:
        infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except Exception as exc:
        raise RuntimeError(f"MEDIA_DNS_FAILED: {host}") from exc
    addresses = {info[4][0] for info in infos if info and info[4]}
    if not addresses:
        raise RuntimeError("MEDIA_DNS_FAILED: no address")
    for raw in addresses:
        ip = ipaddress.ip_address(raw.split("%", 1)[0])
        if not ip.is_global:
            raise RuntimeError(f"MEDIA_URL_REJECTED: non-public address {ip}")
    return parsed.geturl()


def safe_download_temp_video_v30(url: str):
    current = _validate_public_https_url(url)
    response = None
    try:
        for _ in range(MAX_REDIRECTS + 1):
            response = requests.get(
                current,
                stream=True,
                timeout=(10, 45),
                allow_redirects=False,
                headers={"User-Agent": "Mozilla/5.0", "Accept": "video/*,application/octet-stream;q=0.9,*/*;q=0.1"},
            )
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location") or ""
                response.close()
                response = None
                if not location:
                    raise RuntimeError("MEDIA_REDIRECT_REJECTED: empty redirect")
                current = _validate_public_https_url(urljoin(current, location))
                continue
            break
        else:
            raise RuntimeError("MEDIA_REDIRECT_REJECTED: too many redirects")

        if response is None:
            raise RuntimeError("MEDIA_DOWNLOAD_FAILED: no response")
        response.raise_for_status()
        content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        if content_type in {"text/html", "text/plain", "application/json", "application/xml", "text/xml"}:
            raise RuntimeError(f"MEDIA_CONTENT_REJECTED: {content_type}")
        try:
            declared = int(response.headers.get("Content-Length") or 0)
        except Exception:
            declared = 0
        if declared > MAX_VIDEO_BYTES:
            raise RuntimeError("Видео из радара больше 50 МБ")

        total = 0
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".mp4")
        try:
            for chunk in response.iter_content(1024 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_VIDEO_BYTES:
                    raise RuntimeError("Видео из радара больше 50 МБ")
                tmp.write(chunk)
            tmp.close()
            if total < 1024:
                raise RuntimeError("MEDIA_CONTENT_REJECTED: payload is too small to be a video")
            return tmp.name
        except Exception:
            tmp.close()
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise
    finally:
        if response is not None:
            response.close()


# ---------------------------------------------------------------------------
# 3) Capped manual/on-demand Actor refreshes.
# ---------------------------------------------------------------------------

def capped_refresh_actor_items_v30(client, actor_id, run_input):
    add_radar_log(
        f"V30 manual refresh: {actor_id} hard cap ${MANUAL_REFRESH_CAP_USD} / maxItems=1.",
        stage="manual-refresh-budget",
        details={"actor": actor_id, "max_items": 1, "max_total_charge_usd": float(MANUAL_REFRESH_CAP_USD)},
    )
    run = client.actor(actor_id).start(
        run_input=dict(run_input or {}),
        max_items=MANUAL_REFRESH_MAX_ITEMS,
        max_total_charge_usd=MANUAL_REFRESH_CAP_USD,
        restart_on_error=False,
    ) or {}
    run_id = run.get("id") or run.get("runId") or ""
    if not run_id:
        raise RuntimeError(f"{actor_id}: Apify не вернул runId")
    state = {
        "actor_id": actor_id,
        "run_id": run_id,
        "run_client": client.run(run_id),
        "started": time.monotonic(),
        "max_wait": min(180, actor_utils._max_wait_seconds(actor_id)),
        "final_run": run,
        "last_status": "",
        "last_message": "",
        "last_heartbeat_bucket": -1,
    }
    while True:
        final_run = actor_utils._poll_one(state)
        if final_run is not None:
            return actor_utils._items_from_success(client, state, final_run)[:1]
        time.sleep(actor_utils.POLL_SECONDS)


# ---------------------------------------------------------------------------
# 9) Fail-closed motion verification + explicit direct-URL static rejection.
# ---------------------------------------------------------------------------

def screen_prompt_v30(caption, measured, platform):
    base = _BASE_SCREEN_PROMPT(caption, measured, platform)
    return base + """

V30 HARD VISUAL MOTION RULE:
A static image, quote card, screenshot, poster, single-photo video, slideshow, or almost-motionless card is always REJECT even if it contains spoken narration.
If the source is static/slideshow-like, set simple_situation=false AND reproducible_format=false and start reason with STATIC_OR_SLIDESHOW.
A slow digital zoom/pan over one still image does not count as a moving scene.
"""


def classify_file_v30(file_path, caption="", platform=""):
    measured = float(measure_video_duration(file_path, fallback=0) or 0)
    if measured < 1.0 or measured > v28.SOURCE_MAX_DURATION_SEC:
        return v28._reject(
            f"DURATION_GATE: actual video duration {measured:.2f}s is outside 1.00-{v28.SOURCE_MAX_DURATION_SEC:.2f}s"
        )
    motion = inspect_visual_motion(file_path)
    if not motion.checked:
        return v28._reject(f"MOTION_GATE_UNAVAILABLE: fail-closed before Gemini; {motion.reason}")
    if motion.is_static_image_video:
        return v28._reject(f"REJECT_STATIC_IMAGE: {motion.reason}")
    return _BASE_CLASSIFY_FILE(file_path, caption, platform)


# ---------------------------------------------------------------------------
# 1+2) Durable paid-step preflight and ambiguous Actor-start quarantine/adoption.
# ---------------------------------------------------------------------------

def _cloud_mirror_available() -> bool:
    if not os.environ.get("APIFY_API_TOKEN", "").strip():
        return False
    try:
        return cloud_state._store_client() is not None
    except Exception:
        return False


def _ambiguous_start_error(exc) -> bool:
    text = str(exc or "").lower()
    safe_definite = (
        "monthly usage hard limit",
        "monthly usage limit",
        "401",
        "403",
        "invalid input",
        "bad request",
        "validation",
    )
    if any(x in text for x in safe_definite):
        return False
    ambiguous = (
        "timeout",
        "timed out",
        "connection",
        "reset by peer",
        "remote disconnected",
        "502",
        "503",
        "504",
        "temporarily unavailable",
        "eof",
    )
    return any(x in text for x in ambiguous)


def _next_unstarted_source(job):
    for name, source in (job.get("sources") or {}).items():
        if not source.get("run_id"):
            return name, source
    return None, None


def _canonical(value) -> str:
    return json.dumps(value or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _adopt_uncertain_run(client, source):
    actor_id = str((source or {}).get("actor_id") or "")
    intent_at = _parse_dt((source or {}).get("start_intent_at"))
    if not actor_id or not intent_at:
        return None
    try:
        listed = client.actor(actor_id).runs().list(
            limit=8,
            desc=True,
            started_after=intent_at - timedelta(seconds=30),
        )
        items = list(getattr(listed, "items", None) or [])
    except Exception as exc:
        add_radar_log(f"V30 adoption lookup failed: {exc}", level="WARN", stage="apify-start-adopt")
        return None

    expected = _canonical((source or {}).get("input") or {})
    for raw in items:
        run = dict(raw) if isinstance(raw, dict) else getattr(raw, "model_dump", lambda: {})()
        run_id = str(run.get("id") or run.get("runId") or "")
        kvs_id = str(run.get("defaultKeyValueStoreId") or run.get("default_key_value_store_id") or "")
        if not run_id or not kvs_id:
            continue
        try:
            record = client.key_value_store(kvs_id).get_record("INPUT")
            value = cloud_state._record_value(record)
        except Exception:
            continue
        if _canonical(value) != expected:
            continue
        return run
    return None


def start_one_source_v30(client, job):
    name, source = _next_unstarted_source(job)
    if source is None:
        return _BASE_START_SOURCE(client, job)

    # Persist immediately, then prove the cloud mirror API is reachable BEFORE paid work.
    job["durability_preflight_at"] = _now_iso()
    radar_job._persist(job)
    if not _cloud_mirror_available():
        job["phase"] = "durability_blocked"
        job["durability_blocked_at"] = _now_iso()
        job["last_error"] = "DURABLE_CLOUD_MIRROR_UNAVAILABLE"
        radar_job._persist(job)
        set_radar_status(
            "running",
            "Жду durable-хранилище",
            2,
            45,
            "Apify KVS mirror недоступен. Платный Actor НЕ запускаю, чтобы Render restart не создал дубль.",
            warning="DURABLE_CLOUD_MIRROR_UNAVAILABLE",
            details={"run_id": job.get("run_id"), "source": name, "paid_work_started": False},
        )
        return job

    source["start_intent_id"] = hashlib.sha1(
        f"{job.get('run_id')}|{name}|{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:16]
    source["start_intent_at"] = _now_iso()
    source["start_intent_state"] = "armed"
    radar_job._persist(job)

    try:
        result = _BASE_START_SOURCE(client, job)
    except Exception as exc:
        if not _ambiguous_start_error(exc):
            source["start_intent_state"] = "definite_failure"
            source["start_intent_error"] = str(exc)[:500]
            radar_job._persist(job)
            raise
        source["start_intent_state"] = "uncertain"
        source["start_intent_error"] = str(exc)[:500]
        source["start_uncertain_until"] = (
            datetime.now(timezone.utc) + timedelta(seconds=SOURCE_START_UNCERTAIN_SECONDS)
        ).isoformat()
        job["phase"] = "source_start_uncertain"
        radar_job._persist(job)
        add_radar_log(
            "V30 START QUARANTINE: transport error may have started an Actor; automatic duplicate start blocked.",
            level="ERROR",
            stage="apify-start-quarantine",
            details={"source": name, "actor": source.get("actor_id"), "error": str(exc)[:300]},
        )
        return job

    updated = (result.get("sources") or {}).get(name) or source
    if updated.get("run_id"):
        updated["start_intent_state"] = "confirmed"
        updated["start_confirmed_at"] = _now_iso()
        updated.pop("start_intent_error", None)
        updated.pop("start_uncertain_until", None)
        radar_job._persist(result)
    return result


def advance_v30(job):
    phase = str((job or {}).get("phase") or "")
    if phase == "durability_blocked":
        if _cloud_mirror_available():
            job["phase"] = "starting_sources"
            job["last_error"] = ""
            radar_job._persist(job)
            return _BASE_ADVANCE(job)
        set_radar_status(
            "running", "Жду durable-хранилище", 2, 45,
            "KVS mirror всё ещё недоступен; платная работа остаётся заблокированной.",
            warning="DURABLE_CLOUD_MIRROR_UNAVAILABLE",
            details={"run_id": job.get("run_id"), "paid_work_started": False},
        )
        return job

    if phase == "source_start_uncertain":
        name, source = _next_unstarted_source(job)
        if source is None:
            job["phase"] = "discovering"
            radar_job._persist(job)
            return job
        client = radar_job._client()
        adopted = _adopt_uncertain_run(client, source)
        if adopted:
            source["run_id"] = adopted.get("id") or adopted.get("runId") or ""
            source["status"] = str(adopted.get("status") or "READY").upper()
            source["dataset_id"] = adopted.get("defaultDatasetId") or adopted.get("default_dataset_id") or ""
            source["start_intent_state"] = "adopted"
            source["start_adopted_at"] = _now_iso()
            source.pop("start_uncertain_until", None)
            job["phase"] = "discovering" if all(x.get("run_id") for x in job["sources"].values()) else "starting_sources"
            radar_job._persist(job)
            add_radar_log(
                f"V30 START ADOPTED: найден уже созданный run {source['run_id']} вместо повторной оплаты.",
                stage="apify-start-adopt",
                details={"source": name, "run_id": source["run_id"]},
            )
            return job

        until = _parse_dt(source.get("start_uncertain_until"))
        if until and datetime.now(timezone.utc) < until:
            remaining = int((until - datetime.now(timezone.utc)).total_seconds())
            set_radar_status(
                "running", "Проверяю неопределённый Actor start", 3, remaining,
                "Новый Actor не запускаю. Ищу уже созданный run и жду истечения безопасного окна.",
                warning="SOURCE_START_UNCERTAIN",
                details={"run_id": job.get("run_id"), "source": name, "remaining_seconds": remaining},
            )
            return job

        job["phase"] = "failed"
        job["error"] = "SOURCE_START_UNCERTAIN_NOT_ADOPTED"
        job["failed_at"] = _now_iso()
        radar_job._persist(job)
        set_radar_status(
            "error", "Actor start не подтверждён", 0, None,
            "Безопасное окно истекло. Run остановлен вместо слепого повторного платного запуска.",
            warning="SOURCE_START_UNCERTAIN_NOT_ADOPTED",
            details={"run_id": job.get("run_id"), "source": name},
        )
        return job

    return _BASE_ADVANCE(job)


# ---------------------------------------------------------------------------
# 6) Persistent automatic Gemini retry/call-unit budget.
# ---------------------------------------------------------------------------

def process_one_ai_v30(job):
    stats = dict(job.get("stats") or {})
    used = int(stats.get("automatic_ai_ticks_v30") or 0)
    if used >= MAX_AUTOMATIC_AI_TICKS:
        deferred = 0
        for item in job.get("candidates") or []:
            if item.get("ai_done"):
                continue
            item["ai_done"] = True
            item["ai_match"] = False
            item["ai_deferred_budget"] = True
            item["ai_error"] = "AUTOMATIC_AI_BUDGET_GUARD"
            deferred += 1
        stats["automatic_ai_budget_exhausted"] = True
        stats["automatic_ai_deferred"] = deferred
        job["stats"] = stats
        job["phase"] = "finalizing"
        radar_job._persist(job)
        add_radar_log(
            f"V30 GEMINI BUDGET GUARD: {used} screening ticks reached; deferred={deferred}.",
            level="WARN", stage="gemini-budget",
        )
        return job

    stats["automatic_ai_ticks_v30"] = used + 1
    stats["automatic_ai_tick_limit_v30"] = MAX_AUTOMATIC_AI_TICKS
    job["stats"] = stats
    radar_job._persist(job)  # reserve the unit BEFORE any external model call.
    return _BASE_PROCESS_AI(job)


# ---------------------------------------------------------------------------
# 10a) Debounce accidental immediate paid reruns.
# ---------------------------------------------------------------------------

def create_or_resume_v30():
    existing = cloud_state.load_radar_job() or {}
    if not radar_job._is_active(existing) and str(existing.get("phase") or "") == "done":
        finished = _parse_dt(existing.get("completed_at") or existing.get("updated_at"))
        if finished:
            age = (datetime.now(timezone.utc) - finished).total_seconds()
            if age < NEW_RUN_DEBOUNCE_SECONDS:
                remaining = max(1, int(NEW_RUN_DEBOUNCE_SECONDS - age))
                return {
                    **radar_job.public_job(existing),
                    "accepted": False,
                    "completed": True,
                    "rerun_debounced": True,
                    "retry_after_seconds": remaining,
                    "message": f"Предыдущий полный run только что завершён. Новый платный discovery доступен через {remaining} сек.",
                }, 429
    return _BASE_CREATE_JOB()


# ---------------------------------------------------------------------------
# 4+5+10b) Analysis cache/serialization and cross-site mutation guard.
# ---------------------------------------------------------------------------

def _same_origin_mutation_allowed() -> bool:
    fetch_site = str(request.headers.get("Sec-Fetch-Site") or "").lower()
    if fetch_site == "cross-site":
        return False
    origin = str(request.headers.get("Origin") or "").strip()
    if origin:
        try:
            parsed = urlparse(origin)
            if parsed.netloc and parsed.netloc.lower() != request.host.lower():
                return False
        except Exception:
            return False
    return True


def _analysis_fingerprint(post_url: str, duration: float, owned: bool) -> str:
    raw = f"{post_url}|{float(duration or 0):.3f}|{ANALYSIS_MODEL}|{PRODUCTION_PROFILE_VERSION}|{int(owned)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _release_analysis_lock():
    item_id = getattr(g, "v30_analysis_item_id", None)
    if item_id is None or not getattr(g, "v30_analysis_lock_held", False):
        return
    with _ANALYSIS_LOCK:
        _ANALYSIS_ACTIVE.discard(int(item_id))
    g.v30_analysis_lock_held = False


def install_app_guards_v30(app):
    if getattr(app, "_v30_audit_guards", False):
        return

    @app.before_request
    def v30_before_request():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.path.startswith("/api/radar"):
            if not _same_origin_mutation_allowed():
                return jsonify(error="Cross-site mutation blocked by V30"), 403

        match = _ANALYZE_RE.match(request.path) if request.method == "POST" else None
        if not match:
            return None
        item_id = int(match.group(1))
        with _ANALYSIS_LOCK:
            if item_id in _ANALYSIS_ACTIVE:
                return jsonify(error="Этот ролик уже анализируется. Дождись текущего production-анализа."), 409
            _ANALYSIS_ACTIVE.add(item_id)
        g.v30_analysis_item_id = item_id
        g.v30_analysis_lock_held = True

        body = request.get_json(silent=True) or {}
        owned = bool(body.get("owned_or_licensed"))
        g.v30_analysis_owned = owned
        if bool(body.get("force")):
            return None

        with db_conn() as conn:
            post = conn.execute(
                "SELECT post_url,duration_sec,ai_match,screening_profile FROM radar_posts WHERE id=?",
                (item_id,),
            ).fetchone()
            if not post or not int(post["ai_match"] or 0) or str(post["screening_profile"] or "") != SCREENING_PROFILE:
                return None
            fingerprint = _analysis_fingerprint(post["post_url"], float(post["duration_sec"] or 0), owned)
            row = conn.execute(
                """SELECT id,created_at,model,result_json,source_duration_sec,production_profile
                   FROM analyses
                   WHERE source_url=? AND model=? AND production_profile=?
                     AND owned_or_licensed=? AND source_fingerprint=?
                   ORDER BY id DESC LIMIT 1""",
                (post["post_url"], ANALYSIS_MODEL, PRODUCTION_PROFILE_VERSION, 1 if owned else 0, fingerprint),
            ).fetchone()
        if not row:
            return None
        try:
            result = json.loads(row["result_json"] or "{}")
        except Exception:
            return None
        add_radar_log(
            f"V30 ANALYSIS CACHE HIT: Reel #{item_id}, повторная Gemini production-цепочка не запущена.",
            stage="analysis-cache",
            details={"analysis_id": row["id"], "production_profile": PRODUCTION_PROFILE_VERSION},
        )
        return jsonify(
            id=row["id"],
            model=row["model"],
            generation_target="gemini-omni-flash-preview",
            production_profile=PRODUCTION_PROFILE_VERSION,
            source_duration_sec=float(row["source_duration_sec"] or post["duration_sec"] or 0),
            cached=True,
            result=result,
        )

    @app.after_request
    def v30_after_request(response):
        try:
            if request.path == "/api/status" and response.is_json:
                data = response.get_json(silent=True)
                if isinstance(data, dict):
                    with db_conn() as conn:
                        current_matches = conn.execute(
                            """SELECT COUNT(*) FROM radar_posts
                               WHERE datetime(published_at)>=datetime('now','-14 days')
                                 AND ai_match=1 AND COALESCE(screening_profile,'')=?
                                 AND duration_sec>=1.0 AND duration_sec<=15.05""",
                            (SCREENING_PROFILE,),
                        ).fetchone()[0]
                    data["radar_matches"] = int(current_matches or 0)
                    data["radar_audit_profile"] = MODE_VERSION
                    data["radar_snapshot_lookback_days"] = SNAPSHOT_LOOKBACK_DAYS
                    data["radar_snapshot_post_limit"] = SNAPSHOT_POST_LIMIT
                    data["radar_automatic_ai_tick_limit"] = MAX_AUTOMATIC_AI_TICKS
                    data["radar_manual_refresh_cap_usd"] = float(MANUAL_REFRESH_CAP_USD)
                    response.set_data(app.json.dumps(data))
                    response.mimetype = "application/json"

            match = _ANALYZE_RE.match(request.path) if request.method == "POST" else None
            if match and response.status_code == 200 and response.is_json:
                data = response.get_json(silent=True) or {}
                if not data.get("cached") and data.get("id"):
                    item_id = int(match.group(1))
                    owned = bool(getattr(g, "v30_analysis_owned", False))
                    with db_conn() as conn:
                        post = conn.execute("SELECT post_url,duration_sec FROM radar_posts WHERE id=?", (item_id,)).fetchone()
                        if post:
                            duration = float(data.get("source_duration_sec") or post["duration_sec"] or 0)
                            fingerprint = _analysis_fingerprint(post["post_url"], duration, owned)
                            conn.execute(
                                """UPDATE analyses SET production_profile=?,source_duration_sec=?,owned_or_licensed=?,source_fingerprint=?
                                   WHERE id=?""",
                                (PRODUCTION_PROFILE_VERSION, duration, 1 if owned else 0, fingerprint, int(data["id"])),
                            )
                            conn.commit()
        finally:
            _release_analysis_lock()

        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Robots-Tag", "noindex, nofollow")
        if request.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    @app.teardown_request
    def v30_teardown_request(_exc):
        _release_analysis_lock()

    app._v30_audit_guards = True


def apply_audit_v30():
    global _APPLIED, _BASE_START_SOURCE, _BASE_ADVANCE, _BASE_PROCESS_AI, _BASE_CREATE_JOB
    global _BASE_SCREEN_PROMPT, _BASE_CLASSIFY_FILE
    if _APPLIED:
        return {
            "mode": MODE_VERSION,
            "screening_profile": SCREENING_PROFILE,
            "audit_top10_closed": True,
        }
    _APPLIED = True

    # New semantic profile invalidates all old V29 pass/reject cache truth.
    v28.MODE_VERSION = MODE_VERSION
    v28.SCREENING_PROFILE = SCREENING_PROFILE
    v28.SOURCE_MARKER = SOURCE_MARKER
    v29.MODE_VERSION = MODE_VERSION
    v29.SCREENING_PROFILE = SCREENING_PROFILE
    v29.SOURCE_MARKER = SOURCE_MARKER
    legacy_budget.PROFILE_VERSION = SCREENING_PROFILE
    growth.PROFILE_VERSION = SCREENING_PROFILE
    hardening.PROFILE_VERSION = SCREENING_PROFILE
    v17.PROFILE_VERSION = SCREENING_PROFILE

    _ensure_analysis_schema()

    # Snapshot wrappers: keep the existing every-10-candidates throttle, replace its underlying writer.
    cloud_state.save_radar_snapshot = save_radar_snapshot_v30
    growth._ORIGINAL_SNAPSHOT = save_radar_snapshot_v30
    fresh._BASE_RESTORE = restore_radar_snapshot_merge_v30

    # Safe media downloader reaches both direct V28 downloads and Instagram manual downloader.
    radar_service.download_temp_video = safe_download_temp_video_v30
    reel_media.download_temp_video = safe_download_temp_video_v30

    # Any one-item media refresh from the manual analysis path gets platform-side caps.
    reel_media.run_actor_items_checked = capped_refresh_actor_items_v30
    v28.run_actor_items_checked = capped_refresh_actor_items_v30

    _BASE_SCREEN_PROMPT = v28._screen_prompt
    _BASE_CLASSIFY_FILE = v28.classify_file_v28
    v28._screen_prompt = screen_prompt_v30
    v28.classify_file_v28 = classify_file_v30

    _BASE_START_SOURCE = radar_job._start_one_source
    _BASE_ADVANCE = radar_job._advance
    _BASE_PROCESS_AI = radar_job._process_one_ai
    radar_job._start_one_source = start_one_source_v30
    radar_job._advance = advance_v30
    radar_job._process_one_ai = process_one_ai_v30
    radar_job.ACTIVE_PHASES.update({"durability_blocked", "source_start_uncertain"})

    app_module = __import__("sys").modules.get("app")
    if app_module is None:
        raise RuntimeError("V30 audit overlay must be applied from app startup")
    _BASE_CREATE_JOB = app_module.create_or_resume_job
    app_module.create_or_resume_job = create_or_resume_v30
    app_module.PROFILE_VERSION = SCREENING_PROFILE
    install_app_guards_v30(app_module.app)

    with db_conn() as conn:
        conn.execute(
            "UPDATE radar_posts SET ai_checked=0,ai_match=0 WHERE COALESCE(screening_profile,'')<>?",
            (SCREENING_PROFILE,),
        )
        conn.commit()

    info = {
        "mode": MODE_VERSION,
        "screening_profile": SCREENING_PROFILE,
        "platforms": list(v28.PLATFORMS),
        "hashtags": list(v28.TARGET_TAGS),
        "lookback_days": v28.LOOKBACK_DAYS,
        "results_per_tag_per_platform": v29.RESULTS_PER_TAG,
        "max_raw_requested": v29.RESULTS_PER_TAG * len(v28.TARGET_TAGS) * len(v28.PLATFORMS),
        "analyze_limit": v29.AI_ANALYZE_LIMIT,
        "keep_limit": v29.KEEP_LIMIT,
        "speech_required": True,
        "strict_actual_hashtag": True,
        "youtube_direct_gemini": True,
        "automatic_paid_refreshes": False,
        "audit_top10_closed": True,
        "durable_paid_preflight": True,
        "ambiguous_actor_start_quarantine": True,
        "manual_refresh_cap_usd": float(MANUAL_REFRESH_CAP_USD),
        "analysis_cache": True,
        "analysis_singleflight": True,
        "automatic_ai_tick_limit": MAX_AUTOMATIC_AI_TICKS,
        "snapshot_lookback_days": SNAPSHOT_LOOKBACK_DAYS,
        "snapshot_post_limit": SNAPSHOT_POST_LIMIT,
        "safe_media_download": True,
        "motion_gate_fail_closed": True,
        "cross_site_mutation_block": True,
        "new_run_debounce_seconds": NEW_RUN_DEBOUNCE_SECONDS,
        "budget": v29.budget_breakdown_v29(),
    }
    add_radar_log(
        "V30 AUDIT READY: top-10 production risks closed; hard budget and three-platform product scope preserved.",
        stage="startup",
        details=info,
    )
    return info
