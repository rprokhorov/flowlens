"""Интеграционные тесты: полный цикл через Postgres.

Пропускаются, если база недоступна.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from flowlens.core.calendar import WorkCalendar
from flowlens.core.intervals import build_intervals
from flowlens.core.metrics import compute_metrics
from flowlens.db import make_engine
from flowlens.pipeline import recompute_all, seed_demo
from flowlens.repository import load_calendar
from flowlens.testing.scenarios import all_scenarios


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
def seeded(engine):
    """База, заполненная небольшим набором и пересчитанная."""
    seed_demo(engine, ticket_count=60, months=6, seed=7)
    recompute_all(engine)
    return engine


def test_seed_creates_data(seeded) -> None:
    with seeded.begin() as conn:
        tickets = conn.execute(text("SELECT count(*) FROM ticket")).scalar_one()
        events = conn.execute(text("SELECT count(*) FROM ticket_event")).scalar_one()
    assert tickets == 70  # 60 случайных + 10 эталонных сценариев
    assert events > tickets


def test_every_ticket_has_intervals(seeded) -> None:
    with seeded.begin() as conn:
        orphans = conn.execute(
            text(
                "SELECT count(*) FROM ticket t "
                "WHERE NOT EXISTS (SELECT 1 FROM ticket_interval i WHERE i.ticket_id = t.id)"
            )
        ).scalar_one()
    assert orphans == 0


def test_every_ticket_has_metrics(seeded) -> None:
    with seeded.begin() as conn:
        missing = conn.execute(
            text(
                "SELECT count(*) FROM ticket t "
                "WHERE NOT EXISTS (SELECT 1 FROM ticket_metrics m WHERE m.ticket_id = t.id)"
            )
        ).scalar_one()
    assert missing == 0


def test_intervals_are_contiguous_in_db(seeded) -> None:
    """В базе интервалы стыкуются без разрывов."""
    with seeded.begin() as conn:
        gaps = conn.execute(
            text(
                "SELECT count(*) FROM ("
                "  SELECT ticket_id, ended_at,"
                "         lead(started_at) OVER (PARTITION BY ticket_id ORDER BY seq) AS next_start"
                "  FROM ticket_interval"
                ") s WHERE next_start IS NOT NULL AND next_start <> ended_at"
            )
        ).scalar_one()
    assert gaps == 0


def test_only_last_interval_open_in_db(seeded) -> None:
    with seeded.begin() as conn:
        bad = conn.execute(
            text(
                "SELECT count(*) FROM ticket_interval i "
                "WHERE i.ended_at IS NULL AND EXISTS ("
                "  SELECT 1 FROM ticket_interval j "
                "  WHERE j.ticket_id = i.ticket_id AND j.seq > i.seq)"
            )
        ).scalar_one()
    assert bad == 0


def test_business_never_exceeds_calendar(seeded) -> None:
    with seeded.begin() as conn:
        bad = conn.execute(
            text(
                "SELECT count(*) FROM ticket_interval "
                "WHERE duration_business_s > duration_calendar_s"
            )
        ).scalar_one()
    assert bad == 0


def test_flow_efficiency_in_range(seeded) -> None:
    with seeded.begin() as conn:
        bad = conn.execute(
            text(
                "SELECT count(*) FROM ticket_metrics "
                "WHERE flow_efficiency IS NOT NULL "
                "  AND (flow_efficiency < 0 OR flow_efficiency > 1)"
            )
        ).scalar_one()
    assert bad == 0


def test_lead_not_less_than_cycle(seeded) -> None:
    with seeded.begin() as conn:
        bad = conn.execute(
            text(
                "SELECT count(*) FROM ticket_metrics "
                "WHERE lead_time_business_s IS NOT NULL "
                "  AND cycle_time_business_s IS NOT NULL "
                "  AND lead_time_business_s < cycle_time_business_s"
            )
        ).scalar_one()
    assert bad == 0


def test_recompute_is_idempotent(seeded) -> None:
    """Повторный пересчёт не меняет результат и не создаёт дублей."""
    with seeded.begin() as conn:
        before_count = conn.execute(text("SELECT count(*) FROM ticket_interval")).scalar_one()
        before_hash = conn.execute(
            text(
                "SELECT md5(string_agg(x, '|' ORDER BY x)) FROM ("
                "  SELECT ticket_id::text || seq::text || "
                "         coalesce(duration_business_s::text, '-') AS x "
                "  FROM ticket_interval) s"
            )
        ).scalar_one()

    recompute_all(seeded)

    with seeded.begin() as conn:
        after_count = conn.execute(text("SELECT count(*) FROM ticket_interval")).scalar_one()
        after_hash = conn.execute(
            text(
                "SELECT md5(string_agg(x, '|' ORDER BY x)) FROM ("
                "  SELECT ticket_id::text || seq::text || "
                "         coalesce(duration_business_s::text, '-') AS x "
                "  FROM ticket_interval) s"
            )
        ).scalar_one()

    assert before_count == after_count
    assert before_hash == after_hash


def test_db_metrics_match_in_memory(seeded) -> None:
    """Метрики в базе совпадают с расчётом в памяти для эталонных сценариев."""
    cal_db = load_calendar(seeded, _any_team_id(seeded))
    reference = WorkCalendar(name="test", tz="Europe/Moscow")
    assert cal_db.tz == reference.tz

    for scenario_seed in all_scenarios(reference):
        # незакрытые тикеты сравнивать нельзя: в базе открытый интервал
        # копит время до реального «сейчас», а в памяти — до observed_at
        if scenario_seed.scenario in ("still_open", "never_started"):
            continue
        intervals = build_intervals(
            scenario_seed.events, reference, now=scenario_seed.observed_at
        )
        expected = compute_metrics(
            created_at=scenario_seed.created_at,
            intervals=intervals,
            events=scenario_seed.events,
            comments=scenario_seed.comments,
            calendar=reference,
            reporter=scenario_seed.reporter,
        )
        with seeded.begin() as conn:
            row = conn.execute(
                text(
                    "SELECT m.touch_time_business_s, m.blocked_time_business_s, "
                    "       m.reopen_count, m.status_change_count "
                    "FROM ticket_metrics m JOIN ticket t ON t.id = m.ticket_id "
                    "WHERE t.external_key = :key"
                ),
                {"key": scenario_seed.key},
            ).one()
        assert row.touch_time_business_s == expected.touch_time_business_s, scenario_seed.key
        assert row.blocked_time_business_s == expected.blocked_time_business_s, scenario_seed.key
        assert row.reopen_count == expected.reopen_count, scenario_seed.key
        assert row.status_change_count == expected.status_change_count, scenario_seed.key


def test_workload_matches_intervals(seeded) -> None:
    """Сумма дневной нагрузки равна сумме владения по закрытым интервалам."""
    with seeded.begin() as conn:
        from_workload = conn.execute(
            text("SELECT coalesce(sum(owned_business_s), 0) FROM person_workload_daily")
        ).scalar_one()
        from_intervals = conn.execute(
            text(
                "SELECT coalesce(sum(duration_business_s), 0) FROM ticket_interval "
                "WHERE assignee_id IS NOT NULL AND ended_at IS NOT NULL"
            )
        ).scalar_one()
    assert from_workload == from_intervals


def test_blocked_intervals_have_source_status(seeded) -> None:
    """У заблокированных интервалов записано, откуда пришли."""
    with seeded.begin() as conn:
        total = conn.execute(
            text("SELECT count(*) FROM ticket_interval WHERE is_blocked")
        ).scalar_one()
        with_source = conn.execute(
            text(
                "SELECT count(*) FROM ticket_interval "
                "WHERE is_blocked AND blocked_from_status_id IS NOT NULL"
            )
        ).scalar_one()
    assert total > 0
    assert with_source == total


def test_declared_dates_stored(seeded) -> None:
    """Заявленные даты сохраняются сырыми, без согласования."""
    with seeded.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM ticket_declared_date")).scalar_one()
        contradictory = conn.execute(
            text(
                "SELECT count(*) FROM ticket_declared_date d JOIN ticket t ON t.id = d.ticket_id "
                "WHERE d.boundary = 'work_start' AND d.value_at < t.created_at"
            )
        ).scalar_one()
    assert count > 0
    # сценарий declared_contradicts должен сохраниться как есть — чинить будет этап 3
    assert contradictory >= 1


def _any_team_id(engine) -> int:
    with engine.begin() as conn:
        return conn.execute(text("SELECT id FROM team LIMIT 1")).scalar_one()
