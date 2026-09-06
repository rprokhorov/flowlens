"""Тесты входа и прав доступа."""

from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from flowlens import auth
from flowlens.api.app import app, get_engine
from flowlens.db import make_engine


@pytest.fixture(scope="module")
def engine():
    try:
        eng = make_engine()
        with eng.begin() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Postgres недоступен: {exc}")
    return eng


@pytest.fixture(autouse=True)
def enabled_auth(monkeypatch: pytest.MonkeyPatch):
    """Эти тесты проверяют именно вход, поэтому он должен быть включён."""
    monkeypatch.delenv(auth.ENV_AUTH_DISABLED, raising=False)


@pytest.fixture
def users(engine):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_user WHERE username LIKE 'test-%'"))
        calendar_id = conn.execute(
            text("SELECT id FROM calendar ORDER BY id LIMIT 1")
        ).scalar_one_or_none()
        if calendar_id is None:
            pytest.skip("нет календаря — нужен прогон seed_demo")
        team_id = conn.execute(
            text(
                "INSERT INTO team (name, calendar_id) VALUES ('auth-test', :cal) "
                "ON CONFLICT (name, COALESCE(parent_team_id, 0)) DO UPDATE "
                "SET name = EXCLUDED.name RETURNING id"
            ),
            {"cal": calendar_id},
        ).scalar_one()

    admin = auth.create_user(
        engine, username="test-admin", password="admin-pass", is_admin=True
    )
    owner = auth.create_user(engine, username="test-owner", password="owner-pass")
    auth.grant_access(engine, user_id=owner.id, team_id=team_id, role="owner")
    viewer = auth.create_user(engine, username="test-viewer", password="viewer-pass")
    auth.grant_access(engine, user_id=viewer.id, team_id=team_id, role="viewer")
    stranger = auth.create_user(engine, username="test-stranger", password="stranger-pass")

    yield {
        "team_id": team_id,
        "admin": admin,
        "owner": auth.get_user(engine, "test-owner"),
        "viewer": auth.get_user(engine, "test-viewer"),
        "stranger": stranger,
    }

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM app_user WHERE username LIKE 'test-%'"))
        conn.execute(text("DELETE FROM team WHERE name = 'auth-test'"))


@pytest.fixture
def client(engine):
    app.dependency_overrides[get_engine] = lambda: engine
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def basic(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


# --- пароли ------------------------------------------------------------------


def test_password_hash_is_salted() -> None:
    """Одинаковые пароли дают разные хеши: иначе видно, у кого пароль совпадает."""
    assert auth.hash_password("secret") != auth.hash_password("secret")


def test_password_verification() -> None:
    stored = auth.hash_password("secret")
    assert auth.verify_password("secret", stored)
    assert not auth.verify_password("wrong", stored)


def test_password_hash_hides_value() -> None:
    assert "secret" not in auth.hash_password("secret")


@pytest.mark.parametrize("stored", [None, "", "мусор", "pbkdf2_sha256$сломано"])
def test_verify_survives_broken_hash(stored: str | None) -> None:
    """Испорченная запись не должна пропускать вход или ронять сервис."""
    assert not auth.verify_password("secret", stored)


def test_empty_password_rejected() -> None:
    with pytest.raises(ValueError, match="пустой"):
        auth.hash_password("")


# --- вход --------------------------------------------------------------------


def test_authenticate_success(engine, users) -> None:
    assert auth.authenticate(engine, "test-admin", "admin-pass") is not None


def test_authenticate_wrong_password(engine, users) -> None:
    assert auth.authenticate(engine, "test-admin", "nope") is None


def test_authenticate_unknown_user(engine, users) -> None:
    assert auth.authenticate(engine, "test-ghost", "any") is None


def test_inactive_user_cannot_log_in(engine, users) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE app_user SET is_active = false WHERE username = 'test-viewer'")
        )
    assert auth.authenticate(engine, "test-viewer", "viewer-pass") is None


def test_user_needs_password_or_subject(engine) -> None:
    """Пользователь, в который нельзя войти, — ошибка настройки."""
    with pytest.raises(ValueError, match="пароль или внешний"):
        auth.create_user(engine, username="test-nobody")


# --- права -------------------------------------------------------------------


def test_owner_sees_only_own_team(users) -> None:
    owner = users["owner"]
    assert owner.can_view(users["team_id"])
    assert not owner.can_view(999999)


def test_owner_can_manage_own_team(users) -> None:
    owner = users["owner"]
    assert owner.can_manage(users["team_id"])
    assert not owner.can_manage(999999)


def test_viewer_cannot_manage(users) -> None:
    """Смотреть метрики и менять классификацию статусов — разные права."""
    viewer = users["viewer"]
    assert viewer.can_view(users["team_id"])
    assert not viewer.can_manage(users["team_id"])


def test_admin_sees_everything(users) -> None:
    admin = users["admin"]
    assert admin.can_view(999999)
    assert admin.can_manage(999999)


def test_stranger_sees_nothing(users) -> None:
    stranger = users["stranger"]
    assert not stranger.can_view(users["team_id"])
    # без прав хотя бы на одну команду сводка по всей компании недоступна
    assert not stranger.can_view(None)


def test_revoke_access(engine, users) -> None:
    viewer = users["viewer"]
    assert auth.revoke_access(engine, user_id=viewer.id, team_id=users["team_id"])
    assert not auth.get_user(engine, "test-viewer").can_view(users["team_id"])


def test_invalid_role_rejected(engine, users) -> None:
    with pytest.raises(ValueError, match="viewer или owner"):
        auth.grant_access(engine, user_id=users["admin"].id, team_id=1, role="root")


# --- HTTP --------------------------------------------------------------------


def test_api_requires_auth(client) -> None:
    """Главное: без входа данные не отдаются."""
    response = client.get("/api/summary")
    assert response.status_code == 401
    assert "Basic" in response.headers.get("WWW-Authenticate", "")


def test_health_stays_public(client) -> None:
    """Проверка живости нужна мониторингу до всякого входа."""
    assert client.get("/api/health").status_code == 200


def test_api_accepts_valid_credentials(client, users) -> None:
    response = client.get("/api/summary", headers=basic("test-admin", "admin-pass"))
    assert response.status_code == 200


def test_api_rejects_wrong_password(client, users) -> None:
    response = client.get("/api/summary", headers=basic("test-admin", "wrong"))
    assert response.status_code == 401


@pytest.mark.parametrize(
    "header",
    [
        "Basic !!!not-base64!!!",
        "Basic " + base64.b64encode(b"\xff\xfe").decode(),  # не UTF-8
        "Basic " + base64.b64encode(b"no-colon-here").decode(),
        "Bearer some-token",
        "Basic",
    ],
)
def test_api_rejects_malformed_header(client, users, header: str) -> None:
    """Кривой заголовок даёт отказ, а не пятисотку."""
    response = client.get("/api/summary", headers={"Authorization": header})
    assert response.status_code == 401


def test_me_reports_permissions(client, users) -> None:
    response = client.get("/api/me", headers=basic("test-owner", "owner-pass"))
    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "test-owner"
    assert str(users["team_id"]) in {str(k) for k in body["teams"]}


def test_auth_disabled_opens_everything(client, monkeypatch: pytest.MonkeyPatch) -> None:
    """Выключение должно быть явным и полным — без полумер."""
    monkeypatch.setenv(auth.ENV_AUTH_DISABLED, "1")
    assert client.get("/api/summary").status_code == 200


# --- права применяются к данным ----------------------------------------------


def test_stranger_gets_403_on_data(client, users) -> None:
    """Вход есть, доступа нет: вход и права — разные вещи."""
    response = client.get("/api/summary", headers=basic("test-stranger", "stranger-pass"))
    assert response.status_code == 403


def test_cannot_read_other_team(client, users) -> None:
    response = client.get(
        "/api/summary",
        params={"team_id": 999999},
        headers=basic("test-owner", "owner-pass"),
    )
    assert response.status_code == 403


def test_teams_list_is_filtered(client, users) -> None:
    """Названия чужих команд — тоже информация."""
    visible = client.get("/api/teams", headers=basic("test-owner", "owner-pass")).json()
    assert {t["id"] for t in visible} == {users["team_id"]}

    everything = client.get("/api/teams", headers=basic("test-admin", "admin-pass")).json()
    assert len(everything) >= len(visible)


def test_viewer_cannot_patch_status(client, users, engine) -> None:
    """Смотреть и менять классификацию — разные права."""
    with engine.begin() as conn:
        source_id = conn.execute(text("SELECT min(id) FROM source")).scalar_one()
        status_id = conn.execute(
            text(
                "INSERT INTO workflow_status "
                "(source_id, team_id, external_name, phase, is_active_work, "
                " is_queue, is_terminal, board_order) "
                "VALUES (:src, :team, 'auth-check', 'verify', true, false, false, 9) "
                "ON CONFLICT (team_id, external_name) WHERE team_id IS NOT NULL "
                "DO UPDATE SET board_order = 9 RETURNING id"
            ),
            {"src": source_id, "team": users["team_id"]},
        ).scalar_one()

    try:
        denied = client.patch(
            f"/api/statuses/{status_id}",
            json={"is_queue": True},
            headers=basic("test-viewer", "viewer-pass"),
        )
        assert denied.status_code == 403

        allowed = client.patch(
            f"/api/statuses/{status_id}",
            json={"is_queue": True},
            headers=basic("test-owner", "owner-pass"),
        )
        assert allowed.status_code == 200
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM workflow_status WHERE id = :i"), {"i": status_id})


def test_cannot_create_source_for_other_team(client, users) -> None:
    response = client.post(
        "/api/sources",
        json={"team_id": 999999, "base_url": "https://x.example", "jql": "a"},
        headers=basic("test-owner", "owner-pass"),
    )
    assert response.status_code == 403


def test_viewer_cannot_create_source(client, users) -> None:
    response = client.post(
        "/api/sources",
        json={"team_id": users["team_id"], "base_url": "https://x.example", "jql": "a"},
        headers=basic("test-viewer", "viewer-pass"),
    )
    assert response.status_code == 403


# --- защита от подбора -------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_attempts():
    """Счётчики глобальны для процесса — иначе тесты влияли бы друг на друга."""
    auth._attempts.clear()
    yield
    auth._attempts.clear()


def test_lockout_after_repeated_failures(engine, users) -> None:
    """Подбор пароля упирается во время, а не только в скорость сети."""
    for _ in range(10):
        assert auth.authenticate(engine, "test-admin", "wrong") is None

    with pytest.raises(auth.TooManyAttempts):
        auth.authenticate(engine, "test-admin", "wrong")


def test_lockout_blocks_even_correct_password(engine, users) -> None:
    """Иначе блокировка легко обходится: верный пароль как раз и подбирают."""
    for _ in range(10):
        auth.authenticate(engine, "test-admin", "wrong")

    with pytest.raises(auth.TooManyAttempts):
        auth.authenticate(engine, "test-admin", "admin-pass")


def test_lockout_is_per_user(engine, users) -> None:
    """Блокировка одного логина не должна закрывать вход остальным."""
    for _ in range(10):
        auth.authenticate(engine, "test-admin", "wrong")

    assert auth.authenticate(engine, "test-owner", "owner-pass") is not None


def test_success_resets_counter(engine, users) -> None:
    """Человек вспомнил пароль — счётчик обнуляется."""
    for _ in range(5):
        auth.authenticate(engine, "test-admin", "wrong")
    assert auth.authenticate(engine, "test-admin", "admin-pass") is not None
    assert not auth.is_locked("test-admin")


def test_lockout_expires(engine, users) -> None:
    """Блокировка временная: забытый пароль не должен закрывать доступ навсегда."""
    now = 1000.0
    for _ in range(10):
        auth.note_failure("test-admin", now)
    assert auth.is_locked("test-admin", now)
    assert not auth.is_locked("test-admin", now + auth._LOCKOUT_SECONDS + 1)


def test_lockout_reports_retry_time(engine, users) -> None:
    """Человеку нужно сказать, когда пробовать снова."""
    now = 1000.0
    for _ in range(10):
        auth.note_failure("test-admin", now)
    remaining = auth.seconds_until_unlock("test-admin", now + 60)
    assert 0 < remaining <= auth._LOCKOUT_SECONDS


def test_http_returns_429_when_locked(client, users) -> None:
    """429, а не 401: иначе браузер снова покажет форму и человек решит,
    что сервис сломался."""
    for _ in range(10):
        client.get("/api/summary", headers=basic("test-admin", "wrong"))

    response = client.get("/api/summary", headers=basic("test-admin", "admin-pass"))
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


# --- выход и смена пароля ----------------------------------------------------


def test_logout_returns_401(client) -> None:
    """Basic не умеет выходить: 401 заставляет браузер забыть реквизиты."""
    response = client.get("/api/logout")
    assert response.status_code == 401
    assert "Basic" in response.headers.get("WWW-Authenticate", "")


def test_logout_is_public(client, users) -> None:
    """Путь открыт: иначе middleware вернул бы 401 раньше, и клиент
    не отличил бы выход от «требуется вход»."""
    from flowlens.api.app import _is_public

    assert _is_public("/api/logout")


def test_change_password_requires_current(client, users) -> None:
    """Чужой человек за незапертым ноутбуком не должен менять пароль."""
    response = client.post(
        "/api/me/password",
        json={"current_password": "wrong", "new_password": "новый-пароль-12"},
        headers=basic("test-owner", "owner-pass"),
    )
    assert response.status_code == 403


def test_change_password_rejects_short(client, users) -> None:
    response = client.post(
        "/api/me/password",
        json={"current_password": "owner-pass", "new_password": "короткий"[:5]},
        headers=basic("test-owner", "owner-pass"),
    )
    assert response.status_code == 422


def test_change_password_works(client, users, engine) -> None:
    response = client.post(
        "/api/me/password",
        json={"current_password": "owner-pass", "new_password": "совершенно-новый-1"},
        headers=basic("test-owner", "owner-pass"),
    )
    assert response.status_code == 200
    assert auth.authenticate(engine, "test-owner", "совершенно-новый-1") is not None
    assert auth.authenticate(engine, "test-owner", "owner-pass") is None
