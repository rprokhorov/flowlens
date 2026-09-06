"""Тесты метрик использования и админской панели."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from flowlens import usage
from flowlens.db import make_engine


@pytest.fixture(scope="module")
def engine():
    try:
        eng = make_engine()
        with eng.begin() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres недоступен: {exc}")
    return eng


@pytest.fixture(autouse=True)
def clean(engine):
    usage._buffer.clear()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM usage_daily"))
    yield
    usage._buffer.clear()
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM usage_daily"))


@pytest.fixture
def user_id(engine) -> int:
    from flowlens import auth

    user = auth.create_user(engine, username="usage-test", password="pass-1234")
    yield user.id
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_user WHERE username = 'usage-test'"))


# --- отнесение путей к разделам ----------------------------------------------


@pytest.mark.parametrize(
    ("path", "section"),
    [
        ("/api/summary", "Обзор"),
        ("/api/blockers", "Блокировки"),
        ("/api/sle", "Обещания"),
        ("/api/tickets/PROJ-1", "Задачи"),
        ("/api/jira/check", "Импорт"),
    ],
)
def test_section_mapping(path: str, section: str) -> None:
    assert usage.section_for(path) == section


@pytest.mark.parametrize(
    "path", ["/api/health", "/api/me", "/static/dashboard.js", "/", "/api/logout"]
)
def test_service_paths_not_counted(path: str) -> None:
    """Служебные обращения говорят о работе браузера, а не о человеке."""
    assert usage.section_for(path) is None


# --- накопление и сброс ------------------------------------------------------


def test_record_accumulates_in_memory(user_id: int) -> None:
    """Запись на каждое обращение замедлила бы дашборд ради статистики."""
    usage.record(user_id, "/api/summary")
    usage.record(user_id, "/api/summary")
    assert sum(usage._buffer.values()) == 2


def test_anonymous_not_counted() -> None:
    """У анонима id=0 — считать нечего."""
    usage.record(0, "/api/summary")
    assert not usage._buffer


def test_flush_writes_and_clears(engine, user_id: int) -> None:
    usage.record(user_id, "/api/summary")
    usage.record(user_id, "/api/blockers")
    assert usage.flush(engine) == 2
    assert not usage._buffer

    with engine.begin() as conn:
        total = conn.execute(text("SELECT sum(hits) FROM usage_daily")).scalar_one()
    assert total == 2


def test_flush_adds_to_existing(engine, user_id: int) -> None:
    """Второй сброс за те же сутки не затирает первый."""
    usage.record(user_id, "/api/summary")
    usage.flush(engine)
    usage.record(user_id, "/api/summary")
    usage.flush(engine)

    with engine.begin() as conn:
        hits = conn.execute(text("SELECT sum(hits) FROM usage_daily")).scalar_one()
    assert hits == 2


def test_empty_flush_is_cheap(engine) -> None:
    assert usage.flush(engine) == 0


# --- сводка ------------------------------------------------------------------


def test_overview_counts_sections(engine, user_id: int) -> None:
    for _ in range(3):
        usage.record(user_id, "/api/summary")
    usage.record(user_id, "/api/blockers")

    data = usage.overview(engine, days=30)
    sections = {item["section"]: item["hits"] for item in data["sections"]}
    assert sections["Обзор"] == 3
    assert sections["Блокировки"] == 1
    assert data["active_people"] == 1


def test_overview_reports_state(engine) -> None:
    """Состояние продукта: сколько команд, данных, работают ли синхронизации."""
    state = usage.overview(engine)["state"]
    assert state["teams"] >= 1
    assert "failed_syncs" in state


def test_overview_respects_period(engine, user_id: int) -> None:
    old = date.today() - timedelta(days=60)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO usage_daily (day, user_id, section, hits) "
                "VALUES (:day, :uid, 'Обзор', 5)"
            ),
            {"day": old, "uid": user_id},
        )
    assert usage.overview(engine, days=7)["hits"] == 0
    assert usage.overview(engine, days=90)["hits"] == 5


def test_unused_sections_listed(engine, user_id: int) -> None:
    """Неиспользуемое важнее популярного: показывает, что построено зря."""
    usage.record(user_id, "/api/summary")
    unused = usage.unused_sections(engine)
    assert "Обзор" not in unused
    assert "Прогноз" in unused


def test_record_uses_given_date(engine, user_id: int) -> None:
    moment = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    usage.record(user_id, "/api/summary", moment)
    usage.flush(engine)

    with engine.begin() as conn:
        day = conn.execute(text("SELECT day FROM usage_daily")).scalar_one()
    assert day == date(2026, 1, 15)
