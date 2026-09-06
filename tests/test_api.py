"""Тесты HTTP API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from flowlens.api.app import app, get_engine
from flowlens.db import make_engine
from flowlens.pipeline import recompute_all, seed_demo


@pytest.fixture(scope="module")
def client():
    try:
        engine = make_engine()
        with engine.begin() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres недоступен: {exc}")

    seed_demo(engine, ticket_count=200, months=6, seed=5)
    recompute_all(engine)
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def assert_numeric(value, name: str) -> None:
    """Числа должны приходить числами, а не строками.

    Postgres отдаёт sum() как numeric, что сериализуется в строку —
    график тогда сравнивает значения лексикографически и рисует ерунду.
    """
    assert isinstance(value, int | float), f"{name} пришло как {type(value).__name__}: {value!r}"


# --- доступность -------------------------------------------------------------


def test_health(client) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_dashboard_served(client) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "FlowLens" in response.text


def test_static_assets_served(client) -> None:
    assert client.get("/static/dashboard.js").status_code == 200
    assert client.get("/static/echarts.min.js").status_code == 200


# --- сводка ------------------------------------------------------------------


def test_summary(client) -> None:
    data = client.get("/api/summary").json()
    assert data["p50_cycle_s"] <= data["p85_cycle_s"] <= data["p95_cycle_s"]
    assert data["total_tickets"] > 0
    assert data["open_tickets"] + data["completed"] <= data["total_tickets"] * 2
    for field in ("total_tickets", "open_tickets", "completed", "reopens"):
        assert_numeric(data[field], field)


def test_open_tickets_consistent(client) -> None:
    """Незакрытые задачи считаются по closed_at, который заполняет пересчёт."""
    data = client.get("/api/summary").json()
    assert data["open_tickets"] < data["total_tickets"]


# --- CFD ---------------------------------------------------------------------


def test_cfd_returns_numbers(client) -> None:
    data = client.get("/api/cfd").json()
    assert data["series"]
    for series in data["series"]:
        for value in series["values"][:20]:
            assert_numeric(value, f"cfd.{series['phase']}")


def test_cfd_done_monotonic(client) -> None:
    data = client.get("/api/cfd").json()
    done = next((s for s in data["series"] if s["phase"] == "done"), None)
    assert done is not None
    assert done["values"] == sorted(done["values"])


# --- время цикла -------------------------------------------------------------


def test_cycle_time_percentiles(client) -> None:
    data = client.get("/api/cycle-time").json()
    p = data["percentiles"]
    assert p["p50"] <= p["p70"] <= p["p85"] <= p["p95"]
    for key, value in p.items():
        assert_numeric(value, f"percentiles.{key}")


def test_unit_switch_changes_values(client) -> None:
    business = client.get("/api/cycle-time?unit=business").json()
    calendar = client.get("/api/cycle-time?unit=calendar").json()
    assert calendar["percentiles"]["p50"] > business["percentiles"]["p50"]


def test_confidence_filter(client) -> None:
    everything = client.get("/api/cycle-time?min_confidence=low").json()
    trusted = client.get("/api/cycle-time?min_confidence=high").json()
    assert trusted["count"] <= everything["count"]


def test_type_filter(client) -> None:
    everything = client.get("/api/summary").json()
    bugs = client.get("/api/summary?issue_type=Bug").json()
    assert bugs["total_tickets"] < everything["total_tickets"]


def test_unknown_filter_value_is_safe(client) -> None:
    """Несуществующее значение фильтра даёт пустой результат, а не ошибку."""
    response = client.get("/api/cycle-time?issue_type=НетТакого")
    assert response.status_code == 200
    assert response.json()["count"] == 0


# --- остальные отчёты --------------------------------------------------------


def test_flow_efficiency_numbers(client) -> None:
    data = client.get("/api/flow-efficiency").json()
    assert 0 <= data["efficiency"] <= 1
    for phase in data["by_phase"]:
        assert_numeric(phase["total_s"], f"phase.{phase['phase']}.total_s")


def test_people_load_numbers(client) -> None:
    data = client.get("/api/people").json()
    assert data["people"]
    for person in data["people"]:
        assert_numeric(person["owned_s"], "owned_s")
        assert person["touch_s"] <= person["owned_s"]


def test_aging_wip(client) -> None:
    data = client.get("/api/aging-wip").json()
    ages = [item["age_s"] for item in data["items"]]
    assert ages == sorted(ages, reverse=True)


def test_arrival_throughput(client) -> None:
    data = client.get("/api/arrival-throughput?granularity=week").json()
    assert len(data["arrived"]) == len(data["periods"])
    for value in data["arrived"]:
        assert_numeric(value, "arrived")


def test_quality_report(client) -> None:
    data = client.get("/api/quality").json()
    assert data["total_tickets"] > 0
    assert 0 <= data["trustworthy_pct"] <= 100
    assert data["verdict"]


def test_filter_options(client) -> None:
    data = client.get("/api/filters").json()
    assert data["issue_types"]
    assert data["period"]["earliest"]


def test_advice_endpoint(client) -> None:
    """Наблюдения приходят с обязательными полями."""
    data = client.get("/api/advice").json()
    assert "findings" in data
    for finding in data["findings"]:
        assert finding["title"]
        assert finding["detail"]
        assert finding["suggestion"], "наблюдение без подсказки бесполезно"
        assert finding["severity"] in ("act", "watch", "info")


def test_advice_sorted_by_severity(client) -> None:
    order = {"act": 0, "watch": 1, "info": 2}
    findings = client.get("/api/advice").json()["findings"]
    ranks = [order[f["severity"]] for f in findings]
    assert ranks == sorted(ranks)


def test_forecast_endpoint(client) -> None:
    """Прогноз возвращает перцентили в правильном направлении."""
    data = client.get("/api/forecast").json()
    assert "how_long" in data and "how_many" in data

    long_p = data["how_long"]["percentiles"]
    if long_p:
        # выше уверенность — больше срок
        assert int(long_p["50"]) <= int(long_p["85"]) <= int(long_p["95"])

    many_p = data["how_many"]["percentiles"]
    if many_p:
        # выше уверенность — меньше обещанный объём
        assert int(many_p["50"]) >= int(many_p["85"]) >= int(many_p["95"])


def test_forecast_horizon_parameter(client) -> None:
    short = client.get("/api/forecast?horizon_periods=2").json()
    long = client.get("/api/forecast?horizon_periods=8").json()
    if short["how_many"]["percentiles"] and long["how_many"]["percentiles"]:
        assert int(long["how_many"]["percentiles"]["50"]) > int(
            short["how_many"]["percentiles"]["50"]
        )


# --- просмотр исходных данных ------------------------------------------------


def test_ticket_list(client) -> None:
    data = client.get("/api/tickets?limit=10").json()
    assert data["total"] > 0
    assert len(data["items"]) <= 10
    for item in data["items"]:
        assert item["key"]
        assert isinstance(item["anomalies"], list)


def test_ticket_list_pagination(client) -> None:
    first = client.get("/api/tickets?limit=5&offset=0").json()
    second = client.get("/api/tickets?limit=5&offset=5").json()
    assert first["total"] == second["total"]
    first_keys = {i["key"] for i in first["items"]}
    second_keys = {i["key"] for i in second["items"]}
    assert not (first_keys & second_keys), "страницы не должны пересекаться"


def test_ticket_list_sorting(client) -> None:
    data = client.get("/api/tickets?sort=cycle_time&order=desc&limit=20").json()
    values = [i["cycle_s"] for i in data["items"] if i["cycle_s"] is not None]
    assert values == sorted(values, reverse=True)


def test_ticket_list_search(client) -> None:
    everything = client.get("/api/tickets?limit=1").json()
    key = client.get("/api/tickets?limit=1").json()["items"][0]["key"]
    found = client.get(f"/api/tickets?search={key}").json()
    assert found["total"] >= 1
    assert found["total"] < everything["total"]


def test_ticket_list_cycle_range(client) -> None:
    """Фильтр по диапазону — это переход от столбца гистограммы к задачам."""
    data = client.get("/api/tickets?min_cycle_s=0&max_cycle_s=36000&limit=50").json()
    for item in data["items"]:
        assert item["cycle_s"] is None or 0 <= item["cycle_s"] < 36000


def test_ticket_list_only_open(client) -> None:
    data = client.get("/api/tickets?only_open=true&limit=20").json()
    for item in data["items"]:
        assert item["closed_at"] is None


def test_ticket_list_anomaly_filter(client) -> None:
    quality = client.get("/api/quality").json()
    if not quality["anomalies"]:
        return
    code = quality["anomalies"][0]["code"]
    data = client.get(f"/api/tickets?anomaly={code}&limit=20").json()
    assert data["total"] > 0
    for item in data["items"]:
        assert code in item["anomalies"]


def test_ticket_detail(client) -> None:
    """Карточка задачи содержит всё для разбора: события, интервалы, согласование."""
    key = client.get("/api/tickets?limit=1").json()["items"][0]["key"]
    data = client.get(f"/api/tickets/{key}").json()
    assert data["key"] == key
    assert data["intervals"]
    assert data["events"]
    assert data["events"][0]["kind"] == "created"
    assert data["timeline_facts"]
    assert data["metrics"]


def test_ticket_detail_shows_reconciliation(client) -> None:
    """Видно оба сигнала и решение ядра."""
    key = client.get("/api/tickets?limit=1").json()["items"][0]["key"]
    data = client.get(f"/api/tickets/{key}").json()
    for fact in data["timeline_facts"]:
        assert fact["boundary"] in ("work_start", "work_end")
        assert fact["chosen_source"]
        assert fact["confidence"]


def test_ticket_intervals_are_ordered(client) -> None:
    key = client.get("/api/tickets?limit=1").json()["items"][0]["key"]
    data = client.get(f"/api/tickets/{key}").json()
    seqs = [i["seq"] for i in data["intervals"]]
    assert seqs == sorted(seqs)


def test_ticket_detail_not_found(client) -> None:
    assert client.get("/api/tickets/NOPE-999").status_code == 404


def test_phase_intervals(client) -> None:
    data = client.get("/api/phase-intervals").json()
    assert data["items"]
    durations = [i["business_s"] for i in data["items"]]
    assert durations == sorted(durations, reverse=True)


def test_phase_intervals_filtered(client) -> None:
    data = client.get("/api/phase-intervals?phase=in_progress").json()
    for item in data["items"]:
        assert item["phase"] == "in_progress"


def test_all_endpoints_return_200(client) -> None:
    endpoints = [
        "/api/advice",
        "/api/forecast",
        "/api/tickets",
        "/api/phase-intervals",
        "/api/summary",
        "/api/cycle-time",
        "/api/cfd",
        "/api/arrival-throughput",
        "/api/aging-wip",
        "/api/flow-efficiency",
        "/api/people",
        "/api/quality",
        "/api/interventions",
        "/api/filters",
    ]
    for endpoint in endpoints:
        assert client.get(endpoint).status_code == 200, endpoint


def test_date_range_filter(client) -> None:
    from datetime import date, timedelta

    recent = date.today() - timedelta(days=30)
    data = client.get(f"/api/summary?date_from={recent.isoformat()}").json()
    everything = client.get("/api/summary").json()
    assert data["total_tickets"] < everything["total_tickets"]


def test_blockers_endpoint(client) -> None:
    data = client.get("/api/blockers").json()
    assert "pareto" in data
    assert "blocked_rate" in data
    assert "current" in data


def test_blockers_endpoint_respects_filters(client) -> None:
    data = client.get("/api/blockers", params={"issue_types": "Bug"}).json()
    assert data["tickets"] >= data["blocked_tickets"]


def test_sle_endpoint_empty_without_promises(client) -> None:
    data = client.get("/api/sle").json()
    assert "promises" in data


def test_sle_fix_and_read(client) -> None:
    fixed = client.post("/api/sle", params={"percentile": 85})
    assert fixed.status_code == 200
    assert fixed.json()["target_business_s"] > 0

    report = client.get("/api/sle").json()
    assert report["target"] == 0.85
    assert report["promises"]


def test_sle_fix_rejects_small_sample(client) -> None:
    response = client.post("/api/sle", params={"issue_type": "не существует"})
    assert response.status_code == 422


def test_transitions_endpoint(client) -> None:
    data = client.get("/api/transitions").json()
    assert "cells" in data
    assert "backflow_rate" in data


def test_backlog_endpoint(client) -> None:
    data = client.get("/api/backlog").json()
    assert data["size"] == sum(b["count"] for b in data["histogram"])


def test_service_classes_endpoint(client) -> None:
    data = client.get("/api/service-classes").json()
    assert "overall_share" in data
    assert "by_class" in data


def test_hidden_queue_endpoint(client) -> None:
    data = client.get("/api/hidden-queue").json()
    assert "by_phase" in data
    assert "share" in data


def test_dashboard_endpoints_all_registered(client) -> None:
    """Каждый эндпоинт, который дёргает дашборд, должен существовать.

    Дашборд обращается к ним из JS: пропущенная регистрация видна только
    как пустая вкладка в браузере, а не как упавший тест.
    """
    for path in [
        "/api/summary", "/api/cycle-time", "/api/cfd", "/api/arrival-throughput",
        "/api/aging-wip", "/api/flow-efficiency", "/api/blockers", "/api/sle",
        "/api/transitions", "/api/backlog", "/api/service-classes",
        "/api/hidden-queue", "/api/people", "/api/quality", "/api/advice",
    ]:
        assert client.get(path).status_code == 200, path


def test_dashboard_versions_static_assets(client) -> None:
    """Статика подключается с версией, иначе браузер отдаёт старую копию.

    Без этого после обновления dashboard.js браузер берёт закэшированный файл,
    в котором новых вкладок нет, а showTab() молча сводит незнакомое имя
    к «Обзору» — снаружи выглядит как «все вкладки показывают одно и то же».
    """
    html = client.get("/").text
    for asset in ("dashboard.js", "style.css"):
        assert f"/static/{asset}?v=" in html, asset
        assert f'"/static/{asset}"' not in html, f"{asset} подключён без версии"


# --- импорт и выгрузка -------------------------------------------------------


def test_import_format_documented(client) -> None:
    """Формат описан в API: файл можно собрать, не читая исходники."""
    data = client.get("/api/import/format").json()
    assert "external_key" in data["required_ticket_fields"]
    assert data["example"]["ticket"]["events"][0]["kind"] == "created"


def test_import_csv_transitions(client) -> None:
    content = (
        "key,type,status_from,status_to,changed_at\n"
        "IMP-1,Task,new,in progress,2026-01-15T10:00:00+03:00\n"
        "IMP-1,Task,in progress,done,2026-01-16T10:00:00+03:00\n"
    )
    response = client.post(
        "/api/import",
        files={"file": ("moves.csv", content, "text/csv")},
        data={"replace_existing": "false"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["tickets"] == 1
    assert body["source"] == "moves"


def test_import_rejects_unparsable(client) -> None:
    response = client.post(
        "/api/import",
        files={"file": ("bad.csv", "foo,bar\n1,2\n", "text/csv")},
    )
    assert response.status_code == 422
    assert "ключ" in response.json()["detail"]


def test_import_rejects_empty(client) -> None:
    response = client.post(
        "/api/import",
        files={"file": ("empty.csv", "key,created\n", "text/csv")},
    )
    assert response.status_code == 422


def test_export_returns_file(client) -> None:
    response = client.get("/api/export")
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert int(response.headers["X-Flowlens-Tickets"]) > 0


def test_export_anonymized_by_default(client) -> None:
    """Ключи задач не должны уезжать из контура без явного согласия."""
    anon = client.get("/api/export").text
    assert "TASK-" in anon

    full = client.get("/api/export", params={"full": "true"}).text
    assert "TASK-" not in full


def test_export_roundtrips_through_import(client) -> None:
    """Выгруженный файл принимается импортом — иначе им нельзя поделиться."""
    exported = client.get("/api/export").content
    response = client.post(
        "/api/import",
        files={"file": ("slice.ndjson", exported, "application/x-ndjson")},
    )
    assert response.status_code == 200
    assert response.json()["tickets"] > 0


def test_jira_check_requires_reachable_host(client) -> None:
    """Недоступный адрес объясняется человеку, а не роняет запрос."""
    response = client.post(
        "/api/jira/check",
        json={"base_url": "https://jira.invalid.example", "token": "x"},
    )
    assert response.status_code == 422
    assert response.json()["detail"]


def test_jira_sync_validates_payload(client) -> None:
    """JQL обязателен: без него выгружать нечего."""
    response = client.post("/api/jira/sync", json={"base_url": "https://x.example"})
    assert response.status_code == 422


# --- команды и настройка статусов --------------------------------------------


def test_teams_endpoint(client) -> None:
    teams = client.get("/api/teams").json()
    assert teams
    assert teams[0]["name"]
    assert "tickets" in teams[0]


def test_team_statuses_listed(client) -> None:
    teams = [t for t in client.get("/api/teams").json() if t["tickets"]]
    team_id = teams[0]["id"]
    statuses = client.get(f"/api/teams/{team_id}/statuses").json()
    names = {s["external_name"] for s in statuses}
    assert "in progress" in names


def test_patch_status_changes_metrics(client) -> None:
    """Смена классификации обязана дойти до цифр, а не остаться в таблице.

    Данные пересоздаются: тесты импорта выше подменяют содержимое базы,
    и без этого выбранный статус может оказаться без накопленного времени.
    """
    from flowlens.db import make_engine

    engine = make_engine()
    seed_demo(engine, ticket_count=200, months=6, seed=5)
    recompute_all(engine)

    # берём команду с задачами: у пустой менять классификацию нечему,
    # и тест не проверял бы связь настройки с метриками
    teams = [t for t in client.get("/api/teams").json() if t["tickets"]]
    assert teams, "нужна хотя бы одна команда с задачами"
    team_id = max(teams, key=lambda t: t["tickets"])["id"]
    statuses = client.get(f"/api/teams/{team_id}/statuses").json()
    # берём активный статус с наибольшим накопленным временем: перевод пустого
    # статуса в очередь ничего бы не изменил, и тест не проверял бы связь
    phases = {
        p["phase"]: p["total_s"]
        for p in client.get("/api/flow-efficiency", params={"team_id": team_id})
        .json()["by_phase"]
    }
    candidates = [
        (phases.get(s["phase"], 0), s) for s in statuses if s["is_active_work"]
    ]
    weight, active = max(candidates, key=lambda pair: pair[0])
    assert weight > 0, "нужен статус с накопленным временем"

    scoped = {"team_id": team_id}
    before = client.get("/api/flow-efficiency", params=scoped).json()["efficiency"]
    client.patch(
        f"/api/statuses/{active['id']}", json={"is_active_work": False, "is_queue": True}
    )
    after = client.get("/api/flow-efficiency", params=scoped).json()["efficiency"]

    client.patch(
        f"/api/statuses/{active['id']}", json={"is_active_work": True, "is_queue": False}
    )
    restored = client.get("/api/flow-efficiency", params=scoped).json()["efficiency"]

    assert after < before, "перевод активной фазы в очередь снижает эффективность"
    assert restored == pytest.approx(before, abs=0.001)


def test_patch_status_rejects_empty(client) -> None:
    teams = [t for t in client.get("/api/teams").json() if t["tickets"]]
    status_id = client.get(f"/api/teams/{teams[0]['id']}/statuses").json()[0]["id"]
    assert client.patch(f"/api/statuses/{status_id}", json={}).status_code == 422


def test_patch_status_404_for_unknown(client) -> None:
    assert client.patch("/api/statuses/999999", json={"is_queue": True}).status_code == 404
