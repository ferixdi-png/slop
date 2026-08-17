"""Compatibility bridge between mass AI source names and stable radar pipeline."""

_APPLIED = False


def apply_source_alias_compat():
    """Expose mass source datasets under the legacy names consumed downstream.

    The mass profile emits popular_ai / ai_hashtags / ai_keywords /
    known_ai_creators. The stable candidate preparer expects popular / hashtags /
    creators. We patch only the local collector result; no Apify calls are
    duplicated and downstream URL deduplication remains unchanged.
    """
    global _APPLIED
    if _APPLIED:
        return

    import radar_request_job as radar_job

    if getattr(radar_job, "_SOURCE_ALIAS_COMPAT_APPLIED", False):
        _APPLIED = True
        return

    original_collect = radar_job._collect_source_rows

    def collect_source_rows_with_aliases(client, job):
        results = original_collect(client, job)

        def merged(*names):
            rows = []
            seen_objects = set()
            for name in names:
                for row in results.get(name) or []:
                    marker = id(row)
                    if marker in seen_objects:
                        continue
                    seen_objects.add(marker)
                    rows.append(row)
            return rows

        results["popular"] = merged("popular", "popular_ai")
        results["hashtags"] = merged("hashtags", "ai_hashtags", "ai_keywords")
        results["creators"] = merged("creators", "known_ai_creators")
        return results

    radar_job._collect_source_rows = collect_source_rows_with_aliases
    radar_job._SOURCE_ALIAS_COMPAT_APPLIED = True
    radar_job._SOURCE_ALIAS_ORIGINAL_COLLECT = original_collect
    _APPLIED = True
