"""Текстовый разбор метрик через Claude.

Модель получает уже посчитанные показатели и объясняет их связь.
Она не считает и не выдумывает числа: всё, о чём говорится в разборе,
должно присутствовать во входных данных.

Разбор дополняет правила из `advice.py`, а не заменяет их: правила
объяснимы и воспроизводимы, LLM добавляет связность и контекст.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

MODEL = "claude-opus-5"
MAX_TOKENS = 4000

SYSTEM_PROMPT = """\
Ты помогаешь тимлиду разобраться в потоке задач его команды.

Тебе передают уже посчитанные метрики. Твоя работа — объяснить, что они
означают вместе, и на что стоит обратить внимание.

Требования к разбору:

1. Опирайся только на переданные числа. Не додумывай показателей,
   которых нет во входных данных, и не оценивай то, о чём данные молчат.
2. Если достоверность данных низкая, скажи об этом первым делом
   и оговаривай выводы соответственно.
3. Говори о процессе, а не о людях. «Задачи ждут проверки» — уместно,
   «команда работает медленно» — нет. Данные о владении задачами
   не измеряют усилия человека.
4. Различай наблюдение и предположение. Где связь между показателями
   лишь вероятна, так и пиши.
5. Не предлагай универсальных методик. Предлагай то, что следует
   из конкретных чисел.
6. Пиши по-русски, спокойно и по делу. Без восклицаний, без похвалы,
   без канцелярита. Тимлид — специалист, объяснять азбучные вещи не нужно.

Структура ответа:
- Короткий абзац: что происходит с потоком в целом.
- Два-четыре наблюдения с объяснением связи между показателями.
- Что проверить или изменить в первую очередь и почему именно это.

Объём — до 400 слов. Заголовки не нужны."""


@dataclass
class NarrativeRequest:
    """Метрики, передаваемые на разбор."""

    period_label: str
    summary: dict[str, Any]
    flow: dict[str, Any]
    arrival: dict[str, Any]
    aging: dict[str, Any]
    quality: dict[str, Any]
    findings: list[dict[str, Any]] = field(default_factory=list)
    forecast: dict[str, Any] | None = None
    interventions: list[dict[str, Any]] = field(default_factory=list)

    def to_payload(self) -> dict[str, Any]:
        """Компактное представление для модели.

        Секунды переводятся в рабочие часы: так короче и понятнее,
        а модель не занимается арифметикой.
        """
        return {
            "период": self.period_label,
            "общее": _clean_summary(self.summary),
            "распределение_времени": _clean_flow(self.flow),
            "поток": _clean_arrival(self.arrival),
            "незавершённые": _clean_aging(self.aging),
            "качество_данных": _clean_quality(self.quality),
            "найденные_отклонения": [
                {
                    "что": f.get("title"),
                    "подробности": f.get("detail"),
                    "срочность": f.get("severity"),
                }
                for f in self.findings
            ],
            "прогноз": _clean_forecast(self.forecast) if self.forecast else None,
            "изменения_в_процессе": [
                {"когда": i.get("occurred_at", "")[:10], "что": i.get("title")}
                for i in self.interventions
            ],
        }


def _hours(seconds: float | None) -> float | None:
    return round(seconds / 3600, 1) if seconds else None


def _workdays(seconds: float | None) -> float | None:
    return round(seconds / 3600 / 9, 1) if seconds else None


def _clean_summary(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "всего_задач": data.get("total_tickets"),
        "завершено": data.get("completed"),
        "не_завершено": data.get("open_tickets"),
        "время_цикла_медиана_раб_дней": _workdays(data.get("p50_cycle_s")),
        "время_цикла_85_перцентиль_раб_дней": _workdays(data.get("p85_cycle_s")),
        "эффективность_потока": data.get("avg_flow_efficiency"),
        "переоткрытий": data.get("reopens"),
        "задач_с_блокировками": data.get("ever_blocked"),
    }


def _clean_flow(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "доля_активной_работы": data.get("efficiency"),
        "по_фазам": [
            {
                "фаза": p.get("phase"),
                "медиана_часов": _hours(p.get("p50_s")),
                "85_перцентиль_часов": _hours(p.get("p85_s")),
            }
            for p in data.get("by_phase", [])
            if p.get("phase") != "done"
        ],
    }


def _clean_arrival(data: dict[str, Any]) -> dict[str, Any]:
    arrived = data.get("arrived", [])[-8:]
    completed = data.get("completed", [])[-8:]
    return {
        "поступило_по_неделям": arrived,
        "закрыто_по_неделям": completed,
        "накопление_очереди": data.get("net_per_period", [])[-8:],
    }


def _clean_aging(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "всего_незавершённых": data.get("total"),
        "старше_85_перцентиля": data.get("over_p85"),
        "заблокировано_сейчас": data.get("blocked"),
        "самые_старые": [
            {
                "задача": item.get("key"),
                "статус": item.get("status"),
                "возраст_раб_дней": _workdays(item.get("age_s")),
            }
            for item in data.get("items", [])[:5]
        ],
    }


def _clean_quality(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "доля_надёжных_метрик_проц": data.get("trustworthy_pct"),
        "заполненность_дат_проц": data.get("declared_coverage_pct"),
        "проблемы": [
            {"что": a.get("label"), "задач": a.get("count")}
            for a in data.get("anomalies", [])[:5]
        ],
    }


def _clean_forecast(data: dict[str, Any]) -> dict[str, Any]:
    how_long = data.get("how_long", {})
    how_many = data.get("how_many", {})
    health = data.get("wip_health", {})
    return {
        "закроем_текущий_объём_недель": {
            "с_вероятностью_50": how_long.get("percentiles", {}).get(50)
            or how_long.get("percentiles", {}).get("50"),
            "с_вероятностью_85": how_long.get("percentiles", {}).get(85)
            or how_long.get("percentiles", {}).get("85"),
        },
        "успеем_за_4_недели_задач": how_many.get("percentiles", {}).get(85)
        or how_many.get("percentiles", {}).get("85"),
        "объём_работы": {
            "измеренное_время_цикла_дней": health.get("measured_days"),
            "следует_из_объёма_дней": health.get("implied_days"),
        },
    }


class NarrativeUnavailable(RuntimeError):
    """Разбор недоступен: нет ключа или API не отвечает."""


def is_available() -> bool:
    """Есть ли учётные данные для обращения к API."""
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def build_prompt(request: NarrativeRequest) -> str:
    """Собрать текст запроса — вынесено отдельно ради тестируемости."""
    payload = json.dumps(request.to_payload(), ensure_ascii=False, indent=2)
    return (
        f"Метрики потока задач за период «{request.period_label}»:\n\n"
        f"```json\n{payload}\n```\n\n"
        "Разбери эти показатели."
    )


def generate(
    request: NarrativeRequest,
    *,
    model: str = MODEL,
    max_tokens: int = MAX_TOKENS,
    client: Any = None,
) -> str:
    """Получить текстовый разбор метрик.

    Клиент можно подменить — это используется в тестах.
    """
    if client is None:
        if not is_available():
            raise NarrativeUnavailable(
                "Не задан ANTHROPIC_API_KEY. Разбор недоступен, "
                "остальные отчёты работают без него."
            )
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise NarrativeUnavailable(
                "Не установлен пакет anthropic: pip install anthropic"
            ) from exc
        client = anthropic.Anthropic()

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": build_prompt(request)}],
        )
    except Exception as exc:  # noqa: BLE001
        raise NarrativeUnavailable(f"Не удалось получить разбор: {exc}") from exc

    if getattr(response, "stop_reason", None) == "refusal":
        raise NarrativeUnavailable("Модель отказалась отвечать на этот запрос.")

    parts = [
        block.text
        for block in response.content
        if getattr(block, "type", None) == "text"
    ]
    text = "\n".join(parts).strip()
    if not text:
        raise NarrativeUnavailable("Пустой ответ модели.")
    return text


__all__ = [
    "MODEL",
    "SYSTEM_PROMPT",
    "NarrativeRequest",
    "NarrativeUnavailable",
    "build_prompt",
    "generate",
    "is_available",
]
