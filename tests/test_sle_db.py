"""Тесты ожидаемого уровня сервиса (SLE)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import text

from flowlens.analytics import Filters, active_sle, fix_sle, sle_attainment
from flowlens.db import make_engine
from flowlens.pipeline import recompute_all, seed_demo


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
    seed_demo(engine, ticket_count=300, months=6, seed=21)
    recompute_all(engine)
    return engine


@pytest.fixture(autouse=True)
def clean_sle(data):
    """Каждый тест начинает без обещаний: они накапливаются между прогонами."""
    with data.begin() as conn:
        conn.execute(text("DELETE FROM service_level_expectation"))
    yield


# --- фиксация ----------------------------------------------------------------


def test_fix_sle_stores_promise(data) -> None:
    result = fix_sle(data, Filters(), note="baseline")
    assert result["percentile"] == 85
    assert result["target_business_s"] > 0
    assert result["sample_size"] >= 20

    promises = active_sle(data, Filters())
    assert len(promises) == 1
    assert promises[0]["note"] == "baseline"


def test_fix_sle_matches_current_percentile(data) -> None:
    """Обещание фиксируется ровно по наблюдаемому перцентилю."""
    from flowlens.analytics import cycle_time_distribution

    distribution = cycle_time_distribution(data, Filters())
    result = fix_sle(data, Filters())
    assert result["target_business_s"] == distribution["percentiles"]["p85"]


def test_fix_sle_retires_previous_promise(data) -> None:
    """Действующее обещание на класс всегда одно."""
    fix_sle(data, Filters())
    fix_sle(data, Filters())
    assert len(active_sle(data, Filters())) == 1


def test_fix_sle_keeps_promises_for_different_classes(data) -> None:
    fix_sle(data, Filters())
    fix_sle(data, replace(Filters(), issue_types=["Bug"]))
    promises = active_sle(data, Filters())
    assert len(promises) == 2
    assert {p["issue_type"] for p in promises} == {None, "Bug"}


def test_fix_sle_accepts_other_percentiles(data) -> None:
    fix_sle(data, Filters(), percentile=50)
    fix_sle(data, Filters(), percentile=95)
    promises = active_sle(data, Filters())
    targets = {p["percentile"]: p["target_business_s"] for p in promises}
    assert targets[50] < targets[95]


def test_fix_sle_rejects_small_sample(data) -> None:
    """На малой выборке перцентиль пляшет от одной задачи — обещать нельзя."""
    with pytest.raises(ValueError, match="недостаточно данных"):
        fix_sle(data, replace(Filters(), issue_types=["не существует"]))


def test_active_sle_orders_specific_first(data) -> None:
    """Наиболее конкретное обещание должно применяться первым."""
    fix_sle(data, Filters())
    fix_sle(data, replace(Filters(), issue_types=["Bug"]))
    promises = active_sle(data, Filters())
    assert promises[0]["issue_type"] == "Bug"
    assert promises[-1]["issue_type"] is None


# --- попадание ---------------------------------------------------------------


def test_attainment_empty_without_promises(data) -> None:
    report = sle_attainment(data, Filters())
    assert report["periods"] == []
    assert report["target"] is None


def test_attainment_close_to_target_right_after_fixing(data) -> None:
    """Обещание по p85 на тех же данных выполняется примерно в 85% случаев."""
    fix_sle(data, Filters())
    report = sle_attainment(data, Filters())
    values = [a for a in report["attainment"] if a is not None]
    assert values
    overall = sum(report["met"]) / sum(report["counts"])
    assert 0.75 <= overall <= 0.95


def test_attainment_share_within_bounds(data) -> None:
    fix_sle(data, Filters())
    report = sle_attainment(data, Filters())
    for value, met, total in zip(
        report["attainment"], report["met"], report["counts"], strict=True
    ):
        assert 0 <= value <= 1
        assert met <= total


def test_attainment_target_follows_percentile(data) -> None:
    fix_sle(data, Filters(), percentile=95)
    assert sle_attainment(data, Filters())["target"] == 0.95


def test_attainment_uses_most_specific_promise(data) -> None:
    """Обещание по типу задач применяется к своим задачам, общее — к остальным."""
    fix_sle(data, Filters())
    # заведомо жёсткое обещание для одного типа роняет попадание именно по нему
    with data.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO service_level_expectation "
                "  (issue_type, percentile, target_business_s, sample_size) "
                "VALUES ('Bug', 85, 60, 100)"
            )
        )
    report = sle_attainment(data, replace(Filters(), issue_types=["Bug"]))
    assert sum(report["met"]) == 0, "с порогом в минуту не может уложиться никто"
