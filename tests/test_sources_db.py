"""Тесты хранения подключений и планировщика."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from flowlens import secrets, sources
from flowlens.db import make_engine

KEY = "тестовый-ключ-шифрования"


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
def secret_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(secrets.ENV_KEY, KEY)


@pytest.fixture
def team_id(engine) -> int:
    with engine.begin() as conn:
        calendar_id = conn.execute(
            text("SELECT id FROM calendar ORDER BY id LIMIT 1")
        ).scalar_one_or_none()
        if calendar_id is None:
            pytest.skip("нет календаря — нужен прогон seed_demo")
        return conn.execute(
            text(
                "INSERT INTO team (name, calendar_id) VALUES ('src-test', :cal) "
                "ON CONFLICT (name, COALESCE(parent_team_id, 0)) DO UPDATE "
                "SET name = EXCLUDED.name RETURNING id"
            ),
            {"cal": calendar_id},
        ).scalar_one()


@pytest.fixture(autouse=True)
def clean(engine, team_id):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM team_source WHERE team_id = :t"), {"t": team_id})
    yield
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM team_source WHERE team_id = :t"), {"t": team_id})


# --- шифрование --------------------------------------------------------------


def test_secret_roundtrip() -> None:
    assert secrets.decrypt(secrets.encrypt("pat-123")) == "pat-123"


def test_secret_ciphertext_hides_value() -> None:
    """В базе не должно быть открытого текста: дампы живут дольше базы."""
    assert b"pat-123" not in secrets.encrypt("pat-123")


def test_secret_requires_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Без ключа отказываем: молча писать открытый текст хуже, чем не сохранить."""
    monkeypatch.delenv(secrets.ENV_KEY, raising=False)
    assert not secrets.available()
    with pytest.raises(secrets.SecretsUnavailable):
        secrets.encrypt("pat-123")


def test_secret_survives_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Смена ключа — штатная ситуация: просим ввести заново, а не падаем."""
    blob = secrets.encrypt("pat-123")
    monkeypatch.setenv(secrets.ENV_KEY, "совсем-другой-ключ")
    assert secrets.decrypt(blob) is None


def test_secret_rejects_empty() -> None:
    with pytest.raises(ValueError, match="пустой"):
        secrets.encrypt("")


# --- хранение подключений ----------------------------------------------------


def test_save_and_read(engine, team_id: int) -> None:
    saved = sources.save_source(
        engine,
        team_id=team_id,
        base_url="https://jira.example.com",
        jql="project = A",
        secret="pat-abc",
    )
    assert saved.has_secret
    assert sources.read_secret(engine, saved.id) == "pat-abc"


def test_token_never_in_payload(engine, team_id: int) -> None:
    """Ответ API не должен содержать секрет ни в каком виде."""
    saved = sources.save_source(
        engine,
        team_id=team_id,
        base_url="https://jira.example.com",
        jql="project = A",
        secret="pat-secret-value",
    )
    assert "pat-secret-value" not in str(saved.as_dict())
    assert saved.as_dict()["has_secret"] is True


def test_url_normalised(engine, team_id: int) -> None:
    """Хвостовой слэш не должен плодить второе подключение к той же Jira."""
    first = sources.save_source(
        engine, team_id=team_id, base_url="https://jira.example.com/", jql="a", secret="x"
    )
    second = sources.save_source(
        engine, team_id=team_id, base_url="https://jira.example.com", jql="b", secret="x"
    )
    assert first.id == second.id
    assert second.jql == "b"


def test_update_without_token_keeps_it(engine, team_id: int) -> None:
    """Форма не показывает токен, поэтому пустое значение — «оставить как есть»."""
    saved = sources.save_source(
        engine, team_id=team_id, base_url="https://jira.example.com", jql="a", secret="pat-1"
    )
    sources.save_source(
        engine, team_id=team_id, base_url="https://jira.example.com", jql="изменённый"
    )
    assert sources.read_secret(engine, saved.id) == "pat-1"


def test_delete(engine, team_id: int) -> None:
    saved = sources.save_source(
        engine, team_id=team_id, base_url="https://jira.example.com", jql="a", secret="x"
    )
    assert sources.delete_source(engine, saved.id)
    assert sources.get_source(engine, saved.id) is None


# --- расписание --------------------------------------------------------------


def test_due_includes_never_synced(engine, team_id: int) -> None:
    """Только что настроенное подключение не должно ждать целый интервал."""
    saved = sources.save_source(
        engine,
        team_id=team_id,
        base_url="https://jira.example.com",
        jql="a",
        secret="x",
        sync_interval_minutes=60,
    )
    assert saved.id in {s.id for s in sources.due_for_sync(engine)}


def test_due_respects_interval(engine, team_id: int) -> None:
    saved = sources.save_source(
        engine,
        team_id=team_id,
        base_url="https://jira.example.com",
        jql="a",
        secret="x",
        sync_interval_minutes=60,
    )
    sources.mark_sync(engine, saved.id, status="ok")

    assert saved.id not in {s.id for s in sources.due_for_sync(engine)}
    later = datetime.now(UTC) + timedelta(minutes=61)
    assert saved.id in {s.id for s in sources.due_for_sync(engine, later)}


def test_manual_source_never_due(engine, team_id: int) -> None:
    """Без интервала подключение обновляется только по кнопке."""
    saved = sources.save_source(
        engine, team_id=team_id, base_url="https://jira.example.com", jql="a", secret="x"
    )
    assert saved.id not in {s.id for s in sources.due_for_sync(engine)}


def test_source_without_token_never_due(engine, team_id: int) -> None:
    """Планировщик не должен пытаться выгружать без токена."""
    saved = sources.save_source(
        engine,
        team_id=team_id,
        base_url="https://jira.example.com",
        jql="a",
        sync_interval_minutes=30,
    )
    assert not saved.has_secret
    assert saved.id not in {s.id for s in sources.due_for_sync(engine)}


def test_error_is_recorded(engine, team_id: int) -> None:
    """Ошибка хранится рядом с подключением: иначе данные просто не обновляются."""
    saved = sources.save_source(
        engine, team_id=team_id, base_url="https://jira.example.com", jql="a", secret="x"
    )
    sources.mark_sync(engine, saved.id, status="failed", error="401 Unauthorized")

    stored = sources.get_source(engine, saved.id)
    assert stored is not None
    assert stored.last_sync_status == "failed"
    assert "401" in (stored.last_sync_error or "")


def test_next_run_at(engine, team_id: int) -> None:
    saved = sources.save_source(
        engine,
        team_id=team_id,
        base_url="https://jira.example.com",
        jql="a",
        secret="x",
        sync_interval_minutes=15,
    )
    sources.mark_sync(engine, saved.id, status="ok")
    stored = sources.get_source(engine, saved.id)
    assert stored is not None

    following = sources.next_run_at(stored)
    assert following is not None
    assert following > datetime.now(UTC)
