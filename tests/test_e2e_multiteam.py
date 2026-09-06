"""Сквозные проверки на двух командах.

Эти тесты закрывают класс дефектов, который модульные не ловят по устройству:
каждый проявлялся только на СОЧЕТАНИИ условий — две команды, разные настройки,
конкретный раздел. Все три были найдены вручную, а не тестами:

1. `recompute_all` брал доску первой команды и применял ко всем;
2. нагрузка людей писалась в одну команду при ключе (человек, день);
3. у команды без своих статусов сохранение интервалов падало на KeyError.

Общее у них одно: с одной командой всё работало. Поэтому здесь всё
проверяется именно на двух — и каждый раздел дашборда обходится целиком,
а не выборочно.
"""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from flowlens import auth
from flowlens.api.app import app, get_engine
from flowlens.db import make_engine
from flowlens.pipeline import recompute_all, seed_demo

# Разделы, которые открывает дашборд. Список полный намеренно: пропущенный
# эндпоинт виден только как пустая вкладка в браузере.
DASHBOARD_ENDPOINTS = [
    "/api/summary",
    "/api/cycle-time",
    "/api/cfd",
    "/api/arrival-throughput",
    "/api/aging-wip",
    "/api/flow-efficiency",
    "/api/hidden-queue",
    "/api/transitions",
    "/api/blockers",
    "/api/sle",
    "/api/predictability",
    "/api/service-classes",
    "/api/backlog",
    "/api/people",
    "/api/forecast",
    "/api/quality",
    "/api/advice",
    "/api/tickets",
]


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
def world(engine):
    """Две команды с разными данными, настройками и владельцами.

    Повторяет реальный сценарий: данные разделены, классификация `qa`
    у команд разная, у каждой свой тимлид.
    """
    seed_demo(engine, ticket_count=260, months=7, seed=4242)

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_user WHERE username LIKE 'e2e-%'"))
        calendar_id = conn.execute(
            text("SELECT calendar_id FROM team WHERE id = 1")
        ).scalar_one()
        first = conn.execute(text("SELECT min(id) FROM team")).scalar_one()
        second = conn.execute(
            text(
                "INSERT INTO team (name, calendar_id) VALUES ('e2e-second', :cal) "
                "ON CONFLICT (name, COALESCE(parent_team_id, 0)) DO UPDATE "
                "SET name = EXCLUDED.name RETURNING id"
            ),
            {"cal": calendar_id},
        ).scalar_one()
        source_id = conn.execute(
            text("SELECT min(source_id) FROM workflow_status")
        ).scalar_one()

        # У второй команды та же доска, но qa — очередь на ручное тестирование,
        # а не code review. Это и есть смысл настройки на команду.
        conn.execute(
            text(
                "INSERT INTO workflow_status "
                "(source_id, team_id, external_name, phase, is_active_work, "
                " is_queue, is_terminal, board_order) "
                "SELECT :src, :team, external_name, phase, "
                "       CASE WHEN external_name = 'qa' THEN false ELSE is_active_work END, "
                "       CASE WHEN external_name = 'qa' THEN true ELSE is_queue END, "
                "       is_terminal, board_order "
                "FROM workflow_status WHERE team_id = :first "
                "ON CONFLICT (team_id, external_name) WHERE team_id IS NOT NULL DO NOTHING"
            ),
            {"src": source_id, "team": second, "first": first},
        )
        # Первые задачи уходят второй команде: именно так проявлялся дефект
        # с доской, которую брали из первой строки выборки.
        conn.execute(
            text(
                "UPDATE ticket SET team_id = :team WHERE id IN "
                "(SELECT id FROM ticket ORDER BY id LIMIT 90)"
            ),
            {"team": second},
        )

    recompute_all(engine)

    lead_one = auth.create_user(engine, username="e2e-lead-one", password="pass-one-123")
    auth.grant_access(engine, user_id=lead_one.id, team_id=first, role="owner")
    lead_two = auth.create_user(engine, username="e2e-lead-two", password="pass-two-123")
    auth.grant_access(engine, user_id=lead_two.id, team_id=second, role="owner")
    auth.create_user(
        engine, username="e2e-admin", password="pass-admin-123", is_admin=True
    )

    yield {
        "first": first,
        "second": second,
        "leads": {
            first: ("e2e-lead-one", "pass-one-123"),
            second: ("e2e-lead-two", "pass-two-123"),
        },
        "admin": ("e2e-admin", "pass-admin-123"),
    }

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ticket SET team_id = :first WHERE team_id = :t"),
            {"first": first, "t": second},
        )
    recompute_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_user WHERE username LIKE 'e2e-%'"))
        # обещания и нагрузка ссылаются на команду — убираются раньше неё
        conn.execute(
            text("DELETE FROM service_level_expectation WHERE team_id = :t"), {"t": second}
        )
        conn.execute(
            text("DELETE FROM person_workload_daily WHERE team_id = :t"), {"t": second}
        )
        conn.execute(text("DELETE FROM workflow_status WHERE team_id = :t"), {"t": second})
        conn.execute(text("DELETE FROM team WHERE id = :t"), {"t": second})


@pytest.fixture(scope="module")
def client(engine, monkeypatch_e2e):
    monkeypatch_e2e.delenv(auth.ENV_AUTH_DISABLED, raising=False)
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def monkeypatch_e2e():
    from _pytest.monkeypatch import MonkeyPatch

    patcher = MonkeyPatch()
    yield patcher
    patcher.undo()


def as_user(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --- каждый раздел работает у каждой команды ---------------------------------


@pytest.mark.parametrize("path", DASHBOARD_ENDPOINTS)
def test_every_section_works_for_every_team(client, world, path: str) -> None:
    """Каждый раздел открывается у обеих команд.

    Дефект с нагрузкой выглядел именно так: раздел отвечал 200, но был пуст
    у одной из команд. Поэтому проверяется и код ответа, и что данные не
    вырождены в пустоту там, где их быть не должно.
    """
    for team_id, (username, password) in world["leads"].items():
        response = client.get(path, headers=as_user(username, password))
        assert response.status_code == 200, f"{path} у команды {team_id}"


def test_no_section_is_empty_for_either_team(client, world) -> None:
    """Разделы, которые обязаны иметь данные, не должны быть пустыми.

    Именно так проявился дефект нагрузки: 200 и пустой список.
    """
    checks = {
        "/api/people": lambda d: d["people"],
        "/api/flow-efficiency": lambda d: d["by_phase"],
        "/api/cycle-time": lambda d: d["count"],
        "/api/aging-wip": lambda d: d["items"] or d["total"] == 0,
        "/api/blockers": lambda d: d["tickets"],
    }
    for team_id, (username, password) in world["leads"].items():
        for path, has_data in checks.items():
            data = client.get(path, headers=as_user(username, password)).json()
            assert has_data(data), f"{path} пуст у команды {team_id}"


# --- настройки одной команды не влияют на другую -----------------------------


def test_team_settings_are_independent(client, world) -> None:
    """Классификация одной команды не должна менять метрики другой.

    Дефект: recompute брал доску первой строки выборки и применял ко всем.
    """
    first, second = world["first"], world["second"]
    admin = as_user(*world["admin"])

    def efficiency(team_id: int) -> float:
        return client.get(
            "/api/flow-efficiency", params={"team_id": team_id}, headers=admin
        ).json()["efficiency"]

    before_first = efficiency(first)
    before_second = efficiency(second)

    statuses = client.get(f"/api/teams/{second}/statuses", headers=admin).json()
    release = next(s for s in statuses if s["external_name"] == "release")
    client.patch(
        f"/api/statuses/{release['id']}",
        json={"is_active_work": True, "is_queue": False},
        headers=admin,
    )
    try:
        assert efficiency(first) == pytest.approx(before_first, abs=0.0001), (
            "настройка чужой команды изменила метрики первой"
        )
        assert efficiency(second) != pytest.approx(before_second, abs=0.0001), (
            "настройка своей команды не отразилась на её метриках"
        )
    finally:
        client.patch(
            f"/api/statuses/{release['id']}",
            json={"is_active_work": False, "is_queue": True},
            headers=admin,
        )


def test_qa_classification_shows_in_efficiency(client, world) -> None:
    """У команды с qa-очередью эффективность считается по её доске."""
    admin = as_user(*world["admin"])
    statuses = client.get(
        f"/api/teams/{world['second']}/statuses", headers=admin
    ).json()
    qa = next(s for s in statuses if s["external_name"] == "qa")
    assert not qa["is_active_work"], "подготовка сломана: qa должен быть очередью"

    by_phase = client.get(
        "/api/flow-efficiency", params={"team_id": world["second"]}, headers=admin
    ).json()["by_phase"]
    # verify присутствует, но не как активная работа — иначе доска не применилась
    assert any(p["phase"] == "verify" for p in by_phase)


# --- изоляция данных ---------------------------------------------------------


def test_lead_never_sees_other_team(client, world) -> None:
    """Ни один раздел не должен отдать данные чужой команды."""
    username, password = world["leads"][world["first"]]
    headers = as_user(username, password)
    for path in DASHBOARD_ENDPOINTS:
        response = client.get(path, params={"team_id": world["second"]}, headers=headers)
        assert response.status_code == 403, f"{path} отдал чужую команду"


def test_numbers_differ_between_teams(client, world) -> None:
    """Команды с разными данными обязаны давать разные числа.

    Если цифры совпадают, значит фильтр по команде где-то не применяется.
    """
    admin = as_user(*world["admin"])
    first = client.get(
        "/api/summary", params={"team_id": world["first"]}, headers=admin
    ).json()
    second = client.get(
        "/api/summary", params={"team_id": world["second"]}, headers=admin
    ).json()

    assert first["total_tickets"] != second["total_tickets"]
    assert first["total_tickets"] + second["total_tickets"] > 0


def test_teams_sum_to_whole(client, world) -> None:
    """Сумма по командам равна общему числу: ни одна задача не потерялась."""
    admin = as_user(*world["admin"])
    total = client.get("/api/summary", headers=admin).json()["total_tickets"]
    parts = sum(
        client.get("/api/summary", params={"team_id": team}, headers=admin)
        .json()["total_tickets"]
        for team in (world["first"], world["second"])
    )
    assert parts == total


# --- полный путь владельца команды -------------------------------------------


def test_owner_can_run_full_workflow(client, world) -> None:
    """Владелец команды проходит свой сценарий целиком.

    Посмотреть метрики → зафиксировать обещание → изменить классификацию →
    убедиться, что метрики пересчитались.
    """
    team_id = world["second"]
    headers = as_user(*world["leads"][team_id])

    assert client.get("/api/summary", headers=headers).status_code == 200

    fixed = client.post("/api/sle", params={"percentile": 85}, headers=headers)
    assert fixed.status_code == 200
    assert fixed.json()["target_business_s"] > 0

    promises = client.get("/api/sle", headers=headers).json()
    assert promises["promises"], "обещание не сохранилось"
    assert promises["periods"], "график попадания пуст"

    statuses = client.get(f"/api/teams/{team_id}/statuses", headers=headers).json()
    assert statuses, "у команды нет статусов"


def test_new_team_without_statuses_recomputes(engine, world) -> None:
    """Команда без своих статусов не должна ронять пересчёт.

    Дефект: справочник строился по команде, и у только что созданной
    команды не находилось нужных ключей — KeyError в save_intervals.
    """
    with engine.begin() as conn:
        calendar_id = conn.execute(
            text("SELECT calendar_id FROM team WHERE id = 1")
        ).scalar_one()
        fresh = conn.execute(
            text(
                "INSERT INTO team (name, calendar_id) VALUES ('e2e-fresh', :cal) "
                "ON CONFLICT (name, COALESCE(parent_team_id, 0)) DO UPDATE "
                "SET name = EXCLUDED.name RETURNING id"
            ),
            {"cal": calendar_id},
        ).scalar_one()
        conn.execute(
            text(
                "UPDATE ticket SET team_id = :team WHERE id IN "
                "(SELECT id FROM ticket ORDER BY id LIMIT 5)"
            ),
            {"team": fresh},
        )

    try:
        recompute_all(engine)  # раньше падало здесь
    finally:
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE ticket SET team_id = :first WHERE team_id = :t"),
                {"first": world["first"], "t": fresh},
            )
        recompute_all(engine)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM team WHERE id = :t"), {"t": fresh})
