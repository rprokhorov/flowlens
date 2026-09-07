"""Инфраструктура браузерных тестов.

Эти тесты проверяют то, чего не видит API: отрисовался ли график, не пуста
ли вкладка, работает ли кнопка. Такие дефекты находились только скриншотами
вручную — например, кнопка «Загрузить» висела поверх чужой панели, а вкладки
показывали одно и то же из-за закэшированного JS.

Сервер поднимается свой, на отдельном порту: тесты не должны зависеть
от запущенного docker и не должны портить его данные.
"""

from __future__ import annotations

import base64
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import suppress
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from flowlens import auth
from flowlens.db import make_engine
from flowlens.pipeline import recompute_all, seed_demo

ADMIN = ("e2e-admin", "admin-pass-1234")
LEAD_ONE = ("e2e-lead-one", "lead-one-1234")
LEAD_TWO = ("e2e-lead-two", "lead-two-1234")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture(scope="session")
def engine():
    try:
        eng = make_engine()
        with eng.begin() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres недоступен: {exc}")
    return eng


@pytest.fixture(scope="session")
def world(engine) -> Iterator[dict[str, Any]]:
    """Две команды с разными данными, настройками и владельцами.

    Повторяет реальную установку: данные разделены, классификация `qa`
    у команд разная, у каждой свой владелец, плюс администратор.
    """
    seed_demo(engine, ticket_count=240, months=7, seed=9090)

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_user WHERE username LIKE 'e2e-%'"))
        first = conn.execute(text("SELECT min(id) FROM team")).scalar_one()
        conn.execute(
            text("UPDATE team SET name = 'бэкенд' WHERE id = :id"), {"id": first}
        )
        calendar_id = conn.execute(
            text("SELECT calendar_id FROM team WHERE id = :id"), {"id": first}
        ).scalar_one()
        second = conn.execute(
            text(
                "INSERT INTO team (name, calendar_id) VALUES ('фронтенд', :cal) "
                "ON CONFLICT (name, COALESCE(parent_team_id, 0)) DO UPDATE "
                "SET name = EXCLUDED.name RETURNING id"
            ),
            {"cal": calendar_id},
        ).scalar_one()
        source_id = conn.execute(
            text("SELECT min(source_id) FROM workflow_status")
        ).scalar_one()

        # у второй команды qa — очередь на тестирование, а не code review
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
        conn.execute(
            text(
                "UPDATE ticket SET team_id = :team WHERE id IN "
                "(SELECT id FROM ticket ORDER BY id LIMIT 80)"
            ),
            {"team": second},
        )

    recompute_all(engine)

    admin = auth.create_user(
        engine, username=ADMIN[0], password=ADMIN[1], is_admin=True,
        display_name="Администратор",
    )
    lead_one = auth.create_user(
        engine, username=LEAD_ONE[0], password=LEAD_ONE[1], display_name="Лид бэкенда"
    )
    auth.grant_access(engine, user_id=lead_one.id, team_id=first, role="owner")
    lead_two = auth.create_user(
        engine, username=LEAD_TWO[0], password=LEAD_TWO[1], display_name="Лид фронтенда"
    )
    auth.grant_access(engine, user_id=lead_two.id, team_id=second, role="owner")

    yield {
        "first": first,
        "second": second,
        "admin_id": admin.id,
        "lead_one_id": lead_one.id,
        "lead_two_id": lead_two.id,
    }

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ticket SET team_id = :first WHERE team_id = :t"),
            {"first": first, "t": second},
        )
    recompute_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_user WHERE username LIKE 'e2e-%'"))
        for table in ("service_level_expectation", "person_workload_daily", "team_source"):
            conn.execute(text(f"DELETE FROM {table} WHERE team_id = :t"), {"t": second})
        conn.execute(text("DELETE FROM workflow_status WHERE team_id = :t"), {"t": second})
        conn.execute(text("DELETE FROM team WHERE id = :t"), {"t": second})


@pytest.fixture(scope="session")
def base_url(world) -> Iterator[str]:
    """Свой экземпляр сервиса на свободном порту.

    Отдельный процесс, а не TestClient: браузеру нужен настоящий HTTP,
    и только так проверяется реальная отдача статики и заголовков.
    """
    port = free_port()
    env = {
        **os.environ,
        "FLOWLENS_AUTH_DISABLED": "0",
        "FLOWLENS_SECRET_KEY": "browser-tests-key",
        "FLOWLENS_SCHEDULER": "0",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "flowlens.api.app:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    url = f"http://127.0.0.1:{port}"

    deadline = time.time() + 40
    while time.time() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read().decode() if process.stderr else ""
            pytest.fail(f"сервис не запустился: {stderr[-600:]}")
        try:
            if httpx.get(f"{url}/api/health", timeout=1).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    else:
        process.terminate()
        pytest.fail("сервис не ответил за 40 секунд")

    yield url

    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()


def auth_header(credentials: tuple[str, str]) -> dict[str, str]:
    token = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture(scope="session")
def browser() -> Iterator[Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        instance = playwright.chromium.launch()
        yield instance
        instance.close()


def make_page(browser: Any, base_url: str, credentials: tuple[str, str]) -> Any:
    """Страница, вошедшая под указанным пользователем.

    Basic передаётся заголовком: диалог браузера иначе не закрыть,
    а поведение при этом ровно то же.
    """
    context = browser.new_context(
        base_url=base_url,
        extra_http_headers=auth_header(credentials),
        viewport={"width": 1440, "height": 1000},
    )
    page = context.new_page()
    # пересчёт метрик после смены настройки идёт несколько секунд
    page.set_default_timeout(30000)
    # Ошибки копятся на самом объекте страницы, а не в общем словаре по id():
    # Python переиспользует адреса освобождённых объектов, и ошибки одного
    # теста прилипали к странице следующего.
    errors: list[str] = []
    page._flowlens_errors = errors  # noqa: SLF001
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.on(
        "console",
        lambda message: errors.append(message.text) if message.type == "error" else None,
    )
    return page


def page_errors(page: Any) -> list[str]:
    """Ошибки JS, накопленные этой страницей."""
    return getattr(page, "_flowlens_errors", [])


@pytest.fixture
def admin_page(browser, base_url) -> Iterator[Any]:
    page = make_page(browser, base_url, ADMIN)
    yield page
    page.context.close()


@pytest.fixture
def lead_one_page(browser, base_url) -> Iterator[Any]:
    page = make_page(browser, base_url, LEAD_ONE)
    yield page
    page.context.close()


@pytest.fixture
def lead_two_page(browser, base_url) -> Iterator[Any]:
    page = make_page(browser, base_url, LEAD_TWO)
    yield page
    page.context.close()


def open_tab(page: Any, tab: str, *, timeout: int = 20000) -> None:
    """Открыть вкладку и дождаться, пока она отрисуется.

    networkidle недостаточно: графики рисуются уже ПОСЛЕ ответа сети,
    и проверка сразу после него видит пустые контейнеры. Ждём появления
    первого canvas, а если графиков на вкладке нет — просто паузу.
    """
    page.goto("/", wait_until="networkidle")
    page.click(f'.tab[data-tab="{tab}"]')
    page.wait_for_selector(f'.panel[data-panel="{tab}"].active', timeout=timeout)
    page.wait_for_load_state("networkidle")

    charts = page.locator(f'.panel[data-panel="{tab}"] .chart')
    if charts.count():
        # вкладка может честно не иметь данных — это проверяет сам тест
        with suppress(Exception):
            page.wait_for_selector(
                f'.panel[data-panel="{tab}"] .chart canvas', timeout=timeout
            )
    page.wait_for_timeout(400)


def charts_rendered(page: Any, tab: str) -> int:
    """Сколько графиков реально отрисовано на вкладке."""
    return page.locator(f'.panel[data-panel="{tab}"] .chart canvas').count()


def panel_error(page: Any, tab: str) -> str | None:
    node = page.locator(f'.panel[data-panel="{tab}"] .error')
    return node.first.inner_text() if node.count() else None
