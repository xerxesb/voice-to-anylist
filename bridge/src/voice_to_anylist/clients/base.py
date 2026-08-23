"""The shape both sides of the mirror present to the merge engine.

Keep and AnyList model an item very differently -- Keep has a single line of
text plus a checkbox, AnyList has a name, a separate quantity field and a
checked flag.  Each client is responsible for translating into and out of the
common :class:`ListItem` so the engine never has to care which side it is
talking to.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable


class ListClientError(RuntimeError):
    """A side could not be read or written.  Aborts the cycle, never the process."""


class AuthenticationError(ListClientError):
    """Credentials were rejected.  Needs a human, so it is alerted separately."""


@dataclass(frozen=True)
class ListItem:
    id: str
    name: str
    quantity: str | None = None
    checked: bool = False


@runtime_checkable
class ListClient(Protocol):
    """One side of the mirror.

    Implementations must be safe to call repeatedly; the engine polls.
    """

    name: str

    def fetch(self) -> list[ListItem]:
        """Return the current contents of the list."""

    def add(self, name: str, quantity: str | None, checked: bool = False) -> str:
        """Create an item and return its new id."""

    def set_quantity(self, item_id: str, quantity: str | None) -> None:
        """Change an existing item's quantity, leaving its name alone."""

    def set_checked(self, item_id: str, checked: bool) -> None:
        """Tick or untick an item."""

    def remove(self, item_id: str) -> None:
        """Delete an item outright."""

    def commit(self) -> None:
        """Flush any batched writes.  A no-op for clients that write eagerly."""


class FakeListClient:
    """In-memory stand-in used by the engine tests.

    Records every mutation so tests can assert on what the engine *did*, not
    merely on where it ended up -- an engine that deletes and recreates an item
    reaches the same state as one that updates it, but only one of those is
    acceptable against a real API.
    """

    def __init__(self, name: str = "fake", items: list[ListItem] | None = None):
        self.name = name
        self._items: dict[str, ListItem] = {i.id: i for i in (items or [])}
        self._next_id = 1
        self.calls: list[tuple] = []
        self.fail_on_fetch: Exception | None = None

    def fetch(self) -> list[ListItem]:
        if self.fail_on_fetch is not None:
            raise self.fail_on_fetch
        return list(self._items.values())

    def add(self, name: str, quantity: str | None, checked: bool = False) -> str:
        item_id = f"{self.name}-{self._next_id}"
        self._next_id += 1
        self._items[item_id] = ListItem(id=item_id, name=name, quantity=quantity, checked=checked)
        self.calls.append(("add", name, quantity, checked))
        return item_id

    def set_quantity(self, item_id: str, quantity: str | None) -> None:
        self._items[item_id] = replace(self._items[item_id], quantity=quantity)
        self.calls.append(("set_quantity", item_id, quantity))

    def set_checked(self, item_id: str, checked: bool) -> None:
        self._items[item_id] = replace(self._items[item_id], checked=checked)
        self.calls.append(("set_checked", item_id, checked))

    def remove(self, item_id: str) -> None:
        del self._items[item_id]
        self.calls.append(("remove", item_id))

    def commit(self) -> None:
        self.calls.append(("commit",))

    # -- test helpers -------------------------------------------------------

    def replace_items(self, items: list[ListItem]) -> None:
        """Simulate a change made outside the bridge, keeping ids stable."""
        self._items = {i.id: i for i in items}

    @property
    def items(self) -> list[ListItem]:
        """Current contents, for assertions about what survived."""
        return list(self._items.values())

    def names(self) -> set[str]:
        return {i.name for i in self._items.values()}

    def by_name(self, name: str) -> ListItem:
        for item in self._items.values():
            if item.name == name:
                return item
        raise KeyError(name)

    def mutations(self) -> list[tuple]:
        """Every call except commits, which are bookkeeping rather than change."""
        return [c for c in self.calls if c[0] != "commit"]
