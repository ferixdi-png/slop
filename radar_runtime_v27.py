"""Single authoritative production bootstrap for the final radar runtime.

The battle-tested V27 stack remains the infrastructure/content reconstruction
base. V28 is the final product overlay: Instagram + TikTok + YouTube, five strict
post-level hashtags, 14-day discovery and mandatory Gemini speech/timing checks.
"""

from __future__ import annotations

import sys

from flask import request

from apify_start_compat import install_apify_start_compat
from radar_discovery_v27 import install_v27_high_volume_discovery
from radar_logs import add_radar_log, suppress_startup_logs

RUNTIME_VERSION = "multiplatform_speech_v28_strict14d"
PUBLIC_EDGE_PROFILE = RUNTIME_VERSION
_APPLIED = False
_CONTRACT = None
_LIVENESS_INSTALLED = False


def _install_render_liveness(app):
    """Keep Render process/port probes outside SQLite and external APIs."""
    global _LIVENESS_INSTALLED
    if _LIVENESS_INSTALLED:
        return

    @app.before_request
    def _render_fast_head_probe():
        if request.method == "HEAD" and request.path in {"/", "/health", "/healthz"}:
            return "", 200, {
                "Cache-Control": "no-store",
                "X-Radar-Runtime": RUNTIME_VERSION,
            }
        return None

    def _healthz():
        return {
            "ok": True,
            "runtime": RUNTIME_VERSION,
            "pid": __import__("os").getpid(),
        }, 200, {"Cache-Control": "no-store"}

    if "render_healthz_v27" not in app.view_functions:
        app.add_url_rule(
            "/healthz",
            endpoint="render_healthz_v27",
            view_func=_healthz,
            methods=["GET", "HEAD"],
        )

    _LIVENESS_INSTALLED = True


def activate_v27_runtime():
    """Compose proven internal layers, then expose only final V28 behavior."""
    global _APPLIED, _CONTRACT
    if _APPLIED:
        return dict(_CONTRACT or {"runtime": RUNTIME_VERSION})

    app_module = sys.modules.get("app")
    if app_module is None:
        raise RuntimeError("Final runtime must be activated from app startup")

    required = ("app", "tick_job", "create_or_resume_job", "top_eligible")
    missing = [name for name in required if not hasattr(app_module, name)]
    if missing:
        raise RuntimeError(f"Runtime bootstrap missing app prerequisites: {', '.join(missing)}")

    _install_render_liveness(app_module.app)
    install_apify_start_compat()

    with suppress_startup_logs():
        from radar_growth_v6 import apply_growth_overrides
        apply_growth_overrides()

        import radar_budget_v10 as budget
        budget.apply_budget_overrides()

        from radar_highfreq_v12 import apply_highfreq_overrides
        apply_highfreq_overrides()

        from radar_dialogue_v14 import apply_dialogue_first_overrides, top_eligible_dialogue
        apply_dialogue_first_overrides()
        app_module.top_eligible = top_eligible_dialogue

        from overlay_cleanplate_v15 import (
            PRODUCTION_PROFILE_VERSION,
            apply_overlay_cleanplate_overrides,
        )
        apply_overlay_cleanplate_overrides()

        from radar_resilient_v17 import apply_resilient_v17_overrides
        apply_resilient_v17_overrides()

        # Keep monthly budget wrapper inside hardening exactly as in the proven stack.
        app_module.tick_job = budget.wrap_tick_job(app_module.tick_job)

        from radar_hardening_v19 import apply_hardening_v19
        apply_hardening_v19()

        # Compatibility edge layer transitively composes source aggregation,
        # fresh-run isolation, momentum and strict V27 reconstruction behavior.
        from radar_edge_v19 import apply_edge_guards
        edge_info = apply_edge_guards()

        from radar_cancel_v18 import cancel_active_job
        from radar_strict_scope_v27 import (
            COMPRESSED_TARGET_SEC,
            DIRECT_MAX_DURATION_SEC,
            SOURCE_MAX_DURATION_SEC,
            _APPLIED as V27_APPLIED,
        )
        from radar_momentum_cloud_v25 import MOMENTUM_RECORD_KEY

        if not V27_APPLIED:
            raise RuntimeError("V27 reconstruction base did not activate during final bootstrap")

        # Keep the proven V27 Instagram high-volume adapter as an internal stage,
        # then replace its final discovery/screening scope with V28.
        install_v27_high_volume_discovery()
        from radar_multiplatform_v28 import apply_multiplatform_v28
        v28_info = apply_multiplatform_v28()

        final_keep_limit = int(v28_info.get("keep_limit") or budget.KEEP_LIMIT)
        final_budget = budget.budget_breakdown()

        app_module.PROFILE_VERSION = v28_info["screening_profile"]
        app_module.PRODUCTION_PROFILE_VERSION = PRODUCTION_PROFILE_VERSION
        app_module.KEEP_LIMIT = final_keep_limit
        app_module.BUDGET_INFO = final_budget
        app_module.budget_breakdown = budget.budget_breakdown
        app_module.cancel_active_job = cancel_active_job
        app_module.RADAR_RUNTIME = RUNTIME_VERSION
        app_module.EDGE_PROFILE = PUBLIC_EDGE_PROFILE
        app_module.RADAR_MAX_DURATION_SEC = SOURCE_MAX_DURATION_SEC

    _CONTRACT = {
        "runtime": RUNTIME_VERSION,
        "profile": RUNTIME_VERSION,
        "internal_screening_profile": v28_info["screening_profile"],
        "edge_profile": PUBLIC_EDGE_PROFILE,
        "production_profile": app_module.PRODUCTION_PROFILE_VERSION,
        "platforms": v28_info["platforms"],
        "hashtags": v28_info["hashtags"],
        "lookback_days": v28_info["lookback_days"],
        "results_per_tag_per_platform": v28_info["results_per_tag_per_platform"],
        "max_raw_requested": v28_info["max_raw_requested"],
        "analyze_limit": v28_info["analyze_limit"],
        "keep_limit": final_keep_limit,
        "speech_required": True,
        "strict_actual_hashtag": True,
        "youtube_direct_gemini": bool(v28_info.get("youtube_direct_gemini")),
        "direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
        "source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
        "compressed_target_sec": COMPRESSED_TARGET_SEC,
        "momentum_record_key": MOMENTUM_RECORD_KEY,
        "budget": final_budget,
        "legacy_startup_banners_suppressed": True,
        "render_fast_liveness": True,
        "apify_start_compat": True,
        "internal_edge": str((edge_info or {}).get("edge_profile") or ""),
    }
    _APPLIED = True

    add_radar_log(
        "V28 RUNTIME READY: Instagram + TikTok + YouTube; #omni/#veo/#veo3/#ai/#ии; 14 дней; Gemini speech + timing required.",
        stage="startup",
        details=dict(_CONTRACT),
    )
    return dict(_CONTRACT)
