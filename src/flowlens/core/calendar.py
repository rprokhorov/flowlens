"""Рабочий календарь: перевод астрономического времени в рабочее.

Все расчёты ведутся в таймзоне календаря. Рабочие окна задаются по дням недели,
праздники исключаются, перенесённые рабочие дни добавляются.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

WEEKDAY_KEYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# Рабочие окна по умолчанию: пн-пт 10:00-19:00
DEFAULT_WORKWEEK: dict[str, list[tuple[str, str]]] = {
    "mon": [("10:00", "19:00")],
    "tue": [("10:00", "19:00")],
    "wed": [("10:00", "19:00")],
    "thu": [("10:00", "19:00")],
    "fri": [("10:00", "19:00")],
    "sat": [],
    "sun": [],
}


def _parse_hhmm(value: str) -> time:
    hours, minutes = value.split(":")
    return time(int(hours), int(minutes))


@dataclass(frozen=True)
class WorkCalendar:
    """Календарь рабочего времени команды."""

    name: str = "default"
    tz: str = "Europe/Moscow"
    workweek: dict[str, list[tuple[str, str]]] = field(default_factory=lambda: DEFAULT_WORKWEEK)
    holidays: frozenset[date] = frozenset()
    extra_workdays: frozenset[date] = frozenset()

    def __post_init__(self) -> None:
        for key, windows in self.workweek.items():
            if key not in WEEKDAY_KEYS:
                raise ValueError(f"unknown weekday key: {key}")
            for start, end in windows:
                if _parse_hhmm(start) >= _parse_hhmm(end):
                    raise ValueError(f"invalid window {start}-{end} for {key}")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    def _windows_for(self, day: date) -> list[tuple[time, time]]:
        """Рабочие окна конкретного дня с учётом праздников и переносов."""
        if day in self.holidays:
            return []
        key = WEEKDAY_KEYS[day.weekday()]
        raw = self.workweek.get(key, [])
        if not raw and day in self.extra_workdays:
            # перенесённая рабочая суббота — берём режим ближайшего рабочего дня
            for fallback in ("mon", "tue", "wed", "thu", "fri"):
                if self.workweek.get(fallback):
                    raw = self.workweek[fallback]
                    break
        return [(_parse_hhmm(s), _parse_hhmm(e)) for s, e in raw]

    def day_intervals(self, day: date) -> list[tuple[datetime, datetime]]:
        """Абсолютные границы рабочих окон дня.

        Используется fold=0: при осеннем переводе часов неоднозначное локальное
        время трактуется как первое (летнее) вхождение.
        """
        out: list[tuple[datetime, datetime]] = []
        for start, end in self._windows_for(day):
            begin = datetime.combine(day, start, tzinfo=self.zone)
            finish = datetime.combine(day, end, tzinfo=self.zone)
            if finish > begin:
                out.append((begin, finish))
        return out

    def seconds_in_day(self, day: date) -> int:
        return sum(int((e - b).total_seconds()) for b, e in self.day_intervals(day))

    def business_seconds_between(self, start: datetime, end: datetime) -> int:
        """Рабочие секунды между двумя моментами. Отрицательный интервал → 0."""
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("naive datetimes are not supported")
        if end <= start:
            return 0

        local_start = start.astimezone(self.zone)
        local_end = end.astimezone(self.zone)

        total = 0
        day = local_start.date()
        last_day = local_end.date()
        while day <= last_day:
            for begin, finish in self.day_intervals(day):
                lo = max(begin, local_start)
                hi = min(finish, local_end)
                if hi > lo:
                    total += int((hi - lo).total_seconds())
            day += timedelta(days=1)
        return total

    def add_business_seconds(self, start: datetime, seconds: int) -> datetime:
        """Момент, наступающий через N рабочих секунд после start."""
        if seconds < 0:
            raise ValueError("seconds must be non-negative")
        if start.tzinfo is None:
            raise ValueError("naive datetimes are not supported")

        remaining = seconds
        cursor = start.astimezone(self.zone)
        day = cursor.date()

        # если старт в нерабочее время, отсчёт начинается со следующего окна
        guard = 0
        while guard < 3660:  # предохранитель ~10 лет
            for begin, finish in self.day_intervals(day):
                if finish <= cursor:
                    continue
                lo = max(begin, cursor)
                available = int((finish - lo).total_seconds())
                if remaining <= available:
                    return (lo + timedelta(seconds=remaining)).astimezone(start.tzinfo)
                remaining -= available
            day += timedelta(days=1)
            cursor = datetime.combine(day, time(0, 0), tzinfo=self.zone)
            guard += 1
        raise RuntimeError("calendar has no working time in the next 10 years")

    def is_working_moment(self, moment: datetime) -> bool:
        local = moment.astimezone(self.zone)
        return any(b <= local < e for b, e in self.day_intervals(local.date()))


@lru_cache(maxsize=64)
def calendar_from_row(
    name: str,
    tz: str,
    workweek_json: str,
    holidays: tuple[date, ...] = (),
    extra_workdays: tuple[date, ...] = (),
) -> WorkCalendar:
    """Собрать календарь из строки таблицы `calendar` (кэшируется)."""
    import json

    raw = json.loads(workweek_json)
    workweek = {k: [tuple(w) for w in v] for k, v in raw.items()}
    return WorkCalendar(
        name=name,
        tz=tz,
        workweek=workweek,  # type: ignore[arg-type]
        holidays=frozenset(holidays),
        extra_workdays=frozenset(extra_workdays),
    )


def business_seconds_by_day(
    cal: WorkCalendar, start: datetime, end: datetime
) -> dict[date, int]:
    """Разложение рабочих секунд по календарным дням.

    Нужно для person_workload_daily: интервал владения режется по дням.
    """
    if end <= start:
        return {}
    local_start = start.astimezone(cal.zone)
    local_end = end.astimezone(cal.zone)

    out: dict[date, int] = {}
    day = local_start.date()
    while day <= local_end.date():
        acc = 0
        for begin, finish in cal.day_intervals(day):
            lo = max(begin, local_start)
            hi = min(finish, local_end)
            if hi > lo:
                acc += int((hi - lo).total_seconds())
        if acc:
            out[day] = acc
        day += timedelta(days=1)
    return out


__all__ = [
    "DEFAULT_WORKWEEK",
    "WorkCalendar",
    "business_seconds_by_day",
    "calendar_from_row",
]
