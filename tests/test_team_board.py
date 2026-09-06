"""Тесты классификации статусов на команду.

Дефект, который они закрывают: расчёты брали доску из константы `BOARD`,
а не из БД. Настройка через API попадала в таблицу, но метрики её игнорировали —
ошибка без единого сообщения об ошибке.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from flowlens.analytics import Filters, flow_efficiency
from flowlens.core.calendar import WorkCalendar
from flowlens.core.domain import BOARD_BY_NAME, Phase, StatusDef
from flowlens.core.intervals import build_intervals
from flowlens.core.metrics import compute_metrics
from flowlens.db import make_engine
from flowlens.pipeline import recompute_all, seed_demo
from flowlens.repository import load_board
from flowlens.testing.scenarios import happy_path


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
    seed_demo(engine, ticket_count=150, months=5, seed=77)
    recompute_all(engine)
    return engine


@pytest.fixture
def cal() -> WorkCalendar:
    return WorkCalendar(name="t", tz="Europe/Moscow")


# --- доска как параметр расчёта ----------------------------------------------


def queue_board() -> dict[str, StatusDef]:
    """Та же доска, но qa — очередь, а не работа."""
    board = dict(BOARD_BY_NAME)
    board["qa"] = StatusDef(
        "qa", Phase.VERIFY, is_active_work=False, is_queue=True, board_order=4
    )
    return board


def test_metrics_follow_board(cal: WorkCalendar) -> None:
    """Одни и те же события дают разный touch time при разной классификации."""
    seed = happy_path(cal)
    intervals = build_intervals(seed.events, cal)

    as_work = compute_metrics(
        created_at=seed.created_at,
        intervals=intervals,
        events=seed.events,
        comments=[],
        calendar=cal,
    )
    as_queue = compute_metrics(
        created_at=seed.created_at,
        intervals=intervals,
        events=seed.events,
        comments=[],
        calendar=cal,
        board=queue_board(),
    )

    assert as_queue.touch_time_business_s < as_work.touch_time_business_s
    assert as_queue.flow_efficiency < as_work.flow_efficiency


def test_default_board_unchanged(cal: WorkCalendar) -> None:
    """Без явной доски поведение прежнее — иначе сломались бы все вызовы."""
    seed = happy_path(cal)
    intervals = build_intervals(seed.events, cal)
    explicit = build_intervals(seed.events, cal, board=BOARD_BY_NAME)
    assert [i.phase for i in intervals] == [i.phase for i in explicit]


def test_cycle_time_unaffected_by_classification(cal: WorkCalendar) -> None:
    """Время цикла зависит от границ, а не от того, работа это или ожидание."""
    seed = happy_path(cal)
    intervals = build_intervals(seed.events, cal)
    common = {
        "created_at": seed.created_at,
        "intervals": intervals,
        "events": seed.events,
        "comments": [],
        "calendar": cal,
    }
    assert (
        compute_metrics(**common).cycle_time_business_s
        == compute_metrics(**common, board=queue_board()).cycle_time_business_s
    )


# --- загрузка доски из БД ----------------------------------------------------


def test_load_board_returns_team_settings(data) -> None:
    board = load_board(data, 1)
    assert "qa" in board
    assert board["in progress"].is_active_work


def test_load_board_falls_back_to_defaults(data) -> None:
    """Статусы, которых нет в БД, берутся из эталонной доски."""
    board = load_board(data, 999999)
    assert set(BOARD_BY_NAME) <= set(board)


def test_recompute_uses_stored_classification(data) -> None:
    """Главный сценарий: смена настройки в БД меняет метрики после пересчёта.

    Раньше расчёт брал доску из константы, и это изменение не доходило
    до цифр — самая опасная разновидность ошибки, потому что молчаливая.
    """
    before = flow_efficiency(data, Filters())["efficiency"]

    with data.begin() as conn:
        conn.execute(
            text(
                "UPDATE workflow_status SET is_active_work = false, is_queue = true "
                "WHERE external_name = 'qa' AND team_id = 1"
            )
        )
    recompute_all(data)
    after = flow_efficiency(data, Filters())["efficiency"]

    with data.begin() as conn:
        conn.execute(
            text(
                "UPDATE workflow_status SET is_active_work = true, is_queue = false "
                "WHERE external_name = 'qa' AND team_id = 1"
            )
        )
    recompute_all(data)
    restored = flow_efficiency(data, Filters())["efficiency"]

    assert after < before, "перевод qa в очередь обязан снизить эффективность"
    assert restored == pytest.approx(before, abs=0.001)


# --- независимость команд ----------------------------------------------------


def test_teams_classify_same_status_differently(data) -> None:
    """Две команды могут по-разному понимать один и тот же статус доски."""
    with data.begin() as conn:
        calendar_id = conn.execute(
            text("SELECT calendar_id FROM team WHERE id = 1")
        ).scalar_one()
        team_b = conn.execute(
            text(
                "INSERT INTO team (name, calendar_id) VALUES ('test-team-b', :cal) "
                "ON CONFLICT (name, parent_team_id) DO UPDATE SET name = EXCLUDED.name "
                "RETURNING id"
            ),
            {"cal": calendar_id},
        ).scalar_one()
        source_id = conn.execute(
            text("SELECT min(source_id) FROM workflow_status")
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO workflow_status "
                "(source_id, team_id, external_name, phase, is_active_work, "
                " is_queue, is_terminal, board_order) "
                "VALUES (:src, :team, 'qa', 'verify', false, true, false, 4) "
                "ON CONFLICT (source_id, team_id, external_name) "
                "WHERE team_id IS NOT NULL DO NOTHING"
            ),
            {"src": source_id, "team": team_b},
        )

    try:
        assert load_board(data, 1)["qa"].is_active_work
        assert not load_board(data, team_b)["qa"].is_active_work
    finally:
        with data.begin() as conn:
            conn.execute(text("DELETE FROM workflow_status WHERE team_id = :t"), {"t": team_b})
            conn.execute(text("DELETE FROM team WHERE id = :t"), {"t": team_b})
