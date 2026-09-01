"""Отчёт о качестве данных.

Пока доля недостоверных тикетов велика, графики потока вводят в заблуждение.
Поэтому качество данных — самостоятельный отчёт, а не сноска мелким шрифтом.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from flowlens.core.reconciliation import Anomaly, Confidence

# Человекочитаемые описания и подсказки, что делать.
ANOMALY_LABELS: dict[str, tuple[str, str]] = {
    Anomaly.DECLARED_MISSING.value: (
        "Даты работы не заполнены",
        "Метрики посчитаны по истории статусов — они верны настолько, "
        "насколько вовремя двигали задачу на доске.",
    ),
    Anomaly.DECLARED_BEFORE_CREATED.value: (
        "Начало работы раньше создания задачи",
        "Опечатка в дате. Значение отброшено, использована история статусов.",
    ),
    Anomaly.DECLARED_AFTER_NOW.value: (
        "Дата в будущем",
        "Опечатка в дате. Значение отброшено.",
    ),
    Anomaly.END_BEFORE_START.value: (
        "Окончание раньше начала",
        "Даты перепутаны местами. Использована история статусов.",
    ),
    Anomaly.DECLARED_CONTRADICTS_CHANGELOG.value: (
        "Заявленные даты сильно расходятся с историей статусов",
        "Либо задачу двигали с опозданием, либо даты проставлены неточно. "
        "Стоит посмотреть на эти задачи вручную.",
    ),
    Anomaly.DECLARED_ONLY_PARTIAL.value: (
        "Заполнена только одна из дат",
        "У завершённой задачи должны быть обе даты.",
    ),
    Anomaly.BULK_MOVE.value: (
        "Задача проведена по доске одним махом",
        "Все переходы за считанные секунды — доску разгребали задним числом. "
        "История статусов для таких задач недостоверна.",
    ),
    Anomaly.ZERO_DURATION_LIFECYCLE.value: (
        "Нулевая длительность работы",
        "Вся жизнь задачи уместилась в нерабочее время.",
    ),
    Anomaly.STALE_TRANSITION.value: (
        "Аномально долгое пребывание в статусе",
        "Задача висела дольше 95% остальных, а потом закрылась мгновенно.",
    ),
    Anomaly.NO_SYSTEM_SIGNAL.value: (
        "Нет признаков работы",
        "Задача не была в активном статусе и не имеет заявленных дат.",
    ),
    Anomaly.COMPLETED_WITHOUT_WORK.value: (
        "Закрыта, минуя работу",
        "Задача завершена, ни разу не побывав в активном статусе.",
    ),
}


@dataclass
class AnomalyGroup:
    """Аномалия и затронутые ею тикеты."""

    code: str
    label: str
    hint: str
    count: int
    sample_keys: list[str] = field(default_factory=list)


@dataclass
class QualityReport:
    """Сводка достоверности данных."""

    total_tickets: int = 0
    by_confidence: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    anomalies: list[AnomalyGroup] = field(default_factory=list)
    declared_coverage_pct: float = 0.0
    trustworthy_pct: float = 0.0

    @property
    def affected_tickets(self) -> int:
        """Сколько тикетов имеют хотя бы одну аномалию."""
        return self.total_tickets - self.by_confidence.get(Confidence.HIGH.value, 0)

    def verdict(self) -> str:
        """Короткий вывод: можно ли доверять метрикам."""
        if self.total_tickets == 0:
            return "Нет данных."
        if self.trustworthy_pct >= 80:
            return (
                f"Данные пригодны для анализа: {self.trustworthy_pct:.0f}% задач "
                "с надёжными датами."
            )
        if self.trustworthy_pct >= 50:
            return (
                f"Данные пригодны с оговорками: надёжны {self.trustworthy_pct:.0f}% задач. "
                "Стоит посмотреть на список проблем ниже."
            )
        return (
            f"Данным пока доверять нельзя: надёжны только {self.trustworthy_pct:.0f}% задач. "
            "Прогнозы и средние значения будут вводить в заблуждение."
        )


def build_report(
    rows: list[dict],
    *,
    sample_size: int = 5,
) -> QualityReport:
    """Собрать отчёт из записей ticket_timeline_fact.

    Каждая строка: {'key', 'confidence', 'source', 'anomalies', 'has_declared'}.
    """
    report = QualityReport()
    if not rows:
        return report

    by_ticket: dict[str, dict] = {}
    for row in rows:
        key = row["key"]
        existing = by_ticket.get(key)
        if existing is None:
            by_ticket[key] = {
                "confidence": row["confidence"],
                "source": row["source"],
                "anomalies": set(row["anomalies"] or []),
                "has_declared": row.get("has_declared", False),
            }
            continue
        # у тикета две границы: берём худшую уверенность
        existing["anomalies"] |= set(row["anomalies"] or [])
        existing["confidence"] = _worse_confidence(existing["confidence"], row["confidence"])
        existing["has_declared"] = existing["has_declared"] or row.get("has_declared", False)

    report.total_tickets = len(by_ticket)
    report.by_confidence = dict(Counter(t["confidence"] for t in by_ticket.values()))
    report.by_source = dict(Counter(t["source"] for t in by_ticket.values()))

    declared_count = sum(1 for t in by_ticket.values() if t["has_declared"])
    report.declared_coverage_pct = round(100 * declared_count / report.total_tickets, 1)

    high = report.by_confidence.get(Confidence.HIGH.value, 0)
    report.trustworthy_pct = round(100 * high / report.total_tickets, 1)

    counter: Counter[str] = Counter()
    samples: dict[str, list[str]] = {}
    for key, data in by_ticket.items():
        for code in data["anomalies"]:
            counter[code] += 1
            samples.setdefault(code, [])
            if len(samples[code]) < sample_size:
                samples[code].append(key)

    report.anomalies = [
        AnomalyGroup(
            code=code,
            label=ANOMALY_LABELS.get(code, (code, ""))[0],
            hint=ANOMALY_LABELS.get(code, ("", ""))[1],
            count=count,
            sample_keys=sorted(samples.get(code, [])),
        )
        for code, count in counter.most_common()
    ]
    return report


def _worse_confidence(left: str, right: str) -> str:
    order = {Confidence.HIGH.value: 3, Confidence.MEDIUM.value: 2, Confidence.LOW.value: 1}
    return left if order.get(left, 0) <= order.get(right, 0) else right


__all__ = ["ANOMALY_LABELS", "AnomalyGroup", "QualityReport", "build_report"]
