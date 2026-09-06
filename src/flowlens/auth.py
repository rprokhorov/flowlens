"""Пользователи, пароли и права на команды.

Способ входа сейчас один — логин и пароль. Схема при этом рассчитана и на
OIDC: у пользователя есть поле `external_subject` под `sub` из токена
Keycloak, и подключение SSO не потребует ни миграции, ни повторной раздачи
прав.

Права на команду отделены от членства в ней (`person_team`). Членство говорит,
кто над чем работал, — это данные для метрик. Доступ говорит, кому что видно.
Руководитель может смотреть команду, не будучи её исполнителем; уволившийся
разработчик остаётся в истории метрик, но доступ терять должен.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets as stdlib_secrets
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Engine, text

# PBKDF2 из стандартной библиотеки: медленный ровно настолько, насколько нужно,
# и не тянет зависимостей. Число итераций — текущая рекомендация OWASP.
_ITERATIONS = 600_000
_ALGORITHM = "pbkdf2_sha256"

ENV_ADMIN_USER = "FLOWLENS_ADMIN_USER"
ENV_ADMIN_PASSWORD = "FLOWLENS_ADMIN_PASSWORD"
ENV_AUTH_DISABLED = "FLOWLENS_AUTH_DISABLED"


@dataclass
class User:
    """Пользователь сервиса и его права."""

    id: int
    username: str
    display_name: str | None
    email: str | None
    is_admin: bool
    is_active: bool
    external_subject: str | None = None
    # team_id → роль; админу доступны все команды независимо от этой карты
    teams: dict[int, str] = field(default_factory=dict)

    def can_view(self, team_id: int | None) -> bool:
        if self.is_admin:
            return True
        if team_id is None:
            # без явной команды показываем только тем, у кого есть хоть одна:
            # иначе пользователь без прав увидел бы сводку по всей компании
            return bool(self.teams)
        return team_id in self.teams

    def can_manage(self, team_id: int | None) -> bool:
        """Настраивать подключения и классификацию статусов команды."""
        if self.is_admin:
            return True
        return team_id is not None and self.teams.get(team_id) == "owner"

    @property
    def visible_teams(self) -> list[int]:
        return sorted(self.teams)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name or self.username,
            "email": self.email,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
            "teams": self.teams,
            "from_sso": self.external_subject is not None,
        }


# --- пароли ------------------------------------------------------------------


def hash_password(password: str) -> str:
    """Хеш пароля с индивидуальной солью."""
    if not password:
        raise ValueError("пустой пароль")
    salt = stdlib_secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS)
    return f"{_ALGORITHM}${_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    """Проверить пароль.

    Сравнение постоянного времени: обычное сравнение строк выдаёт длину
    совпадающего префикса через время ответа.
    """
    if not stored or not password:
        return False
    try:
        algorithm, iterations, salt, expected = stored.split("$", 3)
    except ValueError:
        return False
    if algorithm != _ALGORITHM:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt.encode(), int(iterations)
    )
    return hmac.compare_digest(digest.hex(), expected)


# --- пользователи ------------------------------------------------------------

_COLUMNS = (
    "id, username, display_name, email, is_admin, is_active, external_subject"
)


def _load_teams(engine: Engine, user_id: int) -> dict[int, str]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT team_id, role FROM team_access WHERE user_id = :uid"),
            {"uid": user_id},
        ).all()
    return {row.team_id: row.role for row in rows}


def _row_to_user(engine: Engine, row: Any) -> User:
    return User(
        id=row.id,
        username=row.username,
        display_name=row.display_name,
        email=row.email,
        is_admin=row.is_admin,
        is_active=row.is_active,
        external_subject=row.external_subject,
        teams=_load_teams(engine, row.id),
    )


def get_user(engine: Engine, username: str) -> User | None:
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {_COLUMNS} FROM app_user WHERE username = :name"),
            {"name": username},
        ).one_or_none()
    return _row_to_user(engine, row) if row else None


def get_user_by_subject(engine: Engine, subject: str) -> User | None:
    """Найти пользователя по `sub` из OIDC — точка входа для Keycloak."""
    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {_COLUMNS} FROM app_user WHERE external_subject = :sub"),
            {"sub": subject},
        ).one_or_none()
    return _row_to_user(engine, row) if row else None


def list_users(engine: Engine) -> list[User]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(f"SELECT {_COLUMNS} FROM app_user ORDER BY username")
        ).all()
    return [_row_to_user(engine, row) for row in rows]


def create_user(
    engine: Engine,
    *,
    username: str,
    password: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    is_admin: bool = False,
    external_subject: str | None = None,
) -> User:
    """Завести пользователя.

    Нужен либо пароль, либо внешний идентификатор: пользователь, в который
    нельзя войти, — это ошибка настройки, а не допустимое состояние.
    """
    if not password and not external_subject:
        raise ValueError("нужен пароль или внешний идентификатор")

    with engine.begin() as conn:
        row = conn.execute(
            text(
                "INSERT INTO app_user "
                "  (username, display_name, email, password_hash, external_subject, is_admin) "
                "VALUES (:name, :display, :email, :hash, :subject, :admin) "
                "ON CONFLICT (username) DO UPDATE SET "
                "  display_name = COALESCE(EXCLUDED.display_name, app_user.display_name), "
                "  email = COALESCE(EXCLUDED.email, app_user.email), "
                "  password_hash = COALESCE(EXCLUDED.password_hash, app_user.password_hash), "
                "  external_subject = COALESCE("
                "      EXCLUDED.external_subject, app_user.external_subject), "
                "  is_admin = EXCLUDED.is_admin "
                f"RETURNING {_COLUMNS}"
            ),
            {
                "name": username,
                "display": display_name,
                "email": email,
                "hash": hash_password(password) if password else None,
                "subject": external_subject,
                "admin": is_admin,
            },
        ).one()
    return _row_to_user(engine, row)


def set_password(engine: Engine, username: str, password: str) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            text("UPDATE app_user SET password_hash = :hash WHERE username = :name"),
            {"hash": hash_password(password), "name": username},
        )
    return result.rowcount > 0


def delete_user(engine: Engine, username: str) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM app_user WHERE username = :name"), {"name": username}
        )
    return result.rowcount > 0


# --- защита от подбора -------------------------------------------------------

# Счётчики живут в памяти процесса, а не в базе: запись на каждую неудачную
# попытку сама превратилась бы в способ нагрузить сервис. Потеря счётчиков
# при перезапуске приемлема — подбор занимает намного больше времени.
_MAX_ATTEMPTS = 10
_LOCKOUT_SECONDS = 300
_attempts: dict[str, list[float]] = {}


def _prune(key: str, now: float) -> list[float]:
    recent = [t for t in _attempts.get(key, []) if now - t < _LOCKOUT_SECONDS]
    if recent:
        _attempts[key] = recent
    else:
        _attempts.pop(key, None)
    return recent


def is_locked(username: str, now: float | None = None) -> bool:
    """Заблокирован ли вход после серии неудач."""
    moment = now if now is not None else time.monotonic()
    return len(_prune(username, moment)) >= _MAX_ATTEMPTS


def seconds_until_unlock(username: str, now: float | None = None) -> int:
    """Через сколько можно пробовать снова — чтобы сказать это человеку."""
    moment = now if now is not None else time.monotonic()
    recent = _prune(username, moment)
    if len(recent) < _MAX_ATTEMPTS:
        return 0
    return max(1, int(_LOCKOUT_SECONDS - (moment - min(recent))))


def note_failure(username: str, now: float | None = None) -> None:
    moment = now if now is not None else time.monotonic()
    _attempts.setdefault(username, []).append(moment)


def reset_attempts(username: str) -> None:
    """Успешный вход снимает блокировку: человек вспомнил пароль."""
    _attempts.pop(username, None)


class TooManyAttempts(RuntimeError):
    """Слишком много неудачных попыток подряд."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"Слишком много попыток. Повторите через {retry_after} с.")
        self.retry_after = retry_after


def authenticate(engine: Engine, username: str, password: str) -> User | None:
    """Проверить логин и пароль.

    Хеш считается даже для несуществующего пользователя: иначе время ответа
    выдавало бы, какие логины заведены в системе.

    После нескольких неудач подряд вход по этому логину временно закрывается —
    иначе подбор пароля упирается только в скорость сети.
    """
    if is_locked(username):
        raise TooManyAttempts(seconds_until_unlock(username))

    with engine.begin() as conn:
        row = conn.execute(
            text(f"SELECT {_COLUMNS}, password_hash FROM app_user WHERE username = :name"),
            {"name": username},
        ).one_or_none()

    stored = row.password_hash if row else None
    matched = verify_password(password, stored)
    if not row or not matched or not row.is_active:
        note_failure(username)
        return None

    reset_attempts(username)

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE app_user SET last_login_at = now() WHERE id = :uid"),
            {"uid": row.id},
        )
    return _row_to_user(engine, row)


# --- права на команды --------------------------------------------------------


def grant_access(engine: Engine, *, user_id: int, team_id: int, role: str = "viewer") -> None:
    if role not in ("viewer", "owner"):
        raise ValueError("роль может быть viewer или owner")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO team_access (user_id, team_id, role) "
                "VALUES (:uid, :tid, :role) "
                "ON CONFLICT (user_id, team_id) DO UPDATE SET role = EXCLUDED.role"
            ),
            {"uid": user_id, "tid": team_id, "role": role},
        )


def revoke_access(engine: Engine, *, user_id: int, team_id: int) -> bool:
    with engine.begin() as conn:
        result = conn.execute(
            text("DELETE FROM team_access WHERE user_id = :uid AND team_id = :tid"),
            {"uid": user_id, "tid": team_id},
        )
    return result.rowcount > 0


# --- режим работы ------------------------------------------------------------


def auth_disabled() -> bool:
    """Выключена ли проверка входа.

    Локальный запуск на своей машине не должен требовать логина. Но выключение
    обязано быть явным: сервис, случайно оказавшийся открытым, хуже неудобства.
    """
    return os.environ.get(ENV_AUTH_DISABLED) == "1"


def ensure_admin(engine: Engine) -> User | None:
    """Создать администратора из окружения, если он задан.

    Пароль берётся только из окружения: значения по умолчанию у админских
    учёток — самый частый способ отдать сервис наружу.
    """
    username = os.environ.get(ENV_ADMIN_USER)
    password = os.environ.get(ENV_ADMIN_PASSWORD)
    if not username or not password:
        return None
    return create_user(
        engine, username=username, password=password, is_admin=True, display_name="Администратор"
    )


ANONYMOUS = User(
    id=0,
    username="anonymous",
    display_name="Без авторизации",
    email=None,
    is_admin=True,
    is_active=True,
)


__all__ = [
    "ANONYMOUS",
    "ENV_ADMIN_PASSWORD",
    "ENV_ADMIN_USER",
    "ENV_AUTH_DISABLED",
    "TooManyAttempts",
    "User",
    "authenticate",
    "auth_disabled",
    "create_user",
    "delete_user",
    "ensure_admin",
    "get_user",
    "get_user_by_subject",
    "grant_access",
    "hash_password",
    "is_locked",
    "list_users",
    "note_failure",
    "reset_attempts",
    "seconds_until_unlock",
    "revoke_access",
    "set_password",
    "verify_password",
]
