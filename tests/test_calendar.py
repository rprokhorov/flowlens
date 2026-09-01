"""Тесты рабочего календаря.

Базовый календарь: пн-пт 10:00-19:00 (9 часов = 32400 секунд), Europe/Moscow.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from tests.conftest import dt

from flowlens.core.calendar import (
    WorkCalendar,
    business_seconds_by_day,
)

MSK = ZoneInfo("Europe/Moscow")
DAY = 9 * 3600  # рабочих секунд в дне


@pytest.fixture
def split_cal() -> WorkCalendar:
    """Календарь с обеденным перерывом: 10-13 и 14-19 (8 часов)."""
    week = {
        "mon": [("10:00", "13:00"), ("14:00", "19:00")],
        "tue": [("10:00", "13:00"), ("14:00", "19:00")],
        "wed": [("10:00", "13:00"), ("14:00", "19:00")],
        "thu": [("10:00", "13:00"), ("14:00", "19:00")],
        "fri": [("10:00", "13:00"), ("14:00", "19:00")],
        "sat": [],
        "sun": [],
    }
    return WorkCalendar(name="split", tz="Europe/Moscow", workweek=week)


# --- базовые случаи ----------------------------------------------------------


def test_within_single_workday(cal: WorkCalendar) -> None:
    # среда, 11:00 -> 15:00
    assert cal.business_seconds_between(dt(2026, 3, 4, 11), dt(2026, 3, 4, 15)) == 4 * 3600


def test_full_workday(cal: WorkCalendar) -> None:
    assert cal.business_seconds_between(dt(2026, 3, 4, 10), dt(2026, 3, 4, 19)) == DAY


def test_zero_length_interval(cal: WorkCalendar) -> None:
    assert cal.business_seconds_between(dt(2026, 3, 4, 12), dt(2026, 3, 4, 12)) == 0


def test_reversed_interval_is_zero(cal: WorkCalendar) -> None:
    assert cal.business_seconds_between(dt(2026, 3, 4, 15), dt(2026, 3, 4, 11)) == 0


def test_overnight_spans_only_working_hours(cal: WorkCalendar) -> None:
    # среда 18:00 -> четверг 11:00 = 1ч (ср) + 1ч (чт)
    assert cal.business_seconds_between(dt(2026, 3, 4, 18), dt(2026, 3, 5, 11)) == 2 * 3600


def test_entirely_outside_working_hours(cal: WorkCalendar) -> None:
    # ночь со среды на четверг
    assert cal.business_seconds_between(dt(2026, 3, 4, 20), dt(2026, 3, 5, 7)) == 0


def test_starts_and_ends_outside_hours(cal: WorkCalendar) -> None:
    # среда 06:00 -> среда 23:00 = полный рабочий день
    assert cal.business_seconds_between(dt(2026, 3, 4, 6), dt(2026, 3, 4, 23)) == DAY


# --- выходные и праздники ----------------------------------------------------


def test_weekend_is_skipped(cal: WorkCalendar) -> None:
    # пятница 18:00 -> понедельник 11:00 = 1ч (пт) + 1ч (пн)
    assert cal.business_seconds_between(dt(2026, 3, 6, 18), dt(2026, 3, 9, 11)) == 2 * 3600


def test_whole_weekend_is_zero(cal: WorkCalendar) -> None:
    # суббота -> воскресенье
    assert cal.business_seconds_between(dt(2026, 3, 7, 0), dt(2026, 3, 8, 23)) == 0


def test_holiday_excluded() -> None:
    cal = WorkCalendar(name="h", tz="Europe/Moscow", holidays=frozenset({date(2026, 3, 5)}))
    # среда 18:00 -> пятница 11:00; четверг 5-го — праздник
    assert cal.business_seconds_between(dt(2026, 3, 4, 18), dt(2026, 3, 6, 11)) == 2 * 3600


def test_extra_workday_counted() -> None:
    cal = WorkCalendar(
        name="e", tz="Europe/Moscow", extra_workdays=frozenset({date(2026, 3, 7)})
    )
    # суббота 7-го объявлена рабочей
    assert cal.business_seconds_between(dt(2026, 3, 7, 10), dt(2026, 3, 7, 19)) == DAY


def test_multi_week_span(cal: WorkCalendar) -> None:
    # понедельник 10:00 -> понедельник через 2 недели 10:00 = 10 рабочих дней
    assert cal.business_seconds_between(dt(2026, 3, 2, 10), dt(2026, 3, 16, 10)) == 10 * DAY


# --- перерывы внутри дня -----------------------------------------------------


def test_lunch_break_excluded(split_cal: WorkCalendar) -> None:
    assert split_cal.business_seconds_between(dt(2026, 3, 4, 10), dt(2026, 3, 4, 19)) == 8 * 3600


def test_interval_inside_lunch_is_zero(split_cal: WorkCalendar) -> None:
    assert split_cal.business_seconds_between(dt(2026, 3, 4, 13, 10), dt(2026, 3, 4, 13, 50)) == 0


# --- переход на летнее время -------------------------------------------------


def test_dst_transition_europe_berlin() -> None:
    """29 марта 2026 в Берлине часы переводят вперёд: 02:00 -> 03:00.

    Рабочие окна заданы в локальном времени, поэтому длина рабочего дня
    не меняется — переход происходит ночью.
    """
    cal = WorkCalendar(name="berlin", tz="Europe/Berlin")
    berlin = ZoneInfo("Europe/Berlin")
    start = datetime(2026, 3, 27, 10, tzinfo=berlin)  # пятница
    end = datetime(2026, 3, 30, 19, tzinfo=berlin)  # понедельник
    # пятница + понедельник (выходные пропущены)
    assert cal.business_seconds_between(start, end) == 2 * DAY


def test_utc_input_is_converted(cal: WorkCalendar) -> None:
    """Вход в UTC приводится к таймзоне календаря."""
    utc = ZoneInfo("UTC")
    # 08:00 UTC = 11:00 MSK, 12:00 UTC = 15:00 MSK
    start = datetime(2026, 3, 4, 8, tzinfo=utc)
    end = datetime(2026, 3, 4, 12, tzinfo=utc)
    assert cal.business_seconds_between(start, end) == 4 * 3600


def test_naive_datetime_rejected(cal: WorkCalendar) -> None:
    with pytest.raises(ValueError, match="naive"):
        cal.business_seconds_between(datetime(2026, 3, 4, 10), datetime(2026, 3, 4, 12))


# --- add_business_seconds ----------------------------------------------------


def test_add_within_day(cal: WorkCalendar) -> None:
    assert cal.add_business_seconds(dt(2026, 3, 4, 11), 2 * 3600) == dt(2026, 3, 4, 13)


def test_add_rolls_to_next_day(cal: WorkCalendar) -> None:
    # среда 18:00 + 2ч = четверг 11:00
    assert cal.add_business_seconds(dt(2026, 3, 4, 18), 2 * 3600) == dt(2026, 3, 5, 11)


def test_add_from_non_working_moment(cal: WorkCalendar) -> None:
    # воскресенье + 1ч = понедельник 11:00
    assert cal.add_business_seconds(dt(2026, 3, 8, 12), 3600) == dt(2026, 3, 9, 11)


def test_add_zero_from_working_moment(cal: WorkCalendar) -> None:
    assert cal.add_business_seconds(dt(2026, 3, 4, 12), 0) == dt(2026, 3, 4, 12)


def test_add_negative_rejected(cal: WorkCalendar) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        cal.add_business_seconds(dt(2026, 3, 4, 12), -1)


def test_add_and_between_are_inverse(cal: WorkCalendar) -> None:
    start = dt(2026, 3, 3, 14)
    for seconds in (0, 600, 3600, DAY, 3 * DAY + 1234):
        end = cal.add_business_seconds(start, seconds)
        assert cal.business_seconds_between(start, end) == seconds


# --- разложение по дням ------------------------------------------------------


def test_by_day_splits_correctly(cal: WorkCalendar) -> None:
    # среда 17:00 -> пятница 11:00
    out = business_seconds_by_day(cal, dt(2026, 3, 4, 17), dt(2026, 3, 6, 11))
    assert out == {
        date(2026, 3, 4): 2 * 3600,
        date(2026, 3, 5): DAY,
        date(2026, 3, 6): 3600,
    }


def test_by_day_skips_weekend(cal: WorkCalendar) -> None:
    out = business_seconds_by_day(cal, dt(2026, 3, 6, 18), dt(2026, 3, 9, 11))
    assert date(2026, 3, 7) not in out
    assert date(2026, 3, 8) not in out
    assert sum(out.values()) == 2 * 3600


def test_by_day_sum_matches_total(cal: WorkCalendar) -> None:
    start, end = dt(2026, 3, 2, 11), dt(2026, 3, 20, 16)
    out = business_seconds_by_day(cal, start, end)
    assert sum(out.values()) == cal.business_seconds_between(start, end)


# --- валидация конфигурации --------------------------------------------------


def test_invalid_window_rejected() -> None:
    with pytest.raises(ValueError, match="invalid window"):
        WorkCalendar(name="bad", workweek={**{k: [] for k in ("tue","wed","thu","fri","sat","sun")},
                                            "mon": [("19:00", "10:00")]})


def test_unknown_weekday_rejected() -> None:
    with pytest.raises(ValueError, match="unknown weekday"):
        WorkCalendar(name="bad", workweek={"funday": [("10:00", "19:00")]})


def test_is_working_moment(cal: WorkCalendar) -> None:
    assert cal.is_working_moment(dt(2026, 3, 4, 12))
    assert not cal.is_working_moment(dt(2026, 3, 4, 22))
    assert not cal.is_working_moment(dt(2026, 3, 8, 12))  # воскресенье


def test_seconds_in_day(cal: WorkCalendar) -> None:
    assert cal.seconds_in_day(date(2026, 3, 4)) == DAY
    assert cal.seconds_in_day(date(2026, 3, 7)) == 0
