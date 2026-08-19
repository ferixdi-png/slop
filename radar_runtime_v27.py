"""Single authoritative production bootstrap for the final radar runtime.

The V30 search/screening/budget contract stays authoritative. V33 replaces the
browser compatibility stack with one fail-open frontend runtime: one HTML, one
CSS block, one JS block, no MutationObserver and no parallel legacy polling.
"""

from __future__ import annotations

import sys

from flask import request

from apify_start_compat import install_apify_start_compat
from radar_discovery_v27 import install_v27_high_volume_discovery
from radar_logs import add_radar_log, suppress_startup_logs

# IMPORTANT: do not change this semantic identity for frontend-only releases.
# Existing durable paid runs must resume after deploy instead of being migrated.
RUNTIME_VERSION = "multiplatform_speech_v30_audit10_budget5"
PUBLIC_EDGE_PROFILE = RUNTIME_VERSION
FRONTEND_PROFILE = "frontend_v33_fail_open_single_runtime"
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
                "X-Frontend-Profile": FRONTEND_PROFILE,
            }
        return None

    def _healthz():
        return {
            "ok": True,
            "runtime": RUNTIME_VERSION,
            "frontend_profile": FRONTEND_PROFILE,
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
    """Compose proven backend layers, then expose V30 + V33 frontend reliability."""
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

        from radar_budget_v29 import apply_budget_v29, budget_breakdown_v29
        v29_info = apply_budget_v29()

        from radar_budget_search_guard_v29 import apply_search_budget_guard_v29
        search_guard_info = apply_search_budget_guard_v29()

        from radar_audit_v30 import apply_audit_v30
        v30_info = apply_audit_v30()

        from radar_v28_finish import apply_v28_finish
        finish_info = apply_v28_finish()

        # V33 is intentionally last. Unlike V32 it does NOT bundle the old browser
        # scripts. It supplies a clean fail-open page with a single client runtime.
        from frontend_failopen_v33 import install_frontend_v33
        frontend_info = install_frontend_v33(app_module.app)

        final_info = {
            **dict(v28_info or {}),
            **dict(v29_info or {}),
            **dict(search_guard_info or {}),
            **dict(v30_info or {}),
        }
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
        "frontend_profile": frontend_info.get("profile", FRONTEND_PROFILE),
        "self_contained_frontend": True,
        "external_static_dependencies": frontend_info.get("external_static_dependencies", 0),
        "frontend_html_bytes": frontend_info.get("html_bytes"),
        "frontend_html_sha256": frontend_info.get("html_sha256"),
        "fail_open_dom": bool(frontend_info.get("fail_open_dom")),
        "single_js_runtime": bool(frontend_info.get("single_js_runtime")),
        "legacy_client_scripts": frontend_info.get("legacy_client_scripts", 0),
        "mutation_observers": frontend_info.get("mutation_observers", 0),
        "parallel_polling_layers": frontend_info.get("parallel_polling_layers", 0),
        "runtime_error_surface": bool(frontend_info.get("runtime_error_surface")),
        "root_db_dependency": bool(frontend_info.get("root_db_dependency", False)),
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
        "automatic_paid_refreshes": bool(final_info.get("automatic_paid_refreshes", False)),
        "v28_14day_api": bool(finish_info.get("v28_14day_api")),
        "direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
        "source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
        "compressed_target_sec": COMPRESSED_TARGET_SEC,
        "momentum_record_key": MOMENTUM_RECORD_KEY,
        "budget": final_budget,
        "hard_total_target_usd": final_budget["hard_total_target_usd"],
        "apify_discovery_hard_cap_usd": final_budget["hard_apify_discovery_caps_usd"],
        "budget_headroom_usd": final_budget["reserved_headroom_usd"],
        "audit_top10_closed": bool(final_info.get("audit_top10_closed")),
        "durable_paid_preflight": bool(final_info.get("durable_paid_preflight")),
        "ambiguous_actor_start_quarantine": bool(final_info.get("ambiguous_actor_start_quarantine")),
        "manual_refresh_cap_usd": final_info.get("manual_refresh_cap_usd"),
        "analysis_cache": bool(final_info.get("analysis_cache")),
        "analysis_singleflight": bool(final_info.get("analysis_singleflight")),
        "automatic_ai_tick_limit": final_info.get("automatic_ai_tick_limit"),
        "snapshot_lookback_days": final_info.get("snapshot_lookback_days"),
        "snapshot_post_limit": final_info.get("snapshot_post_limit"),
        "safe_media_download": bool(final_info.get("safe_media_download")),
        "motion_gate_fail_closed": bool(final_info.get("motion_gate_fail_closed")),
        "cross_site_mutation_block": bool(final_info.get("cross_site_mutation_block")),
        "new_run_debounce_seconds": final_info.get("new_run_debounce_seconds"),
        "legacy_startup_banners_suppressed": True,
        "render_fast_liveness": True,
        "apify_start_compat": True,
        "internal_edge": str((edge_info or {}).get("edge_profile") or ""),
    }
    _APPLIED = True

    add_radar_log(
        "V30 RUNTIME READY + V33 FAIL-OPEN FRONTEND READY: one HTML/CSS/JS runtime; no legacy browser stack; no MutationObserver; V30 paid/search contract unchanged.",
        stage="startup",
        details=dict(_CONTRACT),
    )
    return dict(_CONTRACT)
