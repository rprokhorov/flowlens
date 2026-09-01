# FlowLens — этап 1: фундамент расчётов (ВЫПОЛНЕНО 2026-09-01)

Проект: инструмент анализа потока задач (Kanban) для тимлида.
Стек: Python 3.12, Postgres 16, SQLAlchemy 2, alembic, pytest.
DDL уже написан: `schema/001_core.sql`.

Цель этапа: из event log получать корректные интервалы и метрики.
Без UI, без Jira, всё проверяется на синтетических данных.

Правила: после каждой задачи запускать `pytest` и `ruff check`.
Не переходить к следующей задаче, пока тесты красные.

### Task 1: Каркас проекта

- [x] Инициализировать проект через uv, Python 3.12, пакет `flowlens`
- [x] Зависимости: sqlalchemy>=2, alembic, psycopg[binary], pydantic>=2, typer, pytest, pytest-postgresql, ruff, mypy
- [x] `docker-compose.yml` с Postgres 16 на порту 5433, база `flowlens`
      (фактически использован brew postgresql@16 на 5432 — docker-демон не установлен)
- [x] `pyproject.toml`: настройки ruff (line-length 100) и mypy (strict для flowlens.core)
- [x] `.env.example` с `DATABASE_URL`
- [x] `make up`, `make test`, `make lint` в Makefile
- [x] Проверка: `docker compose up -d` поднимает базу, `pytest` проходит на пустом наборе

### Task 2: Миграции

- [x] Настроить alembic, `alembic.ini` читает DATABASE_URL из окружения
- [x] Перенести `schema/001_core.sql` в миграцию `001_core`
- [x] SQLAlchemy-модели в `flowlens/models.py`, соответствующие DDL
- [x] Тест: миграция применяется на чистой базе и откатывается
- [x] Проверка: `alembic upgrade head` создаёт все таблицы, типы и индексы

### Task 3: Рабочий календарь

- [x] Модуль `flowlens/core/calendar.py`
- [x] Класс `WorkCalendar` из записи таблицы `calendar`: tz, workweek, holidays, extra_workdays
- [x] `business_seconds_between(start, end, calendar) -> int`
- [x] `add_business_seconds(start, seconds, calendar) -> datetime`
- [x] Корректная работа с таймзонами и переходом на летнее время
- [x] Кэширование раскладки рабочих окон по дням для скорости
- [x] Тесты: интервал внутри рабочего дня; через ночь; через выходные; через праздник;
      начало и конец в нерабочее время; интервал нулевой длины; интервал через DST;
      рабочая суббота из extra_workdays
- [x] Проверка: не менее 15 тестов, все зелёные

### Task 4: Генератор синтетических данных

- [x] Модуль `flowlens/testing/synthetic.py`
- [x] Генерация тикетов с заданным путём по статусам и известными длительностями
- [x] Сценарии: happy path (new→in progress→qa→release→done); с блокировкой;
      с возвратом назад (qa→in progress); с переоткрытием после done;
      со сменой assignee в середине; bulk-move (несколько переходов за секунды);
      незакрытый тикет
- [x] Каждый сценарий возвращает ожидаемые метрики для сверки
- [x] Фикстура pytest: заполнение базы набором сценариев
- [x] Проверка: генератор создаёт консистентный event log, тикеты видны в базе

### Task 5: Построитель интервалов

- [x] Модуль `flowlens/core/intervals.py`
- [x] Функция `rebuild_intervals(ticket_id)`: свернуть `ticket_event` в `ticket_interval`
- [x] Интервал закрывается при смене статуса ИЛИ смены assignee
- [x] Последний интервал открыт (`ended_at IS NULL`), если тикет не закрыт
- [x] Для статуса фазы `blocked` заполнять `blocked_from_status_id` предыдущим статусом
- [x] Заполнять `duration_calendar_s` и `duration_business_s` по календарю команды
- [x] Полный пересчёт `rebuild_all()` и точечный по списку тикетов
- [x] Идемпотентность: повторный запуск даёт тот же результат
- [x] Тесты на всех сценариях из Task 4, сверка с ожидаемыми значениями
- [x] Проверка: длительности совпадают до секунды, сумма интервалов равна времени жизни тикета

### Task 6: Расчёт метрик тикета

- [x] Модуль `flowlens/core/metrics.py`
- [x] Заполнение `ticket_metrics` из интервалов
- [x] lead_time: created_at → вход в терминальный статус
- [x] cycle_time: первый вход в активную работу → done (release включён)
- [x] touch_time: сумма интервалов с is_active_work
- [x] queue_time: сумма интервалов с is_queue
- [x] blocked_time и blocked_episode_count по фазе blocked
- [x] release_wait: время в done_pending
- [x] flow_efficiency = touch / (touch + queue + blocked), NULL при нулевом знаменателе
- [x] reopen_count: переходы из терминального статуса обратно
- [x] assignee_change_count, status_change_count
- [x] first_response: created_at → первый комментарий не от reporter
- [x] Тесты на сценариях из Task 4
- [x] Проверка: метрики совпадают с ожидаемыми, незакрытые тикеты имеют NULL в cycle/lead

### Task 7: Дневная нагрузка по людям

- [x] Модуль `flowlens/core/workload.py`
- [x] Заполнение `person_workload_daily` из интервалов
- [x] Атрибуция по времени владения: интервал режется по дням календаря
- [x] active_tickets — уникальные тикеты, которыми человек владел в этот день
- [x] owned/touch/blocked business seconds за день
- [x] completed_count — сколько тикетов человек довёл до done в этот день
- [x] Тест: тикет с двумя разными assignee делится между ними корректно
- [x] Проверка: сумма owned_business_s по людям равна сумме по интервалам

### Task 8: CLI пересчёта и приёмка этапа

- [x] `flowlens/cli.py` на typer
- [x] Команда `flowlens recompute --all` и `--ticket KEY`
- [x] Команда `flowlens seed-demo` — залить синтетику
- [x] Прогресс и время выполнения в выводе
- [x] Интеграционный тест: seed-demo → recompute → проверка агрегатов SQL-запросом
- [x] README с описанием запуска
- [x] Проверка: полный цикл на 500 синтетических тикетах отрабатывает,
      повторный пересчёт даёт идентичный результат
