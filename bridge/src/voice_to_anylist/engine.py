"""Three-way merge between the Keep note, the AnyList list, and the shadow.

Each side is compared against the shadow -- the last state both sides agreed
on -- rather than against each other.  That is what distinguishes a genuinely
new item from one the bridge itself wrote a moment ago, and it is the only
reason this can run in a loop without echoing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace

from .clients.base import ListClient, ListItem
from .normalise import key as normalise_key
from .store import ShadowEntry, ShadowStore

log = logging.getLogger(__name__)

KEEP = "keep"
ANYLIST = "anylist"

_GUARD_SIGNATURE = "guard_signature"


@dataclass(frozen=True)
class Action:
    """One mutation on one side.  Kept declarative so it can be vetted first."""

    side: str
    kind: str  # add | remove | check | quantity | dedupe
    key: str
    item_id: str | None = None
    name: str | None = None
    quantity: str | None = None
    checked: bool = False

    def describe(self) -> str:
        if self.kind == "add":
            return f"{self.side}: add {self.name!r} (qty={self.quantity}, checked={self.checked})"
        if self.kind in {"remove", "dedupe"}:
            return f"{self.side}: {self.kind} {self.key!r}"
        if self.kind == "check":
            return f"{self.side}: {'check' if self.checked else 'uncheck'} {self.key!r}"
        return f"{self.side}: set quantity of {self.key!r} to {self.quantity}"


@dataclass
class GuardConfig:
    """Limits that stop a failing API from being read as a deliberate purge."""

    min_deletes: int = 5
    max_ratio: float = 0.5
    empty_side_min_shadow: int = 3


@dataclass
class SyncOutcome:
    actions: list[Action] = field(default_factory=list)
    applied: bool = False
    guard_tripped: bool = False
    guard_reason: str | None = None
    dry_run: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.actions)


@dataclass
class _KeyPlan:
    key: str
    actions: list[Action]
    shadow: ShadowEntry | None
    deletes_propagated: int = 0


def _index(
    items: list[ListItem], shadow_ids: set[str]
) -> tuple[dict[str, ListItem], list[ListItem]]:
    """Group a side's items by normalised key, picking one winner per key.

    Both sides can genuinely end up holding two lines for the same thing --
    Google appends "strawberries" without noticing the ticked "strawberry"
    already sitting there.  An unchecked item wins, because a fresh request
    beats a completed one; a tie goes to whichever is already mapped in the
    shadow, so identity stays stable across cycles.  Everything else is
    reported as a duplicate for removal.
    """
    grouped: dict[str, list[ListItem]] = {}
    for item in items:
        item_key = normalise_key(item.name)
        if not item_key:
            continue
        grouped.setdefault(item_key, []).append(item)

    canonical: dict[str, ListItem] = {}
    duplicates: list[ListItem] = []
    for item_key, candidates in grouped.items():
        if len(candidates) == 1:
            canonical[item_key] = candidates[0]
            continue
        candidates.sort(key=lambda i: (i.checked, i.id not in shadow_ids, i.id))
        canonical[item_key] = candidates[0]
        duplicates.extend(candidates[1:])
    return canonical, duplicates


def _resolve(current_a, current_b, baseline):
    """Pick a winner for a single field given both sides and their baseline.

    Returns ``(value, changed_a, changed_b)`` where the ``changed_*`` flags say
    which side must be written to.  When both sides moved and disagree, side
    ``b`` -- AnyList -- wins, as the system of record for meal planning.
    """
    a_moved = current_a != baseline
    b_moved = current_b != baseline
    if a_moved and not b_moved:
        return current_a, False, True
    if b_moved and not a_moved:
        return current_b, True, False
    if a_moved and b_moved and current_a != current_b:
        return current_b, True, False
    return current_b if b_moved else baseline, False, False


class SyncEngine:
    def __init__(
        self,
        keep: ListClient,
        anylist: ListClient,
        store: ShadowStore,
        *,
        dry_run: bool = False,
        guard: GuardConfig | None = None,
    ):
        self.keep = keep
        self.anylist = anylist
        self.store = store
        self.dry_run = dry_run
        self.guard = guard or GuardConfig()

    # -- planning -----------------------------------------------------------

    def _plan_key(
        self,
        item_key: str,
        keep_item: ListItem | None,
        anylist_item: ListItem | None,
        shadow: ShadowEntry | None,
    ) -> _KeyPlan:
        actions: list[Action] = []

        # Gone from both sides: nothing to do but forget it.
        if keep_item is None and anylist_item is None:
            return _KeyPlan(item_key, actions, None)

        # Present on one side only.
        if anylist_item is None:
            assert keep_item is not None
            if shadow is not None:
                # It was mapped, so AnyList losing it is a real deletion.
                actions.append(Action(KEEP, "remove", item_key, item_id=keep_item.id))
                return _KeyPlan(item_key, actions, None, deletes_propagated=1)
            actions.append(
                Action(
                    ANYLIST,
                    "add",
                    item_key,
                    name=keep_item.name,
                    quantity=keep_item.quantity,
                    checked=keep_item.checked,
                )
            )
            return _KeyPlan(
                item_key,
                actions,
                ShadowEntry(
                    key=item_key,
                    keep_id=keep_item.id,
                    anylist_id=None,  # filled in once the add returns an id
                    name=keep_item.name,
                    quantity=keep_item.quantity,
                    checked=keep_item.checked,
                ),
            )

        if keep_item is None:
            if shadow is not None:
                actions.append(Action(ANYLIST, "remove", item_key, item_id=anylist_item.id))
                return _KeyPlan(item_key, actions, None, deletes_propagated=1)
            actions.append(
                Action(
                    KEEP,
                    "add",
                    item_key,
                    name=anylist_item.name,
                    quantity=anylist_item.quantity,
                    checked=anylist_item.checked,
                )
            )
            return _KeyPlan(
                item_key,
                actions,
                ShadowEntry(
                    key=item_key,
                    keep_id=None,
                    anylist_id=anylist_item.id,
                    name=anylist_item.name,
                    quantity=anylist_item.quantity,
                    checked=anylist_item.checked,
                ),
            )

        # Present on both sides: reconcile the fields that matter.  Display
        # names are deliberately left alone -- "Strawberries" and "strawberry"
        # share a key, and fighting over the spelling would churn forever.
        base_checked = shadow.checked if shadow else anylist_item.checked or keep_item.checked
        base_quantity = shadow.quantity if shadow else anylist_item.quantity

        checked, write_keep_checked, write_any_checked = _resolve(
            keep_item.checked, anylist_item.checked, base_checked
        )
        quantity, write_keep_qty, write_any_qty = _resolve(
            keep_item.quantity, anylist_item.quantity, base_quantity
        )

        if shadow is None:
            # Adopting an unmapped pair: no baseline, so a fresh request beats
            # a completed one and a stated quantity beats a missing one.
            checked = keep_item.checked and anylist_item.checked
            write_keep_checked = keep_item.checked != checked
            write_any_checked = anylist_item.checked != checked
            quantity = anylist_item.quantity or keep_item.quantity
            write_keep_qty = keep_item.quantity != quantity
            write_any_qty = anylist_item.quantity != quantity

        if write_keep_checked:
            actions.append(
                Action(KEEP, "check", item_key, item_id=keep_item.id, checked=checked)
            )
        if write_any_checked:
            actions.append(
                Action(ANYLIST, "check", item_key, item_id=anylist_item.id, checked=checked)
            )
        if write_keep_qty:
            actions.append(
                Action(
                    KEEP,
                    "quantity",
                    item_key,
                    item_id=keep_item.id,
                    name=keep_item.name,
                    quantity=quantity,
                )
            )
        if write_any_qty:
            actions.append(
                Action(
                    ANYLIST,
                    "quantity",
                    item_key,
                    item_id=anylist_item.id,
                    name=anylist_item.name,
                    quantity=quantity,
                )
            )

        return _KeyPlan(
            item_key,
            actions,
            ShadowEntry(
                key=item_key,
                keep_id=keep_item.id,
                anylist_id=anylist_item.id,
                name=anylist_item.name,
                quantity=quantity,
                checked=checked,
            ),
        )

    # -- guard --------------------------------------------------------------

    def _check_guard(
        self,
        plans: list[_KeyPlan],
        shadow: dict[str, ShadowEntry],
        keep_count: int,
        anylist_count: int,
    ) -> str | None:
        """Return a reason to abort this cycle, or None to proceed.

        The same mass change seen on two consecutive cycles is allowed through:
        a real "clear the list" persists, whereas a transient fetch failure
        does not.  That way the guard delays a legitimate purge by one cycle
        instead of blocking it forever.
        """
        # A dry run must leave no trace, including the confirm-on-repeat
        # signature -- otherwise it would silently arm the next real cycle.
        def remember(value: str | None) -> None:
            if not self.dry_run:
                self.store.set_meta(_GUARD_SIGNATURE, value)

        deleting = sorted(p.key for p in plans if p.deletes_propagated)
        if not deleting:
            remember(None)
            return None

        reason: str | None = None
        if (
            len(shadow) >= self.guard.empty_side_min_shadow
            and (keep_count == 0) != (anylist_count == 0)
        ):
            empty = KEEP if keep_count == 0 else ANYLIST
            reason = f"{empty} returned an empty list while {len(shadow)} items were mapped"
        elif (
            len(deleting) >= self.guard.min_deletes
            and shadow
            and len(deleting) / len(shadow) > self.guard.max_ratio
        ):
            reason = (
                f"{len(deleting)} of {len(shadow)} mapped items would be deleted"
                f" (over {self.guard.max_ratio:.0%})"
            )

        if reason is None:
            remember(None)
            return None

        signature = "\n".join(deleting)
        if self.store.get_meta(_GUARD_SIGNATURE) == signature:
            log.warning("guard: allowing repeated mass change (%s)", reason)
            remember(None)
            return None

        remember(signature)
        return reason

    # -- execution ----------------------------------------------------------

    def _client(self, side: str) -> ListClient:
        return self.keep if side == KEEP else self.anylist

    def _apply(self, plan: _KeyPlan) -> ShadowEntry | None:
        entry = plan.shadow
        for action in plan.actions:
            client = self._client(action.side)
            if action.kind == "add":
                new_id = client.add(action.name or "", action.quantity, action.checked)
                if entry is not None:
                    entry = (
                        replace(entry, keep_id=new_id)
                        if action.side == KEEP
                        else replace(entry, anylist_id=new_id)
                    )
            elif action.kind in {"remove", "dedupe"}:
                client.remove(action.item_id or "")
            elif action.kind == "check":
                client.set_checked(action.item_id or "", action.checked)
            elif action.kind == "quantity":
                client.set_quantity(action.item_id or "", action.quantity)
        return entry

    def run_once(self) -> SyncOutcome:
        shadow = self.store.load()
        keep_items = self.keep.fetch()
        anylist_items = self.anylist.fetch()

        keep_index, keep_dupes = _index(
            keep_items, {e.keep_id for e in shadow.values() if e.keep_id}
        )
        anylist_index, anylist_dupes = _index(
            anylist_items, {e.anylist_id for e in shadow.values() if e.anylist_id}
        )

        plans: list[_KeyPlan] = []
        for item_key in sorted(set(keep_index) | set(anylist_index) | set(shadow)):
            plans.append(
                self._plan_key(
                    item_key,
                    keep_index.get(item_key),
                    anylist_index.get(item_key),
                    shadow.get(item_key),
                )
            )

        # Collapsing same-side duplicates never crosses the mirror, so it is
        # excluded from the deletion guard.
        dedupe_actions = [
            Action(side, "dedupe", normalise_key(item.name), item_id=item.id)
            for side, dupes in ((KEEP, keep_dupes), (ANYLIST, anylist_dupes))
            for item in dupes
        ]

        outcome = SyncOutcome(dry_run=self.dry_run)
        outcome.actions = [a for p in plans for a in p.actions] + dedupe_actions

        reason = self._check_guard(plans, shadow, len(keep_items), len(anylist_items))
        if reason is not None:
            outcome.guard_tripped = True
            outcome.guard_reason = reason
            log.error("guard tripped, skipping cycle: %s", reason)
            return outcome

        if not outcome.actions:
            # Still rewrite the shadow: ids can change under us even when the
            # visible contents have not.
            self.store.replace_all([p.shadow for p in plans if p.shadow])
            return outcome

        for action in outcome.actions:
            log.info("%s%s", "[dry-run] " if self.dry_run else "", action.describe())

        if self.dry_run:
            return outcome

        entries: list[ShadowEntry] = []
        for plan in plans:
            entry = self._apply(plan)
            if entry is not None:
                entries.append(entry)
        for action in dedupe_actions:
            self._client(action.side).remove(action.item_id or "")

        self.keep.commit()
        self.anylist.commit()
        self.store.replace_all(entries)
        outcome.applied = True
        return outcome
