"""Тесты правил построения наблюдений."""

from __future__ import annotations

from flowlens.core.advice import Finding, Severity, Thresholds, analyse

HOUR = 3600
WORKDAY = 9 * HOUR


def inputs(**overrides):
    """Здоровый процесс: правила не должны срабатывать."""
    base = {
        "summary": {
            "total_tickets": 200,
            "open_tickets": 12,
            "completed": 188,
            "p50_cycle_s": 2 * WORKDAY,
            "p85_cycle_s": 5 * WORKDAY,
            "avg_flow_efficiency": 0.55,
            "reopens": 2,
            "ever_blocked": 10,
            "trustworthy_pct": 90.0,
        },
        "flow": {
            "efficiency": 0.55,
            "touch_s": 550 * HOUR,
            "queue_s": 450 * HOUR,
            "blocked_s": 50 * HOUR,
            "by_phase": [
                {"phase": "in_progress", "total_s": 500 * HOUR,
                 "p50_s": 6 * HOUR, "p85_s": 12 * HOUR},
                {"phase": "verify", "total_s": 200 * HOUR,
                 "p50_s": 4 * HOUR, "p85_s": 9 * HOUR},
            ],
        },
        "arrival": {
            "periods": ["2026-07-06", "2026-07-13", "2026-07-20", "2026-07-27"],
            "arrived": [20, 18, 22, 19],
            "completed": [21, 19, 21, 20],
            "net_per_period": [-1, -1, 1, -1],
        },
        "aging": {
            "total": 9,  # по 3 задачи на человека — норма для Kanban
            "over_p85": 1,
            "blocked": 0,
            "reference": {"p50": 2 * WORKDAY, "p85": 5 * WORKDAY},
            "items": [{"key": "P-1", "is_blocked": False}],
        },
        "people": {
            "people": [
                {"person": "Иван", "owned_s": 300 * HOUR, "touch_s": 200 * HOUR,
                 "blocked_s": 10 * HOUR, "completed": 40, "active_days": 60},
                {"person": "Мария", "owned_s": 280 * HOUR, "touch_s": 190 * HOUR,
                 "blocked_s": 8 * HOUR, "completed": 38, "active_days": 60},
                {"person": "Пётр", "owned_s": 260 * HOUR, "touch_s": 180 * HOUR,
                 "blocked_s": 9 * HOUR, "completed": 35, "active_days": 60},
            ]
        },
        "quality": {"trustworthy_pct": 90.0, "anomalies": []},
    }
    base.update(overrides)
    return base


def codes(findings: list[Finding]) -> set[str]:
    return {f.code for f in findings}


# --- здоровый процесс --------------------------------------------------------


def test_healthy_process_has_no_findings() -> None:
    """На хороших показателях правила молчат."""
    assert analyse(**inputs()) == []


# --- качество данных ---------------------------------------------------------


def test_low_data_quality_flagged() -> None:
    findings = analyse(**inputs(quality={
        "trustworthy_pct": 35.0,
        "anomalies": [{"code": "declared_missing", "label": "Даты не заполнены", "count": 80}],
    }))
    finding = next(f for f in findings if f.code == "data_quality")
    assert finding.severity == Severity.ACT
    assert "35" in finding.title


def test_moderate_data_quality_is_watch() -> None:
    findings = analyse(**inputs(quality={"trustworthy_pct": 50.0, "anomalies": []}))
    finding = next(f for f in findings if f.code == "data_quality")
    assert finding.severity == Severity.WATCH


def test_good_data_quality_silent() -> None:
    findings = analyse(**inputs(quality={"trustworthy_pct": 85.0, "anomalies": []}))
    assert "data_quality" not in codes(findings)


# --- рост очереди ------------------------------------------------------------


def test_queue_growth_detected() -> None:
    findings = analyse(**inputs(arrival={
        "periods": ["p1", "p2", "p3", "p4"],
        "arrived": [30, 32, 35, 20],
        "completed": [20, 21, 22, 15],
        "net_per_period": [10, 11, 13, 5],
    }))
    finding = next(f for f in findings if f.code == "queue_growth")
    assert finding.severity == Severity.ACT
    assert finding.evidence["accumulated"] == 34


def test_queue_growth_needs_consecutive_periods() -> None:
    """Единичный всплеск не считается ростом очереди."""
    findings = analyse(**inputs(arrival={
        "periods": ["p1", "p2", "p3", "p4"],
        "arrived": [20, 30, 20, 20],
        "completed": [21, 20, 21, 20],
        "net_per_period": [-1, 10, -1, 0],
    }))
    assert "queue_growth" not in codes(findings)


def test_last_period_ignored() -> None:
    """Последний период обычно неполный и не участвует в оценке."""
    findings = analyse(**inputs(arrival={
        "periods": ["p1", "p2", "p3", "p4"],
        "arrived": [20, 20, 20, 30],
        "completed": [21, 21, 21, 5],
        "net_per_period": [-1, -1, -1, 25],
    }))
    assert "queue_growth" not in codes(findings)


# --- эффективность потока ----------------------------------------------------


def test_low_flow_efficiency() -> None:
    data = inputs()
    data["flow"]["efficiency"] = 0.22
    findings = analyse(**data)
    finding = next(f for f in findings if f.code == "low_flow_efficiency")
    assert finding.severity == Severity.WATCH
    assert "78%" in finding.title  # доля ожидания


def test_critical_flow_efficiency() -> None:
    data = inputs()
    data["flow"]["efficiency"] = 0.10
    findings = analyse(**data)
    finding = next(f for f in findings if f.code == "low_flow_efficiency")
    assert finding.severity == Severity.ACT


def test_threshold_is_configurable() -> None:
    data = inputs()
    data["flow"]["efficiency"] = 0.35
    assert "low_flow_efficiency" not in codes(analyse(**data))
    strict = analyse(**data, thresholds=Thresholds(low_flow_efficiency=0.5))
    assert "low_flow_efficiency" in codes(strict)


# --- блокировки --------------------------------------------------------------


def test_blocked_time_flagged() -> None:
    data = inputs()
    data["flow"]["blocked_s"] = 400 * HOUR
    data["flow"]["by_phase"].append(
        {"phase": "blocked", "total_s": 400 * HOUR, "p50_s": 2 * WORKDAY, "p85_s": 5 * WORKDAY}
    )
    findings = analyse(**data)
    finding = next(f for f in findings if f.code == "blocked_time")
    assert finding.severity == Severity.ACT


def test_small_blocked_time_ignored() -> None:
    assert "blocked_time" not in codes(analyse(**inputs()))


# --- возраст незавершённых ---------------------------------------------------


def test_aging_flagged_with_examples() -> None:
    findings = analyse(**inputs(aging={
        "total": 20,
        "over_p85": 14,
        "blocked": 0,
        "reference": {"p50": 2 * WORKDAY, "p85": 5 * WORKDAY},
        "items": [{"key": f"P-{i}", "is_blocked": False} for i in range(10)],
    }))
    finding = next(f for f in findings if f.code == "aging_wip")
    assert finding.severity == Severity.ACT
    assert len(finding.ticket_keys) == 5  # примеры, а не весь список


def test_aging_ignored_when_few() -> None:
    assert "aging_wip" not in codes(analyse(**inputs()))


# --- незавершённая работа ----------------------------------------------------


def test_high_wip_per_person() -> None:
    data = inputs()
    data["aging"]["total"] = 40  # при трёх участниках — заметно выше нормы
    findings = analyse(**data)
    finding = next(f for f in findings if f.code == "high_wip")
    assert finding.evidence["wip_per_person"] > 10


def test_normal_wip_silent() -> None:
    assert "high_wip" not in codes(analyse(**inputs()))


# --- переоткрытия ------------------------------------------------------------


def test_reopen_rate_flagged() -> None:
    data = inputs()
    data["summary"]["reopens"] = 30
    data["summary"]["completed"] = 188
    findings = analyse(**data)
    finding = next(f for f in findings if f.code == "reopen_rate")
    assert "16%" in finding.title


def test_low_reopen_rate_silent() -> None:
    assert "reopen_rate" not in codes(analyse(**inputs()))


# --- самая долгая фаза -------------------------------------------------------


def test_slowest_phase_when_dominant() -> None:
    data = inputs()
    data["flow"]["by_phase"] = [
        {"phase": "in_progress", "total_s": 100 * HOUR, "p50_s": 2 * HOUR, "p85_s": 4 * HOUR},
        {"phase": "verify", "total_s": 100 * HOUR, "p50_s": 2 * HOUR, "p85_s": 4 * HOUR},
        {"phase": "blocked", "total_s": 100 * HOUR, "p50_s": 20 * HOUR, "p85_s": 40 * HOUR},
    ]
    findings = analyse(**data)
    finding = next(f for f in findings if f.code == "slowest_phase")
    assert "блокировке" in finding.title


def test_no_slowest_phase_when_balanced() -> None:
    assert "slowest_phase" not in codes(analyse(**inputs()))


# --- распределение нагрузки --------------------------------------------------


def test_load_imbalance_detected() -> None:
    data = inputs()
    data["people"]["people"][0]["owned_s"] = 900 * HOUR
    findings = analyse(**data)
    finding = next(f for f in findings if f.code == "load_imbalance")
    assert finding.severity == Severity.INFO
    assert "Иван" in finding.detail


def test_load_imbalance_needs_enough_people() -> None:
    data = inputs()
    data["people"]["people"] = data["people"]["people"][:2]
    data["people"]["people"][0]["owned_s"] = 900 * HOUR
    assert "load_imbalance" not in codes(analyse(**data))


# --- общее поведение ---------------------------------------------------------


def test_findings_sorted_by_severity() -> None:
    data = inputs()
    data["flow"]["efficiency"] = 0.10
    data["aging"] = {
        "total": 20, "over_p85": 15, "blocked": 0,
        "reference": {"p50": WORKDAY, "p85": 5 * WORKDAY},
        "items": [{"key": f"P-{i}", "is_blocked": False} for i in range(5)],
    }
    findings = analyse(**data)
    order = {Severity.ACT: 0, Severity.WATCH: 1, Severity.INFO: 2}
    ranks = [order[f.severity] for f in findings]
    assert ranks == sorted(ranks)


def test_every_finding_has_suggestion() -> None:
    """Наблюдение без подсказки бесполезно."""
    data = inputs()
    data["flow"]["efficiency"] = 0.10
    data["summary"]["reopens"] = 40
    data["quality"] = {"trustworthy_pct": 30.0, "anomalies": []}
    for finding in analyse(**data):
        assert finding.suggestion, finding.code
        assert finding.detail, finding.code


def test_empty_inputs_do_not_crash() -> None:
    """Пустая база не должна ломать правила."""
    findings = analyse(
        summary={"total_tickets": 0, "completed": 0, "reopens": 0, "open_tickets": 0},
        flow={"efficiency": None, "touch_s": 0, "queue_s": 0, "blocked_s": 0, "by_phase": []},
        arrival={"periods": [], "arrived": [], "completed": [], "net_per_period": []},
        aging={"total": 0, "over_p85": 0, "blocked": 0, "reference": {}, "items": []},
        people={"people": []},
        quality={"trustworthy_pct": 100.0, "anomalies": []},
    )
    assert findings == []
