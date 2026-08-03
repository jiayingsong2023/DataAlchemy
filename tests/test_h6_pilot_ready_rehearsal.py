from scripts.run_h6_pilot_ready_rehearsal import build_report, render_markdown


def test_synthetic_pilot_ready_rehearsal_passes_without_closing_external_gate():
    report = build_report()
    assert report["engineering_result"] == "passed"
    assert report["checks_passed"] == report["checks_total"]
    assert report["qualification_state_simulated"] == "pilot_ready"
    assert report["external_gate"] == "blocked"
    assert "synthetic data" in report["limitations"][0]
    assert "GA-01" in render_markdown(report)
