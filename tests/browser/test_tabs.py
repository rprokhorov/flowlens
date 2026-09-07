"""Каждая вкладка открывается и показывает данные.

Закрывает класс дефектов, невидимый для API: эндпоинт отвечает 200, а вкладка
пуста или падает с ошибкой в консоли. Так проявлялись закэшированный
dashboard.js (все вкладки показывали «Обзор») и незарегистрированный
эндпоинт (пустая панель без единого сообщения).
"""

from __future__ import annotations

import pytest
from tests.browser.conftest import charts_rendered, open_tab, page_errors, panel_error

# Вкладки и минимум графиков, который на них обязан отрисоваться.
# Ноль означает, что вкладка состоит из таблиц и текста.
TABS: list[tuple[str, int]] = [
    ("overview", 0),
    ("flow", 4),
    ("cycle", 1),
    ("phases", 4),
    ("wip", 1),
    ("blockers", 1),
    ("sle", 3),
    ("people", 1),
    ("forecast", 1),
    ("quality", 0),
    ("settings", 0),
    ("tickets", 0),
]


@pytest.mark.parametrize(("tab", "min_charts"), TABS)
def test_tab_opens_without_errors(lead_one_page, tab: str, min_charts: int) -> None:
    """Вкладка открывается, рисует графики и не роняет ошибок в консоль."""
    open_tab(lead_one_page, tab)

    assert panel_error(lead_one_page, tab) is None, f"ошибка на вкладке {tab}"
    assert charts_rendered(lead_one_page, tab) >= min_charts, (
        f"на вкладке {tab} отрисовано меньше графиков, чем ожидалось"
    )
    assert not page_errors(lead_one_page), (
        f"ошибки JS на вкладке {tab}: {page_errors(lead_one_page)[:3]}"
    )


@pytest.mark.parametrize(("tab", "_min_charts"), TABS)
def test_tab_works_for_second_team(lead_two_page, tab: str, _min_charts: int) -> None:
    """То же самое у второй команды.

    Дефект нагрузки выглядел именно так: у одной команды вкладка с данными,
    у другой — пустая, при одинаковом ответе 200.
    """
    open_tab(lead_two_page, tab)
    assert panel_error(lead_two_page, tab) is None, f"ошибка на вкладке {tab}"
    assert not page_errors(lead_two_page)


def test_tabs_show_different_content(lead_one_page) -> None:
    """Вкладки не должны показывать одно и то же.

    Закэшированный dashboard.js сводил незнакомые имена к «Обзору»,
    и три разные вкладки выглядели одинаково.
    """
    seen: dict[str, str] = {}
    for tab in ("overview", "blockers", "sle", "settings"):
        open_tab(lead_one_page, tab)
        heading = lead_one_page.locator(f'.panel[data-panel="{tab}"] h2').first
        seen[tab] = heading.inner_text()

    assert len(set(seen.values())) == len(seen), f"вкладки показывают одно и то же: {seen}"


def test_only_one_panel_active(lead_one_page) -> None:
    """Активна ровно одна панель: иначе содержимое накладывается."""
    for tab in ("flow", "phases", "wip"):
        open_tab(lead_one_page, tab)
        assert lead_one_page.locator(".panel.active").count() == 1


def test_people_tab_not_empty(lead_two_page) -> None:
    """Нагрузка второй команды не пуста.

    Ровно этот дефект нашёл пользователь: вкладка отвечала 200, но список
    людей был пуст, потому что вся нагрузка писалась в первую команду.
    """
    open_tab(lead_two_page, "people")
    assert charts_rendered(lead_two_page, "people") >= 1
    rows = lead_two_page.locator('.panel[data-panel="people"] table tbody tr')
    assert rows.count() > 0, "список людей пуст"


def test_settings_lists_statuses(lead_one_page) -> None:
    """Настройки показывают статусы команды с переключателями."""
    open_tab(lead_one_page, "settings")
    rows = lead_one_page.locator(".status-row")
    assert rows.count() >= 5, "статусы команды не показаны"
    assert lead_one_page.locator('.status-row [data-field="is_active_work"]').count() >= 5


def test_static_assets_versioned(lead_one_page) -> None:
    """Статика подключается с версией.

    Без этого браузер отдаёт старый dashboard.js, и новые вкладки молча
    сводятся к «Обзору» — пользователь видит одинаковые страницы.
    """
    lead_one_page.goto("/", wait_until="networkidle")
    # через DOM, а не локатор: script не является отображаемым элементом
    src = lead_one_page.evaluate(
        "document.querySelector('script[src*=\"dashboard.js\"]').getAttribute('src')"
    )
    assert "?v=" in src, f"dashboard.js подключён без версии: {src}"
