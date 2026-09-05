"""Детерминированные правила: наблюдения о процессе с объяснением.

Каждое правило проверяет один порог и говорит, на что смотреть и почему.
Никаких «оценок команды» — только факты о потоке и ссылки на конкретные задачи.

Правила намеренно объяснимы: тимлид должен понимать, откуда взялся вывод,
и иметь возможность не согласиться.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Severity(StrEnum):
    """Насколько срочно стоит посмотреть."""

    INFO = "info"
    WATCH = "watch"
    ACT = "act"


@dataclass
class Finding:
    """Одно наблюдение."""

    code: str
    severity: Severity
    title: str
    detail: str
    suggestion: str
    evidence: dict[str, Any] = field(default_factory=dict)
    ticket_keys: list[str] = field(default_factory=list)


@dataclass
class Thresholds:
    """Пороги правил. Настраиваются: у разных команд разная норма."""

    low_flow_efficiency: float = 0.30
    critical_flow_efficiency: float = 0.15
    blocked_share_of_time: float = 0.20
    queue_growth_periods: int = 3
    aging_over_p85_share: float = 0.25
    reopen_rate: float = 0.10
    # 3-4 одновременные задачи для Kanban — рабочая норма; сигналим с пяти
    wip_per_person: float = 5.0
    data_quality_pct: float = 60.0
    stale_ticket_multiplier: float = 2.0
    handoff_count: float = 3.0
    # выше этой доли блокировок без причины разбивка по причинам недостоверна
    unknown_blocker_share: float = 0.30
    # разовый провал ниже цели — в природе перцентиля; сигналим по серии
    sle_min_periods: int = 3
    sle_tolerance: float = 0.05
    # выше этой доли ожидания статус стоит признать очередью, а не работой
    hidden_queue_share: float = 0.20
    # отношение p98/p50: до 4 разброс рабочий, выше — обещать по медиане нельзя
    predictability_index: float = 4.0


# названия фаз в именительном падеже; для предложного есть отдельный словарь
# в _rule_slowest_phase — согласование по падежам того не стоит, чтобы городить
# морфологию ради двух правил
PHASE_LABELS = {
    "backlog": "бэклог",
    "in_progress": "разработка",
    "blocked": "блокировка",
    "review": "ревью",
    "verify": "проверка",
    "done_pending": "ожидание релиза",
}


def analyse(
    *,
    summary: dict[str, Any],
    flow: dict[str, Any],
    arrival: dict[str, Any],
    aging: dict[str, Any],
    people: dict[str, Any],
    quality: dict[str, Any],
    blockers: dict[str, Any] | None = None,
    sle: dict[str, Any] | None = None,
    hidden: dict[str, Any] | None = None,
    predictability: dict[str, Any] | None = None,
    forecast: dict[str, Any] | None = None,
    thresholds: Thresholds | None = None,
) -> list[Finding]:
    """Прогнать все правила и вернуть наблюдения по убыванию срочности."""
    t = thresholds or Thresholds()
    findings: list[Finding] = []

    for rule in (
        _rule_data_quality,
        _rule_queue_growth,
        _rule_flow_efficiency,
        _rule_blocked_time,
        _rule_blocker_pareto,
        _rule_blocker_reasons_missing,
        _rule_sle_missed,
        _rule_hidden_queue,
        _rule_unpredictable,
        _rule_aging,
        _rule_wip_per_person,
        _rule_reopen_rate,
        _rule_slowest_phase,
        _rule_load_imbalance,
        _rule_handoffs,
        _rule_stalled_work,
    ):
        finding = rule(
            summary=summary,
            flow=flow,
            arrival=arrival,
            aging=aging,
            people=people,
            quality=quality,
            blockers=blockers or {},
            sle=sle or {},
            hidden=hidden or {},
            predictability=predictability or {},
            forecast=forecast or {},
            t=t,
        )
        if finding is not None:
            findings.append(finding)

    order = {Severity.ACT: 0, Severity.WATCH: 1, Severity.INFO: 2}
    findings.sort(key=lambda f: order[f.severity])
    return findings


def _hours(seconds: float | None) -> str:
    if not seconds:
        return "0 ч"
    hours = seconds / 3600
    if hours < 24:
        return f"{hours:.1f} ч"
    return f"{hours / 9:.1f} рабочих дн"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


# --- правила -----------------------------------------------------------------


def _rule_data_quality(*, quality, t, **_) -> Finding | None:
    """Достоверность данных проверяется первой: она обесценивает остальное."""
    trustworthy = quality.get("trustworthy_pct", 100)
    if trustworthy >= t.data_quality_pct:
        return None

    top = quality.get("anomalies", [])[:2]
    causes = ", ".join(f"{a['label'].lower()} ({a['count']})" for a in top)
    return Finding(
        code="data_quality",
        severity=Severity.ACT if trustworthy < 40 else Severity.WATCH,
        title=f"Метрикам можно доверять лишь частично: надёжны {trustworthy}% задач",
        detail=(
            f"Остальные посчитаны по неполным или противоречивым данным. "
            f"Основные причины: {causes}."
            if causes
            else "Данных для надёжного расчёта недостаточно."
        ),
        suggestion=(
            "Прежде чем делать выводы о процессе, стоит договориться о заполнении "
            "дат работы — иначе любые средние и прогнозы будут смещены."
        ),
        evidence={"trustworthy_pct": trustworthy},
    )


def _rule_queue_growth(*, arrival, t, **_) -> Finding | None:
    """Очередь растёт, если поступление превышает закрытие несколько периодов."""
    net = arrival.get("net_per_period", [])
    if len(net) < t.queue_growth_periods + 1:
        return None

    # последний период часто неполный — не учитываем
    recent = net[-(t.queue_growth_periods + 1) : -1]
    if not recent or not all(value > 0 for value in recent):
        return None

    accumulated = sum(recent)
    return Finding(
        code="queue_growth",
        severity=Severity.ACT,
        title=f"Очередь растёт {len(recent)} периода подряд",
        detail=(
            f"За это время поступило на {accumulated} задач больше, чем закрыто. "
            f"При сохранении темпа время ожидания будет расти само по себе, "
            f"без изменения скорости работы."
        ),
        suggestion=(
            "Либо ограничить входящий поток, либо увеличить пропускную способность. "
            "Ускорение отдельных задач здесь не поможет: проблема в балансе."
        ),
        evidence={"net_per_period": recent, "accumulated": accumulated},
    )


def _rule_flow_efficiency(*, flow, t, **_) -> Finding | None:
    """Низкая доля активной работы означает, что задачи в основном ждут."""
    efficiency = flow.get("efficiency")
    if efficiency is None or efficiency >= t.low_flow_efficiency:
        return None

    queue_share = 1 - efficiency
    return Finding(
        code="low_flow_efficiency",
        severity=(Severity.ACT if efficiency < t.critical_flow_efficiency else Severity.WATCH),
        title=f"Задачи ждут {_pct(queue_share)} времени",
        detail=(
            f"Активная работа занимает лишь {_pct(efficiency)} от времени жизни задачи. "
            f"Основное время уходит на ожидание в очередях и блокировках."
        ),
        suggestion=(
            "Ускорять саму работу почти бесполезно: даже двукратное ускорение "
            "сократит общее время незначительно. Работать надо с ожиданием."
        ),
        evidence={"efficiency": efficiency},
    )


def _rule_blocked_time(*, flow, t, **_) -> Finding | None:
    """Блокировки съедают заметную долю времени."""
    blocked = flow.get("blocked_s", 0)
    touch = flow.get("touch_s", 0)
    queue = flow.get("queue_s", 0)
    total = touch + queue
    if not total:
        return None

    share = blocked / total
    if share < t.blocked_share_of_time:
        return None

    phase = next((p for p in flow.get("by_phase", []) if p["phase"] == "blocked"), None)
    median = phase["p50_s"] if phase else None

    return Finding(
        code="blocked_time",
        severity=Severity.ACT if share > 0.35 else Severity.WATCH,
        title=f"Блокировки отнимают {_pct(share)} времени",
        detail=(
            f"Типичная блокировка длится {_hours(median)}. "
            "Это время, когда задача занята, но работа по ней не идёт."
        )
        if median
        else "Значительная часть времени приходится на заблокированные задачи.",
        suggestion=(
            "Стоит разобрать причины блокировок: если они повторяются, "
            "это системная зависимость, а не случайность."
        ),
        evidence={"blocked_share": round(share, 3), "median_block_s": median},
    )


def _rule_blocker_pareto(*, blockers, **_) -> Finding | None:
    """Потери от блокировок сосредоточены в двух-трёх причинах."""
    rows = [r for r in blockers.get("pareto", []) if r["reason"] != "unknown"]
    if len(rows) < 2 or blockers.get("lost_business_s", 0) <= 0:
        return None

    # сколько причин набирают 80% потерь: если мало — есть чёткая цель
    head: list[dict[str, Any]] = []
    for row in rows:
        head.append(row)
        if row["cumulative_share"] >= 0.8:
            break
    if len(head) > 3:
        return None

    names = ", ".join(str(r["label"]).lower() for r in head)
    share = head[-1]["cumulative_share"]
    lost = sum(int(r["business_s"]) for r in head)

    return Finding(
        code="blocker_pareto",
        severity=Severity.WATCH,
        title=f"{_pct(share)} потерь от блокировок дают {len(head)} причины",
        detail=(
            f"Это {names}. Суммарно {_hours(lost)} простоя за период — "
            "остальные причины на их фоне почти не влияют."
        ),
        suggestion=(
            "Разбор одной верхней причины окупается больше, чем любые попытки "
            "ускорить саму работу: время теряется в ожидании, а не в разработке."
        ),
        evidence={
            "top_reasons": [r["reason"] for r in head],
            "covered_share": round(float(share), 3),
            "lost_business_s": lost,
        },
    )


def _rule_blocker_reasons_missing(*, blockers, t, **_) -> Finding | None:
    """Причины блокировок не заполняются, и Парето построить не из чего."""
    unknown = blockers.get("unknown_share", 0.0)
    if not blockers.get("episodes") or unknown < t.unknown_blocker_share:
        return None

    return Finding(
        code="blocker_reasons_missing",
        severity=Severity.WATCH,
        title=f"У {_pct(unknown)} блокировок не указана причина",
        detail=(
            "Длительность известна, а причина — нет. Такие потери нельзя "
            "сгруппировать, и разбор блокировок опирается на догадки."
        ),
        suggestion=(
            "Договориться заполнять причину при постановке флага. "
            "Пока доля высока, разбивка по причинам описывает дисциплину "
            "заполнения, а не реальные помехи."
        ),
        evidence={"unknown_share": round(float(unknown), 3)},
    )


def _rule_sle_missed(*, sle, t, **_) -> Finding | None:
    """Обещание перестало выполняться.

    Смотрим на последние периоды, а не на всю историю: обещание могло быть
    верным полгода назад и устареть с тех пор — это разные ситуации, и лечатся
    они по-разному. Разовый провал не считаем: он в природе перцентиля.
    """
    attainment = [a for a in sle.get("attainment", []) if a is not None]
    target = sle.get("target")
    if not target or len(attainment) < t.sle_min_periods:
        return None

    recent = attainment[-t.sle_min_periods :]
    # разовый провал в серии — нормальный разброс перцентиля; сигналим, только
    # когда ниже цели оказывается большинство периодов
    below = [value for value in recent if value < target - t.sle_tolerance]
    if len(below) <= len(recent) // 2:
        return None

    worst = min(recent)
    average = sum(recent) / len(recent)
    promise = sle.get("promises", [{}])[0]

    return Finding(
        code="sle_missed",
        severity=Severity.ACT if average < target - 2 * t.sle_tolerance else Severity.WATCH,
        title=f"Обещание держится в {_pct(average)} случаев вместо {_pct(target)}",
        detail=(
            f"За последние {len(recent)} периода попадание опускалось до {_pct(worst)}. "
            f"Обещание зафиксировано {promise.get('fixed_at', '')[:10]} "
            f"по выборке из {promise.get('sample_size', 0)} задач."
        ),
        suggestion=(
            "Либо система замедлилась и надо искать причину, либо обещание "
            "перестало ей соответствовать и его пора пересчитать. "
            "Сравнение с датой фиксации показывает, что из двух."
        ),
        evidence={
            "target": target,
            "recent_attainment": [round(a, 3) for a in recent],
            "average": round(average, 3),
        },
    )


def _rule_hidden_queue(*, hidden, t, **_) -> Finding | None:
    """Внутри «активного» статуса прячется очередь.

    Статус помечен работой, но задача в нём ждёт, пока её кто-нибудь возьмёт.
    Это завышает flow efficiency и прячет самую дорогую очередь: по отчётам
    работа идёт, фактически задача лежит.
    """
    phases = [p for p in hidden.get("by_phase", []) if p["share"] >= t.hidden_queue_share]
    if not phases:
        return None

    worst = max(phases, key=lambda p: p["waiting_s"])
    label = PHASE_LABELS.get(worst["phase"], worst["phase"])

    return Finding(
        code="hidden_queue",
        severity=Severity.WATCH,
        title=f"В статусе «{label}» {_pct(worst['share'])} времени — ожидание",
        detail=(
            f"Задачи проводят там {_hours(worst['waiting_s'])} до того, как их "
            "кто-нибудь возьмёт. Статус считается активной работой, поэтому это "
            "время попадает в touch time и завышает эффективность потока."
        ),
        suggestion=(
            "Либо разделить статус на «ждёт» и «в работе», либо признать его "
            "очередью. Пока он считается работой, самая дорогая очередь не видна."
        ),
        evidence={
            "phase": worst["phase"],
            "waiting_share": round(float(worst["share"]), 3),
            "waiting_s": worst["waiting_s"],
        },
    )


def _rule_unpredictable(*, predictability, t, **_) -> Finding | None:
    """Разброс времени цикла слишком велик, чтобы обещать по медиане.

    Индекс — отношение хвоста к медиане. Он не зависит от абсолютной скорости,
    поэтому ловит потерю управляемости даже там, где средние показатели
    выглядят стабильно.
    """
    known = [value for value in predictability.get("index", []) if value is not None]
    if len(known) < 2:
        return None

    latest = known[-1]
    if latest < t.predictability_index:
        return None

    growing = len(known) >= 3 and known[-1] > known[-3]
    p50 = next(
        (d["p50_s"] for d in reversed(predictability.get("details", [])) if d.get("reliable")),
        None,
    )

    return Finding(
        code="unpredictable",
        severity=Severity.ACT if latest >= t.predictability_index * 2 else Severity.WATCH,
        title=f"Долгие задачи идут в {latest:.1f} раза дольше типичных",
        detail=(
            f"Медиана — {_hours(p50)}, но верхние 2% задач тянутся во столько раз "
            "дольше. При таком разбросе обещание по медиане не выполняется чаще, "
            "чем выполняется."
            + (" Разброс растёт последние периоды." if growing else "")
        ),
        suggestion=(
            "Обещать по 85-му перцентилю, а не по среднему. Сам разброс обычно "
            "сокращается не ускорением работы, а уменьшением WIP и разбором "
            "очередей: именно ожидание делает хвост длинным."
        ),
        evidence={
            "index": latest,
            "history": known[-6:],
            "growing": growing,
        },
    )


def _rule_aging(*, aging, t, **_) -> Finding | None:
    """Много задач висит дольше обычного."""
    total = aging.get("total", 0)
    over = aging.get("over_p85", 0)
    if not total or not over:
        return None

    share = over / total
    if share < t.aging_over_p85_share:
        return None

    oldest = aging.get("items", [])[:5]
    return Finding(
        code="aging_wip",
        severity=Severity.ACT if share > 0.5 else Severity.WATCH,
        title=f"{over} из {total} незавершённых задач висят дольше обычного",
        detail=(
            f"Они превысили 85-й перцентиль времени цикла "
            f"({_hours(aging.get('reference', {}).get('p85'))}). "
            "Чем дольше задача открыта, тем меньше шансов, что её закончат быстро."
        ),
        suggestion=(
            "Разобрать самые старые: часть из них, вероятно, стоит закрыть "
            "или вернуть в бэклог, а не держать в работе."
        ),
        evidence={"over_p85": over, "total": total},
        ticket_keys=[item["key"] for item in oldest],
    )


def _rule_wip_per_person(*, aging, people, t, **_) -> Finding | None:
    """Слишком много задач одновременно на человека."""
    active_people = [p for p in people.get("people", []) if p.get("active_days")]
    if not active_people:
        return None

    total_wip = aging.get("total", 0)
    if not total_wip:
        return None

    per_person = total_wip / len(active_people)
    if per_person < t.wip_per_person:
        return None

    return Finding(
        code="high_wip",
        severity=Severity.WATCH,
        title=f"В среднем {per_person:.1f} незавершённых задач на человека",
        detail=(
            f"Всего {total_wip} задач в работе при {len(active_people)} участниках. "
            "Переключение между задачами само по себе съедает время."
        ),
        suggestion=(
            "Ограничение числа одновременных задач обычно сокращает время цикла "
            "без каких-либо других изменений."
        ),
        evidence={"wip_per_person": round(per_person, 1), "people": len(active_people)},
    )


def _rule_reopen_rate(*, summary, t, **_) -> Finding | None:
    """Задачи возвращаются после завершения."""
    completed = summary.get("completed", 0)
    reopens = summary.get("reopens", 0)
    if not completed or not reopens:
        return None

    rate = reopens / completed
    if rate < t.reopen_rate:
        return None

    return Finding(
        code="reopen_rate",
        severity=Severity.WATCH,
        title=f"{_pct(rate)} завершённых задач открывали заново",
        detail=(
            f"{reopens} возвратов на {completed} завершённых задач. "
            "Возврат означает, что задачу посчитали готовой преждевременно."
        ),
        suggestion=(
            "Стоит посмотреть, на каком шаге теряется качество: "
            "нечёткая постановка, пропуск проверки или спешка перед релизом."
        ),
        evidence={"reopen_rate": round(rate, 3), "reopens": reopens},
    )


def _rule_slowest_phase(*, flow, **_) -> Finding | None:
    """Фаза, в которой задачи проводят больше всего времени."""
    phases = [
        p
        for p in flow.get("by_phase", [])
        if p["phase"] not in ("done", "backlog") and p.get("p50_s")
    ]
    if len(phases) < 2:
        return None

    slowest = max(phases, key=lambda p: p["p50_s"])
    others = [p["p50_s"] for p in phases if p is not slowest]
    if not others or slowest["p50_s"] < 2 * (sum(others) / len(others)):
        return None

    labels = {
        "in_progress": "разработке",
        "blocked": "блокировке",
        "verify": "проверке",
        "review": "ревью",
        "done_pending": "ожидании релиза",
    }
    label = labels.get(slowest["phase"], slowest["phase"])

    return Finding(
        code="slowest_phase",
        severity=Severity.INFO,
        title=f"Больше всего времени задачи проводят в {label}",
        detail=(
            f"Медиана — {_hours(slowest['p50_s'])}, "
            f"85-й перцентиль — {_hours(slowest['p85_s'])}. "
            "Это заметно больше остальных фаз."
        ),
        suggestion=(
            "Здесь наибольший потенциал сокращения времени цикла: "
            "улучшения в других фазах дадут меньший эффект."
        ),
        evidence={"phase": slowest["phase"], "p50_s": slowest["p50_s"]},
    )


def _rule_load_imbalance(*, people, **_) -> Finding | None:
    """Нагрузка распределена неравномерно."""
    active = [p for p in people.get("people", []) if p.get("owned_s")]
    if len(active) < 3:
        return None

    loads = sorted(p["owned_s"] for p in active)
    median = loads[len(loads) // 2]
    if not median:
        return None

    highest = max(active, key=lambda p: p["owned_s"])
    ratio = highest["owned_s"] / median
    if ratio < 2.0:
        return None

    return Finding(
        code="load_imbalance",
        severity=Severity.INFO,
        title="Нагрузка распределена неравномерно",
        detail=(
            f"У {highest['person']} задач в {ratio:.1f} раза больше по времени "
            "владения, чем у типичного участника."
        ),
        suggestion=(
            "Возможны две причины: перекос в распределении задач или разная "
            "специализация. Показатель говорит о владении задачами, "
            "а не о затраченных усилиях — стоит уточнить у самого человека."
        ),
        evidence={"ratio": round(ratio, 1), "person": highest["person"]},
    )


def _rule_handoffs(*, summary, aging, t, **_) -> Finding | None:
    """Задачи часто переходят между людьми."""
    blocked_items = [
        item for item in aging.get("items", []) if item.get("is_blocked")
    ]
    if len(blocked_items) < 3:
        return None

    return Finding(
        code="blocked_now",
        severity=Severity.WATCH,
        title=f"Сейчас заблокировано {len(blocked_items)} задач",
        detail="Эти задачи заняты, но работа по ним не идёт.",
        suggestion="Разблокировка обычно даёт быстрый эффект: работа уже начата.",
        evidence={"count": len(blocked_items)},
        ticket_keys=[item["key"] for item in blocked_items[:5]],
    )


def _rule_stalled_work(*, forecast, **_) -> Finding | None:
    """Незавершённой работы намного больше, чем следует из темпа.

    Если из объёма работы следует срок в разы больший измеренного времени
    цикла, значит задачи числятся в работе, но не движутся.
    """
    health = forecast.get("wip_health") or {}
    ratio = health.get("ratio")
    if not ratio or ratio < 3:
        return None

    return Finding(
        code="stalled_work",
        severity=Severity.WATCH,
        title=f"Объём работы в {ratio:.0f} раз превышает пропускную способность",
        detail=(
            f"Измеренное время цикла — {health.get('measured_days')} дн, "
            f"но из объёма незавершённых задач следует {health.get('implied_days')} дн. "
            "Разрыв означает, что значительная часть задач не движется."
        ),
        suggestion=(
            "Стоит разделить задачи, которые действительно в работе, и те, "
            "что фактически лежат в очереди: смешивая их, невозможно оценить "
            "ни загрузку, ни сроки."
        ),
        evidence={"ratio": ratio},
    )


__all__ = ["Finding", "Severity", "Thresholds", "analyse"]
