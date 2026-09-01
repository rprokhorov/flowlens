"""Вероятностные прогнозы методом Монте-Карло.

Прогноз строится на историческом темпе закрытия задач, а не на оценках
трудоёмкости: команда уже показала, сколько она делает за неделю, и это
надёжнее любых оценок.

Результат — не дата, а распределение: «с вероятностью 85% закончим к N».
Одна дата всегда обманывает, потому что скрывает разброс.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

DEFAULT_TRIALS = 10_000
DEFAULT_PERCENTILES = (50, 70, 85, 95)


@dataclass
class ThroughputSample:
    """Историческая пропускная способность по периодам."""

    values: list[int]
    period_days: int = 7

    def is_usable(self) -> bool:
        """Прогноз требует хотя бы несколько периодов с ненулевым темпом."""
        return len(self.values) >= 3 and sum(self.values) > 0

    def mean(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0.0


@dataclass
class HowLongForecast:
    """Сколько времени займёт N задач."""

    items: int
    trials: int
    percentiles: dict[int, int] = field(default_factory=dict)  # перцентиль → периодов
    dates: dict[int, str] = field(default_factory=dict)
    period_days: int = 7
    warning: str | None = None

    def periods_at(self, percentile: int) -> int | None:
        return self.percentiles.get(percentile)


@dataclass
class HowManyForecast:
    """Сколько задач успеем за N периодов."""

    periods: int
    trials: int
    percentiles: dict[int, int] = field(default_factory=dict)  # перцентиль → задач
    period_days: int = 7
    warning: str | None = None


def forecast_how_long(
    backlog_items: int,
    sample: ThroughputSample,
    *,
    trials: int = DEFAULT_TRIALS,
    percentiles: tuple[int, ...] = DEFAULT_PERCENTILES,
    start: date | None = None,
    seed: int | None = None,
) -> HowLongForecast:
    """Сколько периодов потребуется, чтобы закрыть backlog_items задач.

    Каждый прогон случайно выбирает недели из истории (с возвращением)
    и считает, сколько их нужно набрать до нужного числа задач.
    """
    result = HowLongForecast(
        items=backlog_items, trials=trials, period_days=sample.period_days
    )
    if backlog_items <= 0:
        result.percentiles = dict.fromkeys(percentiles, 0)
        return result
    if not sample.is_usable():
        result.warning = (
            "Недостаточно истории для прогноза: нужно хотя бы три периода "
            "с завершёнными задачами."
        )
        return result

    rng = random.Random(seed)
    values = sample.values
    # предохранитель: при почти нулевом темпе прогон может не сойтись
    max_periods = max(200, int(backlog_items / max(sample.mean(), 0.1) * 5))

    outcomes: list[int] = []
    for _ in range(trials):
        done = 0
        periods = 0
        while done < backlog_items and periods < max_periods:
            done += rng.choice(values)
            periods += 1
        outcomes.append(periods)

    outcomes.sort()
    result.percentiles = {p: _percentile(outcomes, p) for p in percentiles}

    if start is not None:
        result.dates = {
            p: (start + timedelta(days=periods * sample.period_days)).isoformat()
            for p, periods in result.percentiles.items()
        }

    hit_limit = sum(1 for value in outcomes if value >= max_periods)
    if hit_limit > trials * 0.01:
        result.warning = (
            "При текущем темпе очередь может не закрыться в обозримом сроке: "
            "поступление сопоставимо с закрытием."
        )
    elif result.percentiles.get(85, 0) * sample.period_days > 730:
        # прогноз дальше двух лет практически бесполезен: за это время
        # изменится и состав команды, и сам поток задач
        years = result.percentiles[85] * sample.period_days / 365
        result.warning = (
            f"Прогноз выходит за пределы разумного горизонта (около {years:.0f} лет). "
            "Либо объём слишком велик для текущего темпа, либо стоит "
            "разбить задачу на этапы."
        )
    return result


def forecast_how_many(
    periods: int,
    sample: ThroughputSample,
    *,
    trials: int = DEFAULT_TRIALS,
    percentiles: tuple[int, ...] = DEFAULT_PERCENTILES,
    seed: int | None = None,
) -> HowManyForecast:
    """Сколько задач будет закрыто за указанное число периодов."""
    result = HowManyForecast(periods=periods, trials=trials, period_days=sample.period_days)
    if periods <= 0:
        result.percentiles = dict.fromkeys(percentiles, 0)
        return result
    if not sample.is_usable():
        result.warning = "Недостаточно истории для прогноза."
        return result

    rng = random.Random(seed)
    values = sample.values

    outcomes = [sum(rng.choice(values) for _ in range(periods)) for _ in range(trials)]
    outcomes.sort()
    # для «сколько успеем» перцентиль переворачивается: 85% уверенности
    # означает пессимистичную оценку, то есть 15-й перцентиль выборки
    result.percentiles = {p: _percentile(outcomes, 100 - p) for p in percentiles}
    return result


def _percentile(sorted_values: list[int], p: float) -> int:
    """Ближайший ранг: значение из выборки, а не интерполяция."""
    from math import ceil

    if not sorted_values:
        return 0
    rank = min(len(sorted_values), max(1, ceil(p / 100 * len(sorted_values))))
    return sorted_values[rank - 1]


def littles_law_capacity(
    average_wip: float, average_cycle_time_days: float
) -> float | None:
    """Пропускная способность по закону Литтла: WIP = темп × время цикла.

    ВАЖНО: закон верен только для устойчивого потока, где WIP и время цикла
    измерены на одном и том же множестве задач. Если WIP включает бэклог,
    а время цикла посчитано лишь по завершённым, результат будет завышен
    в разы — для оценки реального темпа используйте `implied_cycle_time`
    с фактическим throughput.
    """
    if average_cycle_time_days <= 0:
        return None
    return average_wip / average_cycle_time_days


def implied_cycle_time(
    average_wip: float, throughput_per_day: float
) -> float | None:
    """Какое время цикла следует из наблюдаемых WIP и темпа закрытия.

    Обратная задача закона Литтла и практически более полезная: фактический
    темп известен точно, а расхождение с измеренным временем цикла
    показывает, что часть незавершённой работы на деле стоит.
    """
    if throughput_per_day <= 0:
        return None
    return average_wip / throughput_per_day


def wip_health(
    average_wip: float,
    throughput_per_day: float,
    measured_cycle_time_days: float,
) -> dict[str, float | str | None]:
    """Сопоставить фактическое время цикла с тем, что следует из WIP.

    Если из объёма незавершённой работы следует срок в разы больший
    измеренного, значит значительная её часть не движется — она числится
    в работе, но фактически стоит.
    """
    implied = implied_cycle_time(average_wip, throughput_per_day)
    if implied is None or measured_cycle_time_days <= 0:
        return {"implied_days": None, "measured_days": None, "verdict": None}

    ratio = implied / measured_cycle_time_days
    if ratio < 1.5:
        verdict = "Объём незавершённой работы соответствует темпу закрытия."
    elif ratio < 3:
        verdict = (
            "Незавершённой работы больше, чем команда успевает пропускать: "
            "часть задач стоит без движения."
        )
    else:
        verdict = (
            "Значительная часть незавершённых задач не движется — они числятся "
            "в работе, но фактически стоят в очереди."
        )
    return {
        "implied_days": round(implied, 1),
        "measured_days": round(measured_cycle_time_days, 1),
        "ratio": round(ratio, 1),
        "verdict": verdict,
    }


def required_wip(target_throughput_per_day: float, cycle_time_days: float) -> float | None:
    """Сколько задач нужно держать в работе для заданного темпа."""
    if cycle_time_days <= 0:
        return None
    return target_throughput_per_day * cycle_time_days


@dataclass
class SeasonalityProfile:
    """Неравномерность поступления по дням недели."""

    by_weekday: dict[int, float] = field(default_factory=dict)  # 0=пн, среднее в день
    busiest: int | None = None
    quietest: int | None = None
    ratio: float | None = None

    WEEKDAY_NAMES = (
        "понедельник", "вторник", "среда", "четверг",
        "пятница", "суббота", "воскресенье",
    )

    def busiest_name(self) -> str | None:
        return self.WEEKDAY_NAMES[self.busiest] if self.busiest is not None else None

    def quietest_name(self) -> str | None:
        return self.WEEKDAY_NAMES[self.quietest] if self.quietest is not None else None


def analyse_seasonality(counts_by_weekday: dict[int, list[int]]) -> SeasonalityProfile:
    """Найти дни недели с наибольшим и наименьшим потоком.

    На вход: {день недели: [сколько задач пришло в каждый такой день]}.
    """
    profile = SeasonalityProfile()
    averages = {
        weekday: sum(values) / len(values)
        for weekday, values in counts_by_weekday.items()
        if values
    }
    workdays = {w: v for w, v in averages.items() if w < 5}
    if len(workdays) < 3:
        return profile

    profile.by_weekday = averages
    profile.busiest = max(workdays, key=lambda w: workdays[w])
    profile.quietest = min(workdays, key=lambda w: workdays[w])
    quietest_value = workdays[profile.quietest]
    if quietest_value > 0:
        profile.ratio = round(workdays[profile.busiest] / quietest_value, 2)
    return profile


__all__ = [
    "HowLongForecast",
    "HowManyForecast",
    "SeasonalityProfile",
    "ThroughputSample",
    "analyse_seasonality",
    "forecast_how_long",
    "forecast_how_many",
    "implied_cycle_time",
    "littles_law_capacity",
    "required_wip",
    "wip_health",
]
