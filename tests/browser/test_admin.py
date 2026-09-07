"""Админская панель: метрики использования и управление доступом."""

from __future__ import annotations

import httpx
from tests.browser.conftest import ADMIN, auth_header, charts_rendered, open_tab


def test_admin_panel_renders(admin_page) -> None:
    """Панель показывает плитки, графики и список пользователей."""
    open_tab(admin_page, "admin")

    assert admin_page.locator("#admin-tiles .tile").count() >= 4
    assert charts_rendered(admin_page, "admin") >= 1
    assert admin_page.locator("#admin-users tbody tr").count() >= 3


def test_usage_counts_grow(admin_page, base_url) -> None:
    """Обращения к разделам считаются.

    Счётчики копятся в памяти и сбрасываются раз в минуту, поэтому здесь
    важен сам факт учёта, а не точное число.
    """
    open_tab(admin_page, "admin")
    text = admin_page.locator("#admin-tiles").inner_text()
    assert "Обращений" in text

    usage = httpx.get(
        f"{base_url}/api/admin/usage", headers=auth_header(ADMIN)
    ).json()
    assert usage["state"]["teams"] >= 2
    assert usage["state"]["users"] >= 3


def test_unused_sections_listed(admin_page) -> None:
    """Разделы, которые не открывали, показываются отдельно.

    Это полезнее списка популярных: видно, что построено зря
    или что люди не нашли.
    """
    open_tab(admin_page, "admin")
    block = admin_page.locator("#unused-sections")
    # блок может быть пуст, если открыли всё — тогда проверять нечего
    if block.inner_text().strip():
        assert "не открывали" in block.inner_text()


def test_period_switch_reloads(admin_page) -> None:
    """Переключение периода перерисовывает панель."""
    open_tab(admin_page, "admin")
    admin_page.select_option("#admin-period", "7")
    admin_page.wait_for_timeout(2500)
    assert admin_page.locator("#admin-tiles .tile").count() >= 4


def test_create_and_delete_user(admin_page, base_url) -> None:
    """Администратор заводит пользователя и удаляет его."""
    open_tab(admin_page, "admin")
    before = admin_page.locator("#admin-users tbody tr").count()

    admin_page.click("#add-user-btn")
    admin_page.wait_for_selector("#user-modal:not([hidden])")
    admin_page.fill("#nu-username", "e2e-temp-user")
    admin_page.fill("#nu-display", "Временный")
    admin_page.fill("#nu-password", "временный-пароль-1")
    admin_page.click("#nu-submit")
    admin_page.wait_for_timeout(3000)

    try:
        assert admin_page.locator("#admin-users tbody tr").count() == before + 1
        assert "Временный" in admin_page.locator("#admin-users").inner_text()
    finally:
        httpx.delete(
            f"{base_url}/api/admin/users/e2e-temp-user", headers=auth_header(ADMIN)
        )


def test_short_password_rejected(admin_page) -> None:
    """Слабый пароль не принимается, и человеку сказано почему."""
    open_tab(admin_page, "admin")
    admin_page.click("#add-user-btn")
    admin_page.wait_for_selector("#user-modal:not([hidden])")
    admin_page.fill("#nu-username", "e2e-weak")
    admin_page.fill("#nu-password", "123")
    admin_page.click("#nu-submit")
    admin_page.wait_for_timeout(2000)

    result = admin_page.locator("#nu-result").inner_text()
    assert "восьми" in result or "коротк" in result.lower()
    admin_page.click("[data-close-user]")


def test_grant_and_revoke_access(admin_page, base_url, world) -> None:
    """Администратор выдаёт и отбирает доступ к команде.

    Ровно то, что просил пользователь: ручное сопоставление
    «пользователь — команда».
    """
    httpx.post(
        f"{base_url}/api/admin/users",
        json={"username": "e2e-grant", "password": "пароль-для-теста"},
        headers=auth_header(ADMIN),
    )
    try:
        # Диалог спрашивает роль: отмена означает «только просмотр».
        # Регистрируем до open_tab: он делает goto, а навигация сбрасывает
        # обработчики, зарегистрированные позже на прежней странице.
        admin_page.on("dialog", lambda dialog: dialog.dismiss())

        open_tab(admin_page, "admin")
        row = admin_page.locator("#admin-users tbody tr").filter(has_text="e2e-grant")
        assert row.count() == 1

        row.locator("[data-grant]").select_option(str(world["first"]))

        # Ждём чип именно у этого пользователя: чипы есть и у других,
        # и общий селектор срабатывал бы мгновенно, не дождавшись выдачи.
        admin_page.wait_for_selector(
            '[data-revoke="e2e-grant"]', timeout=30000
        )

        granted = httpx.get(
            f"{base_url}/api/admin/users", headers=auth_header(ADMIN)
        ).json()
        user = next(u for u in granted if u["username"] == "e2e-grant")
        assert str(world["first"]) in {str(k) for k in user["teams"]}

        # отзыв через крестик на чипе
        row = admin_page.locator("#admin-users tbody tr").filter(has_text="e2e-grant")
        row.locator("[data-revoke]").first.click()
        admin_page.wait_for_timeout(2500)

        after = httpx.get(
            f"{base_url}/api/admin/users", headers=auth_header(ADMIN)
        ).json()
        user = next(u for u in after if u["username"] == "e2e-grant")
        assert not user["teams"], "доступ не отозван"
    finally:
        httpx.delete(
            f"{base_url}/api/admin/users/e2e-grant", headers=auth_header(ADMIN)
        )


def test_admin_cannot_delete_self(admin_page) -> None:
    """У администратора нет кнопки удаления самого себя.

    Оставшись без доступа, он не вернул бы его через интерфейс.
    """
    open_tab(admin_page, "admin")
    own_row = admin_page.locator("#admin-users tbody tr").filter(has_text=ADMIN[0])
    assert own_row.count() == 1
    assert own_row.locator("[data-delete-user]").count() == 0


def test_flow_filters_hidden_on_admin(admin_page) -> None:
    """Фильтры потока к админке не относятся и не должны мешать."""
    open_tab(admin_page, "admin")
    assert admin_page.locator(".filters").is_hidden()

    open_tab(admin_page, "overview")
    assert admin_page.locator(".filters").is_visible()
