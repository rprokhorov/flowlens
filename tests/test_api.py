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
    assert p["p50"] <= p["p85"] <= p["p95"]
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


def test_all_endpoints_return_200(client) -> None:
    endpoints = [
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
