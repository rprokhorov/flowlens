"""HTTP-клиент Jira Data Center.

Только транспорт: пагинация, ретраи, ограничение частоты запросов.
Разбор полей в термины FlowLens — в `jira.py`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


@dataclass
class JiraConfig:
    """Параметры подключения."""

    base_url: str
    token: str | None = None  # Bearer (PAT) — предпочтительно для DC
    username: str | None = None  # Basic — запасной вариант
    password: str | None = None
    timeout_s: float = 30.0
    page_size: int = 100
    max_retries: int = 5
    min_interval_s: float = 0.0  # пауза между запросами, если сервер строгий
    verify_ssl: bool = True
    retry_base_delay_s: float = 1.0  # стартовая задержка ретраев

    def auth_headers(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def basic_auth(self) -> tuple[str, str] | None:
        if not self.token and self.username and self.password:
            return (self.username, self.password)
        return None


@dataclass
class JiraClient:
    """Тонкая обёртка над REST API Jira DC (api/2)."""

    config: JiraConfig
    transport: httpx.BaseTransport | None = None  # подменяется в тестах
    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    _last_request_at: float = field(default=0.0, init=False, repr=False)

    def __enter__(self) -> JiraClient:
        self._client = httpx.Client(
            base_url=self.config.base_url.rstrip("/"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self.config.auth_headers(),
            },
            auth=self.config.basic_auth(),
            timeout=self.config.timeout_s,
            verify=self.config.verify_ssl,
            transport=self.transport,
        )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _throttle(self) -> None:
        if self.config.min_interval_s <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.config.min_interval_s:
            time.sleep(self.config.min_interval_s - elapsed)

    def get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET с экспоненциальными ретраями на временных ошибках."""
        if self._client is None:
            raise RuntimeError("JiraClient must be used as a context manager")

        delay = self.config.retry_base_delay_s
        last_error: Exception | None = None

        for attempt in range(1, self.config.max_retries + 1):
            self._throttle()
            try:
                response = self._client.get(path, params=params)
                self._last_request_at = time.monotonic()
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                last_error = exc
                log.warning("Jira transport error (попытка %s): %s", attempt, exc)
            else:
                if response.status_code in RETRYABLE_STATUS:
                    retry_after = _retry_after_seconds(response) or delay
                    log.warning(
                        "Jira %s на %s, повтор через %.1f с (попытка %s)",
                        response.status_code,
                        path,
                        retry_after,
                        attempt,
                    )
                    last_error = httpx.HTTPStatusError(
                        f"HTTP {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                    if attempt < self.config.max_retries:
                        time.sleep(retry_after)
                        delay = min(delay * 2, 60.0)
                    continue
                response.raise_for_status()
                return response.json()

            if attempt < self.config.max_retries:
                time.sleep(delay)
                delay = min(delay * 2, 60.0)

        raise RuntimeError(
            f"Jira недоступна после {self.config.max_retries} попыток: {last_error}"
        ) from last_error

    def search(
        self,
        jql: str,
        *,
        fields: list[str] | None = None,
        expand: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Постраничный обход результатов JQL.

        Changelog запрашивается через expand=changelog. У тикетов с очень длинной
        историей Jira отдаёт её усечённой — такие случаи логируются, историю
        нужно дочитывать отдельным запросом.
        """
        start_at = 0
        total: int | None = None

        while True:
            params: dict[str, Any] = {
                "jql": jql,
                "startAt": start_at,
                "maxResults": self.config.page_size,
            }
            if fields:
                params["fields"] = ",".join(fields)
            if expand:
                params["expand"] = ",".join(expand)

            payload = self.get("/rest/api/2/search", params)
            issues = payload.get("issues", [])
            total = payload.get("total", 0)

            yield from issues

            start_at += len(issues)
            if not issues or start_at >= (total or 0):
                break

    def changelog(self, issue_key: str) -> list[dict[str, Any]]:
        """Полная история изменений тикета (для усечённых в search)."""
        entries: list[dict[str, Any]] = []
        start_at = 0
        while True:
            payload = self.get(
                f"/rest/api/2/issue/{issue_key}/changelog",
                {"startAt": start_at, "maxResults": self.config.page_size},
            )
            values = payload.get("values", [])
            entries.extend(values)
            start_at += len(values)
            if not values or start_at >= payload.get("total", 0):
                break
        return entries

    def comments(self, issue_key: str) -> list[dict[str, Any]]:
        """Комментарии тикета."""
        entries: list[dict[str, Any]] = []
        start_at = 0
        while True:
            payload = self.get(
                f"/rest/api/2/issue/{issue_key}/comment",
                {"startAt": start_at, "maxResults": self.config.page_size},
            )
            values = payload.get("comments", [])
            entries.extend(values)
            start_at += len(values)
            if not values or start_at >= payload.get("total", 0):
                break
        return entries

    def fields(self) -> list[dict[str, Any]]:
        """Список полей — нужен, чтобы найти customfield по имени."""
        payload = self.get("/rest/api/2/field")
        return payload if isinstance(payload, list) else []  # type: ignore[return-value]

    def statuses(self) -> list[dict[str, Any]]:
        payload = self.get("/rest/api/2/status")
        return payload if isinstance(payload, list) else []  # type: ignore[return-value]

    def myself(self) -> dict[str, Any]:
        """Проверка доступа."""
        return self.get("/rest/api/2/myself")


def _retry_after_seconds(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


__all__ = ["JiraClient", "JiraConfig"]
