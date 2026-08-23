"""The whole bridge, wired together, minus Google itself.

Everything here is real except the Keep client: the service, the engine, the
shadow store, the HTTP client, and the Node sidecar process.  Only gkeepapi is
stood in for, since it cannot be exercised without a live Google account.

Each test is written as the user story it represents.
"""

from __future__ import annotations

import pytest

from voice_to_anylist.clients.anylist import AnyListClient
from voice_to_anylist.clients.base import FakeListClient, ListItem
from voice_to_anylist.config import Settings
from voice_to_anylist.service import BridgeService
from voice_to_anylist.store import ShadowStore


@pytest.fixture
def bridge(sidecar):
    """A bridge whose AnyList side is real and whose Keep side is simulated."""
    anylist = AnyListClient(sidecar, "Grocery")
    keep = FakeListClient("keep")
    store = ShadowStore(":memory:")
    settings = Settings(
        google_email="x@example.com",
        google_master_token="aas_et/fake",
        anylist_api_url=sidecar,
        anylist_list="Grocery",
    )
    service = BridgeService(settings, keep=keep, anylist=anylist, store=store)
    service.alerter.send = lambda key, message: None  # type: ignore[method-assign]

    # The sidecar stub seeds "milk"; settle so it is not mistaken for news.
    service.run_cycle()
    yield service
    service.close()


def anylist_names(service) -> set[str]:
    return {i.name for i in service.anylist.fetch()}


def speak(service, text: str, item_id: str = "spoken") -> None:
    """Simulate Google appending a heard phrase to the Keep note."""
    from voice_to_anylist.normalise import parse_quantity

    parsed = parse_quantity(text)
    service.keep.replace_items(
        service.keep.fetch()
        + [ListItem(id=item_id, name=parsed.name, quantity=parsed.quantity)]
    )


def test_hey_google_add_strawberries_to_the_shopping_list(bridge):
    speak(bridge, "strawberries")

    bridge.run_cycle()

    assert "strawberries" in anylist_names(bridge)


def test_hey_google_add_two_lemons_splits_the_quantity_out(bridge):
    speak(bridge, "two lemons")

    bridge.run_cycle()

    lemons = next(i for i in bridge.anylist.fetch() if i.name == "lemons")
    assert lemons.quantity == "2", "AnyList should hold a quantity, not an item called '2 lemons'"


def test_nothing_happens_on_a_quiet_cycle(bridge):
    speak(bridge, "strawberries")
    bridge.run_cycle()
    before = bridge.status.actions_applied

    bridge.run_cycle()

    assert bridge.status.actions_applied == before, "a settled mirror should do nothing"


def test_an_item_added_in_the_anylist_app_reaches_the_speaker(bridge):
    bridge.anylist.add("bread", None)

    bridge.run_cycle()

    assert "bread" in {i.name for i in bridge.keep.fetch()}


def test_checking_an_item_off_in_anylist_clears_it_from_the_note(bridge):
    speak(bridge, "strawberries")
    bridge.run_cycle()
    item_id = next(i.id for i in bridge.anylist.fetch() if i.name == "strawberries")

    bridge.anylist.set_checked(item_id, True)
    bridge.run_cycle()

    ticked = next(i for i in bridge.keep.fetch() if i.name == "strawberries")
    assert ticked.checked is True


def test_saying_it_again_revives_the_old_item_instead_of_duplicating(bridge):
    """Last week's ticked-off strawberries, asked for again this week."""
    speak(bridge, "strawberries")
    bridge.run_cycle()
    item_id = next(i.id for i in bridge.anylist.fetch() if i.name == "strawberries")
    bridge.anylist.set_checked(item_id, True)
    bridge.run_cycle()

    # Google appends a fresh line, not noticing the ticked one already there.
    speak(bridge, "Strawberries", item_id="spoken-again")
    bridge.run_cycle()

    matching = [i for i in bridge.anylist.fetch() if i.name.lower().startswith("strawberr")]
    assert len(matching) == 1, f"expected one strawberries item, got {matching}"
    assert matching[0].checked is False, "the existing item should have been revived"


def test_a_restart_does_not_duplicate_anything(bridge):
    speak(bridge, "strawberries")
    bridge.run_cycle()
    before = anylist_names(bridge)

    # A new service over the same store and clients is what a restart looks like.
    restarted = BridgeService(
        bridge.settings, keep=bridge.keep, anylist=bridge.anylist, store=bridge.store
    )
    restarted.alerter.send = lambda key, message: None  # type: ignore[method-assign]
    restarted.run_cycle()

    assert anylist_names(bridge) == before


def test_a_dry_run_changes_nothing(sidecar):
    anylist = AnyListClient(sidecar, "Grocery")
    keep = FakeListClient("keep", [ListItem(id="k1", name="pineapple")])
    settings = Settings(
        google_email="x@example.com",
        google_master_token="aas_et/fake",
        anylist_api_url=sidecar,
        anylist_list="Grocery",
        dry_run=True,
    )
    service = BridgeService(settings, keep=keep, anylist=anylist, store=ShadowStore(":memory:"))
    try:
        service.run_cycle()

        assert "pineapple" not in {i.name for i in anylist.fetch()}
    finally:
        service.close()
