"""Разграничение доступа глазами браузера.

API-тесты проверяют коды ответов. Здесь важно другое: что пользователь
физически не видит чужого — ни в списках, ни в подсказках, ни во вкладках,
которые ему не положены.
"""

from __future__ import annotations

import httpx
from tests.browser.conftest import ADMIN, LEAD_ONE, auth_header, open_tab


def test_admin_tab_hidden_from_lead(lead_one_page) -> None:
    """Владелец команды не должен видеть вкладку админки."""
    lead_one_page.goto("/", wait_until="networkidle")
    admin_tab = lead_one_page.locator('.tab[data-tab="admin"]')
    assert admin_tab.count() == 1
    assert admin_tab.is_hidden(), "вкладка админки видна обычному пользователю"


def test_admin_sees_admin_tab(admin_page) -> None:
    admin_page.goto("/", wait_until="networkidle")
    assert admin_page.locator('.tab[data-tab="admin"]').is_visible()


def test_lead_sees_only_own_team(lead_one_page, world) -> None:
    """В переключателе команд — только своя.

    Название чужой команды тоже информация: по нему видно структуру компании.
    """
    open_tab(lead_one_page, "settings")
    rows = lead_one_page.locator("#teams-table tbody tr")
    assert rows.count() == 1, "видна больше чем одна команда"
    assert "фронтенд" not in rows.first.inner_text()


def test_admin_sees_both_teams(admin_page) -> None:
    open_tab(admin_page, "settings")
    text = admin_page.locator("#teams-table").inner_text()
    assert "бэкенд" in text
    assert "фронтенд" in text


def test_team_selector_hidden_for_single_team(lead_one_page) -> None:
    """Переключатель не нужен тому, у кого одна команда."""
    lead_one_page.goto("/", wait_until="networkidle")
    assert lead_one_page.locator("#f-team").is_hidden()


def test_team_selector_visible_for_admin(admin_page) -> None:
    admin_page.goto("/", wait_until="networkidle")
    assert admin_page.locator("#f-team").is_visible()


def test_profile_shows_own_teams(lead_two_page) -> None:
    """Профиль показывает, к чему есть доступ."""
    lead_two_page.goto("/", wait_until="networkidle")
    lead_two_page.click("#user-button")
    lead_two_page.wait_for_selector("#user-dropdown:not([hidden])")

    assert "Лид фронтенда" in lead_two_page.locator("#user-name").inner_text()
    teams = lead_two_page.locator("#user-teams").inner_text()
    assert "владелец" in teams


def test_admin_profile_says_all_teams(admin_page) -> None:
    admin_page.goto("/", wait_until="networkidle")
    admin_page.click("#user-button")
    admin_page.wait_for_selector("#user-dropdown:not([hidden])")
    assert "все команды" in admin_page.locator("#user-teams").inner_text().lower()


def test_no_access_without_credentials(base_url) -> None:
    """Без входа дашборд не отдаётся вовсе."""
    response = httpx.get(base_url, follow_redirects=False)
    assert response.status_code == 401
    assert "Basic" in response.headers.get("WWW-Authenticate", "")


def test_wrong_password_rejected(base_url) -> None:
    response = httpx.get(base_url, headers=auth_header((LEAD_ONE[0], "неверный")))
    assert response.status_code == 401


def test_health_open_for_monitoring(base_url) -> None:
    """Проверка живости нужна мониторингу до всякого входа."""
    assert httpx.get(f"{base_url}/api/health").status_code == 200


def test_lead_cannot_reach_admin_api(base_url) -> None:
    """Скрытая вкладка — удобство; настоящая защита на сервере."""
    response = httpx.get(
        f"{base_url}/api/admin/usage", headers=auth_header(LEAD_ONE)
    )
    assert response.status_code == 403


def test_lead_cannot_read_other_team_data(base_url, world) -> None:
    """Прямой запрос чужой команды в обход интерфейса."""
    response = httpx.get(
        f"{base_url}/api/summary",
        params={"team_id": world["second"]},
        headers=auth_header(LEAD_ONE),
    )
    assert response.status_code == 403


def test_teams_show_different_numbers(base_url, world) -> None:
    """У команд разные данные — иначе фильтр где-то не применяется."""
    first = httpx.get(
        f"{base_url}/api/summary",
        params={"team_id": world["first"]},
        headers=auth_header(ADMIN),
    ).json()
    second = httpx.get(
        f"{base_url}/api/summary",
        params={"team_id": world["second"]},
        headers=auth_header(ADMIN),
    ).json()
    assert first["total_tickets"] != second["total_tickets"]
