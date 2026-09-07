"""Сценарии, которые пользователь проходит целиком.

Не отдельные экраны, а путь: настроил → увидел, как изменились цифры.
Именно на стыке настройки и метрик находились дефекты, которые ни API-тесты,
ни проверка одной страницы не ловили.
"""

from __future__ import annotations

import httpx
from tests.browser.conftest import ADMIN, auth_header, charts_rendered, open_tab


def read_efficiency(base_url: str, team_id: int) -> float:
    return httpx.get(
        f"{base_url}/api/flow-efficiency",
        params={"team_id": team_id},
        headers=auth_header(ADMIN),
    ).json()["efficiency"]


def test_status_toggle_changes_metrics(lead_one_page, base_url, world) -> None:
    """Переключение статуса в интерфейсе меняет метрики.

    Главный сценарий владельца команды: он решает, что `qa` — это ожидание,
    и сразу видит честную эффективность вместо завышенной.
    """
    team_id = world["first"]
    before = read_efficiency(base_url, team_id)

    open_tab(lead_one_page, "settings")
    row = lead_one_page.locator(".status-row").filter(has_text="qa").first
    checkbox = row.locator('[data-field="is_active_work"]')
    assert checkbox.is_checked(), "подготовка сломана: qa должен быть работой"

    checkbox.uncheck()
    # пересчёт идёт на сервере, поэтому ждём именно изменения цифры
    lead_one_page.wait_for_timeout(3000)
    after = read_efficiency(base_url, team_id)

    try:
        assert after < before, (
            f"перевод qa в очередь не снизил эффективность: {before} → {after}"
        )
    finally:
        checkbox.check()
        lead_one_page.wait_for_timeout(3000)


def test_status_change_does_not_touch_other_team(
    admin_page, base_url, world
) -> None:
    """Настройка одной команды не меняет метрики другой.

    Дефект, найденный вручную: пересчёт брал доску первой команды
    и применял ко всем.
    """
    other_before = read_efficiency(base_url, world["second"])

    open_tab(admin_page, "settings")
    row = admin_page.locator(".status-row").filter(has_text="release").first
    checkbox = row.locator('[data-field="is_active_work"]')
    was_checked = checkbox.is_checked()
    checkbox.set_checked(not was_checked)
    admin_page.wait_for_timeout(3000)

    try:
        other_after = read_efficiency(base_url, world["second"])
        assert abs(other_after - other_before) < 0.0001, (
            "настройка чужой команды изменила метрики второй"
        )
    finally:
        checkbox.set_checked(was_checked)
        admin_page.wait_for_timeout(3000)


def test_fix_sle_fills_promises_tab(lead_two_page) -> None:
    """Владелец фиксирует обещание и видит график попадания.

    До фиксации вкладка пуста — это и был вопрос пользователя.
    """
    open_tab(lead_two_page, "sle")
    lead_two_page.click("#sle-fix-btn")
    lead_two_page.wait_for_timeout(3000)

    promises = lead_two_page.locator("#sle-promises")
    assert "85%" in promises.inner_text(), "обещание не появилось в таблице"
    assert charts_rendered(lead_two_page, "sle") >= 2


def test_completion_filter_changes_numbers(lead_one_page) -> None:
    """Переключатель «готово» меняет время цикла на всех вкладках."""
    open_tab(lead_one_page, "overview")
    tile = lead_one_page.locator(".tile").filter(has_text="Время цикла").first
    before = tile.locator(".value").inner_text()

    lead_one_page.select_option("#f-completion", "work_done")
    lead_one_page.wait_for_timeout(2500)
    after = tile.locator(".value").inner_text()

    assert before != after, (
        f"граница «готово» не повлияла на время цикла: {before} = {after}"
    )


def test_unit_toggle_changes_numbers(lead_one_page) -> None:
    """Переключение рабочих и календарных часов меняет цифры."""
    open_tab(lead_one_page, "overview")
    tile = lead_one_page.locator(".tile").filter(has_text="Время цикла").first
    business = tile.locator(".value").inner_text()

    lead_one_page.click("#u-calendar")
    lead_one_page.wait_for_timeout(2500)
    calendar = tile.locator(".value").inner_text()

    assert business != calendar


def test_filter_by_type_narrows_data(lead_one_page) -> None:
    """Фильтр по типу задач сокращает выборку."""
    open_tab(lead_one_page, "overview")
    total = lead_one_page.locator(".tile").first.locator(".value").inner_text()

    options = lead_one_page.locator("#f-type option").all_inner_texts()
    concrete = next(o for o in options if o and "Все" not in o)
    lead_one_page.select_option("#f-type", label=concrete)
    lead_one_page.wait_for_timeout(2500)

    filtered = lead_one_page.locator(".tile").first.locator(".value").inner_text()
    assert int(filtered) < int(total), "фильтр по типу не сузил выборку"


def test_import_dialog_explains_format(lead_one_page) -> None:
    """Окно импорта сначала объясняет формат, потом просит файл."""
    lead_one_page.goto("/", wait_until="networkidle")
    lead_one_page.click("#import-btn")
    lead_one_page.wait_for_selector("#import-modal:not([hidden])")

    body = lead_one_page.locator("#import-modal .modal-body").inner_text()
    assert "CSV" in body
    assert "NDJSON" in body
    assert lead_one_page.locator(".sample").count() >= 2, "нет примеров формата"
    assert lead_one_page.locator("#import-file").is_visible()


def test_import_source_tabs_switch(lead_one_page) -> None:
    """Переключение «Файл» и «Подключить Jira» показывает разные панели.

    Дефект, найденный вручную: кнопка «Загрузить» из панели файла висела
    поверх панели Jira из-за неправильно закрытого div.
    """
    lead_one_page.goto("/", wait_until="networkidle")
    lead_one_page.click("#import-btn")
    lead_one_page.wait_for_selector("#import-modal:not([hidden])")

    assert lead_one_page.locator("#import-submit").is_visible()
    assert lead_one_page.locator("#jira-check").is_hidden()

    lead_one_page.click('.source-tab[data-source="jira"]')
    lead_one_page.wait_for_timeout(300)

    assert lead_one_page.locator("#jira-check").is_visible()
    assert lead_one_page.locator("#import-submit").is_hidden(), (
        "кнопка загрузки файла видна на панели Jira"
    )


def test_jira_connection_error_is_readable(lead_one_page) -> None:
    """Недоступная Jira объясняется человеку, а не молчит."""
    lead_one_page.goto("/", wait_until="networkidle")
    lead_one_page.click("#import-btn")
    lead_one_page.click('.source-tab[data-source="jira"]')
    lead_one_page.fill("#jira-url", "http://127.0.0.1:1")
    lead_one_page.click("#jira-check")

    lead_one_page.wait_for_function(
        "document.getElementById('jira-check-result').textContent.includes('Не получилось')",
        timeout=30000,
    )
    assert lead_one_page.locator("#jira-config").is_hidden()


def test_ticket_details_open(lead_one_page) -> None:
    """Клик по задаче открывает её карточку."""
    open_tab(lead_one_page, "tickets")
    # именно в активной панели: элементы с data-ticket есть и на других
    # вкладках, но они скрыты и кликнуть по ним нельзя
    ticket = lead_one_page.locator(
        '.panel[data-panel="tickets"] [data-ticket]'
    ).first
    ticket.wait_for(state="visible", timeout=20000)
    ticket.click()

    lead_one_page.wait_for_selector("#modal:not([hidden])", timeout=20000)
    assert lead_one_page.locator("#modal-title").inner_text().strip()


def test_chart_data_dialog_opens(lead_one_page) -> None:
    """Кнопка «Данные» показывает числа, стоящие за графиком.

    Берём вкладку времени цикла: там данные есть всегда, тогда как
    блокировок в выборке может не оказаться вовсе.
    """
    open_tab(lead_one_page, "cycle")
    button = lead_one_page.locator(
        '.panel[data-panel="cycle"] [data-data-for]'
    ).first
    button.wait_for(state="visible", timeout=20000)
    button.click()

    lead_one_page.wait_for_selector("#modal:not([hidden])", timeout=20000)
    body = lead_one_page.locator("#modal-body")
    assert body.inner_text().strip(), "окно данных пусто"


def test_export_downloads_file(lead_one_page) -> None:
    """Выгрузка среза отдаёт файл."""
    lead_one_page.goto("/", wait_until="networkidle")
    lead_one_page.once("dialog", lambda dialog: dialog.dismiss())  # обезличенный

    with lead_one_page.expect_download(timeout=60000) as download:
        lead_one_page.click("#export-btn")
    result = download.value
    assert result.suggested_filename.endswith(".ndjson")
