"""Интеграционные тесты импорта контракта в базу."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from flowlens.contract import (
    RawComment,
    RawDeclaredDate,
    RawEvent,
    RawLink,
    RawPerson,
    RawTicket,
    SourceProfile,
)
from flowlens.db import make_engine
from flowlens.importer import import_tickets, last_watermark, set_watermark
from flowlens.pipeline import recompute_all

MSK = timezone(timedelta(hours=3))


@pytest.fixture(scope="module")
def engine():
    try:
        eng = make_engine()
        with eng.begin() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres недоступен: {exc}")
    return eng


@pytest.fixture
def clean_db(engine):
    from flowlens.repository import reset_data

    reset_data(engine)
    return engine


def profile(name: str = "test-source") -> SourceProfile:
    return SourceProfile(source_kind="jira_dc", source_name=name)


def make_ticket(key: str = "PROJ-1", *, status: str = "done") -> RawTicket:
    """Тикет с полным набором сущностей."""
    base = datetime(2026, 3, 2, 11, tzinfo=MSK)
    return RawTicket(
        external_key=key,
        project_key="PROJ",
        issue_type="Bug",
        priority="High",
        status=status,
        summary="Тестовый тикет",
        components=["api"],
        labels=["regression"],
        created_at=base,
        resolved_at=base + timedelta(days=3),
        updated_at=base + timedelta(days=3),
        reporter="olga",
        assignee="ivan",
        events=[
            RawEvent(kind="created", occurred_at=base, actor="olga", source_event_id=f"{key}-c"),
            RawEvent(
                kind="assignee_change",
                occurred_at=base + timedelta(hours=1),
                actor="ivan",
                field="assignee",
                new_value="ivan",
                source_event_id=f"{key}-a1",
            ),
            RawEvent(
                kind="status_change",
                occurred_at=base + timedelta(hours=1),
                actor="ivan",
                field="status",
                old_value="new",
                new_value="in progress",
                source_event_id=f"{key}-s1",
            ),
            RawEvent(
                kind="status_change",
                occurred_at=base + timedelta(days=3),
                actor="ivan",
                field="status",
                old_value="in progress",
                new_value="done",
                source_event_id=f"{key}-s2",
            ),
        ],
        declared_dates=[
            RawDeclaredDate(
                boundary="work_start",
                value_at=base + timedelta(hours=1),
                source_field="customfield_10014",
            )
        ],
        comments=[
            RawComment(
                external_id=f"{key}-comment-1",
                author="ivan",
                created_at=base + timedelta(hours=2),
                body="Беру в работу",
            )
        ],
        people=[
            RawPerson(external_id="olga", display_name="Ольга", email="olga@example.com"),
            RawPerson(external_id="ivan", display_name="Иван", email="ivan@example.com"),
        ],
    )


# --- базовый импорт ----------------------------------------------------------


def test_imports_all_entities(clean_db) -> None:
    stats = import_tickets(clean_db, profile(), [make_ticket()])
    assert stats.tickets == 1
    assert stats.events == 4
    assert stats.comments == 1
    assert stats.declared == 1
    assert stats.people == 2

    with clean_db.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM ticket")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM ticket_event")).scalar_one() == 4
        assert conn.execute(text("SELECT count(*) FROM person")).scalar_one() == 2


def test_ticket_fields_stored(clean_db) -> None:
    import_tickets(clean_db, profile(), [make_ticket()])
    with clean_db.begin() as conn:
        row = conn.execute(
            text(
                "SELECT external_key, issue_type, priority, components, labels, summary "
                "FROM ticket WHERE external_key = 'PROJ-1'"
            )
        ).one()
    assert row.issue_type == "Bug"
    assert row.priority == "High"
    assert row.components == ["api"]
    assert row.labels == ["regression"]


def test_reimport_is_idempotent(clean_db) -> None:
    """Повторный импорт той же выгрузки не создаёт дублей.

    Событие 'created' не имеет поля field — из-за NULL != NULL это место
    однажды уже давало дубли, поэтому проверяется отдельно.
    """
    tickets = [make_ticket("PROJ-1"), make_ticket("PROJ-2")]
    first = import_tickets(clean_db, profile(), tickets)
    second = import_tickets(clean_db, profile(), tickets)

    assert first.events == 8
    assert second.events == 0  # ничего нового не записано

    with clean_db.begin() as conn:
        assert conn.execute(text("SELECT count(*) FROM ticket")).scalar_one() == 2
        assert conn.execute(text("SELECT count(*) FROM ticket_event")).scalar_one() == 8
        assert conn.execute(text("SELECT count(*) FROM ticket_comment")).scalar_one() == 2
        assert conn.execute(text("SELECT count(*) FROM person")).scalar_one() == 2


def test_created_event_not_duplicated(clean_db) -> None:
    """Прицельная проверка события без поля field."""
    import_tickets(clean_db, profile(), [make_ticket()])
    import_tickets(clean_db, profile(), [make_ticket()])
    with clean_db.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM ticket_event WHERE kind = 'created'")
        ).scalar_one()
    assert count == 1


def test_update_changes_mutable_fields(clean_db) -> None:
    """Повторный импорт обновляет изменившиеся поля тикета."""
    import_tickets(clean_db, profile(), [make_ticket()])
    updated = make_ticket()
    updated.summary = "Новое название"
    updated.priority = "Low"
    import_tickets(clean_db, profile(), [updated])

    with clean_db.begin() as conn:
        row = conn.execute(
            text("SELECT summary, priority FROM ticket WHERE external_key = 'PROJ-1'")
        ).one()
    assert row.summary == "Новое название"
    assert row.priority == "Low"


# --- статусы -----------------------------------------------------------------


def test_statuses_created_with_canonical_phase(clean_db) -> None:
    import_tickets(clean_db, profile(), [make_ticket()])
    with clean_db.begin() as conn:
        rows = conn.execute(
            text("SELECT external_name, phase::text, is_active_work FROM workflow_status")
        ).all()
    mapping = {name: (phase, active) for name, phase, active in rows}
    assert mapping["in progress"] == ("in_progress", True)
    assert mapping["done"][0] == "done"


def test_unknown_status_recorded(clean_db) -> None:
    """Статус вне доски не ломает импорт, но попадает в отчёт."""
    ticket = make_ticket(status="согласование")
    ticket.events.append(
        RawEvent(
            kind="status_change",
            occurred_at=datetime(2026, 3, 4, 11, tzinfo=MSK),
            field="status",
            old_value="done",
            new_value="согласование",
            source_event_id="PROJ-1-s3",
        )
    )
    stats = import_tickets(clean_db, profile(), [ticket])
    assert "согласование" in stats.unknown_statuses


def test_status_mapping_applied(clean_db) -> None:
    """Русские статусы приводятся к каноническим через конфиг источника."""
    source_profile = profile()
    source_profile.status_mapping = {"В работе": "in progress"}
    ticket = make_ticket(status="В работе")
    stats = import_tickets(clean_db, source_profile, [ticket])
    assert "В работе" not in stats.unknown_statuses

    with clean_db.begin() as conn:
        phase = conn.execute(
            text("SELECT phase::text FROM workflow_status WHERE external_name = 'В работе'")
        ).scalar_one()
    assert phase == "in_progress"


# --- люди --------------------------------------------------------------------


def test_people_deduplicated_by_email(clean_db) -> None:
    """Один человек в двух системах — одна запись person, два алиаса."""
    ticket_a = make_ticket("PROJ-1")
    ticket_b = make_ticket("PROJ-2")
    ticket_b.people = [
        RawPerson(external_id="i.petrov", display_name="И. Петров", email="ivan@example.com")
    ]
    ticket_b.assignee = "i.petrov"
    ticket_b.events = [
        RawEvent(
            kind="created",
            occurred_at=datetime(2026, 3, 2, 11, tzinfo=MSK),
            actor="i.petrov",
            source_event_id="PROJ-2-c",
        )
    ]

    import_tickets(clean_db, profile(), [ticket_a, ticket_b])

    with clean_db.begin() as conn:
        people = conn.execute(
            text("SELECT count(*) FROM person WHERE primary_email = 'ivan@example.com'")
        ).scalar_one()
        aliases = conn.execute(
            text(
                "SELECT count(*) FROM person_alias a JOIN person p ON p.id = a.person_id "
                "WHERE p.primary_email = 'ivan@example.com'"
            )
        ).scalar_one()
    assert people == 1
    assert aliases == 2


def test_events_linked_to_people(clean_db) -> None:
    import_tickets(clean_db, profile(), [make_ticket()])
    with clean_db.begin() as conn:
        orphans = conn.execute(
            text(
                "SELECT count(*) FROM ticket_event "
                "WHERE actor_person_id IS NULL AND kind <> 'created'"
            )
        ).scalar_one()
    assert orphans == 0


# --- связи -------------------------------------------------------------------


def test_links_resolved_after_both_tickets_exist(clean_db) -> None:
    """Связь проставляется, даже если целевой тикет идёт позже в выгрузке."""
    first = make_ticket("PROJ-1")
    first.links = [RawLink(to_key="PROJ-2", link_type="blocks")]
    second = make_ticket("PROJ-2")

    stats = import_tickets(clean_db, profile(), [first, second])
    assert stats.links == 1

    with clean_db.begin() as conn:
        row = conn.execute(
            text(
                "SELECT f.external_key, t.external_key, l.link_type "
                "FROM ticket_link l JOIN ticket f ON f.id = l.from_ticket_id "
                "JOIN ticket t ON t.id = l.to_ticket_id"
            )
        ).one()
    assert row[0] == "PROJ-1"
    assert row[1] == "PROJ-2"


def test_link_to_missing_ticket_skipped(clean_db) -> None:
    """Ссылка на тикет вне выгрузки не создаёт битую запись."""
    ticket = make_ticket("PROJ-1")
    ticket.links = [RawLink(to_key="OTHER-999", link_type="relates")]
    stats = import_tickets(clean_db, profile(), [ticket])
    assert stats.links == 0


# --- заявленные даты ---------------------------------------------------------


def test_declared_dates_stored_raw(clean_db) -> None:
    """Даты сохраняются как есть: согласование — работа этапа 3."""
    ticket = make_ticket()
    ticket.declared_dates = [
        RawDeclaredDate(
            boundary="work_start",
            value_at=datetime(2026, 2, 1, 10, tzinfo=MSK),  # раньше создания
            source_field="customfield_10014",
        )
    ]
    import_tickets(clean_db, profile(), [ticket])

    with clean_db.begin() as conn:
        row = conn.execute(
            text(
                "SELECT d.value_at, t.created_at FROM ticket_declared_date d "
                "JOIN ticket t ON t.id = d.ticket_id"
            )
        ).one()
    assert row.value_at < row.created_at  # противоречие сохранено, не исправлено


def test_declared_date_updated_on_reimport(clean_db) -> None:
    import_tickets(clean_db, profile(), [make_ticket()])
    updated = make_ticket()
    updated.declared_dates = [
        RawDeclaredDate(
            boundary="work_start",
            value_at=datetime(2026, 3, 3, 10, tzinfo=MSK),
            source_field="customfield_10014",
        )
    ]
    import_tickets(clean_db, profile(), [updated])

    with clean_db.begin() as conn:
        count = conn.execute(text("SELECT count(*) FROM ticket_declared_date")).scalar_one()
        value = conn.execute(text("SELECT value_at FROM ticket_declared_date")).scalar_one()
    assert count == 1
    assert value.day == 3


# --- журнал синхронизаций ----------------------------------------------------


def test_sync_run_recorded(clean_db) -> None:
    import_tickets(clean_db, profile(), [make_ticket()])
    with clean_db.begin() as conn:
        row = conn.execute(
            text(
                "SELECT status, tickets_seen, events_written FROM sync_run "
                "ORDER BY id DESC LIMIT 1"
            )
        ).one()
    assert row.status == "ok"
    assert row.tickets_seen == 1


def test_watermark_roundtrip(clean_db) -> None:
    import_tickets(clean_db, profile("wm-source"), [make_ticket()])
    assert last_watermark(clean_db, "wm-source") is None

    moment = datetime(2026, 3, 5, 12, tzinfo=MSK)
    set_watermark(clean_db, "wm-source", moment)
    assert last_watermark(clean_db, "wm-source") == moment


# --- сквозной путь -----------------------------------------------------------


def test_import_then_recompute(clean_db) -> None:
    """Импортированные данные проходят полный пересчёт."""
    import_tickets(clean_db, profile(), [make_ticket("PROJ-1"), make_ticket("PROJ-2")])
    result = recompute_all(clean_db)
    assert result["tickets"] == 2

    with clean_db.begin() as conn:
        intervals = conn.execute(text("SELECT count(*) FROM ticket_interval")).scalar_one()
        metrics = conn.execute(
            text("SELECT count(*) FROM ticket_metrics WHERE cycle_time_business_s IS NOT NULL")
        ).scalar_one()
    assert intervals > 0
    assert metrics == 2
