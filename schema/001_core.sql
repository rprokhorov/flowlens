-- =====================================================================
-- FlowLens — ядро схемы (Postgres 16+)
-- Три слоя: RAW (истина от источника) → EFFECTIVE (решения ядра) → METRICS
-- =====================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ---------------------------------------------------------------------
-- СПРАВОЧНИКИ
-- ---------------------------------------------------------------------

CREATE TABLE source (
    id           bigserial PRIMARY KEY,
    kind         text        NOT NULL,          -- 'jira_dc', 'jira_cloud', 'redmine', 'csv'
    name         text        NOT NULL UNIQUE,
    base_url     text,
    profile      jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- source_profile: declared_dates, changelog, hints
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE calendar (
    id           bigserial PRIMARY KEY,
    name         text        NOT NULL UNIQUE,
    tz           text        NOT NULL DEFAULT 'Europe/Moscow',
    -- workweek: {"mon":[["10:00","19:00"]], ..., "sat":[], "sun":[]}
    workweek     jsonb       NOT NULL,
    holidays     date[]      NOT NULL DEFAULT '{}',
    extra_workdays date[]    NOT NULL DEFAULT '{}'   -- перенесённые рабочие субботы
);

CREATE TABLE team (
    id             bigserial PRIMARY KEY,
    name           text        NOT NULL,
    parent_team_id bigint      REFERENCES team(id),
    calendar_id    bigint      NOT NULL REFERENCES calendar(id),
    policy         jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- reconciliation_policy override
    UNIQUE (name, parent_team_id)
);

CREATE TABLE person (
    id            bigserial PRIMARY KEY,
    display_name  text        NOT NULL,
    primary_email text        UNIQUE,
    is_active     boolean     NOT NULL DEFAULT true
);

-- дедупликация людей между системами (4.4)
CREATE TABLE person_alias (
    id          bigserial PRIMARY KEY,
    person_id   bigint      NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    source_id   bigint      NOT NULL REFERENCES source(id),
    external_id text        NOT NULL,
    email       text,
    display_name text,
    UNIQUE (source_id, external_id)
);

-- членство в команде во времени (люди переходят между командами)
CREATE TABLE person_team (
    person_id  bigint      NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    team_id    bigint      NOT NULL REFERENCES team(id),
    valid_from date        NOT NULL,
    valid_to   date,                            -- NULL = по сей день
    role       text,                            -- 'dev','qa','lead','analyst'
    PRIMARY KEY (person_id, team_id, valid_from)
);

-- отсутствия (опционально; уточняет персональные метрики)
CREATE TABLE person_absence (
    id         bigserial PRIMARY KEY,
    person_id  bigint      NOT NULL REFERENCES person(id) ON DELETE CASCADE,
    starts_on  date        NOT NULL,
    ends_on    date        NOT NULL,
    kind       text        NOT NULL DEFAULT 'vacation'  -- vacation|sick|training
);

-- ---------------------------------------------------------------------
-- WORKFLOW: сырые статусы источника → канонические фазы
-- ---------------------------------------------------------------------

CREATE TYPE canonical_phase AS ENUM (
    'backlog',       -- new: ещё не начали
    'triage',        -- разбор/уточнение (пока не используется)
    'in_progress',   -- активная разработка
    'blocked',       -- заблокировано (у вас — отдельный статус)
    'review',        -- code review
    'verify',        -- qa
    'done_pending',  -- release: работа сделана, ждёт доставки
    'done',
    'cancelled'
);

CREATE TABLE workflow_status (
    id             bigserial PRIMARY KEY,
    source_id      bigint      NOT NULL REFERENCES source(id),
    external_name  text        NOT NULL,
    external_id    text,
    phase          canonical_phase NOT NULL,
    is_active_work boolean     NOT NULL,   -- идёт работа → входит в touch time
    is_queue       boolean     NOT NULL,   -- ожидание → входит в queue time
    is_terminal    boolean     NOT NULL DEFAULT false,
    board_order    int,                    -- порядок колонок для CFD
    UNIQUE (source_id, external_name)
);

-- класс обслуживания = issue_type × priority (пункт H)
CREATE TABLE service_class (
    id            bigserial PRIMARY KEY,
    source_id     bigint      NOT NULL REFERENCES source(id),
    issue_type    text        NOT NULL,
    priority      text,                    -- NULL = любой
    name          text        NOT NULL,    -- 'expedite','standard','fixed_date','intangible'
    sla_target_business_hours numeric,
    UNIQUE (source_id, issue_type, priority)
);

-- ---------------------------------------------------------------------
-- ЯДРО: тикеты
-- ---------------------------------------------------------------------

CREATE TABLE ticket (
    id            bigserial PRIMARY KEY,
    source_id     bigint      NOT NULL REFERENCES source(id),
    external_key  text        NOT NULL,       -- 'PROJ-123'
    external_id   text,
    project_key   text        NOT NULL,
    team_id       bigint      REFERENCES team(id),

    issue_type    text        NOT NULL,       -- Bug|Story|Task|Sub-task
    is_subtask    boolean     NOT NULL DEFAULT false,
    priority      text,
    service_class_id bigint   REFERENCES service_class(id),
    components    text[]      NOT NULL DEFAULT '{}',
    labels        text[]      NOT NULL DEFAULT '{}',

    parent_id     bigint      REFERENCES ticket(id),
    epic_id       bigint      REFERENCES ticket(id),

    summary       text,
    created_at    timestamptz NOT NULL,
    resolved_at   timestamptz,
    closed_at     timestamptz,

    current_status_id   bigint REFERENCES workflow_status(id),
    current_assignee_id bigint REFERENCES person(id),
    reporter_id         bigint REFERENCES person(id),

    story_points  numeric,
    raw_fields    jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- 4.3
    ingested_at   timestamptz NOT NULL DEFAULT now(),

    UNIQUE (source_id, external_key)
);

CREATE INDEX ticket_team_created_idx  ON ticket (team_id, created_at DESC);
CREATE INDEX ticket_parent_idx        ON ticket (parent_id) WHERE parent_id IS NOT NULL;
CREATE INDEX ticket_epic_idx          ON ticket (epic_id)   WHERE epic_id IS NOT NULL;
CREATE INDEX ticket_open_idx          ON ticket (team_id)   WHERE closed_at IS NULL;
CREATE INDEX ticket_raw_gin           ON ticket USING gin (raw_fields);

CREATE TABLE ticket_link (
    id            bigserial PRIMARY KEY,
    from_ticket_id bigint     NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    to_ticket_id   bigint     NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    link_type      text       NOT NULL,   -- 'blocks','duplicates','relates'
    created_at     timestamptz,
    removed_at     timestamptz,
    UNIQUE (from_ticket_id, to_ticket_id, link_type)
);

CREATE INDEX ticket_link_to_idx ON ticket_link (to_ticket_id, link_type);

-- ---------------------------------------------------------------------
-- RAW СЛОЙ: event log (источник истины, 3.2 вариант B)
-- ---------------------------------------------------------------------

CREATE TYPE event_type AS ENUM (
    'created', 'status_change', 'assignee_change', 'field_change',
    'link_change', 'flag_change', 'comment', 'resolved', 'reopened'
);

CREATE TABLE ticket_event (
    id              bigserial PRIMARY KEY,
    ticket_id       bigint      NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    kind            event_type  NOT NULL,
    occurred_at     timestamptz NOT NULL,
    actor_person_id bigint      REFERENCES person(id),
    field           text,
    old_value       text,
    new_value       text,
    old_status_id   bigint      REFERENCES workflow_status(id),
    new_status_id   bigint      REFERENCES workflow_status(id),
    source_event_id text,                     -- id записи changelog, для идемпотентности
    payload         jsonb       NOT NULL DEFAULT '{}'::jsonb
);

-- COALESCE, потому что NULL != NULL: события без поля (created, comment)
-- иначе дублировались бы при повторном импорте
CREATE UNIQUE INDEX ticket_event_natural_key_idx
    ON ticket_event (ticket_id, source_event_id, COALESCE(field, ''));

CREATE INDEX ticket_event_ticket_time_idx ON ticket_event (ticket_id, occurred_at);
CREATE INDEX ticket_event_time_idx        ON ticket_event (occurred_at);
CREATE INDEX ticket_event_actor_idx       ON ticket_event (actor_person_id, occurred_at);

-- заявленные вручную даты (пункт F) — хранятся СЫРЫМИ, без согласования
CREATE TABLE ticket_declared_date (
    id          bigserial PRIMARY KEY,
    ticket_id   bigint      NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    boundary    text        NOT NULL,   -- 'work_start' | 'work_end'
    value_at    timestamptz NOT NULL,
    precision   text        NOT NULL DEFAULT 'minute',  -- 'day' | 'minute'
    source_field text,
    observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (ticket_id, boundary)
);

CREATE TABLE ticket_comment (
    id               bigserial PRIMARY KEY,
    ticket_id        bigint      NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    external_id      text,
    author_person_id bigint      REFERENCES person(id),
    created_at       timestamptz NOT NULL,
    updated_at       timestamptz,
    body             text,
    is_internal      boolean     NOT NULL DEFAULT false,
    UNIQUE (ticket_id, external_id)
);

CREATE INDEX ticket_comment_ticket_idx ON ticket_comment (ticket_id, created_at);

-- ---------------------------------------------------------------------
-- EFFECTIVE СЛОЙ: решения ядра (пересчитываемо)
-- ---------------------------------------------------------------------

CREATE TABLE ticket_timeline_fact (
    ticket_id      bigint  NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    boundary       text    NOT NULL,          -- 'work_start' | 'work_end'
    system_at      timestamptz,               -- из changelog
    declared_at    timestamptz,               -- из поля
    effective_at   timestamptz,               -- решение политики
    chosen_source  text    NOT NULL,          -- 'system'|'declared'|'reconciled'|'inferred'
    confidence     text    NOT NULL,          -- 'high'|'medium'|'low'
    discrepancy_business_s bigint,
    anomaly_flags  text[]  NOT NULL DEFAULT '{}',
    policy_version text    NOT NULL,
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (ticket_id, boundary)
);

CREATE INDEX ttf_anomaly_idx ON ticket_timeline_fact USING gin (anomaly_flags);

-- ГЛАВНАЯ ТАБЛИЦА: разложение истории в интервалы
CREATE TABLE ticket_interval (
    id             bigserial PRIMARY KEY,
    ticket_id      bigint      NOT NULL REFERENCES ticket(id) ON DELETE CASCADE,
    seq            int         NOT NULL,
    status_id      bigint      NOT NULL REFERENCES workflow_status(id),
    phase          canonical_phase NOT NULL,
    assignee_id    bigint      REFERENCES person(id),
    is_blocked     boolean     NOT NULL DEFAULT false,
    blocked_from_status_id bigint REFERENCES workflow_status(id),  -- откуда ушли в блок
    blocker_reason text,                       -- категория причины (core/blockers.py)
    started_at     timestamptz NOT NULL,
    ended_at       timestamptz,               -- NULL = длится сейчас
    duration_calendar_s bigint,
    duration_business_s bigint,
    calendar_id    bigint      NOT NULL REFERENCES calendar(id),
    UNIQUE (ticket_id, seq)
);

CREATE INDEX ti_ticket_idx        ON ticket_interval (ticket_id, seq);
CREATE INDEX ti_status_time_idx   ON ticket_interval (status_id, started_at, ended_at);
CREATE INDEX ti_assignee_time_idx ON ticket_interval (assignee_id, started_at, ended_at);
CREATE INDEX ti_open_idx          ON ticket_interval (phase, started_at) WHERE ended_at IS NULL;
CREATE INDEX ti_range_idx         ON ticket_interval USING gist (tstzrange(started_at, ended_at));
CREATE INDEX ti_blocker_reason_idx ON ticket_interval (blocker_reason) WHERE blocker_reason IS NOT NULL;

-- ---------------------------------------------------------------------
-- METRICS СЛОЙ
-- ---------------------------------------------------------------------

CREATE TABLE ticket_metrics (
    ticket_id            bigint PRIMARY KEY REFERENCES ticket(id) ON DELETE CASCADE,
    lead_time_calendar_s   bigint,   -- created → done
    lead_time_business_s   bigint,
    cycle_time_calendar_s  bigint,   -- effective work_start → done
    cycle_time_business_s  bigint,
    touch_time_business_s  bigint,   -- сумма is_active_work
    queue_time_business_s  bigint,   -- сумма is_queue
    blocked_time_business_s bigint,
    release_wait_business_s bigint,  -- время в done_pending
    flow_efficiency        numeric,  -- touch / (touch+queue+blocked)
    reopen_count           int NOT NULL DEFAULT 0,
    assignee_change_count  int NOT NULL DEFAULT 0,
    status_change_count    int NOT NULL DEFAULT 0,
    blocked_episode_count  int NOT NULL DEFAULT 0,
    first_response_business_s bigint,
    confidence             text,     -- худшее из timeline_fact
    computed_at            timestamptz NOT NULL DEFAULT now()
);

-- ожидаемый уровень сервиса: обещание, зафиксированное на момент времени.
-- Пока перцентиль пересчитывается на лету, обещание всегда равно факту,
-- и вопрос «мы всё ещё держим слово?» не имеет смысла.
CREATE TABLE service_level_expectation (
    id            bigserial PRIMARY KEY,
    team_id       bigint REFERENCES team(id),
    issue_type    text,                    -- NULL = любой тип
    priority      text,                    -- NULL = любой приоритет
    percentile    int    NOT NULL DEFAULT 85,
    target_business_s bigint NOT NULL,
    sample_size   int    NOT NULL,         -- на какой выборке зафиксировали
    fixed_at      timestamptz NOT NULL DEFAULT now(),
    fixed_by      bigint REFERENCES person(id),
    note          text,
    retired_at    timestamptz,             -- NULL = действует
    CHECK (percentile BETWEEN 1 AND 99),
    CHECK (target_business_s > 0)
);

-- действующее обещание на класс — ровно одно
CREATE UNIQUE INDEX sle_active_class_idx
    ON service_level_expectation (
        COALESCE(team_id, 0), COALESCE(issue_type, ''), COALESCE(priority, ''), percentile
    )
    WHERE retired_at IS NULL;

-- нагрузка по людям (пункт B, вариант 3: время владения)
CREATE TABLE person_workload_daily (
    person_id     bigint NOT NULL REFERENCES person(id),
    day           date   NOT NULL,
    team_id       bigint REFERENCES team(id),
    active_tickets int   NOT NULL,
    owned_business_s bigint NOT NULL,
    touch_business_s bigint NOT NULL,
    blocked_business_s bigint NOT NULL,
    completed_count int   NOT NULL DEFAULT 0,
    PRIMARY KEY (person_id, day)
);

-- пункт E3: отметки о вмешательствах для baseline-сравнений
CREATE TABLE intervention (
    id          bigserial PRIMARY KEY,
    team_id     bigint      REFERENCES team(id),
    occurred_at timestamptz NOT NULL,
    title       text        NOT NULL,
    description text,
    kind        text,   -- 'wip_limit','process','staffing','tooling'
    created_by  bigint  REFERENCES person(id)
);

-- журнал синхронизаций
CREATE TABLE sync_run (
    id          bigserial PRIMARY KEY,
    source_id   bigint      NOT NULL REFERENCES source(id),
    started_at  timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    status      text        NOT NULL DEFAULT 'running',
    watermark   timestamptz,                 -- до какого updated дошли
    tickets_seen int NOT NULL DEFAULT 0,
    events_written int NOT NULL DEFAULT 0,
    error       text
);
