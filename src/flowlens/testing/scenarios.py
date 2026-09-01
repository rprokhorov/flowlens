"""Сценарии синтетических тикетов.

Каждый сценарий проверяет отдельное поведение построителя интервалов.
Длительности заданы в рабочих секундах, поэтому ожидаемые метрики точны.
"""

from __future__ import annotations

import random
from datetime import datetime
from zoneinfo import ZoneInfo

from flowlens.core.calendar import WorkCalendar
from flowlens.core.domain import TicketSeed
from flowlens.testing.synthetic import HOUR, WORKDAY, TicketBuilder

MSK = ZoneInfo("Europe/Moscow")


def _at(y: int, m: int, d: int, hh: int = 11, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=MSK)


def happy_path(cal: WorkCalendar, key: str = "DEMO-1") -> TicketSeed:
    """Обычный путь: new → in progress → qa → release → done."""
    b = TicketBuilder(key=key, calendar=cal, created_at=_at(2026, 3, 2), reporter="olga")
    b.stay(4 * HOUR)  # лежал в new
    b.assign("ivan").move_to("in progress")
    b.stay(2 * WORKDAY)
    b.move_to("qa").assign("petr")
    b.stay(WORKDAY)
    b.move_to("release")
    b.stay(3 * HOUR)
    b.move_to("done")
    b.declare_from_actual()
    return b.build(scenario="happy_path", summary="Обычная задача")


def with_blocking(cal: WorkCalendar, key: str = "DEMO-2") -> TicketSeed:
    """С блокировкой посередине разработки."""
    b = TicketBuilder(key=key, calendar=cal, created_at=_at(2026, 3, 3), reporter="olga")
    b.stay(HOUR)
    b.assign("ivan").move_to("in progress")
    b.stay(WORKDAY)
    b.move_to("blocked/hold")
    b.stay(3 * WORKDAY)  # ждали внешнюю команду
    b.move_to("in progress")
    b.stay(4 * HOUR)
    b.move_to("qa")
    b.stay(2 * HOUR)
    b.move_to("release").stay(HOUR).move_to("done")
    b.declare_from_actual()
    return b.build(scenario="with_blocking", summary="Задача с блокировкой")


def with_rework(cal: WorkCalendar, key: str = "DEMO-3") -> TicketSeed:
    """Возврат из qa обратно в разработку."""
    b = TicketBuilder(key=key, calendar=cal, created_at=_at(2026, 3, 4), reporter="olga")
    b.stay(2 * HOUR)
    b.assign("ivan").move_to("in progress")
    b.stay(WORKDAY)
    b.move_to("qa").assign("petr")
    b.stay(3 * HOUR)
    b.move_to("in progress").assign("ivan")  # вернули на доработку
    b.stay(5 * HOUR)
    b.move_to("qa").assign("petr")
    b.stay(2 * HOUR)
    b.move_to("release").stay(HOUR).move_to("done")
    b.declare_from_actual()
    return b.build(scenario="with_rework", summary="Задача с возвратом из QA")


def reopened(cal: WorkCalendar, key: str = "DEMO-4") -> TicketSeed:
    """Переоткрытие после done."""
    b = TicketBuilder(key=key, calendar=cal, created_at=_at(2026, 3, 5), reporter="olga")
    b.assign("ivan").move_to("in progress")
    b.stay(WORKDAY)
    b.move_to("qa").stay(HOUR).move_to("release").stay(HOUR).move_to("done")
    b.stay(2 * WORKDAY)  # полежал закрытым
    b.move_to("in progress")  # переоткрыли
    b.stay(4 * HOUR)
    b.move_to("qa").stay(HOUR).move_to("release").stay(HOUR).move_to("done")
    return b.build(scenario="reopened", summary="Переоткрытая задача")


def assignee_handoff(cal: WorkCalendar, key: str = "DEMO-5") -> TicketSeed:
    """Смена исполнителя внутри одного статуса."""
    b = TicketBuilder(key=key, calendar=cal, created_at=_at(2026, 3, 6), reporter="olga")
    b.assign("ivan").move_to("in progress")
    b.stay(WORKDAY)
    b.assign("maria")  # передали другому, статус тот же
    b.stay(2 * WORKDAY)
    b.move_to("qa").assign("petr")
    b.stay(3 * HOUR)
    b.move_to("release").stay(HOUR).move_to("done")
    b.declare_from_actual()
    return b.build(scenario="assignee_handoff", summary="Задача с передачей")


def bulk_move(cal: WorkCalendar, key: str = "DEMO-6") -> TicketSeed:
    """Пятничная разгребка: все переходы за секунды, даты проставлены руками.

    Это ключевой проблемный случай — changelog врёт, declared-даты правдивы.
    """
    created = _at(2026, 3, 2, 10)
    b = TicketBuilder(key=key, calendar=cal, created_at=created, reporter="olga")
    b.stay(4 * WORKDAY)  # реально работали всю неделю, но статус не двигали
    b.assign("ivan").move_to("in progress")
    b.stay(0)
    b.move_to("qa").stay(0).move_to("release").stay(0).move_to("done")
    # сотрудник задним числом проставил реальные даты работы
    b.declare("work_start", _at(2026, 3, 2, 10), precision="day")
    b.declare("work_end", _at(2026, 3, 5, 19), precision="day")
    return b.build(scenario="bulk_move", summary="Задача, закрытая скопом")


def still_open(cal: WorkCalendar, key: str = "DEMO-7") -> TicketSeed:
    """Незакрытый тикет: последний интервал открыт."""
    b = TicketBuilder(key=key, calendar=cal, created_at=_at(2026, 3, 9), reporter="olga")
    b.stay(3 * HOUR)
    b.assign("ivan").move_to("in progress")
    b.stay(2 * WORKDAY)
    b.declare("work_start", _at(2026, 3, 9, 14))
    return b.build(scenario="still_open", summary="Задача в работе")


def never_started(cal: WorkCalendar, key: str = "DEMO-8") -> TicketSeed:
    """Тикет, который так и не начали: только backlog."""
    b = TicketBuilder(key=key, calendar=cal, created_at=_at(2026, 3, 10), reporter="olga")
    b.stay(10 * WORKDAY)
    return b.build(scenario="never_started", summary="Задача в бэклоге")


def declared_contradicts(cal: WorkCalendar, key: str = "DEMO-9") -> TicketSeed:
    """Заявленные даты противоречат changelog больше чем на порог."""
    b = TicketBuilder(key=key, calendar=cal, created_at=_at(2026, 3, 2), reporter="olga")
    b.stay(HOUR)
    b.assign("ivan").move_to("in progress")
    b.stay(WORKDAY)
    b.move_to("qa").stay(HOUR).move_to("release").stay(HOUR).move_to("done")
    # заявил старт на две недели раньше создания тикета — заведомая ошибка
    b.declare("work_start", _at(2026, 2, 16, 10), precision="day")
    return b.build(scenario="declared_contradicts", summary="Противоречивые даты")


def broken_declared_order(cal: WorkCalendar, key: str = "DEMO-10") -> TicketSeed:
    """end date раньше start date."""
    b = TicketBuilder(key=key, calendar=cal, created_at=_at(2026, 3, 11), reporter="olga")
    b.assign("ivan").move_to("in progress")
    b.stay(WORKDAY)
    b.move_to("qa").stay(HOUR).move_to("release").stay(HOUR).move_to("done")
    b.declare("work_start", _at(2026, 3, 13, 10), precision="day")
    b.declare("work_end", _at(2026, 3, 11, 19), precision="day")
    return b.build(scenario="broken_declared_order", summary="Даты наоборот")


SCENARIOS = (
    happy_path,
    with_blocking,
    with_rework,
    reopened,
    assignee_handoff,
    bulk_move,
    still_open,
    never_started,
    declared_contradicts,
    broken_declared_order,
)


def all_scenarios(cal: WorkCalendar) -> list[TicketSeed]:
    """Все эталонные сценарии, по одному тикету на каждый."""
    return [fn(cal, f"DEMO-{i}") for i, fn in enumerate(SCENARIOS, start=1)]


# --- массовая генерация для нагрузки и дашбордов -----------------------------

PEOPLE = ("ivan", "maria", "petr", "anna", "sergey", "dmitry")
TYPES = (("Bug", 0.30), ("Story", 0.45), ("Task", 0.20), ("Sub-task", 0.05))
PRIORITIES = (("Blocker", 0.04), ("High", 0.20), ("Medium", 0.56), ("Low", 0.20))
COMPONENTS = ("api", "web", "billing", "auth", "infra", "reports")


def _weighted(rng: random.Random, options: tuple[tuple[str, float], ...]) -> str:
    return rng.choices([o[0] for o in options], weights=[o[1] for o in options])[0]


def random_ticket(
    cal: WorkCalendar,
    key: str,
    created_at: datetime,
    rng: random.Random,
    open_chance: float = 0.12,
    horizon: datetime | None = None,
) -> TicketSeed:
    """Случайный, но правдоподобный тикет.

    `open_chance` — вероятность, что тикет ещё не завершён. Вызывающий код
    снижает её для давних тикетов: старые задачи почти все уже закрыты.

    `horizon` — «сейчас»: тикет не может продвинуться дальше этого момента,
    иначе недавно созданные задачи уезжали бы в будущее.
    """
    def past_horizon() -> bool:
        return horizon is not None and b.cursor >= horizon
    issue_type = _weighted(rng, TYPES)
    priority = _weighted(rng, PRIORITIES)
    dev = rng.choice(PEOPLE)
    qa_person = rng.choice([p for p in PEOPLE if p != dev])

    b = TicketBuilder(
        key=key,
        calendar=cal,
        created_at=created_at,
        reporter=rng.choice(PEOPLE),
        issue_type=issue_type,
        priority=priority,
        horizon=horizon,
    )

    # багам и блокерам обычно уделяют внимание быстрее
    urgency = 0.25 if priority in ("Blocker", "High") else 1.0
    b.stay(int(rng.expovariate(1 / (WORKDAY * urgency))))

    if rng.random() < 0.08 * (open_chance / 0.12):  # часть задач остаётся в бэклоге
        return b.build(scenario="random_backlog", summary=f"{issue_type} {key}")

    b.assign(dev).move_to("in progress")
    work = max(HOUR, int(rng.lognormvariate(10.4, 0.9)))
    b.stay(work)
    # незавершённость распределяется по фазам: доля задач замирает в разработке,
    # иначе весь WIP скапливался бы в последней проверяемой фазе
    if past_horizon() or rng.random() < open_chance * 0.45:
        return b.build(scenario="random_open", summary=f"{issue_type} {key}")

    if rng.random() < 0.22:  # блокировка
        b.move_to("blocked/hold")
        b.stay(int(rng.expovariate(1 / (2 * WORKDAY))))
        b.move_to("in progress")
        b.stay(max(HOUR, work // 3))

    b.move_to("qa").assign(qa_person)
    b.stay(max(HOUR, int(rng.lognormvariate(9.4, 0.8))))
    if past_horizon() or rng.random() < open_chance * 0.35:
        return b.build(scenario="random_open", summary=f"{issue_type} {key}")

    if rng.random() < 0.18:  # возврат на доработку
        b.move_to("in progress").assign(dev)
        b.stay(max(HOUR, work // 2))
        b.move_to("qa").assign(qa_person)
        b.stay(max(HOUR, int(rng.lognormvariate(9.0, 0.7))))

    if rng.random() < open_chance * 0.2:  # ждёт релиза
        return b.build(scenario="random_open", summary=f"{issue_type} {key}")

    b.move_to("release")
    b.stay(int(rng.expovariate(1 / (WORKDAY // 2))))
    b.move_to("done")

    # большинство проставляет даты, часть забывает, часть ошибается
    roll = rng.random()
    if roll < 0.75:
        b.declare_from_actual(precision="day" if rng.random() < 0.5 else "minute")
    elif roll < 0.85:
        b.declare("work_start", created_at, precision="day")

    return b.build(
        scenario="random",
        summary=f"{issue_type} {key}",
        components=[rng.choice(COMPONENTS)],
    )
