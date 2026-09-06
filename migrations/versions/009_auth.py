"""users, roles and team access

Сервис открыт всем, кто знает адрес. Для одной команды на ноутбуке это
нормально, для корпоративного сервиса — нет: метрики потока говорят о работе
конкретных людей.

Схема сразу рассчитана на два способа входа. Пароль (Basic) работает без
внешних зависимостей и годится, пока сервис не выставлен наружу. Поле
`external_subject` — это `sub` из OIDC-токена Keycloak: неизменный
идентификатор пользователя, в отличие от email и логина, которые меняются.
Когда подключим Keycloak, тот же пользователь просто получит заполненный
`external_subject` — без миграции данных и повторной раздачи прав.

Доступ к команде — отдельная таблица, а не `person_team`. Там членство для
метрик: кто над чем работал. Права и участие в работе — разные вещи:
руководитель может видеть команду, не будучи её исполнителем, а уволившийся
разработчик остаётся в истории метрик, но доступ терять должен.

Revision ID: 009_auth
Revises: 008_team_source
"""

from __future__ import annotations

from alembic import op

revision = "009_auth"
down_revision = "008_team_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS app_user (
            id            bigserial PRIMARY KEY,
            username      text NOT NULL UNIQUE,
            display_name  text,
            email         text,
            -- хеш пароля для входа без SSO; NULL у пользователей из Keycloak
            password_hash text,
            -- 'sub' из OIDC-токена: не меняется, в отличие от логина и почты
            external_subject text UNIQUE,
            -- админ управляет пользователями и правами, остальное — как у всех
            is_admin      boolean NOT NULL DEFAULT false,
            is_active     boolean NOT NULL DEFAULT true,
            created_at    timestamptz NOT NULL DEFAULT now(),
            last_login_at timestamptz,
            CHECK (password_hash IS NOT NULL OR external_subject IS NOT NULL)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS team_access (
            user_id  bigint NOT NULL REFERENCES app_user(id) ON DELETE CASCADE,
            team_id  bigint NOT NULL REFERENCES team(id) ON DELETE CASCADE,
            -- viewer смотрит метрики, owner ещё и настраивает подключения
            -- и классификацию статусов своей команды
            role     text   NOT NULL DEFAULT 'viewer',
            granted_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, team_id),
            CHECK (role IN ('viewer', 'owner'))
        )
        """
    )
    # Задел под автоматическую раздачу прав: когда выяснится, какая группа
    # Keycloak соответствует команде, связь включится заполнением этого поля —
    # без миграции и без ручного переназначения.
    op.execute("ALTER TABLE team ADD COLUMN IF NOT EXISTS external_group text")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS team_external_group_idx "
        "ON team (external_group) WHERE external_group IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS team_external_group_idx")
    op.execute("ALTER TABLE team DROP COLUMN IF EXISTS external_group")
    op.execute("DROP TABLE IF EXISTS team_access")
    op.execute("DROP TABLE IF EXISTS app_user")
