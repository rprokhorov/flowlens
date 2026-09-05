"""Тесты выгрузки среза и обезличивания."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import text

from flowlens.analytics import Filters, cycle_time_distribution, summary
from flowlens.contract import read_ndjson
from flowlens.db import make_engine
from flowlens.export import export_tickets
from flowlens.importer import import_tickets
from flowlens.pipeline import recompute_all, seed_demo
from flowlens.repository import reset_data


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
    seed_demo(engine, ticket_count=200, months=6, seed=31)
    recompute_all(engine)
    return engine


# --- выгрузка ----------------------------------------------------------------


def test_export_writes_all_tickets(data, tmp_path: Path) -> None:
    out = tmp_path / "slice.ndjson"
    stats = export_tickets(data, Filters(), out)
    assert stats.tickets > 0
    assert stats.events > 0
    assert out.exists()


def test_export_respects_filters(data, tmp_path: Path) -> None:
    everything = export_tickets(data, Filters(), tmp_path / "all.ndjson")
    bugs = export_tickets(
        data, replace(Filters(), issue_types=["Bug"]), tmp_path / "bugs.ndjson"
    )
    assert bugs.tickets < everything.tickets


def test_export_readable_by_contract(data, tmp_path: Path) -> None:
    """Файл должен читаться тем же кодом, что и выгрузка коллектора."""
    out = tmp_path / "slice.ndjson"
    export_tickets(data, Filters(), out)
    profile, tickets = read_ndjson(out)
    assert profile.source_kind == "flowlens_export"
    assert len(tickets) > 0
    assert all(t.events for t in tickets)


# --- обезличивание -----------------------------------------------------------


def test_anonymized_export_hides_identifiers(data, tmp_path: Path) -> None:
    out = tmp_path / "anon.ndjson"
    export_tickets(data, Filters(), out, anonymize=True, salt="fixed")
    _, tickets = read_ndjson(out)

    for ticket in tickets:
        assert ticket.external_key.startswith("TASK-")
        assert ticket.summary == ""
        assert ticket.project_key == "PROJ"
        assert not ticket.components
        assert not ticket.labels
        if ticket.assignee:
            assert ticket.assignee.startswith("user-")


def test_anonymized_export_keeps_statuses(data, tmp_path: Path) -> None:
    """Статусы обезличивать нельзя — на них держится вся модель фаз."""
    out = tmp_path / "anon.ndjson"
    export_tickets(data, Filters(), out, anonymize=True, salt="fixed")
    _, tickets = read_ndjson(out)

    statuses = {
        event.new_value
        for ticket in tickets
        for event in ticket.events
        if event.kind == "status_change" and event.new_value
    }
    assert "in progress" in statuses
    assert "done" in statuses


def test_pseudonyms_are_stable_within_export(data, tmp_path: Path) -> None:
    """Один человек получает один псевдоним — иначе развалится атрибуция."""
    out = tmp_path / "anon.ndjson"
    export_tickets(data, Filters(), out, anonymize=True, salt="fixed")
    raw = out.read_text().splitlines()[1:]

    mapping: dict[str, set[str]] = {}
    for line in raw:
        ticket = json.loads(line)
        for event in ticket["events"]:
            if event["kind"] == "assignee_change" and event.get("new_value"):
                mapping.setdefault(event["new_value"], set()).add(event["new_value"])
    for names in mapping.values():
        assert len(names) == 1


def test_different_salts_give_different_pseudonyms(data, tmp_path: Path) -> None:
    """Две выгрузки нельзя сопоставить между собой."""
    first = tmp_path / "a.ndjson"
    second = tmp_path / "b.ndjson"
    export_tickets(data, Filters(), first, anonymize=True, salt="one")
    export_tickets(data, Filters(), second, anonymize=True, salt="two")

    _, tickets_a = read_ndjson(first)
    _, tickets_b = read_ndjson(second)
    keys_a = {t.external_key for t in tickets_a}
    keys_b = {t.external_key for t in tickets_b}
    assert not (keys_a & keys_b)


def test_full_export_keeps_identifiers(data, tmp_path: Path) -> None:
    out = tmp_path / "full.ndjson"
    export_tickets(data, Filters(), out, anonymize=False)
    _, tickets = read_ndjson(out)
    assert any(t.summary for t in tickets)
    assert not any(t.external_key.startswith("TASK-") for t in tickets)


# --- round-trip --------------------------------------------------------------


def test_roundtrip_preserves_metrics(data, tmp_path: Path) -> None:
    """Главный критерий: импорт выгрузки даёт те же цифры.

    Обезличивание трогает содержание задач, а метрики считаются по статусам
    и времени — значит расхождения быть не должно ни в одной цифре.
    """
    out = tmp_path / "slice.ndjson"
    export_tickets(data, Filters(), out, anonymize=True, salt="fixed")

    before = summary(data, Filters())
    before_cycle = cycle_time_distribution(data, Filters())

    reset_data(data)
    profile, tickets = read_ndjson(out)
    import_tickets(data, profile, tickets)
    recompute_all(data)

    after = summary(data, Filters())
    after_cycle = cycle_time_distribution(data, Filters())

    assert after["total_tickets"] == before["total_tickets"]
    assert after["p50_cycle_s"] == before["p50_cycle_s"]
    assert after["p85_cycle_s"] == before["p85_cycle_s"]
    assert after_cycle["count"] == before_cycle["count"]
