"""AnyList side of the mirror, spoken to over loopback.

Thin by design: the Node sidecar owns the protocol quirks, this owns the
translation into :class:`ListItem` and the error taxonomy the engine expects.
"""

from __future__ import annotations

import logging

import httpx

from ..normalise import key as normalise_key
from .base import AuthenticationError, ListClientError, ListItem, RetryLater

log = logging.getLogger(__name__)


class AnyListClient:
    name = "anylist"

    def __init__(
        self,
        base_url: str,
        list_name: str,
        token: str = "",
        timeout: float = 30.0,
    ):
        self.list_name = list_name
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as error:
            raise ListClientError(f"AnyList sidecar unreachable: {error}") from error
        if response.status_code == 401:
            raise AuthenticationError("AnyList sidecar rejected the API token")
        retry_after = _retry_after(response)
        if response.status_code in (429, 503) and retry_after is not None:
            raise RetryLater(
                f"AnyList {method} {path} deferred for {retry_after:.0f}s: {_error_of(response)}",
                retry_after,
            )
        if response.status_code >= 400:
            detail = response.text[:300]
            if response.status_code >= 500 and "auth" in detail.lower():
                raise AuthenticationError(f"AnyList login failed: {detail}")
            raise ListClientError(f"AnyList {method} {path} -> {response.status_code}: {detail}")
        return response

    def lists(self) -> list[str]:
        """Every list name on the account, for `doctor` to print.

        The configured name has to be matched exactly, and the failure mode of
        getting it wrong is a sync that proposes deleting everything.
        """
        return list(self._request("GET", "/lists").json().get("lists", []))

    def fetch(self) -> list[ListItem]:
        payload = self._request("GET", "/items", params={"list": self.list_name}).json()
        return [
            ListItem(
                id=raw["id"],
                name=raw["name"],
                quantity=raw.get("quantity"),
                checked=bool(raw.get("checked")),
            )
            for raw in payload.get("items", [])
            # An item whose name normalises to nothing has no identity to match
            # on, so it is left strictly alone rather than mirrored.
            if normalise_key(raw.get("name") or "")
        ]

    def add(self, name: str, quantity: str | None, checked: bool = False) -> str:
        payload = self._request(
            "POST",
            "/items",
            json={
                "list": self.list_name,
                "name": name,
                "quantity": quantity,
                "checked": checked,
            },
        ).json()
        return payload["id"]

    def set_quantity(self, item_id: str, quantity: str | None) -> None:
        self._request(
            "PATCH", f"/items/{item_id}", json={"list": self.list_name, "quantity": quantity}
        )

    def set_checked(self, item_id: str, checked: bool) -> None:
        self._request(
            "PATCH", f"/items/{item_id}", json={"list": self.list_name, "checked": checked}
        )

    def remove(self, item_id: str) -> None:
        self._request("DELETE", f"/items/{item_id}", json={"list": self.list_name})

    def commit(self) -> None:
        """AnyList writes are applied per request; nothing is batched."""

    def download_stats(self) -> dict | None:
        """How often the sidecar has downloaded the account, for `vta status`.

        Best-effort: a status page must not fail because the sidecar is down.
        """
        try:
            return dict(self._request("GET", "/stats").json())
        except (ListClientError, ValueError):
            return None

    def healthy(self) -> bool:
        try:
            return bool(self._request("GET", "/health").json().get("ok"))
        except ListClientError:
            return False


def _retry_after(response: httpx.Response) -> float | None:
    """Retry-After in seconds, if the sidecar sent a usable one."""
    raw = response.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


def _error_of(response: httpx.Response) -> str:
    try:
        return str(response.json().get("error") or response.text[:300])
    except ValueError:
        return response.text[:300]
