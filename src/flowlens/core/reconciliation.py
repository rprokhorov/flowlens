"""Согласование противоречивых сигналов о времени работы.

Два источника говорят о том, когда работа началась и закончилась:

- changelog (system): точный timestamp, но неверная семантика — человек
  двигает задачу на доске тогда, когда вспомнит;
- заявленные даты (declared): верная семантика, но заполняются не всегда
  и могут противоречить сами себе.

Ядро хранит оба и выбирает effective по политике. Политика версионируется:
при её изменении метрики пересчитываются без обращения к источнику.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from enum import StrEnum

from flowlens.core.calendar import WorkCalendar

POLICY_VERSION = "1.0"


class Anomaly(StrEnum):
    """Признаки проблем с данными. Основа отчёта о качестве."""

    DECLARED_MISSING = "declared_missing"
    DECLARED_BEFORE_CREATED = "declared_before_created"
    DECLARED_AFTER_NOW = "declared_after_now"
    END_BEFORE_START = "end_before_start"
    DECLARED_CONTRADICTS_CHANGELOG = "declared_contradicts_changelog"
    DECLARED_ONLY_PARTIAL = "declared_only_partial"
    BULK_MOVE = "bulk_move"
    ZERO_DURATION_LIFECYCLE = "zero_duration_lifecycle"
    STALE_TRANSITION = "stale_transition"
    NO_SYSTEM_SIGNAL = "no_system_signal"
    COMPLETED_WITHOUT_WORK = "completed_without_work"


class Source(StrEnum):
    """Откуда взято итоговое значение."""

    SYSTEM = "system"
    DECLARED = "declared"
    RECONCILED = "reconciled"
    INFERRED = "inferred"
    NONE = "none"


class Confidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True)
class ReconciliationPolicy:
    """Настройки согласования. Переопределяются на уровне команды."""

    version: str = POLICY_VERSION
    prefer: str = "declared"  # declared | system | system_only
    # порог расхождения, после которого заявленная дата считается подозрительной
    discrepancy_threshold_business_days: float = 3.0
    # что делать при расхождении больше порога
    on_conflict: str = "use_declared_flag_anomaly"  # или use_system_flag_anomaly
    # дневная точность: начало работы — на начало дня, конец — на конец
    day_precision_start_at_day_start: bool = True
    # сколько переходов за сколько секунд считать разгребкой доски
    bulk_move_max_seconds: int = 120
    bulk_move_min_transitions: int = 3
    # доверие к changelog при отсутствии заявленных дат
    confidence_without_declared: Confidence = Confidence.MEDIUM

    def discrepancy_threshold_seconds(self, workday_seconds: int = 9 * 3600) -> int:
        return int(self.discrepancy_threshold_business_days * workday_seconds)


@dataclass
class BoundaryFact:
    """Результат согласования одной границы (начало или конец работы)."""

    boundary: str  # work_start | work_end
    system_at: datetime | None = None
    declared_at: datetime | None = None
    effective_at: datetime | None = None
    source: Source = Source.NONE
    confidence: Confidence = Confidence.MEDIUM
    discrepancy_business_s: int | None = None
    anomalies: set[Anomaly] = field(default_factory=set)
    policy_version: str = POLICY_VERSION


@dataclass
class TimelineSignals:
    """Сырые сигналы о тикете, на входе согласования."""

    created_at: datetime
    system_start: datetime | None  # первый переход в активную работу
    system_end: datetime | None  # переход в терминальный статус
    declared_start: datetime | None
    declared_end: datetime | None
    declared_start_precision: str = "minute"
    declared_end_precision: str = "minute"
    is_completed: bool = False
    transition_times: list[datetime] = field(default_factory=list)
    max_interval_business_s: int = 0


def reconcile(
    signals: TimelineSignals,
    policy: ReconciliationPolicy,
    calendar: WorkCalendar,
    *,
    now: datetime | None = None,
) -> tuple[BoundaryFact, BoundaryFact]:
    """Согласовать начало и конец работы.

    Возвращает пару фактов; аномалии, относящиеся ко всему тикету
    (bulk move, мгновенный жизненный цикл), проставляются обеим границам.
    """
    moment = now or datetime.now(calendar.zone)

    start = _reconcile_boundary(
        boundary="work_start",
        system_at=signals.system_start,
        declared_raw=signals.declared_start,
        precision=signals.declared_start_precision,
        signals=signals,
        policy=policy,
        calendar=calendar,
        now=moment,
    )
    end = _reconcile_boundary(
        boundary="work_end",
        system_at=signals.system_end,
        declared_raw=signals.declared_end,
        precision=signals.declared_end_precision,
        signals=signals,
        policy=policy,
        calendar=calendar,
        now=moment,
    )

    _check_order(start, end, signals, policy, calendar)
    _check_partial(start, end, signals)

    ticket_anomalies = _ticket_level_anomalies(signals, policy, calendar)
    start.anomalies |= ticket_anomalies
    end.anomalies |= ticket_anomalies
    if Anomaly.BULK_MOVE in ticket_anomalies:
        for fact in (start, end):
            if fact.source == Source.SYSTEM:
                fact.confidence = Confidence.LOW

    return start, end


def _reconcile_boundary(
    *,
    boundary: str,
    system_at: datetime | None,
    declared_raw: datetime | None,
    precision: str,
    signals: TimelineSignals,
    policy: ReconciliationPolicy,
    calendar: WorkCalendar,
    now: datetime,
) -> BoundaryFact:
    fact = BoundaryFact(
        boundary=boundary,
        system_at=system_at,
        policy_version=policy.version,
    )

    declared = _normalize_precision(declared_raw, precision, boundary, policy, calendar)
    if (
        declared is not None
        and precision == "day"
        and boundary == "work_start"
        and not _is_before_creation(declared, signals.created_at, precision, calendar)
    ):
        # день указан верно, но рабочее утро раньше создания тикета
        declared = _clamp_to_creation(declared, signals.created_at)
    fact.declared_at = declared

    if declared is not None and system_at is not None:
        fact.discrepancy_business_s = calendar.business_seconds_between(
            min(declared, system_at), max(declared, system_at)
        )

    # заявленная дата отсутствует
    if declared is None:
        if system_at is None:
            fact.source = Source.NONE
            fact.confidence = Confidence.LOW
            fact.anomalies.add(Anomaly.NO_SYSTEM_SIGNAL)
            if signals.is_completed and boundary == "work_start":
                fact.anomalies.add(Anomaly.COMPLETED_WITHOUT_WORK)
            return fact
        fact.effective_at = system_at
        fact.source = Source.SYSTEM
        fact.confidence = policy.confidence_without_declared
        fact.anomalies.add(Anomaly.DECLARED_MISSING)
        return fact

    # заявленная дата заведомо невозможна.
    # При дневной точности сравниваем по дате: «начал 2 марта» на тикете,
    # созданном 2 марта в 11:00, — это корректное указание дня, а не ошибка.
    impossible = False
    if _is_before_creation(declared, signals.created_at, precision, calendar):
        fact.anomalies.add(Anomaly.DECLARED_BEFORE_CREATED)
        impossible = True
    if declared > now:
        fact.anomalies.add(Anomaly.DECLARED_AFTER_NOW)
        impossible = True

    if impossible:
        if system_at is not None:
            fact.effective_at = system_at
            fact.source = Source.SYSTEM
            fact.confidence = Confidence.LOW
        else:
            fact.effective_at = signals.created_at
            fact.source = Source.INFERRED
            fact.confidence = Confidence.LOW
        return fact

    # обе даты известны — сверяем расхождение
    threshold = policy.discrepancy_threshold_seconds()
    conflicting = (
        fact.discrepancy_business_s is not None and fact.discrepancy_business_s > threshold
    )
    if conflicting:
        fact.anomalies.add(Anomaly.DECLARED_CONTRADICTS_CHANGELOG)

    if policy.prefer == "system_only":
        fact.effective_at = system_at or declared
        fact.source = Source.SYSTEM if system_at else Source.DECLARED
        fact.confidence = Confidence.HIGH if system_at else Confidence.MEDIUM
        return fact

    if conflicting and policy.on_conflict == "use_system_flag_anomaly" and system_at is not None:
        fact.effective_at = system_at
        fact.source = Source.SYSTEM
        fact.confidence = Confidence.LOW
        return fact

    if policy.prefer == "declared":
        fact.effective_at = declared
        fact.source = Source.DECLARED
        fact.confidence = Confidence.LOW if conflicting else Confidence.HIGH
    else:
        fact.effective_at = system_at or declared
        fact.source = Source.SYSTEM if system_at else Source.DECLARED
        fact.confidence = Confidence.LOW if conflicting else Confidence.HIGH
    return fact


def _normalize_precision(
    value: datetime | None,
    precision: str,
    boundary: str,
    policy: ReconciliationPolicy,
    calendar: WorkCalendar,
) -> datetime | None:
    """Развернуть дневную точность в конкретный момент.

    Поле «start date» без времени означает «в этот день»; для начала работы
    берём начало рабочего дня, для конца — конец.
    """
    if value is None or precision != "day":
        return value

    local = value.astimezone(calendar.zone)
    windows = calendar.day_intervals(local.date())
    if not windows:
        # выходной: начало работы сдвигаем вперёд, конец — назад
        if boundary == "work_start":
            return calendar.add_business_seconds(local, 0)
        probe = datetime.combine(local.date(), time(23, 59), tzinfo=calendar.zone)
        for _ in range(14):
            probe -= timedelta(days=1)
            day_windows = calendar.day_intervals(probe.date())
            if day_windows:
                return day_windows[-1][1]
        return local

    if boundary == "work_start" and policy.day_precision_start_at_day_start:
        return windows[0][0]
    return windows[-1][1]


def _clamp_to_creation(value: datetime, created_at: datetime) -> datetime:
    """Не позволяем началу работы оказаться раньше создания тикета."""
    return max(value, created_at)


def _is_before_creation(
    declared: datetime,
    created_at: datetime,
    precision: str,
    calendar: WorkCalendar,
) -> bool:
    """Заявленная дата раньше создания тикета?

    При дневной точности сравнение идёт по календарным датам: указание
    того же дня, что и создание, ошибкой не является.
    """
    if precision == "day":
        local_declared = declared.astimezone(calendar.zone).date()
        local_created = created_at.astimezone(calendar.zone).date()
        return local_declared < local_created
    return declared < created_at


def _check_order(
    start: BoundaryFact,
    end: BoundaryFact,
    signals: TimelineSignals,
    policy: ReconciliationPolicy,
    calendar: WorkCalendar,
) -> None:
    """Конец не может быть раньше начала."""
    if start.effective_at is None or end.effective_at is None:
        return
    if end.effective_at >= start.effective_at:
        return

    start.anomalies.add(Anomaly.END_BEFORE_START)
    end.anomalies.add(Anomaly.END_BEFORE_START)

    # откатываемся к системным значениям, если они непротиворечивы
    if (
        signals.system_start is not None
        and signals.system_end is not None
        and signals.system_end >= signals.system_start
    ):
        start.effective_at = signals.system_start
        start.source = Source.SYSTEM
        end.effective_at = signals.system_end
        end.source = Source.SYSTEM
    start.confidence = Confidence.LOW
    end.confidence = Confidence.LOW


def _check_partial(start: BoundaryFact, end: BoundaryFact, signals: TimelineSignals) -> None:
    """Заполнена только одна из двух дат у завершённого тикета."""
    if not signals.is_completed:
        return
    if (signals.declared_start is None) != (signals.declared_end is None):
        start.anomalies.add(Anomaly.DECLARED_ONLY_PARTIAL)
        end.anomalies.add(Anomaly.DECLARED_ONLY_PARTIAL)


def _ticket_level_anomalies(
    signals: TimelineSignals, policy: ReconciliationPolicy, calendar: WorkCalendar
) -> set[Anomaly]:
    """Аномалии, относящиеся к тикету целиком."""
    found: set[Anomaly] = set()
    times = sorted(signals.transition_times)

    if len(times) >= policy.bulk_move_min_transitions:
        span = (times[-1] - times[0]).total_seconds()
        if span <= policy.bulk_move_max_seconds:
            found.add(Anomaly.BULK_MOVE)

    if signals.is_completed and signals.system_start and signals.system_end:
        worked = calendar.business_seconds_between(signals.system_start, signals.system_end)
        if worked == 0:
            found.add(Anomaly.ZERO_DURATION_LIFECYCLE)

    return found


def stale_transition_threshold(durations: list[int], percentile_value: float = 95.0) -> int | None:
    """Порог «неправдоподобно долгого» интервала — перцентиль по выборке."""
    from flowlens.core.metrics import percentile

    if not durations:
        return None
    value = percentile([float(d) for d in durations], percentile_value)
    return int(value) if value is not None else None


__all__ = [
    "POLICY_VERSION",
    "Anomaly",
    "BoundaryFact",
    "Confidence",
    "ReconciliationPolicy",
    "Source",
    "TimelineSignals",
    "reconcile",
    "stale_transition_threshold",
]
