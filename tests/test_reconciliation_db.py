"""Интеграционные тесты согласования: полный путь через базу."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from flowlens.core.calendar import WorkCalendar
from flowlens.core.quality import build_report
from flowlens.core.reconciliation import Anomaly, ReconciliationPolicy
from flowlens.db import make_engine
from flowlens.importer import import_tickets
from flowlens.pipeline import recompute_all
from flowlens.repository import load_quality_rows, reset_data
from flowlens.testing.export import seed_to_raw_ticket, synthetic_profile
from flowlens.testing.scenarios import all_scenarios


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
def scenarios_db(engine):
    """База с эталонными сценариями: каждый содержит известную патологию."""
    reset_data(engine)
    cal = WorkCalendar(name="test", tz="Europe/Moscow")
    seeds = all_scenarios(cal)
    raw = [seed_to_raw_ticket(s) for s in seeds]
    import_tickets(engine, synthetic_profile(), raw)
    recompute_all(engine)
    return engine


def anomalies_of(engine, key: str) -> set[str]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT f.anomaly_flags FROM ticket_timeline_fact f "
                "JOIN ticket t ON t.id = f.ticket_id WHERE t.external_key = :key"
            ),
            {"key": key},
        ).all()
    found: set[str] = set()
    for (flags,) in rows:
        found |= set(flags or [])
    return found


def fact_of(engine, key: str, boundary: str) -> dict:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT f.system_at, f.declared_at, f.effective_at, f.chosen_source, "
                "       f.confidence, f.discrepancy_business_s, f.policy_version "
                "FROM ticket_timeline_fact f JOIN ticket t ON t.id = f.ticket_id "
                "WHERE t.external_key = :key AND f.boundary = :boundary"
            ),
            {"key": key, "boundary": boundary},
        ).one()
    return dict(row._mapping)


# --- факты создаются ---------------------------------------------------------


def test_every_ticket_has_two_facts(scenarios_db) -> None:
    with scenarios_db.begin() as conn:
        tickets = conn.execute(text("SELECT count(*) FROM ticket")).scalar_one()
        facts = conn.execute(text("SELECT count(*) FROM ticket_timeline_fact")).scalar_one()
    assert facts == tickets * 2


def test_policy_version_recorded(scenarios_db) -> None:
    fact = fact_of(scenarios_db, "DEMO-1", "work_start")
    assert fact["policy_version"] == "1.0"


def test_metrics_confidence_filled(scenarios_db) -> None:
    with scenarios_db.begin() as conn:
        missing = conn.execute(
            text("SELECT count(*) FROM ticket_metrics WHERE confidence IS NULL")
        ).scalar_one()
    assert missing == 0


# --- эталонные патологии распознаны ------------------------------------------


def test_happy_path_is_clean(scenarios_db) -> None:
    """Аккуратно заполненный тикет не должен иметь замечаний."""
    assert anomalies_of(scenarios_db, "DEMO-1") == set()
    fact = fact_of(scenarios_db, "DEMO-1", "work_start")
    assert fact["chosen_source"] == "declared"
    assert fact["confidence"] == "high"


def test_bulk_move_detected(scenarios_db) -> None:
    """Ключевой случай: changelog схлопнут, даты проставлены руками."""
    assert Anomaly.BULK_MOVE.value in anomalies_of(scenarios_db, "DEMO-6")


def test_bulk_move_recovers_real_duration(scenarios_db) -> None:
    """Согласование восстанавливает реальную длительность работы.

    По changelog задача сделана мгновенно; заявленные даты говорят
    о четырёх рабочих днях — им и верим.
    """
    start = fact_of(scenarios_db, "DEMO-6", "work_start")
    end = fact_of(scenarios_db, "DEMO-6", "work_end")

    system_span = (end["system_at"] - start["system_at"]).total_seconds()
    effective_span = (end["effective_at"] - start["effective_at"]).total_seconds()

    assert system_span == 0  # changelog утверждает, что работы не было
    assert effective_span > 3 * 24 * 3600  # согласование восстановило дни
    assert start["chosen_source"] == "declared"


def test_declared_before_created_detected(scenarios_db) -> None:
    assert Anomaly.DECLARED_BEFORE_CREATED.value in anomalies_of(scenarios_db, "DEMO-9")
    fact = fact_of(scenarios_db, "DEMO-9", "work_start")
    assert fact["chosen_source"] == "system"  # невозможная дата отброшена
    assert fact["confidence"] == "low"


def test_end_before_start_detected(scenarios_db) -> None:
    assert Anomaly.END_BEFORE_START.value in anomalies_of(scenarios_db, "DEMO-10")


def test_missing_declared_flagged(scenarios_db) -> None:
    """У переоткрытого тикета даты не заполнялись."""
    assert Anomaly.DECLARED_MISSING.value in anomalies_of(scenarios_db, "DEMO-4")


def test_partial_declared_flagged(scenarios_db) -> None:
    """У DEMO-7 заполнено только начало, но тикет не завершён — не аномалия."""
    flags = anomalies_of(scenarios_db, "DEMO-7")
    assert Anomaly.DECLARED_ONLY_PARTIAL.value not in flags


# --- смена политики ----------------------------------------------------------


def test_policy_change_flips_source(scenarios_db) -> None:
    """Пересчёт с другой политикой меняет решения без обращения к источнику."""
    before = fact_of(scenarios_db, "DEMO-6", "work_start")
    assert before["chosen_source"] == "declared"

    recompute_all(
        scenarios_db,
        policy=ReconciliationPolicy(version="system-only", prefer="system_only"),
    )
    after = fact_of(scenarios_db, "DEMO-6", "work_start")
    assert after["chosen_source"] == "system"
    assert after["policy_version"] == "system-only"
    # сырые сигналы не изменились — переписаны только решения
    assert after["system_at"] == before["system_at"]
    assert after["declared_at"] == before["declared_at"]

    recompute_all(scenarios_db, policy=ReconciliationPolicy())


def test_raw_data_untouched_by_policy_change(scenarios_db) -> None:
    """Слой RAW неизменен: меняются только производные значения."""
    with scenarios_db.begin() as conn:
        events_before = conn.execute(text("SELECT count(*) FROM ticket_event")).scalar_one()
        declared_before = conn.execute(
            text("SELECT count(*) FROM ticket_declared_date")
        ).scalar_one()

    recompute_all(scenarios_db, policy=ReconciliationPolicy(prefer="system_only"))

    with scenarios_db.begin() as conn:
        events_after = conn.execute(text("SELECT count(*) FROM ticket_event")).scalar_one()
        declared_after = conn.execute(
            text("SELECT count(*) FROM ticket_declared_date")
        ).scalar_one()

    assert events_before == events_after
    assert declared_before == declared_after
    recompute_all(scenarios_db, policy=ReconciliationPolicy())


def test_threshold_affects_anomaly_count(scenarios_db) -> None:
    """Мягкий порог убирает часть замечаний о расхождении."""
    recompute_all(
        scenarios_db,
        policy=ReconciliationPolicy(version="strict", discrepancy_threshold_business_days=0.1),
    )
    strict_rows = load_quality_rows(scenarios_db)
    strict = sum(
        1
        for r in strict_rows
        if Anomaly.DECLARED_CONTRADICTS_CHANGELOG.value in (r["anomalies"] or [])
    )

    recompute_all(
        scenarios_db,
        policy=ReconciliationPolicy(version="lenient", discrepancy_threshold_business_days=30),
    )
    lenient_rows = load_quality_rows(scenarios_db)
    lenient = sum(
        1
        for r in lenient_rows
        if Anomaly.DECLARED_CONTRADICTS_CHANGELOG.value in (r["anomalies"] or [])
    )

    assert strict >= lenient
    recompute_all(scenarios_db, policy=ReconciliationPolicy())


def test_recompute_is_idempotent(scenarios_db) -> None:
    """Повторный пересчёт не меняет факты."""
    with scenarios_db.begin() as conn:
        before = conn.execute(
            text(
                "SELECT md5(string_agg(x, '|' ORDER BY x)) FROM ("
                "  SELECT ticket_id::text || boundary || chosen_source || confidence "
                "         || coalesce(effective_at::text, '-') AS x "
                "  FROM ticket_timeline_fact) s"
            )
        ).scalar_one()

    recompute_all(scenarios_db)

    with scenarios_db.begin() as conn:
        after = conn.execute(
            text(
                "SELECT md5(string_agg(x, '|' ORDER BY x)) FROM ("
                "  SELECT ticket_id::text || boundary || chosen_source || confidence "
                "         || coalesce(effective_at::text, '-') AS x "
                "  FROM ticket_timeline_fact) s"
            )
        ).scalar_one()

    assert before == after


# --- отчёт о качестве --------------------------------------------------------


def test_quality_report_from_db(scenarios_db) -> None:
    rows = load_quality_rows(scenarios_db)
    report = build_report(rows)
    assert report.total_tickets == 10
    assert report.anomalies
    assert report.verdict()


def test_report_lists_known_problems(scenarios_db) -> None:
    rows = load_quality_rows(scenarios_db)
    report = build_report(rows)
    codes = {group.code for group in report.anomalies}
    assert Anomaly.BULK_MOVE.value in codes
    assert Anomaly.DECLARED_BEFORE_CREATED.value in codes
