"""Planner behaviour, exercised entirely against in-memory fakes.

These decide whether the bridge is safe to point at a real shopping list, so
they assert on the mutations issued, not just the end state.

The model is one-directional: AnyList is master, and the Keep note is a
projection of its *active* items plus an inbox for voice adds. A crossed-off
AnyList row is history and is off-limits.
"""

from voice_to_anylist.clients.base import ListItem
from voice_to_anylist.engine import ANYLIST, GuardConfig, SyncEngine


def item(item_id, name, quantity=None, checked=False):
    return ListItem(id=item_id, name=name, quantity=quantity, checked=checked)


def anylist_removals(outcome):
    return [a for a in outcome.actions if a.side == ANYLIST and a.kind == "remove"]


# -- history is untouchable --------------------------------------------------
#
# The two cases below are what a live cycle against the real list actually did
# before it was stopped: it planned to copy 1091 crossed-off rows into the Keep
# note, and deleted 19 crossed-off rows as duplicates.


def test_crossed_off_items_are_never_copied_into_the_note(engine, keep, anylist):
    anylist.replace_items(
        [item(f"a{i}", f"thing {i}", checked=True) for i in range(50)]
        + [item("live", "milk")]
    )

    engine.run_once()

    assert keep.names() == {"milk"}


def test_crossed_off_duplicates_are_never_deduped(engine, keep, anylist):
    """Seven crossed-off 'ham' rows are seven shopping trips, not six mistakes."""
    anylist.replace_items([item(f"h{i}", "ham", checked=True) for i in range(7)])

    outcome = engine.run_once()

    assert anylist_removals(outcome) == []
    assert [a for a in outcome.actions if a.kind == "dedupe"] == []
    assert len(anylist.items) == 7


def test_the_bridge_never_deletes_an_anylist_row(engine, keep, anylist, settled):
    """The only AnyList removal it may emit is an active-item dedupe."""
    settled([item("k1", "milk")], [item("a1", "milk")])
    keep.replace_items([])

    outcome = engine.run_once()

    assert anylist_removals(outcome) == []
    assert any(a.id == "a1" for a in anylist.items)


def test_a_crossed_off_row_is_not_revived_just_by_existing(engine, keep, anylist):
    anylist.replace_items([item("a1", "ham", checked=True)])

    engine.run_once()

    assert anylist.items[0].checked is True
    assert keep.names() == set()


# -- the voice inbox ---------------------------------------------------------


def test_a_new_line_in_the_note_is_added_to_anylist(engine, keep, anylist, settled):
    settled([], [item("a1", "milk")])
    keep.replace_items([item("k9", "strawberries")])

    engine.run_once()

    assert "strawberries" in anylist.names()
    assert not [i for i in anylist.items if i.name == "strawberries" and i.checked]


def test_asking_for_something_crossed_off_revives_it(engine, keep, anylist, settled):
    settled([], [item("a1", "ham", checked=True)])
    keep.replace_items([item("k9", "Ham")])

    engine.run_once()

    assert len(anylist.items) == 1, "revive must not append a second row"
    assert anylist.items[0].checked is False


def test_a_quantity_spoken_into_the_note_reaches_anylist(engine, keep, anylist, settled):
    settled([], [])
    keep.replace_items([item("k1", "lemons", quantity="2")])

    engine.run_once()

    added = [i for i in anylist.items if i.name.lower().startswith("lemon")]
    assert added and added[0].quantity == "2"


# -- purchase ----------------------------------------------------------------


def test_ticking_in_the_note_marks_it_purchased_in_anylist(engine, keep, anylist, settled):
    settled([item("k1", "milk")], [item("a1", "milk")])
    keep.replace_items([item("k1", "milk", checked=True)])

    engine.run_once()

    assert anylist.items[0].checked is True


def test_removing_a_mapped_line_from_the_note_marks_it_purchased(
    engine, keep, anylist, settled
):
    """Assistant deletes the line rather than ticking it when told 'I got the milk'."""
    settled([item("k1", "milk")], [item("a1", "milk")])
    keep.replace_items([])

    engine.run_once()

    assert anylist.items[0].checked is True


def test_a_purchased_item_leaves_the_note(engine, keep, anylist, settled):
    settled([item("k1", "milk")], [item("a1", "milk")])
    anylist.replace_items([item("a1", "milk", checked=True)])

    engine.run_once()

    assert keep.names() == set()


# -- projection --------------------------------------------------------------


def test_an_active_anylist_item_missing_from_the_note_is_restored(
    engine, keep, anylist, settled
):
    settled([item("k1", "milk")], [item("a1", "milk"), item("a2", "bread")])

    engine.run_once()

    assert "bread" in keep.names()


def test_unchecking_in_anylist_puts_it_back_in_the_note(engine, keep, anylist, settled):
    settled([], [item("a1", "ham", checked=True)])
    anylist.replace_items([item("a1", "ham")])

    engine.run_once()

    assert "ham" in {n.lower() for n in keep.names()}


def test_a_quiet_cycle_changes_nothing(engine, keep, anylist, settled):
    settled([item("k1", "milk")], [item("a1", "milk")])

    outcome = engine.run_once()

    assert outcome.actions == []
    assert keep.mutations() == []
    assert anylist.mutations() == []


def test_nothing_echoes_back_on_the_following_cycle(engine, keep, anylist, settled):
    settled([], [item("a1", "milk")])
    keep.replace_items([item("k9", "strawberries")])
    engine.run_once()
    keep.calls.clear()
    anylist.calls.clear()

    outcome = engine.run_once()

    assert outcome.actions == []


# -- bootstrap ---------------------------------------------------------------


def test_the_first_sync_replaces_the_note_with_anylists_active_set(
    engine, keep, anylist
):
    keep.replace_items([item("k1", "leftover"), item("k2", "stale")])
    anylist.replace_items(
        [item("a1", "milk"), item("a2", "bread"), item("a3", "ham", checked=True)]
    )

    engine.run_once()

    assert keep.names() == {"milk", "bread"}
    assert len(anylist.items) == 3, "bootstrap must not write to the master"


def test_bootstrap_happens_once_even_if_the_shadow_stays_empty(
    engine, keep, anylist
):
    """A list with nothing active leaves the shadow empty after bootstrap.

    Keying bootstrap off an empty shadow therefore re-ran it every cycle,
    wiping each voice add before it could reach the master -- permanently.
    """
    anylist.replace_items([item("a1", "ham", checked=True)])
    engine.run_once()

    keep.replace_items([item("k9", "strawberries")])
    engine.run_once()

    assert "strawberries" in anylist.names()


def test_bootstrap_does_not_cross_anything_off(engine, keep, anylist):
    keep.replace_items([item("k1", "leftover")])
    anylist.replace_items([item("a1", "milk")])

    engine.run_once()

    assert anylist.items[0].checked is False


# -- active-item dedupe ------------------------------------------------------


def test_two_active_rows_for_one_thing_collapse(engine, keep, anylist, settled):
    settled([], [])
    anylist.replace_items([item("a1", "Bananas"), item("a2", "banana")])

    outcome = engine.run_once()

    assert len([a for a in outcome.actions if a.kind == "dedupe"]) == 1
    assert len(anylist.items) == 1


def test_dedupe_never_picks_a_crossed_off_row_to_delete(engine, keep, anylist, settled):
    settled([], [])
    anylist.replace_items([item("a1", "ham", checked=True), item("a2", "ham")])

    outcome = engine.run_once()

    assert anylist_removals(outcome) == []
    assert [a for a in outcome.actions if a.kind == "dedupe"] == []


# -- the guard ---------------------------------------------------------------


def test_an_empty_note_does_not_mark_everything_purchased(engine, keep, anylist, settled):
    """A failed Keep fetch looks exactly like 'I bought all of it'."""
    settled(
        [item(f"k{i}", f"thing {i}") for i in range(6)],
        [item(f"a{i}", f"thing {i}") for i in range(6)],
    )
    keep.replace_items([])

    outcome = engine.run_once()

    assert outcome.guard_tripped
    assert not any(i.checked for i in anylist.items)


def test_the_same_mass_change_twice_is_taken_as_deliberate(engine, keep, anylist, settled):
    settled(
        [item(f"k{i}", f"thing {i}") for i in range(6)],
        [item(f"a{i}", f"thing {i}") for i in range(6)],
    )
    keep.replace_items([])
    engine.run_once()

    engine.run_once()

    assert all(i.checked for i in anylist.items)


def test_a_dry_run_writes_nothing_and_arms_nothing(keep, anylist, store, settled):
    engine = SyncEngine(keep, anylist, store, dry_run=True)
    keep.replace_items([item("k1", "milk")])
    anylist.replace_items([item("a1", "bread")])

    outcome = engine.run_once()

    assert outcome.dry_run and not outcome.applied
    assert keep.mutations() == [] and anylist.mutations() == []


def test_the_guard_ignores_dedupe(engine, keep, anylist, settled):
    """Collapsing duplicates is not a purge, however many there are."""
    settled([], [])
    anylist.replace_items(
        [item(f"a{i}", "banana") for i in range(8)] + [item("keepme", "milk")]
    )

    outcome = engine.run_once()

    assert not outcome.guard_tripped
    assert len([a for a in outcome.actions if a.kind == "dedupe"]) == 7


def test_a_stricter_guard_threshold_is_honoured(keep, anylist, store):
    """min_deletes is configurable, so a small list can be protected too."""
    engine = SyncEngine(keep, anylist, store, guard=GuardConfig(min_deletes=2))
    keep.replace_items([item("k1", "milk"), item("k2", "bread")])
    anylist.replace_items([item("a1", "milk"), item("a2", "bread")])
    engine.run_once()  # bootstrap
    keep.replace_items([])

    outcome = engine.run_once()

    assert outcome.guard_tripped
