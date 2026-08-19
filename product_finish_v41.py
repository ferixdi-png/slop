"""V41 final product polish: two-prompt modal, universal prompt access, clean TikTok links.

Product rules:
- every trend visible in the broad TOP can request prompts, regardless of legacy
  ai_match/screening-profile state;
- one Gemini call returns exactly two ready-to-copy prompts: Frame-0 photo and
  video+Russian audio/voice;
- if media download fails, use Gemini on stored trend metadata; if Gemini is also
  unavailable, return a clearly marked deterministic fallback instead of a dead button;
- TikTok rows must use a canonical public /@user/video/<id> URL built from Actor
  item identity; Actor error/private/slideshow items never enter the broad TOP;
- existing V30 same-origin/single-flight/cache, V35 manual-start and V40 metric
  truth remain authoritative.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

from flask import jsonify, request
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

import frontend_broad_v34 as broad_frontend
import frontend_failopen_v33 as v33_frontend
import gemini_service
import radar_broad_v34 as broad
import radar_multiplatform_v28 as v28
import radar_quality
import radar_request_job as radar_job
from db import db_conn
from radar_logs import add_radar_log

PROFILE = "product_finish_v41_prompt_modal_tiktok_truth"
PROMPT_PROFILE = "prompt_pair_v41"
_APPLIED = False
_BASE_NORMALIZE = None
_BASE_BROAD_ELIGIBLE = None
_APP_MODULE = None

_TIKTOK_CANONICAL_RE = re.compile(
    r"^https://(?:www\.)?tiktok\.com/@([A-Za-z0-9._]+)/video/(\d+)(?:[/?#].*)?$",
    re.IGNORECASE,
)
_TIKTOK_ID_RE = re.compile(r"(?:/video/|video/|\b)(\d{8,})")
_TRANSIENT_CODES = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (3.0, 8.0)


class PromptPairV41(BaseModel):
    photo_prompt: str = Field(min_length=40)
    video_prompt: str = Field(min_length=80)
    source_summary: str = ""
    confidence: int = Field(default=80, ge=0, le=100)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dict(value):
    return value if isinstance(value, dict) else {}


def _safe_creator(value: Any) -> str:
    text = str(value or "").strip().lstrip("@").split("?", 1)[0].split("/", 1)[0]
    return text if re.fullmatch(r"[A-Za-z0-9._]{1,40}", text or "") else ""


def _tiktok_id(raw: dict, url: str = "") -> str:
    for key in ("id", "videoId", "video_id", "awemeId", "aweme_id"):
        value = str((raw or {}).get(key) or "").strip()
        if value.isdigit() and len(value) >= 8:
            return value
    match = _TIKTOK_ID_RE.search(str(url or ""))
    return match.group(1) if match else ""


def canonicalize_tiktok_raw(raw: dict) -> dict | None:
    """Return a clean Clockworks TikTok item or None when it cannot be a usable post."""
    data = dict(raw or {})
    if data.get("errorCode") or data.get("error_code"):
        return None
    author_meta = _dict(data.get("authorMeta"))
    if bool(author_meta.get("privateAccount")):
        return None
    if bool(data.get("isSlideshow")) or bool(data.get("slideshowImageLinks")):
        return None

    existing = str(data.get("webVideoUrl") or data.get("submittedVideoUrl") or "").strip()
    match = _TIKTOK_CANONICAL_RE.match(existing)
    creator = _safe_creator(
        author_meta.get("name") or author_meta.get("uniqueId") or data.get("author")
        or data.get("uniqueId") or (match.group(1) if match else "")
    )
    video_id = _tiktok_id(data, existing) or (match.group(2) if match else "")
    if not creator or not video_id:
        return None

    data["webVideoUrl"] = f"https://www.tiktok.com/@{creator}/video/{video_id}"
    data["id"] = video_id
    return data


def is_canonical_tiktok_url(url: str) -> bool:
    return bool(_TIKTOK_CANONICAL_RE.match(str(url or "").strip()))


def _source_is_tiktok(raw: dict, source: str) -> bool:
    text = " ".join(
        [
            str(source or ""),
            str((raw or {}).get("webVideoUrl") or ""),
            str((raw or {}).get("submittedVideoUrl") or ""),
            str((raw or {}).get("url") or ""),
        ]
    ).lower()
    return "tiktok" in text


def normalize_candidate_v41(raw, source, creator_stats=None):
    data = dict(raw or {})
    if _source_is_tiktok(data, source):
        data = canonicalize_tiktok_raw(data)
        if data is None:
            return None
    item = _BASE_NORMALIZE(data, source, creator_stats)
    if not item:
        return None
    if str(item.get("platform") or "") == "TikTok":
        canonical = canonicalize_tiktok_raw(
            {
                **data,
                "authorMeta": {
                    **_dict(data.get("authorMeta")),
                    "name": item.get("creator") or _dict(data.get("authorMeta")).get("name"),
                },
                "webVideoUrl": item.get("post_url") or data.get("webVideoUrl"),
            }
        )
        if canonical is None:
            return None
        item["post_url"] = canonical["webVideoUrl"]
        item["tiktok_link_canonical"] = True
    return item


def broad_eligible_v41(row) -> bool:
    if not _BASE_BROAD_ELIGIBLE(row):
        return False
    if str((row or {}).get("platform") or "") == "TikTok":
        return is_canonical_tiktok_url(str((row or {}).get("post_url") or ""))
    return True


def _repair_existing_tiktok_rows() -> dict:
    repaired = 0
    hidden = 0
    try:
        with db_conn() as conn:
            rows = conn.execute(
                """SELECT id,creator,post_url FROM radar_posts
                   WHERE platform='TikTok' AND datetime(published_at)>=datetime('now','-14 days')"""
            ).fetchall()
            for row in rows:
                creator = _safe_creator(row["creator"])
                url = str(row["post_url"] or "")
                match = _TIKTOK_CANONICAL_RE.match(url)
                video_id = (match.group(2) if match else "") or _tiktok_id({}, url)
                if creator and video_id:
                    canonical = f"https://www.tiktok.com/@{creator}/video/{video_id}"
                    if canonical != url:
                        conn.execute("UPDATE radar_posts SET post_url=? WHERE id=?", (canonical, row["id"]))
                        repaired += 1
                elif not is_canonical_tiktok_url(url):
                    hidden += 1
            conn.commit()
    except Exception as exc:
        add_radar_log(f"V41 TikTok repair warning: {exc}", level="WARN", stage="tiktok-link")
    return {"repaired_existing_tiktok_urls": repaired, "hidden_unrecoverable_tiktok_urls": hidden}


def _status_code(exc) -> int:
    for attr in ("status_code", "code"):
        try:
            code = int(getattr(exc, attr, 0) or 0)
            if code:
                return code
        except Exception:
            pass
    response = getattr(exc, "response", None)
    try:
        code = int(getattr(response, "status_code", 0) or 0)
        if code:
            return code
    except Exception:
        pass
    text = str(exc or "").upper()
    for code in _TRANSIENT_CODES:
        if f"{code} " in text or f"{code}:" in text:
            return code
    return 0


def _is_transient(exc) -> bool:
    if _status_code(exc) in _TRANSIENT_CODES:
        return True
    text = str(exc or "").lower()
    return any(
        marker in text
        for marker in (
            "high demand",
            "temporarily unavailable",
            "service unavailable",
            "resource_exhausted",
            "unavailable",
            "rate limit",
            "too many requests",
        )
    )


def _retry(stage: str, fn):
    import time

    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            return fn()
        except Exception as exc:
            if not _is_transient(exc) or attempt >= len(_RETRY_DELAYS):
                raise
            delay = _RETRY_DELAYS[attempt]
            add_radar_log(
                f"V41 Gemini transient {stage}: retry {attempt + 1}/{len(_RETRY_DELAYS)} in {delay:.0f}s",
                level="WARN",
                stage="v41-prompts",
                details={"attempt": attempt + 1, "status_code": _status_code(exc), "delay_seconds": delay},
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")


def _target_duration(row: dict) -> float:
    duration = float(row.get("duration_sec") or 0)
    if duration <= 0:
        return 10.0
    if duration > v28.DIRECT_MAX_DURATION_SEC:
        return float(v28.COMPRESSED_TARGET_SEC)
    return round(duration, 2)


def _row_context(row: dict) -> str:
    fields = {
        "platform": row.get("platform", ""),
        "creator": row.get("creator", ""),
        "duration_sec": row.get("duration_sec", 0),
        "target_duration_sec": _target_duration(row),
        "hook": row.get("hook", ""),
        "scene_description": row.get("scene_description", ""),
        "joke": row.get("joke", ""),
        "ending": row.get("ending", ""),
        "dialogue_summary": row.get("dialogue_summary", ""),
        "caption": str(row.get("caption") or "")[:1800],
        "reason": str(row.get("reason") or "")[:500],
    }
    try:
        fields.update(broad.adaptation_fields(row))
    except Exception:
        pass
    return json.dumps(fields, ensure_ascii=False)


def _system_prompt(row: dict, media_available: bool) -> str:
    target = _target_duration(row)
    media_rule = (
        "The source video and audio are attached. Inspect the whole clip and use them as the primary factual source."
        if media_available
        else
        "The source media could not be fetched. Use only the supplied trend metadata and do not pretend to have seen frames or heard exact words."
    )
    return f"""
You are a senior prompt engineer converting one short-video trend into TWO production-ready prompts.
{media_rule}

Return exactly PromptPairV41 with only these useful outputs:
1) photo_prompt: the literal FRAME 0 / initial still image prompt.
2) video_prompt: the complete animation prompt that starts from that generated frame and already contains AUDIO, VOICE and RUSSIAN SPEECH instructions.

PHOTO PROMPT RULES
- Write a standalone detailed English image-generation prompt for vertical 9:16.
- Describe only the literal opening frame, never future actions.
- Specify people count, adult/child only when supported, appearance without identifying real persons, clothing, exact screen positions, pose, head/gaze, mouth state, hands/props, background geometry, camera height/distance/lens, crop/headroom, lighting and realistic smartphone texture.
- Preserve the trend's staging/mechanic while rendering believable live action, natural skin/fabric/hair/materials, slight phone-camera imperfections, no fake HDR or plastic AI look.
- No visible text, subtitles, logos or watermarks unless the trend mechanic absolutely requires a physical sign/object.

VIDEO PROMPT RULES
- Assume photo_prompt is already supplied as Frame 0. Do NOT waste text redescribing the whole initial image.
- Target duration is exactly {target:.2f} seconds.
- Give an explicit chronological action sequence from Frame 0 through setup -> action -> reaction -> payoff/end.
- Preserve character/object continuity: no duplicates, teleporting, disappearing props, swapped people or impossible hands.
- Specify handheld smartphone camera behavior, framing changes, focus/exposure behavior and motion realism.
- Include a clearly labeled AUDIO / VOICE / RUSSIAN SPEECH section inside the same video_prompt.
- If source speech is audible, preserve speaker order, intent and ordinary short wording as supported; adapt non-Russian speech naturally into Russian rather than phonetically copying it.
- If there is no usable speech, add at most one concise natural Russian reaction/line only when it strengthens the same trend mechanic.
- Bind each line to the visible speaker. Only that speaker moves lips; every other visible mouth stays still during the line. No overlapping or leaked dialogue.
- Describe voice age range/presentation, tone, pace, emotion, breaths/laughter when supported, plus environmental audio/SFX.
- The prompt must be directly pasteable into a modern image-to-video generator and must not say "same as source", "as in reference", or require the original video.

TREND METADATA
{_row_context(row)}
""".strip()


def _generate_video_pair(file_path: str, row: dict) -> PromptPairV41:
    def once():
        def run(client, uploaded):
            response = client.models.generate_content(
                model=gemini_service.ANALYSIS_MODEL,
                contents=types.Content(
                    parts=[
                        gemini_service.video_part(uploaded, gemini_service.FORENSIC_VIDEO_FPS),
                        types.Part(text="Analyze the entire short video and audio, then return the two final prompts now."),
                    ]
                ),
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_level="high"),
                    system_instruction=_system_prompt(row, True),
                    response_mime_type="application/json",
                    response_schema=PromptPairV41,
                    max_output_tokens=4096,
                ),
            )
            return gemini_service.parse_response(response, PromptPairV41)

        return gemini_service.with_uploaded_file(file_path, run)

    return _retry("video-prompt-pair", once)


def _generate_metadata_pair(row: dict) -> PromptPairV41:
    key = str(os.environ.get("GEMINI_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not configured")

    def once():
        client = genai.Client(api_key=key)
        try:
            response = client.models.generate_content(
                model=gemini_service.ANALYSIS_MODEL,
                contents="Create the two production-ready prompts from the supplied trend metadata. Be concrete but do not invent exact observed details that are absent.",
                config=types.GenerateContentConfig(
                    thinking_config=types.ThinkingConfig(thinking_level="high"),
                    system_instruction=_system_prompt(row, False),
                    response_mime_type="application/json",
                    response_schema=PromptPairV41,
                    max_output_tokens=4096,
                ),
            )
            return gemini_service.parse_response(response, PromptPairV41)
        finally:
            try:
                client.close()
            except Exception:
                pass

    return _retry("metadata-prompt-pair", once)


def _local_fallback_pair(row: dict) -> PromptPairV41:
    target = _target_duration(row)
    scene = str(
        row.get("scene_description") or row.get("hook") or row.get("caption")
        or "short viral real-world scene"
    ).strip()
    scene = re.sub(r"\s+", " ", scene)[:1000]
    hook = re.sub(r"\s+", " ", str(row.get("hook") or ""))[:500]
    ending = re.sub(r"\s+", " ", str(row.get("ending") or row.get("joke") or ""))[:500]
    photo = (
        "Ultra-realistic vertical 9:16 smartphone Frame 0 for a short social video. "
        f"Opening situation: {scene}. "
        "Use believable live-action people and real materials, natural skin pores and hair strands, ordinary fabric folds, "
        "physically plausible hands and props, practical environmental light, realistic phone exposure and white balance, "
        "slight handheld framing imperfection, no beauty filter, no fake HDR, no artificial bokeh, no subtitles, no watermark. "
        "Freeze the instant immediately before the first meaningful action; mouths neutral unless the first phoneme begins at frame zero."
    )
    video = (
        f"Use the supplied image as Frame 0. Create exactly {target:.2f} seconds, vertical 9:16, realistic handheld smartphone video.\n\n"
        f"CORE TREND MECHANIC: {scene}\n"
        + (f"OPENING HOOK: {hook}\n" if hook else "")
        + "ACTION ORDER: establish the setup immediately, perform the single key action once, show the natural reaction, then land the payoff and stop without an extra ending.\n"
        + (f"PAYOFF / ENDING: {ending}\n" if ending else "")
        + "CONTINUITY: keep every person, prop, screen side and hand/object relationship consistent; no duplicates, teleporting or disappearing objects.\n"
        + "CAMERA: casual handheld phone framing with subtle micro-shake, believable autofocus/exposure response and natural motion softness; no cinematic dolly or artificial slow motion unless required by the mechanic.\n\n"
        + "AUDIO / VOICE / RUSSIAN SPEECH: use natural location ambience and only sounds caused by visible actions. If the mechanic needs speech, add one short natural Russian reaction line matched to the visible active speaker. Use a believable conversational voice, normal pace and emotion appropriate to the reaction. Only the active speaker moves lips; all other visible mouths remain still. No overlapping dialogue."
    )
    return PromptPairV41(
        photo_prompt=photo,
        video_prompt=video,
        source_summary="metadata/local fallback",
        confidence=45,
    )


def _save_pair(app_module, row: dict, pair: PromptPairV41, source_duration: float) -> int:
    result = pair.model_dump()
    result.update(
        prompt_profile=PROMPT_PROFILE,
        generated_at=_now_iso(),
        source_duration_sec=round(float(source_duration or row.get("duration_sec") or 0), 2),
        target_duration_sec=_target_duration(row),
    )
    return int(
        app_module.save_analysis(
            (f"@{row.get('creator','')} — {row.get('hook') or 'trend prompt pair'}")[:160],
            row.get("post_url", ""),
            row.get("views", 0),
            row.get("viral_score_v2", 0),
            result,
        )
    )


def _prompt_payload(
    row: dict,
    pair: PromptPairV41,
    *,
    source_mode: str,
    source_duration: float,
    analysis_id=None,
    cached=False,
    warning="",
):
    result = pair.model_dump()
    result.update(
        prompt_profile=PROMPT_PROFILE,
        source_duration_sec=round(float(source_duration or row.get("duration_sec") or 0), 2),
        target_duration_sec=_target_duration(row),
    )
    return {
        "id": analysis_id,
        "model": gemini_service.ANALYSIS_MODEL if source_mode != "local_fallback" else "local-fallback",
        "prompt_profile": PROMPT_PROFILE,
        "source_mode": source_mode,
        "source_duration_sec": result["source_duration_sec"],
        "target_duration_sec": result["target_duration_sec"],
        "cached": bool(cached),
        "creator": row.get("creator", ""),
        "platform": row.get("platform", ""),
        "trend_title": row.get("hook") or row.get("scene_description") or "Тренд",
        "warning": warning,
        "result": result,
    }


def _analyze_any_visible_trend(app_module, item_id: int):
    with db_conn() as conn:
        record = conn.execute("SELECT * FROM radar_posts WHERE id=?", (item_id,)).fetchone()
    if not record:
        return jsonify(error="Ролик не найден"), 404
    row = dict(record)
    if not app_module.top_eligible(row):
        return jsonify(error="Этот ролик больше не входит в актуальную выдачу трендов."), 410

    tmp = None
    media_error = ""
    source_duration = float(row.get("duration_sec") or 0)
    pair = None
    source_mode = "video"
    try:
        try:
            tmp, refreshed_duration = app_module.download_reel_for_analysis(row)
            if refreshed_duration:
                source_duration = float(refreshed_duration)
                row["duration_sec"] = source_duration
            pair = _generate_video_pair(tmp, row)
        except Exception as exc:
            media_error = str(exc)[:700]
            add_radar_log(
                f"V41 video prompt path unavailable for {row.get('platform')} @{row.get('creator','')}; using metadata Gemini.",
                level="WARN",
                stage="v41-prompts",
                details={"item_id": item_id, "media_error": media_error},
            )
            source_mode = "metadata_gemini"
            try:
                pair = _generate_metadata_pair(row)
            except Exception as gemini_exc:
                source_mode = "local_fallback"
                pair = _local_fallback_pair(row)
                media_error = f"media: {media_error}; gemini: {str(gemini_exc)[:500]}"

        analysis_id = None
        if source_mode in {"video", "metadata_gemini"}:
            analysis_id = _save_pair(app_module, row, pair, source_duration)
        payload = _prompt_payload(
            row,
            pair,
            source_mode=source_mode,
            source_duration=source_duration,
            analysis_id=analysis_id,
            warning=(
                "Видео не удалось получить; Gemini собрал промпты по метаданным."
                if source_mode == "metadata_gemini"
                else "Временный fallback: Gemini/медиа сейчас недоступны; повторный клик позже попробует Gemini снова."
                if source_mode == "local_fallback"
                else ""
            ),
        )
        add_radar_log(
            f"V41 PROMPT PAIR READY: {row.get('platform')} @{row.get('creator','')} mode={source_mode}",
            stage="v41-prompts",
            details={"item_id": item_id, "source_mode": source_mode, "analysis_id": analysis_id},
        )
        return jsonify(payload), 200
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _install_prompt_interceptor(app, app_module) -> None:
    if getattr(app, "_v41_prompt_interceptor", False):
        return
    analyze_re = re.compile(r"^/api/radar/(\d+)/analyze$")

    def v41_prompt_access():
        if request.method != "POST":
            return None
        match = analyze_re.match(request.path)
        if not match:
            return None
        return _analyze_any_visible_trend(app_module, int(match.group(1)))

    v41_prompt_access.__name__ = "v41_prompt_access"
    funcs = app.before_request_funcs.setdefault(None, [])
    insert_at = 0
    for index, fn in enumerate(funcs):
        if getattr(fn, "__name__", "") == "v30_before_request":
            insert_at = index + 1
            break
    funcs.insert(insert_at, v41_prompt_access)
    app._v41_prompt_interceptor = True


def _prompt_pair_from_any_result(result: dict) -> tuple[str, str]:
    d = dict(result or {})
    photo = str(d.get("photo_prompt") or d.get("block_1_frame0_prompt") or "")
    video = d.get("video_prompt")
    if not video:
        core = d.get("block_3_video") or ""
        audio = d.get("block_4_audio") or ""
        core_text = core if isinstance(core, str) else json.dumps(core, ensure_ascii=False, indent=2)
        audio_text = audio if isinstance(audio, str) else json.dumps(audio, ensure_ascii=False, indent=2)
        video = core_text
        if audio_text and audio_text != "{}":
            video += "\n\nAUDIO / VOICE / RUSSIAN SPEECH:\n" + audio_text
    return photo.strip(), str(video or "").strip()


def _patch_frontend() -> dict:
    html = broad_frontend.HTML

    css = r'''
.prompt-modal{position:fixed;inset:0;z-index:2147483000;display:grid;place-items:center;padding:24px;background:rgba(3,6,10,.78);backdrop-filter:blur(10px);overflow:auto}.prompt-dialog{width:min(1080px,96vw);max-height:min(900px,92vh);overflow:auto;border:1px solid #394453;border-radius:22px;background:linear-gradient(180deg,#121922,#0a0f15);box-shadow:0 30px 100px rgba(0,0,0,.55);padding:22px}.prompt-modal-head{display:flex;align-items:flex-start;justify-content:space-between;gap:18px;margin-bottom:18px}.prompt-modal-head h2{margin:3px 0 5px;font-size:24px;letter-spacing:-.02em}.prompt-modal-meta{color:var(--muted);font-size:11px;line-height:1.45}.prompt-close{width:38px;height:38px;flex:0 0 38px;border:1px solid var(--line);border-radius:12px;background:#11171e;color:#dbe3ed;font-size:22px;line-height:1;cursor:pointer}.prompt-close:hover{background:#1a222c}.prompt-loading{padding:44px 18px;text-align:center;border:1px solid var(--line);border-radius:16px;background:#0b1016;color:#dce5ef}.prompt-spinner{width:28px;height:28px;margin:0 auto 14px;border:3px solid #26303b;border-top-color:var(--accent);border-radius:50%;animation:promptspin .8s linear infinite}@keyframes promptspin{to{transform:rotate(360deg)}}.prompt-warning{margin:0 0 14px;padding:11px 13px;border:1px solid #6b552d;border-radius:12px;background:#21190d;color:#ffd48f;font-size:11px;line-height:1.5}.prompt-error{padding:16px;border:1px solid #743842;border-radius:14px;background:#281318;color:#ffd8dc;font-size:12px;line-height:1.55}.prompt-grid{display:grid;gap:14px}.prompt-card{position:relative;border:1px solid var(--line);border-radius:17px;background:#0b1016;padding:18px}.prompt-card-head{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-bottom:12px}.prompt-card-title{display:flex;align-items:center;gap:10px;font-size:13px;font-weight:950}.prompt-number{width:27px;height:27px;display:grid;place-items:center;border-radius:9px;background:var(--accent);color:#091008;font-size:12px;font-weight:950}.prompt-card pre{margin:0;white-space:pre-wrap;word-break:break-word;color:#d7dfe9;font:12px/1.65 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}.prompt-card .btn{flex:0 0 auto}.prompt-hint{margin-top:7px;color:var(--muted);font-size:10px;line-height:1.4}@media(max-width:700px){.prompt-modal{padding:10px;align-items:end}.prompt-dialog{width:100%;max-height:94vh;border-radius:20px 20px 12px 12px;padding:15px}.prompt-modal-head h2{font-size:20px}.prompt-card{padding:14px}.prompt-card-head{align-items:flex-start}.prompt-card pre{font-size:11px}}
'''
    if ".prompt-modal{" not in html:
        html = html.replace("</style>", css + "\n</style>", 1)

    old_section = '''    <div class="section-head"><div><h2>PRODUCTION-ПАКЕТ</h2><span>PHOTO PROMPT · VIDEO PROMPT · речь/аудио · CapCut overlay plan.</span></div></div>
    <div class="analysis-loading card" id="analysisLoading" hidden>Gemini собирает production-пакет. Основной интерфейс остаётся доступен.</div>
    <div class="analysis-result" id="analysisResult"><div class="empty">Выбери ролик в TOP и нажми «ПОЛУЧИТЬ УЛЬТРА-ПРОМПТЫ».</div></div>

'''
    html = html.replace(old_section, "", 1)

    modal = r'''
<div class="prompt-modal" id="promptModal" hidden aria-hidden="true">
  <div class="prompt-dialog" role="dialog" aria-modal="true" aria-labelledby="promptModalTitle">
    <div class="prompt-modal-head">
      <div><div class="eyebrow">GEMINI · ГОТОВЫЕ ПРОМПТЫ</div><h2 id="promptModalTitle">Промпты для тренда</h2><div class="prompt-modal-meta" id="promptModalMeta">Фото Frame 0 + видео с озвучкой</div></div>
      <button class="prompt-close" type="button" data-prompt-close aria-label="Закрыть">×</button>
    </div>
    <div class="prompt-loading" id="promptModalLoading"><div class="prompt-spinner"></div><b>Gemini разбирает тренд…</b><div class="prompt-hint">Окно можно закрыть; поиск и остальные карточки не блокируются.</div></div>
    <div class="prompt-error" id="promptModalError" hidden></div>
    <div class="prompt-warning" id="promptModalWarning" hidden></div>
    <div class="prompt-grid" id="promptModalContent" hidden></div>
  </div>
</div>
'''
    if 'id="promptModal"' not in html:
        html = html.replace("</body>", modal + "\n</body>", 1)

    html = html.replace(">УЛЬТРА-ПРОМПТЫ</button>", ">ПРОМПТЫ GEMINI</button>")

    start = html.find("function block(title,text){")
    end = html.find("\n\n$('syncRadar')", start) if start >= 0 else -1
    if start >= 0 and end > start:
        new_js = r'''function promptText(v){if(typeof v==='string')return v;if(v===null||v===undefined)return '';try{return JSON.stringify(v,null,2);}catch(_){return String(v||'');}}
function promptPairFromResult(d){d=d||{};const photo=String(d.photo_prompt||d.block_1_frame0_prompt||'').trim();let video=String(d.video_prompt||'').trim();if(!video){const core=promptText(d.block_3_video).trim();const audio=promptText(d.block_4_audio).trim();video=core+(audio&&audio!=='{}'?`\n\nAUDIO / VOICE / RUSSIAN SPEECH:\n${audio}`:'');}return {photo,video};}
function promptCard(n,title,text,hint){return `<div class="prompt-card"><div class="prompt-card-head"><div><div class="prompt-card-title"><span class="prompt-number">${n}</span>${esc(title)}</div>${hint?`<div class="prompt-hint">${esc(hint)}</div>`:''}</div><button class="btn" data-copy="${encodeURIComponent(String(text||''))}">КОПИРОВАТЬ</button></div><pre>${esc(text||'—')}</pre></div>`;}
function openPromptModal(){const m=$('promptModal');m.hidden=false;m.setAttribute('aria-hidden','false');document.body.style.overflow='hidden';$('promptModalLoading').hidden=false;$('promptModalError').hidden=true;$('promptModalWarning').hidden=true;$('promptModalContent').hidden=true;$('promptModalContent').innerHTML='';}
function closePromptModal(){const m=$('promptModal');m.hidden=true;m.setAttribute('aria-hidden','true');document.body.style.overflow='';}
function renderPromptModal(data){const d=data?.result||{};const pair=promptPairFromResult(d);if(!pair.photo||!pair.video)throw new Error('Gemini вернул неполную пару промптов');const source=String(data?.source_mode||'');const cached=data?.cached?' · кэш':'';$('promptModalTitle').textContent=String(data?.trend_title||'Промпты для тренда');$('promptModalMeta').textContent=[data?.platform,data?.creator?`@${data.creator}`:'',source?`режим: ${source}`:'',cached].filter(Boolean).join(' · ');const w=String(data?.warning||'');$('promptModalWarning').hidden=!w;$('promptModalWarning').textContent=w;$('promptModalContent').innerHTML=promptCard(1,'PHOTO PROMPT · FRAME 0',pair.photo,'Начальный нулевой кадр — генерируй фото первым.')+promptCard(2,'VIDEO PROMPT · С ОЗВУЧКОЙ',pair.video,'Подавай вместе с готовым Frame 0 в image-to-video. Озвучка уже внутри.');$('promptModalLoading').hidden=true;$('promptModalError').hidden=true;$('promptModalContent').hidden=false;}
async function refreshMetrics(id,button){if(!id)return;const old=button.textContent;button.disabled=true;button.textContent='ОБНОВЛЯЮ…';try{await api(`/api/radar/${id}/metrics-refresh`,{method:'POST'},190000);await refreshLists(true);clearRuntimeError();}catch(e){showRuntimeError(`Метрики: ${e.message}`);}finally{button.disabled=false;button.textContent=old;}}
async function analyze(id,button){if(!id)return;const old=button.textContent;button.disabled=true;button.textContent='GEMINI…';openPromptModal();$('promptModalTitle').textContent='Gemini готовит промпты';$('promptModalMeta').textContent='Фото Frame 0 + видео с русской озвучкой';try{const data=await api(`/api/radar/${id}/analyze`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({owned_or_licensed:false,prompt_pair_v41:true})},220000);renderPromptModal(data||{});}catch(e){$('promptModalLoading').hidden=true;$('promptModalContent').hidden=true;$('promptModalWarning').hidden=true;$('promptModalError').hidden=false;$('promptModalError').textContent=`Не удалось получить промпты: ${e.message}. Закрой окно и нажми кнопку ещё раз.`;}finally{button.disabled=false;button.textContent=old;}}
'''
        html = html[:start] + new_js + html[end:]

    click_marker = "document.addEventListener('click',async e=>{"
    if click_marker in html and "data-prompt-close" not in html[html.find(click_marker):html.find(click_marker)+500]:
        html = html.replace(
            click_marker,
            click_marker + "const pc=e.target.closest('[data-prompt-close]');if(pc||e.target?.id==='promptModal'){closePromptModal();return;}",
            1,
        )
    if "window.addEventListener('keydown',e=>{if(e.key==='Escape'" not in html:
        unload_marker = "window.addEventListener('beforeunload'"
        key_js = "window.addEventListener('keydown',e=>{if(e.key==='Escape'&&!$('promptModal').hidden)closePromptModal();});\n"
        pos = html.find(unload_marker)
        if pos >= 0:
            html = html[:pos] + key_js + html[pos:]

    html_bytes = html.encode("utf-8")
    html_sha = hashlib.sha256(html_bytes).hexdigest()[:16]
    broad_frontend.HTML = html
    broad_frontend.HTML_BYTES = html_bytes
    broad_frontend.HTML_SHA256 = html_sha
    v33_frontend.HTML = html
    v33_frontend.HTML_BYTES = html_bytes
    v33_frontend.HTML_SHA256 = html_sha
    return {
        "prompt_modal": True,
        "prompt_blocks": 2,
        "copy_buttons": True,
        "close_and_reopen": True,
        "inline_production_section_removed": True,
        "html_sha256": html_sha,
        "html_bytes": len(html_bytes),
    }


def _install_tiktok_truth(app_module) -> dict:
    global _BASE_NORMALIZE, _BASE_BROAD_ELIGIBLE
    _BASE_NORMALIZE = v28.normalize_multiplatform_candidate
    _BASE_BROAD_ELIGIBLE = broad.broad_eligible
    v28.normalize_multiplatform_candidate = normalize_candidate_v41
    radar_job.normalize_reel = normalize_candidate_v41
    broad.broad_eligible = broad_eligible_v41
    v28.top_eligible_v28 = broad_eligible_v41
    radar_quality.top_eligible = broad_eligible_v41
    radar_job.top_eligible = broad_eligible_v41
    app_module.top_eligible = broad_eligible_v41
    repair = _repair_existing_tiktok_rows()
    return {
        "tiktok_canonical_public_url": True,
        "tiktok_error_items_rejected": True,
        "tiktok_private_items_rejected": True,
        "tiktok_slideshows_rejected": True,
        "broken_tiktok_hidden_from_top": True,
        **repair,
    }


def install_product_finish_v41(app=None, app_module=None) -> dict:
    global _APPLIED, _APP_MODULE
    if _APPLIED:
        return diagnostics()
    if app_module is None:
        import sys
        app_module = sys.modules.get("app")
    if app_module is None:
        raise RuntimeError("V41 must be installed from app startup")
    if app is None:
        app = app_module.app
    _APP_MODULE = app_module

    tiktok_info = _install_tiktok_truth(app_module)
    _install_prompt_interceptor(app, app_module)
    frontend_info = _patch_frontend()
    _APPLIED = True
    info = {**diagnostics(), **tiktok_info, **frontend_info}
    add_radar_log(
        "V41 PRODUCT FINISH READY: two-prompt modal; every visible trend has prompt fallback; TikTok URLs canonicalized and broken rows hidden.",
        stage="startup",
        details=info,
    )
    return info


def diagnostics() -> dict:
    return {
        "profile": PROFILE,
        "prompt_profile": PROMPT_PROFILE,
        "universal_visible_trend_prompt_access": True,
        "video_gemini_first": True,
        "metadata_gemini_fallback": True,
        "local_last_resort_fallback": True,
        "prompt_pair_fields": ["photo_prompt", "video_prompt"],
        "video_prompt_includes_audio_voice_russian_speech": True,
        "legacy_profile_gate_removed_for_visible_trends": True,
        "v30_singleflight_preserved": True,
        "v30_cache_preserved": True,
        "v35_manual_start_preserved": True,
        "v40_metrics_preserved": True,
    }
