"""Merge behaviour, exercised entirely against in-memory fakes.

These are the tests that decide whether the bridge is safe to point at a real
shopping list, so they assert on the mutations issued, not just the end state.
"""

import pytest

from voice_to_anylist.clients.base import ListItem
from voice_to_anylist.engine import GuardConfig, SyncEngine


def item(item_id, name, quantity=None, checked=False):
    return ListItem(id=item_id, name=name, quantity=quantity, checked=checked)


# -- cold start -------------------------------------------------------------


def test_cold_start_with_nothing_anywhere(engine, keep, anylist):
    outcome = engine.run_once()
    assert outcome.actions == []
    assert keep.mutations() == []
    assert anylist.mutations() == []


def test_cold_start_copies_each_side_to_the_other_and_deletes_nothing(engine, keep, anylist):
    keep.replace_items([item("k1", "strawberries"), item("k2", "milk")])
    anylist.replace_items([item("a1", "bread")])

    engine.run_once()

    assert anylist.names() == {"strawberries", "milk", "bread"}
    assert keep.names() == {"strawberries", "milk", "bread"}
    assert not any(c[0] == "remove" for c in keep.mutations() + anylist.mutations())


def test_cold_start_adopts_fuzzy_matches_instead_of_duplicating(engine, keep, anylist):
    """An empty shadow must not turn "Strawberries" and "strawberry" into two items."""
    keep.replace_items([item("k1", "Strawberries")])
    anylist.replace_items([item("a1", "strawberry")])

    engine.run_once()

    assert len(keep.fetch()) == 1
    assert len(anylist.fetch()) == 1
    assert not any(c[0] == "add" for c in keep.mutations() + anylist.mutations())


# -- steady state -----------------------------------------------------------


def test_second_cycle_does_nothing(engine, keep, anylist, settled):
    settled([item("k1", "milk")], [])

    outcome = engine.run_once()

    assert outcome.actions == []
    assert keep.mutations() == []
    assert anylist.mutations() == []


def test_voice_add_reaches_anylist(engine, keep, anylist, settled):
    settled([item("k1", "milk")], [item("a1", "milk")])

    keep.replace_items([item("k1", "milk"), item("k9", "strawberries")])
    engine.run_once()

    assert "strawberries" in anylist.names()
    assert anylist.by_name("strawberries").checked is False


def test_voice_add_with_a_quantity_is_split_out(engine, keep, anylist, settled):
    """Keep stores one line of text; AnyList gets a real quantity field."""
    settled([], [])

    keep.replace_items([item("k9", "lemons", quantity="2")])
    engine.run_once()

    added = anylist.by_name("lemons")
    assert added.quantity == "2"


def test_app_add_reaches_keep(engine, keep, anylist, settled):
    settled([item("k1", "milk")], [item("a1", "milk")])

    anylist.replace_items([item("a1", "milk"), item("a9", "bread")])
    engine.run_once()

    assert "bread" in keep.names()


# -- deletion ---------------------------------------------------------------


def test_delete_in_anylist_removes_from_keep(engine, keep, anylist, settled):
    settled([item("k1", "milk"), item("k2", "bread")], [item("a1", "milk"), item("a2", "bread")])

    anylist.replace_items([item("a1", "milk")])
    engine.run_once()

    assert keep.names() == {"milk"}


def test_delete_in_keep_removes_from_anylist(engine, keep, anylist, settled):
    settled([item("k1", "milk"), item("k2", "bread")], [item("a1", "milk"), item("a2", "bread")])

    keep.replace_items([item("k1", "milk")])
    engine.run_once()

    assert anylist.names() == {"milk"}


def test_deleted_item_stays_deleted_on_the_next_cycle(engine, keep, anylist, settled):
    """The classic echo bug: a delete that reappears as an add next cycle."""
    settled([item("k1", "milk"), item("k2", "bread")], [item("a1", "milk"), item("a2", "bread")])

    keep.replace_items([item("k1", "milk")])
    engine.run_once()
    keep.calls.clear()
    anylist.calls.clear()

    outcome = engine.run_once()

    assert outcome.actions == []
    assert keep.names() == {"milk"}
    assert anylist.names() == {"milk"}


# -- checking ---------------------------------------------------------------


def test_checking_in_anylist_ticks_the_keep_line(engine, keep, anylist, settled):
    settled([item("k1", "milk")], [item("a1", "milk")])

    anylist.replace_items([item("a1", "milk", checked=True)])
    engine.run_once()

    assert keep.by_name("milk").checked is True


def test_checking_in_keep_ticks_anylist(engine, keep, anylist, settled):
    settled([item("k1", "milk")], [item("a1", "milk")])

    keep.replace_items([item("k1", "milk", checked=True)])
    engine.run_once()

    assert anylist.by_name("milk").checked is True


def test_readding_a_checked_item_unchecks_it_rather_than_duplicating(
    engine, keep, anylist, settled
):
    """The core dedupe case.

    "strawberry" is ticked off from last week's shop.  Somebody says "hey
    Google, add strawberries", so Google appends a fresh line to the Keep note
    without noticing the ticked one already there.
    """
    settled([item("k1", "strawberries", checked=True)], [item("a1", "strawberry", checked=True)])

    keep.replace_items(
        [item("k1", "strawberries", checked=True), item("k2", "strawberries")]
    )
    engine.run_once()

    assert len(anylist.fetch()) == 1, "must not create a second AnyList item"
    assert anylist.by_name("strawberry").checked is False, "existing item should be revived"
    assert len(keep.fetch()) == 1, "the stale ticked line should be collapsed away"


# -- quantity ---------------------------------------------------------------


def test_quantity_change_updates_rather_than_recreating(engine, keep, anylist, settled):
    settled([item("k1", "lemons", quantity="2")], [item("a1", "lemons", quantity="2")])

    anylist.replace_items([item("a1", "lemons", quantity="6")])
    engine.run_once()

    assert keep.by_name("lemons").quantity == "6"
    kinds = {c[0] for c in keep.mutations()}
    assert kinds == {"set_quantity"}, f"expected an in-place update, got {keep.mutations()}"


def test_anylist_wins_when_both_sides_change_the_quantity(engine, keep, anylist, settled):
    settled([item("k1", "lemons", quantity="2")], [item("a1", "lemons", quantity="2")])

    keep.replace_items([item("k1", "lemons", quantity="3")])
    anylist.replace_items([item("a1", "lemons", quantity="6")])
    engine.run_once()

    assert keep.by_name("lemons").quantity == "6"
    assert anylist.by_name("lemons").quantity == "6"


# -- guard ------------------------------------------------------------------


def test_guard_blocks_a_side_that_reads_back_empty(engine, keep, anylist, settled):
    """An API returning nothing must never be read as "the user cleared it"."""
    settled(
        [item(f"k{i}", n) for i, n in enumerate(["milk", "bread", "eggs", "jam"])],
        [item(f"a{i}", n) for i, n in enumerate(["milk", "bread", "eggs", "jam"])],
    )

    keep.replace_items([])
    outcome = engine.run_once()

    assert outcome.guard_tripped
    assert "empty" in (outcome.guard_reason or "")
    assert anylist.mutations() == [], "nothing should have been deleted"


def test_guard_lets_the_same_mass_change_through_on_the_next_cycle(
    engine, keep, anylist, settled
):
    """A real purge persists; a transient fetch failure does not."""
    settled(
        [item(f"k{i}", n) for i, n in enumerate(["milk", "bread", "eggs", "jam"])],
        [item(f"a{i}", n) for i, n in enumerate(["milk", "bread", "eggs", "jam"])],
    )

    keep.replace_items([])
    assert engine.run_once().guard_tripped

    outcome = engine.run_once()

    assert not outcome.guard_tripped
    assert anylist.fetch() == []


def test_guard_recovers_without_deleting_when_the_side_comes_back(
    engine, keep, anylist, settled
):
    original = [item(f"k{i}", n) for i, n in enumerate(["milk", "bread", "eggs", "jam"])]
    settled(original, [item(f"a{i}", n) for i, n in enumerate(["milk", "bread", "eggs", "jam"])])

    keep.replace_items([])
    assert engine.run_once().guard_tripped

    keep.replace_items(original)
    outcome = engine.run_once()

    assert not outcome.guard_tripped
    assert outcome.actions == []
    assert len(anylist.fetch()) == 4


def test_guard_blocks_deleting_most_of_the_list(keep, anylist, store, settled):
    engine = SyncEngine(keep, anylist, store, guard=GuardConfig(min_deletes=3, max_ratio=0.5))
    names = ["milk", "bread", "eggs", "jam", "rice"]
    settled(
        [item(f"k{i}", n) for i, n in enumerate(names)],
        [item(f"a{i}", n) for i, n in enumerate(names)],
    )

    # Four of five removed in the app -- plausible, but worth pausing over.
    anylist.replace_items([item("a0", "milk")])
    outcome = engine.run_once()

    assert outcome.guard_tripped
    assert len(keep.fetch()) == 5


def test_small_deletions_are_not_guarded(engine, keep, anylist, settled):
    names = ["milk", "bread", "eggs", "jam", "rice"]
    settled(
        [item(f"k{i}", n) for i, n in enumerate(names)],
        [item(f"a{i}", n) for i, n in enumerate(names)],
    )

    anylist.replace_items([item(f"a{i}", n) for i, n in enumerate(names) if n != "jam"])
    outcome = engine.run_once()

    assert not outcome.guard_tripped
    assert keep.names() == {"milk", "bread", "eggs", "rice"}


# -- dry run ----------------------------------------------------------------


def test_dry_run_plans_without_touching_either_side(keep, anylist, store):
    engine = SyncEngine(keep, anylist, store, dry_run=True)
    keep.replace_items([item("k1", "strawberries")])

    outcome = engine.run_once()

    assert outcome.dry_run and not outcome.applied
    assert [a.kind for a in outcome.actions] == ["add"]
    assert anylist.mutations() == []
    assert store.load() == {}, "a dry run must not advance the shadow"


def test_dry_run_leaves_the_same_work_for_the_real_run(keep, anylist, store):
    keep.replace_items([item("k1", "strawberries")])
    dry = SyncEngine(keep, anylist, store, dry_run=True).run_once()

    wet = SyncEngine(keep, anylist, store).run_once()

    assert [a.describe() for a in dry.actions] == [a.describe() for a in wet.actions]
    assert wet.applied


# -- restart ----------------------------------------------------------------


def test_shadow_survives_a_restart_without_duplicating(keep, anylist, store, settled):
    settled([item("k1", "milk")], [item("a1", "milk")])

    # A new engine over the same store is what a container restart looks like.
    outcome = SyncEngine(keep, anylist, store).run_once()

    assert outcome.actions == []
    assert len(keep.fetch()) == 1
    assert len(anylist.fetch()) == 1


@pytest.mark.parametrize("side", ["keep", "anylist"])
def test_items_that_normalise_to_nothing_are_ignored(engine, keep, anylist, side):
    target = keep if side == "keep" else anylist
    target.replace_items([item("x1", "   ")])

    outcome = engine.run_once()

    assert outcome.actions == []


def test_dry_run_does_not_arm_the_guards_confirm_on_repeat(keep, anylist, store, settled):
    """Otherwise a rehearsal would quietly authorise the next real purge."""
    names = ["milk", "bread", "eggs", "jam"]
    settled(
        [item(f"k{i}", n) for i, n in enumerate(names)],
        [item(f"a{i}", n) for i, n in enumerate(names)],
    )
    keep.replace_items([])

    assert SyncEngine(keep, anylist, store, dry_run=True).run_once().guard_tripped
    assert SyncEngine(keep, anylist, store).run_once().guard_tripped
    assert len(anylist.fetch()) == 4


def test_cold_start_prefers_wanting_an_item_over_having_bought_it(engine, keep, anylist):
    """With no shadow there is no baseline, so the tie has to be broken by policy.

    Losing a "please buy this" is a real failure; losing a "already bought" is
    a minor annoyance. So the unticked side wins.
    """
    keep.replace_items([item("k1", "milk", checked=False)])
    anylist.replace_items([item("a1", "milk", checked=True)])

    engine.run_once()

    assert anylist.by_name("milk").checked is False
    assert keep.by_name("milk").checked is False


def test_cold_start_keeps_an_item_ticked_when_both_sides_agree(engine, keep, anylist):
    keep.replace_items([item("k1", "milk", checked=True)])
    anylist.replace_items([item("a1", "milk", checked=True)])

    engine.run_once()

    assert anylist.by_name("milk").checked is True
    assert keep.by_name("milk").checked is True


def test_cold_start_adopts_a_quantity_from_whichever_side_states_one(engine, keep, anylist):
    keep.replace_items([item("k1", "lemons")])
    anylist.replace_items([item("a1", "lemons", quantity="6")])

    engine.run_once()

    assert keep.by_name("lemons").quantity == "6"


def test_renaming_an_item_in_the_app_replaces_it_on_the_note(engine, keep, anylist, settled):
    """A rename changes the identity, so it reads as a delete plus an add."""
    settled([item("k1", "milk")], [item("a1", "milk")])

    anylist.replace_items([item("a1", "almond milk")])
    engine.run_once()

    assert keep.names() == {"almond milk"}


def test_an_item_reappearing_after_deletion_is_added_back(engine, keep, anylist, settled):
    settled([item("k1", "milk")], [item("a1", "milk")])
    keep.replace_items([])
    engine.run_once()
    assert anylist.fetch() == []

    # Asked for again a week later.
    keep.replace_items([item("k7", "milk")])
    engine.run_once()

    assert anylist.names() == {"milk"}
