"""Тесты сборки запроса на текстовый разбор.

Обращения к API здесь нет: клиент подменяется заглушкой.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from flowlens.core.narrative import (
    MODEL,
    SYSTEM_PROMPT,
    NarrativeRequest,
    NarrativeUnavailable,
    build_prompt,
    generate,
    is_available,
)


@dataclass
class FakeBlock:
    type: str
    text: str = ""


@dataclass
class FakeResponse:
    content: list[FakeBlock]
    stop_reason: str = "end_turn"


class FakeClient:
    """Заглушка клиента: запоминает запрос и отдаёт заданный ответ."""

    def __init__(self, text: str = "Разбор.", stop_reason: str = "end_turn"):
        self.text = text
        self.stop_reason = stop_reason
        self.captured: dict[str, Any] = {}
        self.messages = self

    def create(self, **kwargs: Any) -> FakeResponse:
        self.captured = kwargs
        return FakeResponse(
            content=[FakeBlock(type="thinking"), FakeBlock(type="text", text=self.text)],
            stop_reason=self.stop_reason,
        )


class FailingClient:
    def __init__(self) -> None:
        self.messages = self

    def create(self, **kwargs: Any):
        raise ConnectionError("сеть недоступна")


def sample_request() -> NarrativeRequest:
    return NarrativeRequest(
        period_label="последние 90 дней",
        summary={
            "total_tickets": 500,
            "completed": 450,
            "open_tickets": 50,
            "p50_cycle_s": 2 * 9 * 3600,
            "p85_cycle_s": 5 * 9 * 3600,
            "avg_flow_efficiency": 0.42,
            "reopens": 8,
            "ever_blocked": 90,
        },
        flow={
            "efficiency": 0.42,
            "by_phase": [
                {"phase": "in_progress", "p50_s": 6 * 3600, "p85_s": 14 * 3600},
                {"phase": "blocked", "p50_s": 20 * 3600, "p85_s": 40 * 3600},
                {"phase": "done", "p50_s": 0, "p85_s": 0},
            ],
        },
        arrival={
            "arrived": [30] * 10,
            "completed": [25] * 10,
            "net_per_period": [5] * 10,
        },
        aging={
            "total": 50,
            "over_p85": 20,
            "blocked": 5,
            "items": [
                {"key": "P-1", "status": "qa", "age_s": 30 * 9 * 3600},
                {"key": "P-2", "status": "new", "age_s": 25 * 9 * 3600},
            ],
        },
        quality={
            "trustworthy_pct": 68.0,
            "declared_coverage_pct": 75.0,
            "anomalies": [{"label": "Даты не заполнены", "count": 120}],
        },
        findings=[
            {"title": "Очередь растёт", "detail": "Три периода подряд", "severity": "act"}
        ],
    )


# --- подготовка данных -------------------------------------------------------


def test_payload_converts_seconds_to_workdays() -> None:
    """Модель не должна заниматься арифметикой: время приходит в днях и часах."""
    payload = sample_request().to_payload()
    assert payload["общее"]["время_цикла_медиана_раб_дней"] == 2.0
    assert payload["общее"]["время_цикла_85_перцентиль_раб_дней"] == 5.0


def test_payload_excludes_terminal_phase() -> None:
    """Фаза «готово» не несёт информации о длительности."""
    payload = sample_request().to_payload()
    phases = {p["фаза"] for p in payload["распределение_времени"]["по_фазам"]}
    assert "done" not in phases
    assert "blocked" in phases


def test_payload_limits_arrival_history() -> None:
    """В запрос идут последние периоды, а не вся история."""
    payload = sample_request().to_payload()
    assert len(payload["поток"]["поступило_по_неделям"]) <= 8


def test_payload_limits_oldest_tickets() -> None:
    payload = sample_request().to_payload()
    assert len(payload["незавершённые"]["самые_старые"]) <= 5


def test_payload_includes_quality_first() -> None:
    """Достоверность данных обязательно попадает в запрос."""
    payload = sample_request().to_payload()
    assert payload["качество_данных"]["доля_надёжных_метрик_проц"] == 68.0


def test_payload_is_json_serializable() -> None:
    payload = sample_request().to_payload()
    assert json.loads(json.dumps(payload, ensure_ascii=False))


def test_payload_preserves_russian() -> None:
    prompt = build_prompt(sample_request())
    assert "Даты не заполнены" in prompt
    assert "\\u" not in prompt  # кириллица не экранирована


# --- системная инструкция ----------------------------------------------------


def test_system_prompt_forbids_inventing_numbers() -> None:
    assert "только на переданные числа" in SYSTEM_PROMPT


def test_system_prompt_protects_people() -> None:
    """Разбор не должен превращаться в оценку людей."""
    assert "процессе, а не о людях" in SYSTEM_PROMPT


def test_system_prompt_requires_quality_caveat() -> None:
    assert "достоверность" in SYSTEM_PROMPT.lower()


# --- вызов -------------------------------------------------------------------


def test_generate_uses_expected_model() -> None:
    client = FakeClient()
    generate(sample_request(), client=client)
    assert client.captured["model"] == MODEL


def test_generate_enables_adaptive_thinking() -> None:
    client = FakeClient()
    generate(sample_request(), client=client)
    assert client.captured["thinking"] == {"type": "adaptive"}


def test_generate_passes_system_prompt() -> None:
    client = FakeClient()
    generate(sample_request(), client=client)
    assert client.captured["system"] == SYSTEM_PROMPT


def test_generate_returns_text_only() -> None:
    """Блоки размышлений в результат не попадают."""
    client = FakeClient(text="Поток стабилен.")
    assert generate(sample_request(), client=client) == "Поток стабилен."


def test_generate_handles_refusal() -> None:
    client = FakeClient(stop_reason="refusal")
    with pytest.raises(NarrativeUnavailable, match="отказалась"):
        generate(sample_request(), client=client)


def test_generate_handles_empty_response() -> None:
    client = FakeClient(text="")
    with pytest.raises(NarrativeUnavailable, match="Пустой"):
        generate(sample_request(), client=client)


def test_generate_handles_api_failure() -> None:
    with pytest.raises(NarrativeUnavailable, match="Не удалось"):
        generate(sample_request(), client=FailingClient())


def test_missing_key_reported_clearly(monkeypatch) -> None:
    """Отсутствие ключа — не ошибка, а сообщение: остальное работает."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    assert not is_available()
    with pytest.raises(NarrativeUnavailable, match="ANTHROPIC_API_KEY"):
        generate(sample_request())


def test_key_detected_when_present(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert is_available()
