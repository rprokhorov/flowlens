# FlowLens

Анализ потока задач (Kanban) для тимлида: cycle time, CFD, aging WIP,
flow efficiency, нагрузка по людям, прогнозы.

Источник данных — Jira Data Center; архитектура source-agnostic,
коллекторы подключаемые.

## Состояние

Завершены этапы 1–5: фундамент расчётов, коллектор Jira, согласование данных,
API и дашборд, наблюдения по правилам.
Живое подключение к Jira не проверялось (нет доступа) — коллектор протестирован
на фикстурах и мок-транспорте. Дальше — прогнозы, см. `PLAN.md`.

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
.venv/bin/flowlens seed-demo --tickets 500 --months 12   # синтетика в базу
.venv/bin/flowlens export-demo --output demo.ndjson      # синтетика в файл
.venv/bin/flowlens import demo.ndjson                    # импорт выгрузки
.venv/bin/flowlens recompute --all                       # пересчёт
.venv/bin/flowlens quality -v                            # качество данных
.venv/bin/flowlens stats                                 # сводка
.venv/bin/flowlens advice                                # что стоит посмотреть
.venv/bin/flowlens serve                                 # дашборд на :8000
```

## Дашборд

`flowlens serve` поднимает веб-интерфейс: накопительная диаграмма потока,
распределение времени цикла с перцентилями, поступление против закрытия,
время по фазам, распределение нагрузки, список висящих задач и качество данных.

Фильтры (период, тип, приоритет, компонент, достоверность) и переключатель
рабочих/календарных часов действуют на все графики сразу.

Интерфейс подхватывает тему системы; кнопка в правом верхнем углу переключает
вручную. ECharts подключён локально — интернет не нужен.

Сбор данных из Jira (нужен `JIRA_TOKEN` в окружении):

```bash
cp examples/collector.jira.yml my-jira.yml   # поправьте под свою инсталляцию
export JIRA_TOKEN=...
.venv/bin/flowlens sync --config my-jira.yml --dry-run --output dump.ndjson
.venv/bin/flowlens sync --config my-jira.yml
```

## Согласование данных

Ключевая проблема реальных данных: сотрудники двигают задачи на доске
с опозданием, а настоящие даты проставляют вручную. Получается два
противоречивых источника истины.

FlowLens хранит **оба** сигнала сырыми и выбирает итоговое значение
по настраиваемой политике, помечая расхождения:

```bash
# посмотреть, что будет, если верить только истории статусов
.venv/bin/flowlens recompute-policy --prefer system_only --version system-only

# вернуться к доверию заявленным датам
.venv/bin/flowlens recompute-policy --prefer declared --version default
```

Смена политики пересчитывает метрики из event log — обращение к Jira не нужно.

Обнаруживаемые проблемы: незаполненные даты, даты раньше создания задачи,
перепутанные местами начало и конец, расхождение с историей статусов сверх
порога, проведение задачи по доске одним махом, нулевая длительность работы.

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
