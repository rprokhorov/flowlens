"""Причины блокировок: из свободного текста в счётные категории.

Длительность блокировки без причины — это лишь размер потерь. Чтобы из потерь
получился план улучшений, причины надо сгруппировать: обычно две-три группы
дают большую часть простоя, и тогда понятно, за что браться.

Источник причины в Jira — обычно значение флага или комментарий при постановке
на удержание. Текст там свободный, поэтому нормализуем его по ключевым словам,
а всё неопознанное честно относим к `unknown`: пустая категория лучше выдуманной.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

UNKNOWN = "unknown"

# Порядок важен: правила проверяются сверху вниз, первое совпадение выигрывает.
# Более специфичные категории должны идти раньше общих.
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "defect",
        ("баг", "дефект", "ошибк", "bug", "regression", "регресс"),
    ),
    (
        "waiting_external",
        ("внешн", "подрядчик", "вендор", "vendor", "external", "контрагент", "партнёр"),
    ),
    (
        "waiting_team",
        ("смежник", "соседн", "other team", "платформенн"),
    ),
    (
        "waiting_customer",
        ("заказчик", "клиент", "пользовател", "бизнес", "customer", "product owner"),
    ),
    (
        "waiting_review",
        ("ревью", "review", "апрув", "approve", "согласован", "проверк"),
    ),
    (
        "environment",
        ("стенд", "окружен", "среда", "environment", "деплой", "deploy", "инфраструктур", "доступ"),
    ),
    (
        "requirements",
        ("требован", "постановк", "уточнен", "непонятн", "аналитик", "спек", "requirement"),
    ),
    (
        "dependency",
        ("зависим", "блокирует", "ждём задачу", "ждет задачу", "depends", "blocked by"),
    ),
    (
        "capacity",
        ("нет людей", "занят", "отпуск", "болен", "ресурс", "capacity", "нагрузк"),
    ),
)

# человекочитаемые названия для UI
LABELS: dict[str, str] = {
    "waiting_external": "Ждём внешнюю сторону",
    "waiting_team": "Ждём смежную команду",
    "waiting_customer": "Ждём заказчика",
    "waiting_review": "Ждём ревью или согласование",
    "environment": "Окружение и доступы",
    "requirements": "Требования не готовы",
    "dependency": "Зависимость от другой задачи",
    "defect": "Дефект в смежном коде",
    "capacity": "Нет свободных людей",
    UNKNOWN: "Причина не указана",
}

# Русская морфология не даёт опознать категорию одним словом: «смежная команда» и
# «смежный сервис» начинаются одинаково, но значат разное. Такие случаи требуют
# совпадения двух основ сразу, и проверяются раньше одиночных ключевых слов.
_PAIR_RULES: tuple[tuple[str, tuple[str, str]], ...] = (
    ("waiting_team", ("смежн", "команд")),
    ("waiting_team", ("другой", "команд")),
    ("waiting_team", ("другая", "команд")),
    ("defect", ("смежн", "сервис")),
    ("defect", ("смежн", "код")),
)

CATEGORIES: tuple[str, ...] = (*[name for name, _ in _RULES], UNKNOWN)


def classify(text: str | None) -> str:
    """Отнести свободный текст причины к категории.

    Регистр и форма слова не важны: ищем вхождение основы. Ничего не нашли —
    `unknown`, потому что угаданная категория хуже отсутствующей: она создаёт
    ложную уверенность в Парето.
    """
    if not text:
        return UNKNOWN
    lowered = text.casefold()
    for name, (first, second) in _PAIR_RULES:
        if first in lowered and second in lowered:
            return name
    for name, keywords in _RULES:
        if any(keyword in lowered for keyword in keywords):
            return name
    return UNKNOWN


@dataclass(frozen=True)
class BlockerEpisode:
    """Один эпизод блокировки: сколько длился и почему."""

    reason: str
    business_s: int
    calendar_s: int

    @property
    def label(self) -> str:
        return LABELS.get(self.reason, self.reason)


def pareto(episodes: list[BlockerEpisode]) -> list[dict[str, object]]:
    """Причины по убыванию потерь с накопленной долей.

    Возвращает готовые данные для диаграммы Парето: бары — потери по причине,
    линия — накопленный процент. Обычно 80% приходится на две-три причины.
    """
    if not episodes:
        return []

    totals: dict[str, dict[str, int]] = {}
    for episode in episodes:
        bucket = totals.setdefault(episode.reason, {"business_s": 0, "count": 0})
        bucket["business_s"] += episode.business_s
        bucket["count"] += 1

    overall = sum(b["business_s"] for b in totals.values())
    ordered = sorted(totals.items(), key=lambda kv: kv[1]["business_s"], reverse=True)

    rows: list[dict[str, object]] = []
    running = 0
    for reason, bucket in ordered:
        running += bucket["business_s"]
        rows.append(
            {
                "reason": reason,
                "label": LABELS.get(reason, reason),
                "business_s": bucket["business_s"],
                "episodes": bucket["count"],
                "share": round(bucket["business_s"] / overall, 4) if overall else 0.0,
                "cumulative_share": round(running / overall, 4) if overall else 0.0,
            }
        )
    return rows


_FLAG_PREFIX = re.compile(r"^\s*(flagged|impediment|blocked|блок\w*)\s*[:\-—]?\s*", re.IGNORECASE)


def clean_reason_text(raw: str | None) -> str | None:
    """Убрать служебный префикс флага, оставив собственно причину."""
    if not raw:
        return None
    cleaned = _FLAG_PREFIX.sub("", raw).strip()
    return cleaned or None


__all__ = [
    "CATEGORIES",
    "LABELS",
    "UNKNOWN",
    "BlockerEpisode",
    "classify",
    "clean_reason_text",
    "pareto",
]
