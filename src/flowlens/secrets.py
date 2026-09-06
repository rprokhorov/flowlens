"""Шифрование секретов источников.

Токен доступа к Jira хранится в базе зашифрованным. Смысл не в защите от
администратора базы — он и так может многое, — а в том, что дамп или бэкап
базы не даёт доступа к чужой Jira. Резервные копии живут дольше и попадают
в больше рук, чем сама база.

Ключ берётся из окружения и в репозиторий не попадает. Без ключа сохранить
секрет нельзя: молча писать открытый текст хуже, чем отказаться.
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

ENV_KEY = "FLOWLENS_SECRET_KEY"


class SecretsUnavailable(RuntimeError):
    """Ключ шифрования не задан — работать с секретами нельзя."""


def _fernet() -> Fernet:
    raw = os.environ.get(ENV_KEY)
    if not raw:
        raise SecretsUnavailable(
            f"Не задан {ENV_KEY}. Сгенерировать: "
            "python -c \"import secrets; print(secrets.token_urlsafe(32))\" — "
            "и передать сервису через окружение."
        )
    # Ключ произвольной длины приводится к 32 байтам: так его можно задать
    # любой строкой, не подбирая длину под требования Fernet.
    digest = hashlib.sha256(raw.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def available() -> bool:
    """Можно ли работать с секретами — для честного сообщения в интерфейсе."""
    return bool(os.environ.get(ENV_KEY))


def encrypt(value: str) -> bytes:
    """Зашифровать секрет для хранения."""
    if not value:
        raise ValueError("пустой секрет")
    return _fernet().encrypt(value.encode())


def decrypt(blob: bytes | memoryview | None) -> str | None:
    """Расшифровать секрет.

    Возвращает None, если расшифровать нечем или нечего: сменившийся ключ —
    штатная ситуация (например, сервис переехал), и она должна приводить
    к просьбе ввести токен заново, а не к падению.
    """
    if not blob:
        return None
    try:
        return _fernet().decrypt(bytes(blob)).decode()
    except (InvalidToken, SecretsUnavailable):
        return None


__all__ = ["ENV_KEY", "SecretsUnavailable", "available", "decrypt", "encrypt"]
