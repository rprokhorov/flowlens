# FlowLens

Анализ потока задач (Kanban) для тимлида: cycle time, CFD, aging WIP,
flow efficiency, нагрузка по людям, прогнозы.

Источник данных — Jira Data Center; архитектура source-agnostic,
коллекторы подключаемые.

## Состояние

Этап 1 (фундамент расчётов) завершён. Работает на синтетических данных,
подключения к Jira пока нет — см. `PLAN.md`.

## Архитектура

Три слоя, каждый следующий полностью пересчитывается из предыдущего:

```
RAW        ticket_event + ticket_declared_date   ← источник истины
EFFECTIVE  ticket_interval                        ← свёртка событий в интервалы
METRICS    ticket_metrics, person_workload_daily  ← агрегаты
```

Смена календаря или политики согласования не требует повторного похода
в Jira — достаточно пересчёта.

`ticket_interval` — центральная таблица: отрезок, на котором неизменны
статус и исполнитель. Из неё считаются все графики.

## Установка

Postgres 16 (локально через brew):

```bash
brew install postgresql@16
brew services start postgresql@16
createdb flowlens
```

Либо через Docker: `docker compose up -d` (порт 5433, поправьте `.env`).

Питон:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env
.venv/bin/alembic upgrade head
```

## Использование

```bash
.venv/bin/flowlens seed-demo --tickets 500 --months 12   # синтетика
.venv/bin/flowlens recompute --all                        # пересчёт
.venv/bin/flowlens stats                                  # сводка
```

## Разработка

```bash
make test    # pytest
make lint    # ruff
```

Тесты не требуют базы, кроме `tests/test_pipeline_db.py` — он пропускается,
если Postgres недоступен.

## Модель времени

Все длительности считаются одновременно в календарных и рабочих секундах.
Рабочий календарь задаётся на команду (`calendar.workweek`, праздники,
перенесённые рабочие субботы), учитываются таймзона и переход на летнее время.

## Доска

| Статус | Фаза | Активная работа | Очередь |
|---|---|---|---|
| new | backlog | нет | да |
| in progress | in_progress | да | нет |
| blocked/hold | blocked | нет | да |
| qa | verify | да | нет |
| release | done_pending | нет | да |
| done | done | — | — |

`qa` считается активной работой: фактически это code review, тесты
автоматические. `release` включён в cycle time — команда отвечает «от и до».

Блокировка хранится и как фаза, и как флаг `is_blocked` на интервале,
с запоминанием `blocked_from_status_id` — из какого статуса ушли в блок.
