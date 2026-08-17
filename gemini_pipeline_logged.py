from gemini_service import (
    audit_package,
    audit_passes,
    build_forensic_map,
    build_production_package,
    normalize_package,
    with_uploaded_file,
)
from radar_logs import add_radar_log


def analyze_video_logged(file_path, owned=False, expected_duration=None):
    """Run the full forensic -> production -> QA -> repair pipeline with Render-visible logs."""
    add_radar_log(
        "Начинаю полный production-анализ Gemini.",
        stage="gemini",
        details={"expected_duration_sec": expected_duration},
    )

    def run(client, uploaded):
        add_radar_log("Видео загружено в Gemini Files API и перешло в ACTIVE.", stage="gemini")

        add_radar_log("PASS 1/3: forensic-карта исходника — старт.", stage="gemini")
        forensic = build_forensic_map(client, uploaded, owned, expected_duration)
        add_radar_log(
            "PASS 1/3: forensic-карта исходника — готово.",
            stage="gemini",
            details={
                "duration": getattr(forensic, "measured_duration_sec", None),
                "characters": len(getattr(forensic, "characters", []) or []),
                "timeline_events": len(getattr(forensic, "timeline_events", []) or []),
                "dialogue_turns": len(getattr(forensic, "dialogue_turns", []) or []),
            },
        )

        add_radar_log("PASS 2/3: PHOTO + VIDEO production package — старт.", stage="gemini")
        package = build_production_package(client, uploaded, forensic, owned, expected_duration)
        add_radar_log(
            "PASS 2/3: PHOTO + VIDEO production package — готово.",
            stage="gemini",
            details={
                "dialogue_lines": len(getattr(package.block_3_video, "dialogue", []) or []),
                "timeline_steps": len(getattr(package.block_3_video, "narrative_timeline", []) or []),
            },
        )

        add_radar_log("PASS 3/3: строгий QA-аудит — старт.", stage="gemini")
        audit = audit_package(client, forensic, package, expected_duration)
        add_radar_log(
            f"PASS 3/3: QA-аудит — {int(audit.overall_match_score or 0)}/100.",
            stage="gemini",
            level="INFO" if audit_passes(audit) else "WARN",
            details={
                "critical_issues": list(audit.critical_issues or []),
                "repair_instructions": list(audit.repair_instructions or []),
            },
        )

        if not audit_passes(audit):
            add_radar_log(
                "QA ниже порога или найден критический mismatch — запускаю автоматический repair-pass.",
                stage="gemini",
                level="WARN",
            )
            package = build_production_package(
                client,
                uploaded,
                forensic,
                owned,
                expected_duration,
                repair={
                    "overall_match_score": audit.overall_match_score,
                    "critical_issues": audit.critical_issues,
                    "repair_instructions": audit.repair_instructions,
                },
            )
            add_radar_log("Repair package готов. Запускаю повторный QA.", stage="gemini")
            audit = audit_package(client, forensic, package, expected_duration)
            add_radar_log(
                f"Повторный QA — {int(audit.overall_match_score or 0)}/100.",
                stage="gemini",
                level="INFO" if audit_passes(audit) else "WARN",
                details={"critical_issues": list(audit.critical_issues or [])},
            )

        result = normalize_package(package, expected_duration, audit.overall_match_score)
        add_radar_log(
            "Полный Gemini production pipeline завершён.",
            stage="gemini",
            details={"final_qa": int(audit.overall_match_score or 0)},
        )
        return result

    try:
        return with_uploaded_file(file_path, run)
    except Exception as exc:
        add_radar_log(f"Gemini production pipeline завершился ошибкой: {exc}", level="ERROR", stage="gemini")
        raise
