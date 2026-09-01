"""Дедупликация людей между источниками.

Один человек может иметь разные учётные записи в Jira, Redmine и т.д.
Ядро сводит их в одну сущность `person` через таблицу алиасов.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


@dataclass(frozen=True)
class PersonIdentity:
    """Идентичность человека в конкретном источнике."""

    external_id: str
    display_name: str | None = None
    email: str | None = None

    def normalized_email(self) -> str | None:
        if not self.email:
            return None
        return self.email.strip().lower() or None

    def match_key(self) -> str:
        """Ключ слияния: email надёжнее имени, но имя лучше, чем ничего."""
        email = self.normalized_email()
        if email:
            return f"email:{email}"
        if self.display_name:
            return f"name:{normalize_name(self.display_name)}"
        return f"ext:{self.external_id.strip().lower()}"


def normalize_name(name: str) -> str:
    """Привести имя к сопоставимому виду.

    Убирает регистр, диакритику и порядок слов: «Петров Иван» и «Иван Петров»
    должны считаться одним человеком.
    """
    lowered = unicodedata.normalize("NFKD", name.strip().lower())
    stripped = "".join(ch for ch in lowered if not unicodedata.combining(ch))
    transliterated = "".join(TRANSLIT.get(ch, ch) for ch in stripped)
    words = sorted(re.findall(r"[a-z0-9]+", transliterated))
    return " ".join(words)


class PersonResolver:
    """Сводит учётные записи разных источников к одному человеку.

    Слияние по email — надёжное. Слияние по имени применяется только
    когда email отсутствует, и может ошибаться на полных тёзках.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, int] = {}
        self._identities: dict[int, list[PersonIdentity]] = {}
        self._next_id = 1

    def resolve(self, identity: PersonIdentity) -> int:
        """Вернуть внутренний id человека, создав его при необходимости."""
        keys = self._candidate_keys(identity)
        for key in keys:
            existing = self._by_key.get(key)
            if existing is not None:
                for k in keys:
                    self._by_key.setdefault(k, existing)
                self._identities[existing].append(identity)
                return existing

        person_id = self._next_id
        self._next_id += 1
        for key in keys:
            self._by_key[key] = person_id
        self._identities[person_id] = [identity]
        return person_id

    def _candidate_keys(self, identity: PersonIdentity) -> list[str]:
        keys = []
        email = identity.normalized_email()
        if email:
            keys.append(f"email:{email}")
        if identity.display_name:
            keys.append(f"name:{normalize_name(identity.display_name)}")
        keys.append(f"ext:{identity.external_id.strip().lower()}")
        return keys

    def identities(self, person_id: int) -> list[PersonIdentity]:
        return self._identities.get(person_id, [])

    def people_count(self) -> int:
        return len(self._identities)

    def best_display_name(self, person_id: int) -> str | None:
        """Наиболее полное из известных имён."""
        names = [i.display_name for i in self.identities(person_id) if i.display_name]
        return max(names, key=len) if names else None

    def best_email(self, person_id: int) -> str | None:
        for identity in self.identities(person_id):
            email = identity.normalized_email()
            if email:
                return email
        return None


__all__ = ["PersonIdentity", "PersonResolver", "normalize_name"]
