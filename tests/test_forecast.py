"""Тесты вероятностных прогнозов."""

from __future__ import annotations

from datetime import date

from flowlens.core.forecast import (
    ThroughputSample,
    analyse_seasonality,
    forecast_how_long,
    forecast_how_many,
    littles_law_capacity,
    required_wip,
)


def sample(values: list[int]) -> ThroughputSample:
    return ThroughputSample(values=values, period_days=7)


# --- пригодность выборки -----------------------------------------------------


def test_short_history_rejected() -> None:
    """Двух недель мало, чтобы судить о темпе."""
    result = forecast_how_long(50, sample([10, 12]), seed=1)
    assert result.warning is not None
    assert not result.percentiles


def test_zero_throughput_rejected() -> None:
    result = forecast_how_long(50, sample([0, 0, 0, 0]), seed=1)
    assert result.warning is not None


def test_usable_sample_accepted() -> None:
    result = forecast_how_long(50, sample([10, 12, 9, 11]), seed=1)
    assert result.warning is None
    assert result.percentiles


# --- сколько времени займёт --------------------------------------------------


def test_how_long_percentiles_ordered() -> None:
    """Чем выше требуемая уверенность, тем больше срок."""
    result = forecast_how_long(100, sample([10, 12, 8, 11, 9]), seed=42)
    p = result.percentiles
    assert p[50] <= p[70] <= p[85] <= p[95]


def test_how_long_matches_average_pace() -> None:
    """Медиана близка к делению объёма на средний темп."""
    result = forecast_how_long(100, sample([10, 10, 10, 10]), seed=7)
    assert result.percentiles[50] == 10  # 100 задач по 10 в неделю


def test_variance_widens_the_range() -> None:
    """Нестабильный темп даёт более широкий разброс прогноза."""
    stable = forecast_how_long(100, sample([10, 10, 10, 10, 10]), seed=3)
    volatile = forecast_how_long(100, sample([2, 18, 3, 17, 10]), seed=3)
    stable_spread = stable.percentiles[95] - stable.percentiles[50]
    volatile_spread = volatile.percentiles[95] - volatile.percentiles[50]
    assert volatile_spread > stable_spread


def test_empty_backlog_needs_no_time() -> None:
    result = forecast_how_long(0, sample([10, 12, 9]), seed=1)
    assert all(v == 0 for v in result.percentiles.values())


def test_dates_computed_from_start() -> None:
    result = forecast_how_long(
        100, sample([10, 10, 10, 10]), seed=7, start=date(2026, 9, 1)
    )
    assert result.dates[50] == "2026-11-10"  # 10 недель от 1 сентября


def test_slow_pace_produces_warning() -> None:
    """При темпе, близком к нулю, прогноз честно предупреждает."""
    result = forecast_how_long(10_000, sample([1, 0, 1, 0, 1]), seed=5, trials=500)
    assert result.warning is not None


def test_deterministic_with_seed() -> None:
    a = forecast_how_long(100, sample([10, 12, 8, 11]), seed=99)
    b = forecast_how_long(100, sample([10, 12, 8, 11]), seed=99)
    assert a.percentiles == b.percentiles


# --- сколько успеем ----------------------------------------------------------


def test_how_many_percentiles_are_inverted() -> None:
    """Высокая уверенность означает меньшее обещание.

    «Успеем 85% гарантированно» — это пессимистичная оценка объёма,
    в отличие от прогноза срока, где 85% даёт больший срок.
    """
    result = forecast_how_many(10, sample([10, 12, 8, 11, 9]), seed=42)
    p = result.percentiles
    assert p[50] >= p[70] >= p[85] >= p[95]


def test_how_many_matches_average() -> None:
    result = forecast_how_many(10, sample([10, 10, 10, 10]), seed=7)
    assert result.percentiles[50] == 100


def test_zero_periods_yields_nothing() -> None:
    result = forecast_how_many(0, sample([10, 12, 9]), seed=1)
    assert all(v == 0 for v in result.percentiles.values())


def test_how_many_needs_history() -> None:
    result = forecast_how_many(10, sample([5]), seed=1)
    assert result.warning is not None


# --- закон Литтла ------------------------------------------------------------


def test_littles_law_capacity() -> None:
    """20 задач в работе при цикле 4 дня — это 5 задач в день."""
    assert littles_law_capacity(20, 4) == 5.0


def test_littles_law_rejects_zero_cycle() -> None:
    assert littles_law_capacity(20, 0) is None


def test_required_wip() -> None:
    """Чтобы делать 5 задач в день при цикле 4 дня, нужно держать 20 в работе."""
    assert required_wip(5, 4) == 20.0


def test_littles_law_is_reversible() -> None:
    wip, cycle = 20.0, 4.0
    throughput = littles_law_capacity(wip, cycle)
    assert throughput is not None
    assert required_wip(throughput, cycle) == wip


# --- сезонность --------------------------------------------------------------


def test_seasonality_finds_extremes() -> None:
    profile = analyse_seasonality({
        0: [20, 22, 21],  # понедельник
        1: [15, 16, 14],
        2: [12, 13, 11],
        3: [10, 11, 9],   # четверг — самый тихий
        4: [14, 15, 13],
    })
    assert profile.busiest == 0
    assert profile.quietest == 3
    assert profile.busiest_name() == "понедельник"
    assert profile.ratio == 2.1


def test_seasonality_ignores_weekends() -> None:
    """Выходные не участвуют: там просто не работают."""
    profile = analyse_seasonality({
        0: [20], 1: [18], 2: [16], 3: [15], 4: [14],
        5: [1], 6: [0],
    })
    assert profile.quietest == 4  # пятница, а не воскресенье


def test_seasonality_needs_enough_days() -> None:
    profile = analyse_seasonality({0: [20], 1: [18]})
    assert profile.busiest is None


def test_seasonality_handles_empty_input() -> None:
    profile = analyse_seasonality({})
    assert profile.busiest is None
    assert profile.ratio is None
