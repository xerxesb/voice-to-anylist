"""How often the sidecar downloads the AnyList account.

Each download is the *whole* account -- every list, recipes, the meal plan --
because the `anylist` package has no way to read one list.  Downloading on
every poll came to about 180 an hour, and AnyList suspended syncing for the
whole account, official apps included.  These tests pin the replacement
behaviour through the real Python client and the real Express server, with
only the AnyList service itself stubbed.
"""

from __future__ import annotations

import httpx
import pytest

from voice_to_anylist.clients.anylist import AnyListClient
from voice_to_anylist.clients.base import FakeListClient, ListClientError, ListItem, RetryLater
from voice_to_anylist.engine import SyncEngine
from voice_to_anylist.store import ShadowStore


def _stub(base_url: str, route: str, body: dict | None = None) -> None:
    httpx.post(f"{base_url}/__stub/{route}", json=body or {}, timeout=5).raise_for_status()


def _downloads(base_url: str) -> int:
    return httpx.get(f"{base_url}/__stub/state", timeout=5).json()["downloads"]


def _stats(base_url: str) -> dict:
    return httpx.get(f"{base_url}/stats", timeout=5).json()


@pytest.fixture
def limited(limited_sidecar):
    _stub(limited_sidecar, "reset")
    api = AnyListClient(limited_sidecar, "Grocery")
    yield limited_sidecar, api
    api.close()


@pytest.fixture
def eager(eager_sidecar):
    _stub(eager_sidecar, "reset")
    api = AnyListClient(eager_sidecar, "Grocery")
    yield eager_sidecar, api
    api.close()


# -- the regression itself ----------------------------------------------------


def test_repeated_polls_download_the_account_once(limited):
    base_url, api = limited

    for _ in range(50):
        api.fetch()

    assert _downloads(base_url) == 1


def test_a_bridge_running_many_cycles_with_voice_adds_downloads_once(limited):
    """The shape of the incident: a poll every cycle, writes now and then.

    Before, this was one download per cycle plus one per write, plus another
    per write when AnyList's push echoed it back.
    """
    base_url, api = limited
    keep = FakeListClient("keep")
    store = ShadowStore(":memory:")
    engine = SyncEngine(keep, api, store)
    try:
        # The first sync bootstraps from AnyList as the master, so settle it
        # before any voice adds arrive.
        engine.run_once()
        for cycle in range(40):
            if cycle % 8 == 0:
                keep.replace_items(
                    [*keep.fetch(), ListItem(id=f"k{cycle}", name=f"thing {cycle}")]
                )
            engine.run_once()
    finally:
        store.close()

    names = {i.name for i in api.fetch()}
    assert {"thing 0", "thing 8", "thing 16", "thing 24", "thing 32"} <= names
    assert _downloads(base_url) == 1


def test_writes_read_back_without_a_download(limited):
    base_url, api = limited
    api.fetch()

    new_id = api.add("strawberries", "2")
    api.set_checked(new_id, True)
    api.set_quantity(new_id, "3")
    after = {i.id: i for i in api.fetch()}

    assert after[new_id].checked is True
    assert after[new_id].quantity == "3"
    assert _downloads(base_url) == 1


def test_a_push_inside_the_floor_is_owed_not_forgotten(limited):
    base_url, api = limited
    api.fetch()

    _stub(base_url, "external-edit", {"name": "milk", "checked": True})
    api.fetch()

    stats = _stats(base_url)
    assert stats["stale"] is True
    assert stats["stale_reason"] == "push"
    assert _downloads(base_url) == 1


def test_list_names_come_from_the_same_copy(limited):
    base_url, api = limited
    api.fetch()

    assert api.lists() == ["Grocery"]
    assert _downloads(base_url) == 1


# -- staying current ------------------------------------------------------------


def test_an_edit_in_the_app_is_seen_after_its_push(eager):
    base_url, api = eager
    api.fetch()

    _stub(base_url, "external-edit", {"name": "milk", "checked": True})

    assert next(i for i in api.fetch() if i.name == "milk").checked is True
    assert _downloads(base_url) == 2
    assert _stats(base_url)["last_refresh_reason"] == "push"


def test_with_no_push_there_is_no_download(eager):
    base_url, api = eager
    api.fetch()

    for _ in range(20):
        api.fetch()

    assert _downloads(base_url) == 1


def test_a_dropped_websocket_falls_back_to_polling_and_reconnects(eager):
    base_url, api = eager
    api.fetch()
    _stub(base_url, "websocket", {"open": False})

    api.fetch()

    assert _downloads(base_url) == 2
    assert _stats(base_url)["last_refresh_reason"] == "websocket-down"
    assert httpx.get(f"{base_url}/__stub/state", timeout=5).json()["reconnects"] == 1


def test_a_refused_write_forces_a_fresh_read(eager):
    base_url, api = eager
    api.fetch()
    _stub(base_url, "fail-write", {"statusCode": 500})

    with pytest.raises(ListClientError):
        api.add("bread", None)
    api.fetch()

    assert _downloads(base_url) == 2


# -- backing off ------------------------------------------------------------------


def test_a_failed_download_tells_the_bridge_when_to_come_back(limited):
    base_url, api = limited
    _stub(base_url, "fail-download", {"statusCode": 500})

    with pytest.raises(RetryLater) as raised:
        api.fetch()

    assert 55 <= raised.value.retry_after <= 61
    assert _downloads(base_url) == 1


def test_during_the_pause_nothing_reaches_anylist(limited):
    base_url, api = limited
    _stub(base_url, "fail-download", {"statusCode": 500})
    with pytest.raises(RetryLater):
        api.fetch()

    for _ in range(10):
        with pytest.raises(RetryLater):
            api.fetch()
        with pytest.raises(RetryLater):
            api.add("bread", None)

    assert _downloads(base_url) == 1


def test_anylist_asking_for_an_hour_gets_an_hour(limited):
    base_url, api = limited
    _stub(base_url, "fail-download", {"statusCode": 429, "retryAfter": 3600})

    with pytest.raises(RetryLater) as raised:
        api.fetch()

    assert raised.value.retry_after >= 3590
    assert "429" in str(raised.value)


def test_a_throttled_write_pauses_reads_too(limited):
    base_url, api = limited
    api.fetch()
    _stub(base_url, "fail-write", {"statusCode": 429, "retryAfter": 600})

    with pytest.raises(RetryLater) as refused:
        api.add("bread", None)
    with pytest.raises(RetryLater) as raised:
        api.fetch()

    assert refused.value.retry_after >= 590
    assert raised.value.retry_after >= 590


def test_stats_report_the_budget(limited):
    base_url, api = limited
    api.fetch()

    stats = _stats(base_url)
    assert stats["downloads_last_hour"] == 1
    assert stats["hourly_budget"] == 40
    assert stats["last_refresh_reason"] == "no-copy"
    assert stats["websocket_open"] is True


def test_the_python_client_reads_the_stats_for_vta_status(limited):
    _, api = limited
    api.fetch()

    stats = api.download_stats()

    assert stats is not None
    assert stats["downloads_last_hour"] == 1


def test_stats_are_none_rather_than_an_error_when_the_sidecar_is_gone():
    api = AnyListClient("http://127.0.0.1:9", "Grocery", timeout=1)
    try:
        assert api.download_stats() is None
    finally:
        api.close()
