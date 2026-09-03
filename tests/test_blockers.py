"""Тесты классификации причин блокировок и диаграммы Парето."""

from __future__ import annotations

import pytest

from flowlens.core.blockers import (
    UNKNOWN,
    BlockerEpisode,
    classify,
    clean_reason_text,
    pareto,
)

# --- классификация -----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ждём смежную команду", "waiting_team"),
        ("ждем ответа заказчика", "waiting_customer"),
        ("Стенд недоступен", "environment"),
        ("нет доступа к окружению", "environment"),
        ("Требования не уточнены", "requirements"),
        ("Зависимость от другой задачи", "dependency"),
        ("Ждём ревью от архитектора", "waiting_review"),
        ("Баг в смежном сервисе", "defect"),
        ("подрядчик не отвечает", "waiting_external"),
        ("разработчик в отпуске", "capacity"),
    ],
)
def test_classify_known_reasons(text: str, expected: str) -> None:
    assert classify(text) == expected


@pytest.mark.parametrize("text", [None, "", "   ", "какой-то текст без ключевых слов"])
def test_classify_falls_back_to_unknown(text: str | None) -> None:
    """Угаданная категория хуже отсутствующей: она создаёт ложную уверенность."""
    assert classify(text) == UNKNOWN


def test_classify_ignores_case_and_word_form() -> None:
    assert classify("ЗАКАЗЧИК не отвечает") == "waiting_customer"
    assert classify("требованиями занимается аналитик") == "requirements"


def test_classify_prefers_more_specific_rule() -> None:
    """Внешний подрядчик — не то же самое, что смежная команда."""
    assert classify("ждём внешнего подрядчика") == "waiting_external"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Ждём смежную команду", "waiting_team"),
        ("ждём смежников", "waiting_team"),
        ("проблема в смежном сервисе", "defect"),
        ("баг в смежном коде", "defect"),
    ],
)
def test_classify_disambiguates_adjacent_team_from_adjacent_service(
    text: str, expected: str
) -> None:
    """«Смежная команда» и «смежный сервис» начинаются одинаково, но значат разное."""
    assert classify(text) == expected


# --- очистка текста ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Flagged: ждём заказчика", "ждём заказчика"),
        ("blocked - нет стенда", "нет стенда"),
        ("Impediment — ждём ревью", "ждём ревью"),
        ("Блокировка: зависимость", "зависимость"),
        ("просто причина", "просто причина"),
    ],
)
def test_clean_reason_strips_flag_prefix(raw: str, expected: str) -> None:
    assert clean_reason_text(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "Flagged:"])
def test_clean_reason_empty(raw: str | None) -> None:
    assert clean_reason_text(raw) is None


# --- Парето ------------------------------------------------------------------


def episode(reason: str, hours: int) -> BlockerEpisode:
    return BlockerEpisode(reason=reason, business_s=hours * 3600, calendar_s=hours * 3600)


def test_pareto_sorted_by_loss() -> None:
    rows = pareto(
        [
            episode("waiting_team", 10),
            episode("environment", 30),
            episode("waiting_customer", 20),
        ]
    )
    assert [r["reason"] for r in rows] == ["environment", "waiting_customer", "waiting_team"]


def test_pareto_cumulative_reaches_one() -> None:
    rows = pareto([episode("waiting_team", 10), episode("environment", 30)])
    assert rows[-1]["cumulative_share"] == pytest.approx(1.0)


def test_pareto_cumulative_is_monotonic() -> None:
    rows = pareto([episode(f"r{i}", i + 1) for i in range(6)])
    shares = [r["cumulative_share"] for r in rows]
    assert shares == sorted(shares)


def test_pareto_aggregates_repeated_reasons() -> None:
    rows = pareto([episode("environment", 5), episode("environment", 7)])
    assert len(rows) == 1
    assert rows[0]["episodes"] == 2
    assert rows[0]["business_s"] == 12 * 3600


def test_pareto_empty() -> None:
    assert pareto([]) == []


def test_pareto_labels_are_human_readable() -> None:
    rows = pareto([episode("waiting_team", 1)])
    assert rows[0]["label"] == "Ждём смежную команду"


def test_pareto_handles_zero_duration() -> None:
    """Мгновенная блокировка не должна ронять расчёт долей."""
    rows = pareto([episode("environment", 0)])
    assert rows[0]["share"] == 0.0
