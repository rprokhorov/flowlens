"""Тесты классификации статусов на команду.

Дефект, который они закрывают: расчёты брали доску из константы `BOARD`,
а не из БД. Настройка через API попадала в таблицу, но метрики её игнорировали —
ошибка без единого сообщения об ошибке.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

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
                "ON CONFLICT (name, COALESCE(parent_team_id, 0)) DO UPDATE "
                "SET name = EXCLUDED.name "
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
                "ON CONFLICT (team_id, external_name) "
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


def test_one_status_row_per_team_and_name(data) -> None:
    """Команда, тянущая данные из двух источников, не должна получать дубли.

    Раньше уникальность включала source_id, поэтому импорт из второго
    источника создавал вторую строку для того же `qa`: в интерфейсе она
    дублировалась, а правка одной оставляла вторую нетронутой.
    """
    with data.begin() as conn:
        duplicates = conn.execute(
            text(
                "SELECT team_id, external_name, count(*) AS n "
                "FROM workflow_status WHERE team_id IS NOT NULL "
                "GROUP BY team_id, external_name HAVING count(*) > 1"
            )
        ).all()
    assert not duplicates, f"дубли статусов: {[dict(r._mapping) for r in duplicates]}"


def test_second_source_reuses_team_statuses(data) -> None:
    """Импорт из другого источника переиспользует статусы команды."""
    from flowlens.contract import RawEvent, RawTicket, SourceProfile
    from flowlens.importer import import_tickets

    with data.begin() as conn:
        before = conn.execute(
            text("SELECT count(*) FROM workflow_status WHERE team_id = 1")
        ).scalar_one()

    created = datetime(2026, 2, 2, 10, 0, tzinfo=UTC)
    import_tickets(
        data,
        SourceProfile(source_kind="csv", source_name="second-source", changelog="full"),
        [
            RawTicket(
                external_key="SECOND-1",
                project_key="SECOND",
                issue_type="Task",
                status="in progress",
                created_at=created,
                events=[
                    RawEvent(kind="created", occurred_at=created),
                    RawEvent(
                        kind="status_change",
                        occurred_at=created + timedelta(hours=2),
                        field="status",
                        old_value="new",
                        new_value="in progress",
                    ),
                ],
            )
        ],
    )

    with data.begin() as conn:
        after = conn.execute(
            text("SELECT count(*) FROM workflow_status WHERE team_id = 1")
        ).scalar_one()
        conn.execute(text("DELETE FROM ticket WHERE external_key = 'SECOND-1'"))
        # sync_run ссылается на источник, поэтому убирается раньше него
        conn.execute(
            text(
                "DELETE FROM sync_run WHERE source_id = "
                "(SELECT id FROM source WHERE name = 'second-source')"
            )
        )
        conn.execute(text("DELETE FROM source WHERE name = 'second-source'"))

    assert after == before, "второй источник не должен плодить статусы команды"


def test_recompute_uses_each_team_board(data) -> None:
    """Пересчёт обязан брать доску КАЖДОЙ команды, а не первого тикета.

    Дефект, который тест закрывает: recompute_all брал team_id из первой
    строки выборки и применял её классификацию ко всем. С одной командой
    это работало, с двумя — часть метрик считалась по чужой доске,
    без единого сообщения об ошибке.

    Сравниваем одну и ту же команду до и после смены её классификации:
    состав задач при этом не меняется, и разница может быть только от доски.
    """
    from flowlens.analytics import flow_efficiency

    with data.begin() as conn:
        calendar_id = conn.execute(
            text("SELECT calendar_id FROM team WHERE id = 1")
        ).scalar_one()
        second = conn.execute(
            text(
                "INSERT INTO team (name, calendar_id) VALUES ('board-split', :cal) "
                "ON CONFLICT (name, COALESCE(parent_team_id, 0)) DO UPDATE "
                "SET name = EXCLUDED.name RETURNING id"
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
                "SELECT :src, :team, external_name, phase, is_active_work, "
                "       is_queue, is_terminal, board_order "
                "FROM workflow_status WHERE team_id = 1 "
                "ON CONFLICT (team_id, external_name) WHERE team_id IS NOT NULL DO NOTHING"
            ),
            {"src": source_id, "team": second},
        )
        # Первые задачи уходят второй команде: тогда сломанный recompute
        # возьмёт её доску (она в первой строке выборки) и применит ко ВСЕМ,
        # включая первую команду. Проверять надо именно первую — ту, чья
        # доска при дефекте подменяется чужой.
        conn.execute(
            text(
                "UPDATE ticket SET team_id = :team WHERE id IN "
                "(SELECT id FROM ticket ORDER BY id LIMIT 40)"
            ),
            {"team": second},
        )

    first_scope = replace(Filters(), team_id=1)
    try:
        recompute_all(data)
        before = flow_efficiency(data, first_scope)["efficiency"]
        assert before is not None, "метрики первой команды не посчитаны"

        # Меняем классификацию ТОЛЬКО у второй команды. На метрики первой
        # это влиять не должно: у неё своя доска.
        with data.begin() as conn:
            conn.execute(
                text(
                    "UPDATE workflow_status SET is_active_work = false, is_queue = true "
                    "WHERE team_id = :team AND external_name = 'qa'"
                ),
                {"team": second},
            )
        recompute_all(data)
        after = flow_efficiency(data, first_scope)["efficiency"]

        assert after == pytest.approx(before, abs=0.0001), (
            "классификация чужой команды повлияла на метрики первой — "
            "значит доска берётся не по команде тикета"
        )
    finally:
        with data.begin() as conn:
            conn.execute(
                text("UPDATE ticket SET team_id = 1 WHERE team_id = :t"), {"t": second}
            )
        # пересчёт до удаления статусов: интервалы ещё ссылаются на них
        recompute_all(data)
        with data.begin() as conn:
            conn.execute(text("DELETE FROM workflow_status WHERE team_id = :t"), {"t": second})
            conn.execute(text("DELETE FROM team WHERE id = :t"), {"t": second})
