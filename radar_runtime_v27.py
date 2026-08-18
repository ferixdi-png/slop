"""Single authoritative production bootstrap for the final V27 radar.

`app.py` must activate product behavior only through `activate_v27_runtime()`.
Older versioned modules remain implementation dependencies because they contain
battle-tested STOP/START, budget, cache, source aggregation and production
helpers, but they are no longer independent product runtimes.

Their legacy `stage=startup` READY banners are suppressed while composing the
stack. Operational logs (warnings, errors, source/tick progress) are untouched.
After composition this module emits one authoritative V27 startup line.
"""

from __future__ import annotations

import sys

from radar_logs import add_radar_log, suppress_startup_logs

RUNTIME_VERSION = "omni_veo_veo3_v27_strict_compress10"
PUBLIC_EDGE_PROFILE = RUNTIME_VERSION
_APPLIED = False
_CONTRACT = None


def activate_v27_runtime():
    """Compose the proven internal layers and expose only the final V27 runtime."""
    global _APPLIED, _CONTRACT
    if _APPLIED:
        return dict(_CONTRACT or {"runtime": RUNTIME_VERSION})

    app_module = sys.modules.get("app")
    if app_module is None:
        raise RuntimeError("V27 runtime must be activated from app startup")

    required = ("app", "tick_job", "create_or_resume_job", "top_eligible")
    missing = [name for name in required if not hasattr(app_module, name)]
    if missing:
        raise RuntimeError(f"V27 bootstrap missing app prerequisites: {', '.join(missing)}")

    # The sequence below deliberately preserves the already-tested internal
    # composition order. The architectural cleanup is that only THIS module is
    # allowed to activate it from the product entrypoint.
    with suppress_startup_logs():
        from radar_growth_v6 import apply_growth_overrides
        apply_growth_overrides()

        from radar_budget_v10 import (
            KEEP_LIMIT,
            apply_budget_overrides,
            budget_breakdown,
            wrap_tick_job,
        )
        budget_info = apply_budget_overrides()

        from radar_highfreq_v12 import apply_highfreq_overrides
        budget_info = apply_highfreq_overrides()

        from radar_dialogue_v14 import apply_dialogue_first_overrides, top_eligible_dialogue
        budget_info = apply_dialogue_first_overrides()
        app_module.top_eligible = top_eligible_dialogue

        from overlay_cleanplate_v15 import (
            PRODUCTION_PROFILE_VERSION,
            apply_overlay_cleanplate_overrides,
        )
        apply_overlay_cleanplate_overrides()

        from radar_resilient_v17 import apply_resilient_v17_overrides
        budget_info = apply_resilient_v17_overrides()

        # The monthly budget wrapper must remain inside the V19 hardening
        # wrapper exactly as before.
        app_module.tick_job = wrap_tick_job(app_module.tick_job)

        from radar_hardening_v19 import PROFILE_VERSION, apply_hardening_v19
        budget_info = apply_hardening_v19()

        # This compatibility edge layer transitively activates source
        # aggregation, exact hashtag discovery, fresh-run isolation, momentum
        # persistence and finally V27 strict/compression behavior.
        from radar_edge_v19 import apply_edge_guards
        edge_info = apply_edge_guards()

        from radar_cancel_v18 import cancel_active_job
        from radar_strict_scope_v27 import (
            COMPRESSED_TARGET_SEC,
            DIRECT_MAX_DURATION_SEC,
            SOURCE_MAX_DURATION_SEC,
            TARGET_TAGS,
            _APPLIED as V27_APPLIED,
        )
        from radar_momentum_cloud_v25 import MOMENTUM_RECORD_KEY

        if not V27_APPLIED:
            raise RuntimeError("V27 strict scope did not activate during final bootstrap")

        # These are compatibility/data-profile values. Public status is V27.
        app_module.PROFILE_VERSION = PROFILE_VERSION
        app_module.PRODUCTION_PROFILE_VERSION = PRODUCTION_PROFILE_VERSION
        app_module.KEEP_LIMIT = KEEP_LIMIT
        app_module.BUDGET_INFO = budget_breakdown()
        app_module.budget_breakdown = budget_breakdown
        app_module.cancel_active_job = cancel_active_job
        app_module.RADAR_RUNTIME = RUNTIME_VERSION
        app_module.EDGE_PROFILE = PUBLIC_EDGE_PROFILE
        app_module.RADAR_MAX_DURATION_SEC = SOURCE_MAX_DURATION_SEC

    # Re-read the final budget after V24/V25 adjusted the three-source caps.
    final_budget = app_module.budget_breakdown()
    _CONTRACT = {
        "runtime": RUNTIME_VERSION,
        "profile": RUNTIME_VERSION,
        "internal_screening_profile": app_module.PROFILE_VERSION,
        "edge_profile": PUBLIC_EDGE_PROFILE,
        "production_profile": app_module.PRODUCTION_PROFILE_VERSION,
        "hashtags": list(TARGET_TAGS),
        "hashtag_limit_each": 250,
        "max_raw_requested": 750,
        "analyze_limit": 420,
        "keep_limit": int(app_module.KEEP_LIMIT),
        "strict_actual_hashtag": True,
        "direct_max_duration_sec": DIRECT_MAX_DURATION_SEC,
        "source_max_duration_sec": SOURCE_MAX_DURATION_SEC,
        "compressed_target_sec": COMPRESSED_TARGET_SEC,
        "momentum_record_key": MOMENTUM_RECORD_KEY,
        "budget": final_budget,
        "legacy_startup_banners_suppressed": True,
        "internal_edge": str((edge_info or {}).get("edge_profile") or ""),
    }
    _APPLIED = True

    add_radar_log(
        "V27 RUNTIME READY: единственный production bootstrap; только #omni/#veo/#veo3, strict hashtag provenance, 1–15.05s source и natural 10s compression.",
        stage="startup",
        details=dict(_CONTRACT),
    )
    return dict(_CONTRACT)
