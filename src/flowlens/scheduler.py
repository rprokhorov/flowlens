"""Фоновое обновление данных по расписанию.

Владелец команды настраивает интервал один раз, дальше данные обновляются сами.
Планировщик намеренно простой: один цикл, последовательная обработка, без
внешних очередей. Синхронизаций у команды единицы в час, а лишняя инфраструктура
в сервисе, который ставят одной командой, обошлась бы дороже, чем даёт.

Ошибка одного подключения не останавливает остальные и сохраняется рядом с ним:
владелец команды должен видеть, почему его данные не обновились.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import Engine

from flowlens import sources

log = logging.getLogger(__name__)

# Как часто заглядывать в список подключений. Минимальный интервал синхронизации
# — 5 минут, поэтому проверять чаще незачем.
TICK_SECONDS = 60


def run_due(engine: Engine, now: datetime | None = None) -> list[dict[str, object]]:
    """Обработать все подключения, которым пора обновиться.

    Синхронно и последовательно: параллельная выгрузка из одной Jira упёрлась
    бы в её же ограничения по частоте запросов.
    """
    results: list[dict[str, object]] = []
    for source in sources.due_for_sync(engine, now):
        started = datetime.now(UTC)
        try:
            stats = sources.sync_source(engine, source)
            log.info(
                "синхронизация %s (команда %s): %s задач за %.1f с",
                source.base_url,
                source.team_id,
                stats["tickets"],
                (datetime.now(UTC) - started).total_seconds(),
            )
            results.append({"source_id": source.id, "status": "ok", **stats})
        except Exception as exc:  # noqa: BLE001
            # причина уже записана в team_source.last_sync_error
            log.warning("синхронизация %s не удалась: %s", source.base_url, exc)
            results.append({"source_id": source.id, "status": "failed", "error": str(exc)})
    return results


async def run_forever(engine: Engine, tick_seconds: int = TICK_SECONDS) -> None:
    """Цикл планировщика для фонового запуска вместе с сервисом."""
    log.info("планировщик запущен, проверка каждые %s с", tick_seconds)
    while True:
        try:
            # выгрузка блокирующая, поэтому уводим её из событийного цикла:
            # иначе дашборд перестал бы отвечать на время синхронизации
            await asyncio.to_thread(run_due, engine)
        except Exception:  # noqa: BLE001
            log.exception("сбой в цикле планировщика")
        await asyncio.sleep(tick_seconds)


__all__ = ["TICK_SECONDS", "run_due", "run_forever"]
