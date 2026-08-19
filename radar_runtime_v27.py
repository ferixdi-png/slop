"""Single authoritative production bootstrap for the final radar runtime.

The battle-tested V27 stack remains the infrastructure/content reconstruction
base. V28 supplies the three-platform speech/timing product behavior and V29
adds the final hard <$5 spend guard without changing that product scope.
"""

from __future__ import annotations

import sys

from flask import request

from apify_start_compat import install_apify_start_compat
from radar_discovery_v27 import install_v27_high_volume_discovery
from radar_logs import add_radar_log, suppress_startup_logs

RUNTIME_VERSION = "multiplatform_speech_v29_budget5"
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
    """Compose proven internal layers, then expose only final V29 behavior."""
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

        app_module.tick_job = budget.wrap_tick_job(app_module.tick_job)

        from radar_hardening_v19 import apply_hardening_v19
        apply_hardening_v19()

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

        install_v27_high_volume_discovery()
        from radar_multiplatform_v28 import apply_multiplatform_v28
        v28_info = apply_multiplatform_v28()

        # Apply the spend guard BEFORE importing the 14-day finish module so its
        # imported profile constants are already the final V29 values.
        from radar_budget_v29 import apply_budget_v29, budget_breakdown_v29
        v29_info = apply_budget_v29()

        from radar_v28_finish import apply_v28_finish
        finish_info = apply_v28_finish()

        final_info = {**dict(v28_info or {}), **dict(v29_info or {})}
        final_keep_limit = int(final_info.get("keep_limit") or budget.KEEP_LIMIT)
        final_budget = budget_breakdown_v29()

        app_module.PROFILE_VERSION = final_info["screening_profile"]
        app_module.PRODUCTION_PROFILE_VERSION = PRODUCTION_PROFILE_VERSION
        app_module.KEEP_LIMIT = final_keep_limit
        app_module.BUDGET_INFO = final_budget
        app_module.budget_breakdown = budget_breakdown_v29
        app_module.cancel_active_job = cancel_active_job
        app_module.RADAR_RUNTIME = RUNTIME_VERSION
        app_module.EDGE_PROFILE = PUBLIC_EDGE_PROFILE
        app_module.RADAR_MAX_DURATION_SEC = SOURCE_MAX_DURATION_SEC

    _CONTRACT = {
        "runtime": RUNTIME_VERSION,
        "profile": RUNTIME_VERSION,
        "internal_screening_profile": final_info["screening_profile"],
        "edge_profile": PUBLIC_EDGE_PROFILE,
        "production_profile": app_module.PRODUCTION_PROFILE_VERSION,
        "platforms": final_info["platforms"],
        "hashtags": final_info["hashtags"],
        "lookback_days": final_info["lookback_days"],
        "results_per_tag_per_platform": final_info["results_per_tag_per_platform"],
        "max_raw_requested": final_info["max_raw_requested"],
        "analyze_limit": final_info["analyze_limit"],
        "keep_limit": final_keep_limit,
        "speech_required": True,
        "strict_actual_hashtag": True,
        "youtube_direct_gemini": bool(final_info.get("youtube_direct_gemini")),
        "v28_14day_api": bool(finish_info.get("v28_14day_api")),
        "direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
        "source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
        "compressed_target_sec": COMPRESSED_TARGET_SEC,
        "momentum_record_key": MOMENTUM_RECORD_KEY,
        "budget": final_budget,
        "hard_total_target_usd": final_budget["hard_total_target_usd"],
        "apify_discovery_hard_cap_usd": final_budget["hard_apify_discovery_caps_usd"],
        "budget_headroom_usd": final_budget["reserved_headroom_usd"],
        "legacy_startup_banners_suppressed": True,
        "render_fast_liveness": True,
        "apify_start_compat": True,
        "internal_edge": str((edge_info or {}).get("edge_profile") or ""),
    }
    _APPLIED = True

    add_radar_log(
        "V29 RUNTIME READY: Instagram + TikTok + YouTube; 14 дней; speech/timing; Apify discovery hard cap $2.80; total target <$5.",
        stage="startup",
        details=dict(_CONTRACT),
    )
    return dict(_CONTRACT)
