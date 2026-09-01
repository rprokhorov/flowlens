"""Тесты отчёта о качестве данных."""

from __future__ import annotations

from flowlens.core.quality import build_report
from flowlens.core.reconciliation import Anomaly


def row(key: str, confidence: str, source: str, anomalies=None, has_declared=True) -> dict:
    return {
        "key": key,
        "confidence": confidence,
        "source": source,
        "anomalies": anomalies or [],
        "has_declared": has_declared,
    }


def test_empty_report() -> None:
    report = build_report([])
    assert report.total_tickets == 0
    assert report.verdict() == "Нет данных."


def test_counts_unique_tickets() -> None:
    """У тикета две границы — считается один раз."""
    rows = [
        row("P-1", "high", "declared"),
        row("P-1", "high", "declared"),
        row("P-2", "medium", "system"),
        row("P-2", "medium", "system"),
    ]
    report = build_report(rows)
    assert report.total_tickets == 2


def test_worst_confidence_wins() -> None:
    """Если одна граница ненадёжна, весь тикет считается ненадёжным."""
    rows = [
        row("P-1", "high", "declared"),
        row("P-1", "low", "system"),
    ]
    report = build_report(rows)
    assert report.by_confidence == {"low": 1}


def test_trustworthy_percentage() -> None:
    rows = [row(f"P-{i}", "high", "declared") for i in range(8)]
    rows += [row(f"Q-{i}", "low", "system") for i in range(2)]
    report = build_report(rows)
    assert report.trustworthy_pct == 80.0


def test_declared_coverage() -> None:
    rows = [row(f"P-{i}", "high", "declared", has_declared=True) for i in range(3)]
    rows += [row(f"Q-{i}", "medium", "system", has_declared=False) for i in range(1)]
    report = build_report(rows)
    assert report.declared_coverage_pct == 75.0


def test_anomalies_grouped_and_sorted() -> None:
    rows = [
        row("P-1", "low", "system", [Anomaly.DECLARED_MISSING.value]),
        row("P-2", "low", "system", [Anomaly.DECLARED_MISSING.value]),
        row("P-3", "low", "system", [Anomaly.BULK_MOVE.value]),
    ]
    report = build_report(rows)
    assert report.anomalies[0].code == Anomaly.DECLARED_MISSING.value
    assert report.anomalies[0].count == 2
    assert report.anomalies[1].count == 1


def test_anomaly_has_human_label() -> None:
    rows = [row("P-1", "low", "system", [Anomaly.BULK_MOVE.value])]
    report = build_report(rows)
    group = report.anomalies[0]
    assert "одним махом" in group.label
    assert group.hint  # подсказка, что делать


def test_anomalies_merged_across_boundaries() -> None:
    rows = [
        row("P-1", "low", "system", [Anomaly.DECLARED_MISSING.value]),
        row("P-1", "low", "system", [Anomaly.END_BEFORE_START.value]),
    ]
    report = build_report(rows)
    codes = {a.code for a in report.anomalies}
    assert codes == {Anomaly.DECLARED_MISSING.value, Anomaly.END_BEFORE_START.value}


def test_sample_keys_limited() -> None:
    rows = [
        row(f"P-{i}", "low", "system", [Anomaly.DECLARED_MISSING.value]) for i in range(20)
    ]
    report = build_report(rows, sample_size=3)
    assert len(report.anomalies[0].sample_keys) == 3
    assert report.anomalies[0].count == 20


def test_verdict_reflects_quality() -> None:
    good = build_report([row(f"P-{i}", "high", "declared") for i in range(10)])
    assert "пригодны для анализа" in good.verdict()

    mixed = build_report(
        [row(f"P-{i}", "high", "declared") for i in range(6)]
        + [row(f"Q-{i}", "low", "system") for i in range(4)]
    )
    assert "с оговорками" in mixed.verdict()

    bad = build_report(
        [row(f"P-{i}", "high", "declared") for i in range(2)]
        + [row(f"Q-{i}", "low", "system") for i in range(8)]
    )
    assert "доверять нельзя" in bad.verdict()


def test_affected_tickets_counted() -> None:
    rows = [row(f"P-{i}", "high", "declared") for i in range(7)]
    rows += [row(f"Q-{i}", "medium", "system") for i in range(3)]
    report = build_report(rows)
    assert report.affected_tickets == 3
