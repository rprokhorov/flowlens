"""Общие фикстуры и хелперы тестов."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from flowlens.core.calendar import WorkCalendar

MSK = ZoneInfo("Europe/Moscow")
DAY = 9 * 3600  # рабочих секунд в дне базового календаря


def dt(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    """Момент времени в московской зоне."""
    return datetime(y, m, d, hh, mm, tzinfo=MSK)


@pytest.fixture
def cal() -> WorkCalendar:
    """Базовый календарь: пн-пт 10:00-19:00, Москва."""
    return WorkCalendar(name="test", tz="Europe/Moscow")
