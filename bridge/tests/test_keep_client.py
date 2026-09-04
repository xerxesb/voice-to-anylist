"""Google's own failures must be classified, not mistaken for bugs in here.

The service acts on the distinction: an `AuthenticationError` pages a human at
once, a `ListClientError` is absorbed until it has happened five times running,
and anything else is reported as an unexpected error with a stack trace. So
what a Google outage is translated into decides whether it wakes somebody up.

`gkeepapi.exception.APIException` is the trap: it descends from `Exception`
rather than `KeepException`, so it slips past every `except` clause that
reaches for the Keep hierarchy.
"""

from __future__ import annotations

import pytest
from gkeepapi.exception import APIException, LoginException, SyncException

from voice_to_anylist.clients.base import AuthenticationError, ListClientError
from voice_to_anylist.clients.keep import KeepClient


def api_error(code: int, message: str = "Something went wrong") -> APIException:
    """An APIException shaped like the ones Google actually returns."""
    return APIException(
        code,
        {
            "code": code,
            "message": message,
            "errors": [{"message": message, "domain": "global", "reason": "backendError"}],
            "status": "UNAVAILABLE" if code >= 500 else "ERROR",
        },
    )


class StubKeep:
    """Stands in for a connected gkeepapi.Keep, failing on demand."""

    def __init__(self, error: Exception):
        self.error = error

    def sync(self, resync: bool = False) -> None:
        raise self.error

    def dump(self) -> dict:
        return {}

    def find(self, **kwargs):
        return iter(())


@pytest.fixture
def client(tmp_path):
    keep = KeepClient(
        email="x@example.com",
        master_token="aas_et/fake",
        note_title="Shopping list",
        state_path=tmp_path / "keep_state.json",
    )
    return keep


def connected(client: KeepClient, error: Exception) -> KeepClient:
    """Skip authentication and fail at the sync instead."""
    client._keep = StubKeep(error)
    client._note = object()  # commit() only checks that a note was loaded
    return client


# -- what the live alert was about ------------------------------------------


@pytest.mark.parametrize("method", ["fetch", "commit"])
def test_a_transient_google_5xx_is_a_client_error(client, method):
    """The reported incident: Google's auth backend answered 503.

    Transient, Google-side, and nothing to do with this code or the
    credentials -- so it belongs on the blip path, not the bug path.
    """
    error = api_error(503, "Authentication backend unavailable")

    with pytest.raises(ListClientError) as raised:
        getattr(connected(client, error), method)()

    assert not isinstance(raised.value, AuthenticationError)
    assert "503" in str(raised.value)


@pytest.mark.parametrize("code", [500, 502, 503, 504])
def test_every_server_error_lands_on_the_blip_path(client, code):
    with pytest.raises(ListClientError) as raised:
        connected(client, api_error(code)).fetch()

    assert not isinstance(raised.value, AuthenticationError)


def test_rate_limiting_is_a_client_error(client):
    """gkeepapi retries 429s itself, so seeing one here means it gave up."""
    with pytest.raises(ListClientError) as raised:
        connected(client, api_error(429, "Too many requests")).fetch()

    assert not isinstance(raised.value, AuthenticationError)


# -- the failure the alerting exists for ------------------------------------


@pytest.mark.parametrize("method", ["fetch", "commit"])
def test_an_unauthorised_response_is_an_authentication_error(client, method):
    """A revoked master token mid-run surfaces as APIException, not LoginException.

    gkeepapi refreshes the OAuth token twice before giving up, and what it
    raises then is a 401 APIException -- so this, not LoginException, is how a
    dead token usually reaches us once the process is already running.
    """
    with pytest.raises(AuthenticationError) as raised:
        getattr(connected(client, api_error(401, "Unauthorized")), method)()

    assert "bootstrap" in str(raised.value), "the message must say how to fix it"


def test_a_login_failure_during_sync_is_an_authentication_error(client):
    """The other route a dead token takes: refresh() failing inside sync().

    LoginException is a sibling of SyncException under KeepException, so it is
    missed by an `except (OSError, SyncException)` too.
    """
    with pytest.raises(AuthenticationError) as raised:
        connected(client, LoginException("BadAuthentication")).fetch()

    assert "bootstrap" in str(raised.value)


def test_authentication_failure_at_connect_still_reports_clearly(client, monkeypatch):
    """The restart path, which was already correct and must stay that way."""

    class FailingKeep:
        def authenticate(self, *args, **kwargs):
            raise LoginException("BadAuthentication")

    monkeypatch.setattr("gkeepapi.Keep", FailingKeep)

    with pytest.raises(AuthenticationError) as raised:
        client.fetch()

    assert "bootstrap" in str(raised.value)


def test_a_server_error_at_connect_is_not_an_authentication_error(client, monkeypatch):
    """Google being down at startup is not the same as being locked out."""

    class FailingKeep:
        def authenticate(self, *args, **kwargs):
            raise api_error(503, "Authentication backend unavailable")

    monkeypatch.setattr("gkeepapi.Keep", FailingKeep)

    with pytest.raises(ListClientError) as raised:
        client.fetch()

    assert not isinstance(raised.value, AuthenticationError)


# -- existing behaviour that must not regress -------------------------------


@pytest.mark.parametrize(
    "error",
    [SyncException("sync failed"), OSError("connection reset")],
    ids=["sync", "network"],
)
def test_sync_and_network_failures_remain_client_errors(client, error):
    with pytest.raises(ListClientError) as raised:
        connected(client, error).fetch()

    assert not isinstance(raised.value, AuthenticationError)


def test_an_unrecognised_api_code_is_still_not_a_bug(client):
    """A 4xx we have no specific handling for is Google's answer, not a crash."""
    with pytest.raises(ListClientError) as raised:
        connected(client, api_error(418, "I'm a teapot")).fetch()

    assert "418" in str(raised.value)


def test_a_failure_of_the_resync_itself_is_translated(client):
    """The retry lives inside an except block, so it is easy to leave uncovered."""
    from gkeepapi.exception import ResyncRequiredException

    class ResyncThenFail:
        def __init__(self):
            self.calls = 0

        def sync(self, resync: bool = False) -> None:
            self.calls += 1
            if self.calls == 1:
                raise ResyncRequiredException("resync required")
            raise api_error(503, "Authentication backend unavailable")

        def dump(self) -> dict:
            return {}

    client._keep = ResyncThenFail()

    with pytest.raises(ListClientError) as raised:
        client.fetch()

    assert "503" in str(raised.value)
