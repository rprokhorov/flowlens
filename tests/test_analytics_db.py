"""Тесты аналитических запросов."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from flowlens.analytics import (
    Filters,
    aging_wip,
    arrival_vs_throughput,
    arrivals_by_weekday,
    average_wip,
    backlog_age,
    blockers,
    cumulative_flow,
    cycle_time_distribution,
    expedite_share,
    flow_efficiency,
    hidden_queue,
    interventions,
    open_backlog_size,
    people_load,
    predictability,
    summary,
    throughput_history,
    transition_matrix,
)
from flowlens.db import make_engine
from flowlens.pipeline import recompute_all, seed_demo


@pytest.fixture(scope="module")
def engine():
    try:
        eng = make_engine()
        with eng.begin() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres недоступен: {exc}")
    return eng


@pytest.fixture(scope="module")
def data(engine):
    seed_demo(engine, ticket_count=300, months=6, seed=13)
    recompute_all(engine)
    return engine


# --- целостность данных ------------------------------------------------------


def test_no_negative_durations(data) -> None:
    """Длительность не может быть отрицательной."""
    with data.begin() as conn:
        bad = conn.execute(
            text(
                "SELECT count(*) FROM ticket_interval "
                "WHERE duration_calendar_s < 0 OR duration_business_s < 0"
            )
        ).scalar_one()
    assert bad == 0


def test_no_events_in_future(data) -> None:
    """Данных из будущего быть не должно."""
    with data.begin() as conn:
        bad = conn.execute(
            text("SELECT count(*) FROM ticket_event WHERE occurred_at > now()")
        ).scalar_one()
    assert bad == 0


def test_wip_spread_across_phases(data) -> None:
    """Незавершённые задачи распределены по фазам, а не собраны в одной."""
    with data.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT ws.external_name, count(*) AS n FROM ticket_interval ti "
                "JOIN workflow_status ws ON ws.id = ti.status_id "
                "WHERE ti.ended_at IS NULL AND NOT ws.is_terminal "
                "GROUP BY 1"
            )
        ).all()
    assert len(rows) >= 2, "весь WIP скопился в одной фазе — вероятно, артефакт генератора"


# --- сводка ------------------------------------------------------------------


def test_summary_shape(data) -> None:
    result = summary(data, Filters())
    assert result["total_tickets"] > 0
    assert result["completed"] <= result["total_tickets"]
    assert 0 <= result["trustworthy_pct"] <= 100
    assert result["p50_cycle_s"] <= result["p85_cycle_s"] <= result["p95_cycle_s"]


def test_summary_respects_type_filter(data) -> None:
    everything = summary(data, Filters())
    bugs = summary(data, Filters(issue_types=["Bug"]))
    assert 0 < bugs["total_tickets"] < everything["total_tickets"]


def test_summary_respects_date_filter(data) -> None:
    recent = summary(data, Filters(date_from=date.today() - timedelta(days=30)))
    everything = summary(data, Filters())
    assert recent["total_tickets"] < everything["total_tickets"]


# --- распределение времени цикла ---------------------------------------------


def test_cycle_time_percentiles_ordered(data) -> None:
    result = cycle_time_distribution(data, Filters())
    p = result["percentiles"]
    assert p["p50"] <= p["p70"] <= p["p85"] <= p["p95"]
    assert result["min"] <= p["p50"] <= result["max"]


def test_percentile_is_observed_value(data) -> None:
    """Перцентиль — реально наблюдавшееся значение, а не интерполяция."""
    result = cycle_time_distribution(data, Filters())
    with data.begin() as conn:
        exists = conn.execute(
            text("SELECT count(*) FROM ticket_metrics WHERE cycle_time_business_s = :value"),
            {"value": result["percentiles"]["p85"]},
        ).scalar_one()
    assert exists > 0


def test_histogram_covers_all_tickets(data) -> None:
    result = cycle_time_distribution(data, Filters())
    assert sum(bucket["count"] for bucket in result["histogram"]) == result["count"]


def test_calendar_unit_gives_larger_values(data) -> None:
    """Календарное время не меньше рабочего."""
    business = cycle_time_distribution(data, Filters(unit="business"))
    calendar = cycle_time_distribution(data, Filters(unit="calendar"))
    assert calendar["percentiles"]["p50"] > business["percentiles"]["p50"]


def test_confidence_filter_reduces_sample(data) -> None:
    """Фильтр по достоверности отсекает ненадёжные метрики."""
    everything = cycle_time_distribution(data, Filters(min_confidence="low"))
    trusted = cycle_time_distribution(data, Filters(min_confidence="high"))
    assert trusted["count"] < everything["count"]


def test_empty_result_is_safe(data) -> None:
    """Пустая выборка не ломает расчёт."""
    result = cycle_time_distribution(data, Filters(issue_types=["Несуществующий"]))
    assert result["count"] == 0
    assert result["histogram"] == []


# --- CFD ---------------------------------------------------------------------


def test_cfd_has_all_days(data) -> None:
    result = cumulative_flow(data, Filters())
    assert len(result["days"]) > 30
    for series in result["series"]:
        assert len(series["values"]) == len(result["days"])


def test_cfd_done_is_monotonic(data) -> None:
    """Число завершённых задач не может уменьшаться."""
    result = cumulative_flow(data, Filters())
    done = next((s for s in result["series"] if s["phase"] == "done"), None)
    assert done is not None
    assert done["values"] == sorted(done["values"])


def test_cfd_values_non_negative(data) -> None:
    result = cumulative_flow(data, Filters())
    for series in result["series"]:
        assert all(v >= 0 for v in series["values"]), series["phase"]


def test_cfd_is_fast(data) -> None:
    """CFD должен считаться быстро: это основной график дашборда."""
    import time

    started = time.monotonic()
    cumulative_flow(data, Filters())
    assert time.monotonic() - started < 1.0


# --- поступление и закрытие --------------------------------------------------


def test_arrival_throughput_aligned(data) -> None:
    result = arrival_vs_throughput(data, Filters())
    assert len(result["arrived"]) == len(result["periods"])
    assert len(result["completed"]) == len(result["periods"])


def test_backlog_delta_accumulates(data) -> None:
    result = arrival_vs_throughput(data, Filters())
    expected = 0
    for i, (a, c) in enumerate(zip(result["arrived"], result["completed"], strict=True)):
        expected += a - c
        assert result["backlog_delta"][i] == expected


def test_granularity_changes_period_count(data) -> None:
    weekly = arrival_vs_throughput(data, Filters(), granularity="week")
    monthly = arrival_vs_throughput(data, Filters(), granularity="month")
    assert len(weekly["periods"]) > len(monthly["periods"])


# --- возраст незавершённых ---------------------------------------------------


def test_aging_wip_sorted_by_age(data) -> None:
    result = aging_wip(data, Filters())
    ages = [item["age_s"] for item in result["items"]]
    assert ages == sorted(ages, reverse=True)


def test_aging_wip_excludes_completed(data) -> None:
    """В списке висящих задач не должно быть завершённых."""
    result = aging_wip(data, Filters())
    keys = [item["key"] for item in result["items"]]
    if not keys:
        return
    with data.begin() as conn:
        closed = conn.execute(
            text(
                "SELECT count(*) FROM ticket "
                "WHERE external_key = ANY(:keys) AND closed_at IS NOT NULL"
            ),
            {"keys": keys},
        ).scalar_one()
    assert closed == 0


def test_aging_wip_marks_over_p85(data) -> None:
    """Порог берётся из перцентилей своего типа задач."""
    result = aging_wip(data, Filters())
    for item in result["items"]:
        p85 = item["reference"].get("p85")
        assert item["over_p85"] == bool(p85 and item["age_s"] > p85)


def test_aging_wip_marks_over_p95(data) -> None:
    """95-й перцентиль отделяет по-настоящему застрявшие задачи."""
    result = aging_wip(data, Filters())
    reference = result["reference"]
    assert reference["p85"] <= reference["p95"]
    for item in result["items"]:
        p95 = item["reference"].get("p95")
        assert item["over_p95"] == bool(p95 and item["age_s"] > p95)
        if item["over_p95"]:
            assert item["over_p85"], "превышение p95 влечёт превышение p85"
    assert result["over_p95"] <= result["over_p85"]


def test_aging_wip_age_counts_from_work_start(data) -> None:
    """Возраст задачи больше времени в текущем статусе, если она уже переезжала."""
    result = aging_wip(data, Filters())
    moved = [i for i in result["items"] if i["age_s"] > i["status_age_s"]]
    assert moved, "в выборке должны быть задачи, сменившие статус"
    for item in result["items"]:
        assert item["age_s"] >= item["status_age_s"]


def test_aging_wip_excludes_backlog(data) -> None:
    """Бэклог — очередь, а не незавершённая работа."""
    result = aging_wip(data, Filters())
    assert all(item["phase"] != "backlog" for item in result["items"])


def test_aging_wip_percentiles_scoped_by_type(data) -> None:
    """У разных типов задач свои пороги, если выборки хватает."""
    result = aging_wip(data, Filters())
    for percentiles in result["reference_by_type"].values():
        assert percentiles["p50"] <= percentiles["p85"] <= percentiles["p95"]


# --- эффективность потока ----------------------------------------------------


def test_flow_efficiency_in_range(data) -> None:
    result = flow_efficiency(data, Filters())
    assert result["efficiency"] is None or 0 <= result["efficiency"] <= 1


def test_phase_percentiles_ordered(data) -> None:
    """Перцентили по фазам не убывают."""
    result = flow_efficiency(data, Filters())
    for phase in result["by_phase"]:
        assert phase["p50_s"] <= phase["p85_s"] <= phase["p95_s"], phase["phase"]


def test_phases_sorted_by_total_time(data) -> None:
    result = flow_efficiency(data, Filters())
    totals = [phase["total_s"] for phase in result["by_phase"]]
    assert totals == sorted(totals, reverse=True)


def test_blocked_time_counted(data) -> None:
    result = flow_efficiency(data, Filters())
    phases = {phase["phase"] for phase in result["by_phase"]}
    assert "blocked" in phases


# --- нагрузка ----------------------------------------------------------------


def test_people_load_sorted(data) -> None:
    result = people_load(data, Filters())
    owned = [person["owned_s"] for person in result["people"]]
    assert owned == sorted(owned, reverse=True)


def test_touch_not_exceeding_owned(data) -> None:
    """Активная работа — часть времени владения."""
    result = people_load(data, Filters())
    for person in result["people"]:
        assert person["touch_s"] <= person["owned_s"]


def test_people_load_respects_dates(data) -> None:
    everything = people_load(data, Filters())
    recent = people_load(data, Filters(date_from=date.today() - timedelta(days=14)))
    total_all = sum(p["owned_s"] for p in everything["people"])
    total_recent = sum(p["owned_s"] for p in recent["people"])
    assert total_recent < total_all


# --- отметки об изменениях ---------------------------------------------------


def test_interventions_empty_by_default(data) -> None:
    assert interventions(data, Filters()) == []


def test_intervention_roundtrip(data) -> None:
    with data.begin() as conn:
        team_id = conn.execute(text("SELECT id FROM team LIMIT 1")).scalar_one()
        conn.execute(
            text(
                "INSERT INTO intervention (team_id, occurred_at, title, kind) "
                "VALUES (:team, now() - interval '10 days', :title, 'process')"
            ),
            {"team": team_id, "title": "Ввели лимит незавершённой работы"},
        )
    result = interventions(data, Filters())
    assert any("лимит" in item["title"] for item in result)

    with data.begin() as conn:
        conn.execute(text("DELETE FROM intervention"))


# --- данные для прогноза -----------------------------------------------------


def test_throughput_history_excludes_current_period(data) -> None:
    """Текущий период неполон и занизил бы прогноз."""
    values = throughput_history(data, Filters(), periods=8)
    assert values
    assert all(isinstance(v, int) for v in values)


def test_throughput_history_respects_limit(data) -> None:
    assert len(throughput_history(data, Filters(), periods=4)) <= 4


def test_backlog_size_matches_summary(data) -> None:
    assert open_backlog_size(data, Filters()) == summary(data, Filters())["open_tickets"]


def test_average_wip_positive(data) -> None:
    assert average_wip(data, Filters()) > 0


def test_arrivals_by_weekday_excludes_weekends(data) -> None:
    """Задачи создаются в рабочие дни: генератор не работает по выходным."""
    arrivals = arrivals_by_weekday(data, Filters())
    assert arrivals
    assert all(0 <= weekday <= 6 for weekday in arrivals)
    workday_total = sum(sum(v) for w, v in arrivals.items() if w < 5)
    weekend_total = sum(sum(v) for w, v in arrivals.items() if w >= 5)
    assert workday_total > weekend_total


# --- блокировки --------------------------------------------------------------


def test_blockers_rate_within_bounds(data) -> None:
    result = blockers(data, Filters())
    assert 0 <= result["blocked_rate"] <= 1
    assert result["blocked_tickets"] <= result["tickets"]


def test_blockers_pareto_is_ordered_and_complete(data) -> None:
    """Бары идут по убыванию, накопленная доля доходит до единицы."""
    rows = blockers(data, Filters())["pareto"]
    assert rows, "в демо-данных должны быть блокировки"
    losses = [r["business_s"] for r in rows]
    assert losses == sorted(losses, reverse=True)
    assert rows[-1]["cumulative_share"] == pytest.approx(1.0, abs=1e-3)


def test_blockers_pareto_totals_match_sum(data) -> None:
    result = blockers(data, Filters())
    assert sum(r["business_s"] for r in result["pareto"]) == result["lost_business_s"]


def test_blockers_reasons_are_classified(data) -> None:
    """Причины должны распознаваться, а не сваливаться в unknown."""
    result = blockers(data, Filters())
    assert result["unknown_share"] < 0.5


def test_blockers_current_are_open(data) -> None:
    """В списке текущих блокировок только незакрытые интервалы."""
    result = blockers(data, Filters())
    for item in result["current"]:
        assert item["age_s"] >= 0
        assert item["reason"]


# --- переходы между статусами ------------------------------------------------


def test_transition_matrix_counts_moves(data) -> None:
    result = transition_matrix(data, Filters())
    assert result["total_moves"] > 0
    assert sum(c["moves"] for c in result["cells"]) == result["total_moves"]


def test_transition_matrix_marks_backflow(data) -> None:
    """Возврат из проверки в разработку — движение назад по доске."""
    result = transition_matrix(data, Filters())
    backflows = {(c["from"], c["to"]) for c in result["cells"] if c["is_backflow"]}
    assert ("qa", "in progress") in backflows


def test_transition_matrix_excludes_unblocking(data) -> None:
    """Выход из блокировки — возобновление работы, а не доработка."""
    result = transition_matrix(data, Filters())
    for cell in result["cells"]:
        if "blocked" in cell["from"] or "blocked" in cell["to"]:
            assert not cell["is_backflow"]


def test_transition_matrix_rate_within_bounds(data) -> None:
    result = transition_matrix(data, Filters())
    assert 0 <= result["backflow_rate"] <= 1


# --- очередь -----------------------------------------------------------------


def test_backlog_age_counts_unstarted_only(data) -> None:
    """В очереди только то, что ещё не начали."""
    result = backlog_age(data, Filters())
    assert result["size"] == sum(b["count"] for b in result["histogram"])


def test_backlog_age_histogram_buckets(data) -> None:
    result = backlog_age(data, Filters())
    labels = [b["label"] for b in result["histogram"]]
    assert labels == ["до месяца", "1–3 месяца", "3–6 месяцев", "больше полугода"]


def test_backlog_age_oldest_sorted(data) -> None:
    ages = [t["age_s"] for t in backlog_age(data, Filters())["oldest"]]
    assert ages == sorted(ages, reverse=True)


# --- классы обслуживания -----------------------------------------------------


def test_expedite_share_within_bounds(data) -> None:
    result = expedite_share(data, Filters())
    assert 0 <= result["overall_share"] <= 1
    for value in result["share"]:
        assert 0 <= value <= 1


def test_expedite_share_classes_cover_all_tickets(data) -> None:
    """Каждая задача попадает ровно в один класс."""
    result = expedite_share(data, Filters())
    total = sum(c["count"] for c in result["by_class"])
    with data.begin() as conn:
        tickets = conn.execute(text("SELECT count(*) FROM ticket")).scalar_one()
    assert total == tickets


def test_expedite_share_flags_devalued_priority(data) -> None:
    """Флаг обесценивания включается ровно выше десятой части."""
    result = expedite_share(data, Filters())
    assert result["priority_devalued"] == (result["overall_share"] > 0.10)


# --- скрытое ожидание --------------------------------------------------------


def test_hidden_queue_share_within_bounds(data) -> None:
    result = hidden_queue(data, Filters())
    assert 0 <= result["share"] <= 1
    for phase in result["by_phase"]:
        assert 0 <= phase["share"] <= 1
        assert phase["waiting_s"] <= phase["total_s"]


def test_hidden_queue_only_active_phases(data) -> None:
    """Очередь ищем внутри статусов, помеченных работой, — в очередях она и так видна."""
    result = hidden_queue(data, Filters())
    phases = {p["phase"] for p in result["by_phase"]}
    assert "backlog" not in phases
    assert "blocked" not in phases


def test_hidden_queue_detects_review_waiting(data) -> None:
    """Часть задач лежит в проверке до того, как её возьмёт ревьюер."""
    result = hidden_queue(data, Filters())
    verify = next((p for p in result["by_phase"] if p["phase"] == "verify"), None)
    assert verify is not None
    assert verify["waiting_s"] > 0


def test_hidden_queue_totals_match_phases(data) -> None:
    result = hidden_queue(data, Filters())
    assert sum(p["waiting_s"] for p in result["by_phase"]) == result["hidden_waiting_s"]
    assert sum(p["total_s"] for p in result["by_phase"]) == result["active_s"]


# --- предсказуемость ---------------------------------------------------------


def test_predictability_index_is_ratio(data) -> None:
    """Индекс — отношение хвоста к медиане, всегда не меньше единицы."""
    result = predictability(data, Filters())
    for detail in result["details"]:
        if detail["index"] is not None:
            assert detail["index"] >= 1.0
            assert detail["p98_s"] >= detail["p50_s"]


def test_predictability_hides_small_samples(data) -> None:
    """На малой выборке p98 описывает один выброс, а не систему."""
    result = predictability(data, Filters())
    for detail in result["details"]:
        if detail["count"] < result["min_sample"]:
            assert detail["index"] is None
            assert not detail["reliable"]


def test_predictability_overall_matches_details(data) -> None:
    result = predictability(data, Filters())
    known = [d["index"] for d in result["details"] if d["index"] is not None]
    if known:
        assert result["overall"] == pytest.approx(sum(known) / len(known), abs=0.01)
        assert result["latest"] == known[-1]


def test_predictability_empty_without_data(data) -> None:
    result = predictability(data, replace(Filters(), issue_types=["не существует"]))
    assert result["periods"] == []
    assert result["overall"] is None


# --- критерий завершения -----------------------------------------------------


def test_completion_criterion_changes_cycle_time(data) -> None:
    """Без ожидания релиза время цикла меньше — на величину этого ожидания."""
    terminal = summary(data, Filters())
    work_done = summary(data, replace(Filters(), completion="work_done"))
    assert work_done["p50_cycle_s"] <= terminal["p50_cycle_s"]


def test_completion_criterion_keeps_sample_size(data) -> None:
    """Меняется граница, а не состав выборки: незавершённые остаются вне её."""
    terminal = cycle_time_distribution(data, Filters())
    work_done = cycle_time_distribution(data, replace(Filters(), completion="work_done"))
    assert terminal["count"] == work_done["count"]


def test_completion_criterion_never_negative(data) -> None:
    work_done = cycle_time_distribution(data, replace(Filters(), completion="work_done"))
    for bucket in work_done["histogram"]:
        assert bucket["from"] >= 0
