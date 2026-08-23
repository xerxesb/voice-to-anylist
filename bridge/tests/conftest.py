import pytest

from voice_to_anylist.clients.base import FakeListClient, ListItem
from voice_to_anylist.engine import SyncEngine
from voice_to_anylist.store import ShadowStore


@pytest.fixture
def store():
    s = ShadowStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def keep():
    return FakeListClient("keep")


@pytest.fixture
def anylist():
    return FakeListClient("anylist")


@pytest.fixture
def engine(keep, anylist, store):
    return SyncEngine(keep, anylist, store)


def item(item_id, name, quantity=None, checked=False):
    return ListItem(id=item_id, name=name, quantity=quantity, checked=checked)


@pytest.fixture
def make_item():
    return item


@pytest.fixture
def settled(keep, anylist, engine):
    """A mirror that has already synced, so the shadow is populated.

    Most interesting behaviour only shows up on the *second* cycle, once the
    engine has a baseline to compare against.
    """

    def _settle(keep_items, anylist_items):
        keep.replace_items(keep_items)
        anylist.replace_items(anylist_items)
        engine.run_once()
        keep.calls.clear()
        anylist.calls.clear()

    return _settle
