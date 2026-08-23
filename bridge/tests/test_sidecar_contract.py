"""End-to-end contract between the Python bridge and the Node sidecar.

The seam between the two languages is the likeliest place for a silent
mismatch -- a renamed field or a differently-typed quantity would not show up
in either side's own unit tests.  This drives the real Express server, with the
`anylist` package swapped for an in-memory stub, through the real client.
"""

from __future__ import annotations

import httpx
import pytest

from voice_to_anylist.clients.anylist import AnyListClient


@pytest.fixture
def client(sidecar):
    api = AnyListClient(sidecar, "Grocery")
    yield api
    api.close()


def test_the_seeded_item_round_trips_through_the_python_client(client):
    items = client.fetch()

    assert [(i.id, i.name, i.quantity, i.checked) for i in items] == [
        ("a1", "milk", None, False)
    ]


def test_add_then_read_back(client):
    new_id = client.add("strawberries", "2", checked=False)

    added = next(i for i in client.fetch() if i.id == new_id)
    assert added.name == "strawberries"
    assert added.quantity == "2"
    assert added.checked is False


def test_check_and_uncheck_survive_a_round_trip(client):
    new_id = client.add("bread", None)

    client.set_checked(new_id, True)
    assert next(i for i in client.fetch() if i.id == new_id).checked is True

    client.set_checked(new_id, False)
    assert next(i for i in client.fetch() if i.id == new_id).checked is False


def test_quantity_updates_in_place(client):
    new_id = client.add("lemons", "2")

    client.set_quantity(new_id, "6")

    assert next(i for i in client.fetch() if i.id == new_id).quantity == "6"


def test_clearing_a_quantity_reads_back_as_absent_not_empty_string(client):
    """The engine compares quantities; "" and None must not look different."""
    new_id = client.add("rice", "1")

    client.set_quantity(new_id, None)

    assert next(i for i in client.fetch() if i.id == new_id).quantity is None


def test_remove_deletes_the_item(client):
    new_id = client.add("jam", None)

    client.remove(new_id)

    assert new_id not in {i.id for i in client.fetch()}


def test_adding_an_existing_name_revives_it_instead_of_duplicating(client):
    """Mirrors what the official clients do with checked-off items."""
    first = client.add("olives", None, checked=True)

    second = client.add("olives", "3", checked=False)

    assert second == first
    revived = next(i for i in client.fetch() if i.id == first)
    assert revived.checked is False
    assert revived.quantity == "3"


def test_an_unknown_list_reports_which_lists_exist(client, sidecar):
    missing = AnyListClient(sidecar, "Nope")
    try:
        with pytest.raises(Exception, match="Grocery"):
            missing.fetch()
    finally:
        missing.close()


# -- an unconfigured sidecar stays up ----------------------------------------


def test_it_serves_the_reason_rather_than_exiting(unconfigured_sidecar):
    """Exiting earns a launchd restart loop that buries the explanation."""
    response = httpx.get(f"{unconfigured_sidecar}/health", timeout=5)

    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["unconfigured"] == ["ANYLIST_EMAIL", "ANYLIST_PASSWORD"]


def test_every_other_route_reports_it_too(unconfigured_sidecar):
    response = httpx.get(f"{unconfigured_sidecar}/lists", timeout=5)

    assert response.status_code == 503
    assert "ANYLIST_EMAIL" in response.text
