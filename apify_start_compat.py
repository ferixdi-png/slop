"""Compatibility shim for Apify ActorClient.start across client versions.

V27 passes restart_on_error=False only as an explicit spelling of the default
behavior. Some installed Apify client builds reject that keyword even though the
rest of the start API (run_input / max_items / max_total_charge_usd) works.

Patch the concrete ActorClient class once and strip only restart_on_error. This
keeps item/cost caps intact and avoids version-specific TypeError loops.
"""

from __future__ import annotations

from apify_client import ApifyClient

_APPLIED = False
_ACTOR_CLASS = None


def install_apify_start_compat():
    global _APPLIED, _ACTOR_CLASS
    if _APPLIED:
        return True

    # actor() is a local client factory and does not perform a network request.
    probe = ApifyClient("compat-probe").actor("apify/instagram-hashtag-scraper")
    actor_cls = type(probe)

    if getattr(actor_cls, "_slop_restart_on_error_compat", False):
        _APPLIED = True
        _ACTOR_CLASS = actor_cls
        return True

    original_start = actor_cls.start
    actor_cls._slop_original_start = original_start

    def start_compat(self, *args, **kwargs):
        # False is already the desired/default behavior. Removing only this
        # optional keyword preserves max_items and max_total_charge_usd guards.
        kwargs.pop("restart_on_error", None)
        return actor_cls._slop_original_start(self, *args, **kwargs)

    start_compat.__name__ = getattr(original_start, "__name__", "start")
    start_compat.__doc__ = getattr(original_start, "__doc__", None)
    actor_cls.start = start_compat
    actor_cls._slop_restart_on_error_compat = True

    _APPLIED = True
    _ACTOR_CLASS = actor_cls
    return True
