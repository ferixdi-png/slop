"""Startup compatibility patch for radar source aliases.

The mass AI profile renamed Apify source keys to popular_ai / ai_hashtags /
ai_keywords / known_ai_creators, while the stable request-state-machine candidate
preparer still consumes popular / hashtags / creators. This bridge makes the
stable collector expose both naming schemes without duplicating network calls.
"""

try:
    import radar_request_job as _radar_job

    if not getattr(_radar_job, "_SOURCE_ALIAS_COMPAT_APPLIED", False):
        _original_collect_source_rows = _radar_job._collect_source_rows

        def _collect_source_rows_with_aliases(client, job):
            results = _original_collect_source_rows(client, job)

            def merged(*names):
                rows = []
                seen = set()
                for name in names:
                    for row in results.get(name) or []:
                        # Preserve every distinct source row while avoiding exact
                        # object duplicates when both legacy and new aliases exist.
                        marker = id(row)
                        if marker in seen:
                            continue
                        seen.add(marker)
                        rows.append(row)
                return rows

            results["popular"] = merged("popular", "popular_ai")
            results["hashtags"] = merged("hashtags", "ai_hashtags", "ai_keywords")
            results["creators"] = merged("creators", "known_ai_creators")
            return results

        _radar_job._collect_source_rows = _collect_source_rows_with_aliases
        _radar_job._SOURCE_ALIAS_COMPAT_APPLIED = True
except Exception:
    # Startup must never be taken down by an optional compatibility bridge.
    # The normal application logger will expose downstream radar errors.
    pass
