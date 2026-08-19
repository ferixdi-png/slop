"""Single authoritative production bootstrap for the final radar runtime.

V30 remains the durable/budget/safety persistence contract. V34 exposes a broad
50-100 momentum pool, V35 makes work manual-start-only, V36 hardens production
prompts, V38 makes structured Gemini screening reliable, V39 restores durable
TOP/prompt cache passively, and V40 makes public view metrics auditable while
handling temporary Gemini overload without turning it into an internal 500.
"""

from __future__ import annotations

import sys

from flask import request

from apify_start_compat import install_apify_start_compat
from radar_discovery_v27 import install_v27_high_volume_discovery
from radar_logs import add_radar_log, suppress_startup_logs

# Persistence identity deliberately remains V30 so deploys do not invalidate or
# duplicate a paid durable search. Later versions are product/control/reliability
# overlays and must not change this semantic job identity.
RUNTIME_VERSION = "multiplatform_speech_v30_audit10_budget5"
PUBLIC_EDGE_PROFILE = RUNTIME_VERSION
PRODUCT_MODE = "multiplatform_broad_v34_trendpool100"
FRONTEND_PROFILE = "frontend_v34_broad_fail_open"
CONTROL_MODE = "manual_start_only_v35"
PROMPT_MODE = "production_prompts_v36_authenticated_media"
METRICS_MODE = "metrics_truth_v40_public_counts"
GEMINI_RELIABILITY_MODE = "gemini_overload_v40"
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
                "X-Radar-Product": PRODUCT_MODE,
                "X-Frontend-Profile": FRONTEND_PROFILE,
            }
        return None

    def _healthz():
        return {
            "ok": True,
            "runtime": RUNTIME_VERSION,
            "product_mode": PRODUCT_MODE,
            "control_mode": CONTROL_MODE,
            "prompt_mode": PROMPT_MODE,
            "metrics_mode": METRICS_MODE,
            "gemini_reliability_mode": GEMINI_RELIABILITY_MODE,
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
    """Compose every production layer once, in dependency order."""
    global _APPLIED, _CONTRACT
    if _APPLIED:
        return dict(_CONTRACT or {"runtime": RUNTIME_VERSION, "product_mode": PRODUCT_MODE})

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

        from overlay_cleanplate_v15 import PRODUCTION_PROFILE_VERSION, apply_overlay_cleanplate_overrides
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

        # V38 must be explicitly installed after V30 captured its base classifier.
        # V38 also activates V39 passive read-only recovery. Previous tests could
        # import diagnostics without proving this production activation; V40 makes
        # the bootstrap authoritative.
        from radar_json_headroom_v38 import install_json_headroom_v38
        v38_info = install_json_headroom_v38()

        from radar_v28_finish import apply_v28_finish
        finish_info = apply_v28_finish()

        # Current google-genai Interactions adapter for public YouTube URLs.
        from youtube_genai_v34 import install_youtube_v34
        youtube_info = install_youtube_v34()

        # V34 broad product: momentum is the visibility gate; AI is enrichment only.
        from radar_broad_v34 import apply_broad_v34
        v34_info = apply_broad_v34()

        # Full 5-tag / 14-day cross-run measured view growth.
        from radar_momentum_v34 import apply_momentum_v34
        momentum_info = apply_momentum_v34()

        # V35 MUST be installed after every backend work wrapper so it is the final
        # authority over create/resume/tick. Opening/F5/deploy cannot advance work.
        from radar_manual_start_v35 import install_manual_start_v35
        manual_info = install_manual_start_v35(app_module)

        # Patch the already-built V34 single-runtime HTML before it is served.
        from frontend_manual_start_v35 import patch_frontend_v35
        manual_frontend_info = patch_frontend_v35()

        # V36 is intentionally after V30 media hardening. It keeps all V30 safety
        # checks but adds scoped Apify auth and broad-candidate prompt access.
        from prompt_reliability_v36 import install_prompt_reliability_v36
        prompt_info = install_prompt_reliability_v36(app_module)

        # V40 metric truth is installed after V34 momentum + V35 HTML patch so it
        # can guard metric-history provenance and patch the actual final HTML. It
        # never starts a paid refresh automatically; one-item refresh is user-only.
        from metrics_truth_v40 import install_metrics_truth_v40
        metrics_info = install_metrics_truth_v40(app_module.app)

        # Temporary Gemini high-demand failures get bounded stage retries and, if
        # still unavailable, a truthful HTTP 503 rather than an internal 500.
        from gemini_overload_v40 import install_gemini_overload_v40
        overload_info = install_gemini_overload_v40(app_module.app)

        # One fail-open browser runtime only. Its globals already contain V34+V35+V40.
        from frontend_failopen_v33 import install_frontend_v33
        frontend_info = install_frontend_v33(app_module.app)

        final_keep_limit = int(v34_info.get("target_output_max") or v30_info.get("keep_limit") or budget.KEEP_LIMIT)
        final_budget = budget_breakdown_v29()

        app_module.PROFILE_VERSION = v30_info["screening_profile"]
        app_module.PRODUCTION_PROFILE_VERSION = PRODUCTION_PROFILE_VERSION
        app_module.KEEP_LIMIT = final_keep_limit
        app_module.BUDGET_INFO = final_budget
        app_module.budget_breakdown = budget_breakdown_v29
        app_module.cancel_active_job = cancel_active_job
        app_module.RADAR_RUNTIME = RUNTIME_VERSION
        app_module.EDGE_PROFILE = PUBLIC_EDGE_PROFILE
        app_module.RADAR_MAX_DURATION_SEC = SOURCE_MAX_DURATION_SEC

    passive_recovery = dict(v38_info.get("passive_recovery") or {})
    _CONTRACT = {
        "runtime": RUNTIME_VERSION,
        "profile": RUNTIME_VERSION,
        "product_mode": PRODUCT_MODE,
        "control_mode": CONTROL_MODE,
        "prompt_mode": PROMPT_MODE,
        "metrics_mode": METRICS_MODE,
        "gemini_reliability_mode": GEMINI_RELIABILITY_MODE,
        "internal_screening_profile": v30_info["screening_profile"],
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
        "manual_start_only": bool(manual_info.get("manual_start_only")),
        "auto_resume_on_page_load": bool(manual_info.get("auto_resume_on_page_load", False)),
        "tick_requires_driver_token": bool(manual_info.get("tick_requires_driver_token")),
        "driver_token_persisted": bool(manual_info.get("driver_token_persisted", False)),
        "deploy_resume_policy": manual_info.get("deploy_resume_policy"),
        "frontend_manual_start_only": bool(manual_frontend_info.get("manual_start_only")),
        "platforms": v30_info["platforms"],
        "hashtags": v30_info["hashtags"],
        "lookback_days": v30_info["lookback_days"],
        "results_per_tag_per_platform": v30_info["results_per_tag_per_platform"],
        "max_raw_requested": v30_info["max_raw_requested"],
        "analyze_limit": v30_info["analyze_limit"],
        "keep_limit": final_keep_limit,
        "target_output_min": v34_info["target_output_min"],
        "target_output_max": v34_info["target_output_max"],
        "ai_enrich_limit": v34_info["ai_enrich_limit"],
        "ai_role": "enrichment_only",
        "speech_required": False,
        "no_speech_policy": "keep_and_add_russian_speech",
        "timing_reject_policy": "keep_and_rewrite_to_10s",
        "ai_error_policy": "keep_for_manual_review",
        "strict_actual_hashtag": True,
        "youtube_direct_gemini": True,
        "youtube_interactions_schema": youtube_info["youtube_interactions_schema"],
        "youtube_failure_policy": youtube_info["youtube_failure_policy"],
        "momentum_profile": momentum_info["profile"],
        "momentum_ranking": momentum_info["ranking"],
        "momentum_scope_tags": momentum_info["tags"],
        "momentum_scope_days": momentum_info["lookback_days"],
        "automatic_paid_refreshes": False,
        "v28_14day_api": bool(finish_info.get("v28_14day_api")),
        "direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
        "source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
        "compressed_target_sec": COMPRESSED_TARGET_SEC,
        "momentum_record_key": MOMENTUM_RECORD_KEY,
        "budget": final_budget,
        "hard_total_target_usd": final_budget["hard_total_target_usd"],
        "apify_discovery_hard_cap_usd": final_budget["hard_apify_discovery_caps_usd"],
        "budget_headroom_usd": final_budget["reserved_headroom_usd"],
        "audit_top10_closed": bool(v30_info.get("audit_top10_closed")),
        "durable_paid_preflight": bool(v30_info.get("durable_paid_preflight")),
        "ambiguous_actor_start_quarantine": bool(v30_info.get("ambiguous_actor_start_quarantine")),
        "manual_refresh_cap_usd": v30_info.get("manual_refresh_cap_usd"),
        "analysis_cache": bool(v30_info.get("analysis_cache")),
        "analysis_singleflight": bool(v30_info.get("analysis_singleflight")),
        "automatic_ai_tick_limit": v30_info.get("automatic_ai_tick_limit"),
        "snapshot_lookback_days": v30_info.get("snapshot_lookback_days"),
        "snapshot_post_limit": v30_info.get("snapshot_post_limit"),
        "safe_media_download": bool(v30_info.get("safe_media_download")),
        "motion_gate_fail_closed": bool(v30_info.get("motion_gate_fail_closed")),
        "cross_site_mutation_block": bool(v30_info.get("cross_site_mutation_block")),
        "new_run_debounce_seconds": v30_info.get("new_run_debounce_seconds"),
        "apify_private_media_auth": prompt_info.get("apify_private_media_auth"),
        "apify_signed_record_fallback": bool(prompt_info.get("apify_signed_record_fallback")),
        "broad_candidate_prompt_access": bool(prompt_info.get("broad_candidate_prompt_access")),
        "analysis_cache_for_broad_candidates": bool(prompt_info.get("analysis_cache_for_broad_candidates")),
        "prompt_token_in_url": bool(prompt_info.get("token_in_url")),
        "redirect_token_forwarding": bool(prompt_info.get("redirect_token_forwarding")),
        "structured_output_max_tokens": v38_info.get("structured_output_max_tokens"),
        "v38_installed": True,
        "passive_recovery_profile": passive_recovery.get("profile"),
        "passive_recovery_active": True,
        "passive_recovery_paid_discovery_started": bool(passive_recovery.get("paid_discovery_started", False)),
        "durable_prompt_cache_limit": passive_recovery.get("analysis_cache_limit"),
        "metrics_profile": metrics_info.get("profile"),
        "instagram_views_metric": metrics_info.get("instagram_views"),
        "instagram_views_deprecated_fallback": metrics_info.get("instagram_deprecated_fallback"),
        "tiktok_views_metric": metrics_info.get("tiktok_views"),
        "youtube_views_metric": metrics_info.get("youtube_views"),
        "metrics_timestamped": bool(metrics_info.get("metrics_timestamped")),
        "stale_metric_overwrite_blocked": bool(metrics_info.get("stale_metric_overwrite_blocked")),
        "momentum_metric_source_guard": bool(metrics_info.get("momentum_metric_source_guard")),
        "manual_metric_refresh": bool(metrics_info.get("manual_metric_refresh")),
        "automatic_metric_refresh": bool(metrics_info.get("automatic_metric_refresh", False)),
        "manual_metric_refresh_hard_cap_usd": metrics_info.get("manual_metric_refresh_hard_cap_usd"),
        "gemini_overload_profile": overload_info.get("profile"),
        "gemini_transient_retry_delays_seconds": overload_info.get("retry_delays_seconds"),
        "gemini_transient_max_extra_attempts": overload_info.get("max_extra_attempts"),
        "gemini_validation_errors_retried": bool(overload_info.get("validation_errors_retried")),
        "gemini_final_overload_http": overload_info.get("final_http_status"),
        "legacy_startup_banners_suppressed": True,
        "render_fast_liveness": True,
        "apify_start_compat": True,
        "internal_edge": str((edge_info or {}).get("edge_profile") or ""),
    }
    _APPLIED = True

    add_radar_log(
        "V40 MVP READY: V38/V39 explicitly active; public platform view metrics are timestamped/provenanced; metric-source changes cannot fake momentum; manual one-item metric refresh only; transient Gemini overload gets bounded retry + HTTP 503; hard search budget unchanged.",
        stage="startup",
        details=dict(_CONTRACT),
    )
    return dict(_CONTRACT)
