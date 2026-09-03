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
        "blockers": {
            "episodes": 12,
            "lost_business_s": 40 * HOUR,
            "unknown_share": 0.05,
            # потери размазаны по многим причинам: чёткой цели для разбора нет
            "pareto": [
                {"reason": "waiting_team", "label": "Ждём смежную команду",
                 "business_s": 10 * HOUR, "episodes": 3,
                 "share": 0.25, "cumulative_share": 0.25},
                {"reason": "environment", "label": "Окружение и доступы",
                 "business_s": 9 * HOUR, "episodes": 3,
                 "share": 0.225, "cumulative_share": 0.475},
                {"reason": "requirements", "label": "Требования не готовы",
                 "business_s": 8 * HOUR, "episodes": 2,
                 "share": 0.2, "cumulative_share": 0.675},
                {"reason": "waiting_review", "label": "Ждём ревью",
                 "business_s": 7 * HOUR, "episodes": 2,
                 "share": 0.175, "cumulative_share": 0.85},
                {"reason": "defect", "label": "Дефект в смежном коде",
                 "business_s": 6 * HOUR, "episodes": 2,
                 "share": 0.15, "cumulative_share": 1.0},
            ],
            "current": [],
        },
        "sle": {
            "target": 0.85,
            "attainment": [0.86, 0.88, 0.85, 0.87],
            "counts": [40, 42, 38, 41],
            "met": [34, 37, 32, 36],
            "periods": ["2026-05", "2026-06", "2026-07", "2026-08"],
            "promises": [{"fixed_at": "2026-05-01T00:00:00", "sample_size": 200,
                          "percentile": 85}],
        },
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


# --- застоявшаяся работа -----------------------------------------------------


def test_stalled_work_detected() -> None:
    """Разрыв между объёмом работы и темпом означает, что задачи стоят."""
    findings = analyse(**inputs(), forecast={
        "wip_health": {"ratio": 12.0, "measured_days": 2.5, "implied_days": 30.0}
    })
    finding = next(f for f in findings if f.code == "stalled_work")
    assert finding.severity == Severity.WATCH
    assert "12" in finding.title


def test_stalled_work_silent_when_balanced() -> None:
    findings = analyse(**inputs(), forecast={
        "wip_health": {"ratio": 1.2, "measured_days": 2.5, "implied_days": 3.0}
    })
    assert "stalled_work" not in codes(findings)


def test_stalled_work_without_forecast() -> None:
    """Отсутствие прогноза не ломает остальные правила."""
    assert "stalled_work" not in codes(analyse(**inputs()))


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


# --- блокировки --------------------------------------------------------------


def test_blocker_pareto_flagged_when_few_reasons_dominate() -> None:
    """Если 80% потерь дают две причины, есть чёткая цель для разбора."""
    findings = analyse(
        **inputs(
            blockers={
                "episodes": 20,
                "lost_business_s": 100 * HOUR,
                "unknown_share": 0.0,
                "pareto": [
                    {
                        "reason": "waiting_team",
                        "label": "Ждём смежную команду",
                        "business_s": 60 * HOUR,
                        "episodes": 10,
                        "share": 0.6,
                        "cumulative_share": 0.6,
                    },
                    {
                        "reason": "environment",
                        "label": "Окружение и доступы",
                        "business_s": 25 * HOUR,
                        "episodes": 6,
                        "share": 0.25,
                        "cumulative_share": 0.85,
                    },
                    {
                        "reason": "defect",
                        "label": "Дефект в смежном коде",
                        "business_s": 15 * HOUR,
                        "episodes": 4,
                        "share": 0.15,
                        "cumulative_share": 1.0,
                    },
                ],
                "current": [],
            }
        )
    )
    finding = next(f for f in findings if f.code == "blocker_pareto")
    assert finding.evidence["top_reasons"] == ["waiting_team", "environment"]
    assert "смежную команду" in finding.detail


def test_blocker_pareto_silent_when_losses_are_spread() -> None:
    """Когда причин много и все мелкие, указывать не на что."""
    assert "blocker_pareto" not in codes(analyse(**inputs()))


def test_blocker_pareto_ignores_unknown_reason() -> None:
    """Неуказанная причина не может быть целью для разбора."""
    findings = analyse(
        **inputs(
            blockers={
                "episodes": 10,
                "lost_business_s": 100 * HOUR,
                "unknown_share": 0.7,
                "pareto": [
                    {
                        "reason": "unknown",
                        "label": "Причина не указана",
                        "business_s": 70 * HOUR,
                        "episodes": 7,
                        "share": 0.7,
                        "cumulative_share": 0.7,
                    },
                    {
                        "reason": "environment",
                        "label": "Окружение и доступы",
                        "business_s": 30 * HOUR,
                        "episodes": 3,
                        "share": 0.3,
                        "cumulative_share": 1.0,
                    },
                ],
                "current": [],
            }
        )
    )
    assert "blocker_pareto" not in codes(findings)


def test_blocker_reasons_missing_flagged() -> None:
    """Высокая доля блокировок без причины делает разбивку недостоверной."""
    findings = analyse(
        **inputs(
            blockers={
                "episodes": 10,
                "lost_business_s": 100 * HOUR,
                "unknown_share": 0.7,
                "pareto": [],
                "current": [],
            }
        )
    )
    finding = next(f for f in findings if f.code == "blocker_reasons_missing")
    assert finding.evidence["unknown_share"] == 0.7


def test_blocker_reasons_missing_silent_when_filled() -> None:
    assert "blocker_reasons_missing" not in codes(analyse(**inputs()))


def test_blocker_rules_silent_without_episodes() -> None:
    """Нет блокировок — нечего и разбирать."""
    findings = analyse(
        **inputs(
            blockers={
                "episodes": 0,
                "lost_business_s": 0,
                "unknown_share": 0.0,
                "pareto": [],
                "current": [],
            }
        )
    )
    assert "blocker_pareto" not in codes(findings)
    assert "blocker_reasons_missing" not in codes(findings)


# --- ожидаемый уровень сервиса -----------------------------------------------


def test_sle_missed_flagged_on_sustained_shortfall() -> None:
    """Серия периодов ниже цели означает, что обещание больше не держится."""
    findings = analyse(**inputs(sle={
        "target": 0.85,
        "attainment": [0.84, 0.62, 0.58, 0.55],
        "counts": [40, 42, 38, 41],
        "met": [34, 26, 22, 23],
        "periods": ["2026-05", "2026-06", "2026-07", "2026-08"],
        "promises": [{"fixed_at": "2026-05-01T00:00:00", "sample_size": 200,
                      "percentile": 85}],
    }))
    finding = next(f for f in findings if f.code == "sle_missed")
    assert finding.severity is Severity.ACT
    assert finding.evidence["average"] < 0.85


def test_sle_single_dip_ignored() -> None:
    """Разовый провал ниже цели — в природе перцентиля, а не сигнал."""
    findings = analyse(**inputs(sle={
        "target": 0.85,
        "attainment": [0.86, 0.88, 0.60, 0.87],
        "counts": [40, 42, 38, 41],
        "met": [34, 37, 23, 36],
        "periods": ["2026-05", "2026-06", "2026-07", "2026-08"],
        "promises": [{"fixed_at": "2026-05-01T00:00:00", "sample_size": 200,
                      "percentile": 85}],
    }))
    assert "sle_missed" not in codes(findings)


def test_sle_healthy_is_silent() -> None:
    assert "sle_missed" not in codes(analyse(**inputs()))


def test_sle_silent_without_promise() -> None:
    """Без зафиксированного обещания сравнивать не с чем."""
    findings = analyse(**inputs(sle={
        "target": None, "attainment": [], "counts": [], "met": [],
        "periods": [], "promises": [],
    }))
    assert "sle_missed" not in codes(findings)


def test_sle_silent_on_short_history() -> None:
    findings = analyse(**inputs(sle={
        "target": 0.85,
        "attainment": [0.4, 0.3],
        "counts": [10, 12], "met": [4, 4],
        "periods": ["2026-07", "2026-08"],
        "promises": [{"fixed_at": "2026-05-01T00:00:00", "sample_size": 200,
                      "percentile": 85}],
    }))
    assert "sle_missed" not in codes(findings)


def test_sle_mild_shortfall_is_watch_not_act() -> None:
    findings = analyse(**inputs(sle={
        "target": 0.85,
        "attainment": [0.78, 0.77, 0.79],
        "counts": [40, 42, 38], "met": [31, 32, 30],
        "periods": ["2026-06", "2026-07", "2026-08"],
        "promises": [{"fixed_at": "2026-05-01T00:00:00", "sample_size": 200,
                      "percentile": 85}],
    }))
    finding = next(f for f in findings if f.code == "sle_missed")
    assert finding.severity is Severity.WATCH
