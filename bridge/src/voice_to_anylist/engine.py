"""Project AnyList's active items onto the Keep note, and collect voice adds.

AnyList is the master.  The Keep note is two things at once: a projection of
AnyList's *unchecked* items, and an inbox for lines the speakers write into it.
Nothing else about the note is authoritative.

The asymmetry is the whole design.  The real list is a catalogue -- 1422 of its
1432 rows are crossed off, meaning "we buy this, not right now" -- so a
symmetric mirror reads it as 1422 items to replicate into a Keep note and a few
hundred duplicates to collapse.  Both are catastrophic and both are what a
mirror is supposed to do.  Hence two invariants:

* A crossed-off AnyList row is never copied to Keep, never deduped, never
  deleted, and never modified except by a revive.
* The bridge never deletes an AnyList row.  The only removal it may emit on
  that side is collapsing a duplicate among *active* items.

The shadow -- the last state both sides agreed on -- still distinguishes a
genuinely new Keep line from one the bridge itself wrote, which is what lets
this run in a loop without echoing.
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
_BOOTSTRAPPED = "bootstrapped"


@dataclass(frozen=True)
class Action:
    """One mutation on one side.  Declarative, so it can be vetted first."""

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
            verb = "mark purchased" if self.checked else "revive"
            return f"{self.side}: {verb} {self.key!r}"
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
    bootstrapped: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.actions)


@dataclass
class _KeyPlan:
    key: str
    actions: list[Action]
    shadow: ShadowEntry | None
    destructive: int = 0


def _index_active(
    items: list[ListItem], shadow_ids: set[str]
) -> tuple[dict[str, ListItem], list[ListItem]]:
    """Group *unchecked* items by key, picking one winner each.

    A tie goes to whichever is already mapped in the shadow, so identity stays
    stable across cycles.  The losers are duplicates to collapse -- and because
    only unchecked items reach here, a crossed-off row can never be one.
    """
    grouped: dict[str, list[ListItem]] = {}
    for item in items:
        if item.checked:
            continue
        item_key = normalise_key(item.name)
        if not item_key:
            continue
        grouped.setdefault(item_key, []).append(item)

    canonical: dict[str, ListItem] = {}
    duplicates: list[ListItem] = []
    for item_key, candidates in grouped.items():
        candidates.sort(key=lambda i: (i.id not in shadow_ids, i.id))
        canonical[item_key] = candidates[0]
        duplicates.extend(candidates[1:])
    return canonical, duplicates


def _index_history(items: list[ListItem]) -> dict[str, ListItem]:
    """Crossed-off rows by key, for reviving.  Never a candidate for deletion."""
    history: dict[str, ListItem] = {}
    for item in items:
        if not item.checked:
            continue
        item_key = normalise_key(item.name)
        if item_key and item_key not in history:
            history[item_key] = item
    return history


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
        active: ListItem | None,
        history: ListItem | None,
        shadow: ShadowEntry | None,
    ) -> _KeyPlan:
        actions: list[Action] = []

        # -- the voice inbox: an *unmapped* line with nothing active behind it.
        # Unmapped is what makes it a voice add; a mapped line with no active
        # row behind it means the master crossed it off, handled below.
        if (
            keep_item is not None
            and not keep_item.checked
            and active is None
            and shadow is None
        ):
            if history is not None:
                # Revive rather than append: one row per thing, ticked meaning
                # "not needed right now".
                actions.append(
                    Action(
                        ANYLIST,
                        "check",
                        item_key,
                        item_id=history.id,
                        name=history.name,
                        checked=False,
                    )
                )
                return _KeyPlan(
                    item_key,
                    actions,
                    ShadowEntry(
                        key=item_key,
                        keep_id=keep_item.id,
                        anylist_id=history.id,
                        name=history.name,
                        quantity=keep_item.quantity,
                        checked=False,
                    ),
                )
            actions.append(
                Action(
                    ANYLIST,
                    "add",
                    item_key,
                    name=keep_item.name,
                    quantity=keep_item.quantity,
                    checked=False,
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
                    checked=False,
                ),
            )

        # -- nothing active on the master: the note must not show it
        if active is None:
            if keep_item is not None:
                actions.append(Action(KEEP, "remove", item_key, item_id=keep_item.id))
            return _KeyPlan(item_key, actions, None)

        # -- active on the master, ticked or gone from the note: purchased
        gone_from_note = keep_item is None and shadow is not None
        if (keep_item is not None and keep_item.checked) or gone_from_note:
            actions.append(
                Action(ANYLIST, "check", item_key, item_id=active.id, checked=True)
            )
            if keep_item is not None:
                actions.append(Action(KEEP, "remove", item_key, item_id=keep_item.id))
            # Marking the master purchased in bulk is this model's destructive
            # act, so it is what the guard counts.
            return _KeyPlan(item_key, actions, None, destructive=1)

        # -- active on the master, absent from the note: project it
        if keep_item is None:
            actions.append(
                Action(
                    KEEP,
                    "add",
                    item_key,
                    name=active.name,
                    quantity=active.quantity,
                    checked=False,
                )
            )
            return _KeyPlan(
                item_key,
                actions,
                ShadowEntry(
                    key=item_key,
                    keep_id=None,  # filled in once the add returns an id
                    anylist_id=active.id,
                    name=active.name,
                    quantity=active.quantity,
                    checked=False,
                ),
            )

        # -- present and active on both: reconcile quantity, master wins.
        # Display names are left alone; "Strawberries" and "strawberry" share a
        # key, and fighting over the spelling would churn forever.
        quantity = active.quantity
        if shadow is not None and keep_item.quantity != shadow.quantity:
            # The note moved and the master did not: accept the spoken quantity.
            if active.quantity == shadow.quantity:
                quantity = keep_item.quantity
                actions.append(
                    Action(
                        ANYLIST,
                        "quantity",
                        item_key,
                        item_id=active.id,
                        name=active.name,
                        quantity=quantity,
                    )
                )
        if keep_item.quantity != quantity and not actions:
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

        return _KeyPlan(
            item_key,
            actions,
            ShadowEntry(
                key=item_key,
                keep_id=keep_item.id,
                anylist_id=active.id,
                name=active.name,
                quantity=quantity,
                checked=False,
            ),
        )

    def _plan_bootstrap(
        self, keep_items: list[ListItem], active: dict[str, ListItem]
    ) -> list[_KeyPlan]:
        """First contact: assert the master, and write nothing to it.

        With an empty shadow there is no basis for telling a fresh voice add
        from a leftover line, so rather than guess, the note is replaced with
        exactly the master's active set.
        """
        plans = [
            _KeyPlan(
                normalise_key(k.name) or k.id,
                [Action(KEEP, "remove", normalise_key(k.name) or k.id, item_id=k.id)],
                None,
            )
            for k in keep_items
        ]
        for item_key, item in sorted(active.items()):
            plans.append(
                _KeyPlan(
                    item_key,
                    [
                        Action(
                            KEEP,
                            "add",
                            item_key,
                            name=item.name,
                            quantity=item.quantity,
                            checked=False,
                        )
                    ],
                    ShadowEntry(
                        key=item_key,
                        keep_id=None,
                        anylist_id=item.id,
                        name=item.name,
                        quantity=item.quantity,
                        checked=False,
                    ),
                )
            )
        return plans

    # -- guard --------------------------------------------------------------

    def _check_guard(
        self,
        plans: list[_KeyPlan],
        shadow: dict[str, ShadowEntry],
        keep_count: int,
        active_count: int,
    ) -> str | None:
        """Return a reason to abort this cycle, or None to proceed.

        The same mass change on two consecutive cycles is allowed through: a
        real "I bought all of it" persists, a transient fetch failure does not.
        So the guard delays a legitimate purge by one cycle rather than
        blocking it forever.
        """

        def remember(value: str | None) -> None:
            # A dry run must leave no trace, including the confirm-on-repeat
            # signature -- otherwise it silently arms the next real cycle.
            if not self.dry_run:
                self.store.set_meta(_GUARD_SIGNATURE, value)

        purchasing = sorted(p.key for p in plans if p.destructive)
        if not purchasing:
            remember(None)
            return None

        reason: str | None = None
        if len(shadow) >= self.guard.empty_side_min_shadow and keep_count == 0:
            reason = (
                f"the note came back empty while {len(shadow)} items were mapped; "
                "that would mark every active item purchased"
            )
        elif (
            len(purchasing) >= self.guard.min_deletes
            and shadow
            and len(purchasing) / len(shadow) > self.guard.max_ratio
        ):
            reason = (
                f"{len(purchasing)} of {len(shadow)} mapped items would be marked "
                f"purchased (over {self.guard.max_ratio:.0%})"
            )

        if reason is None:
            remember(None)
            return None

        signature = "\n".join(purchasing)
        if self.store.get_meta(_GUARD_SIGNATURE) == signature:
            log.warning("guard: allowing repeated mass change (%s)", reason)
            remember(None)
            return None

        remember(signature)
        _ = active_count  # reported by the service, not used to decide
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

        active, active_dupes = _index_active(
            anylist_items, {e.anylist_id for e in shadow.values() if e.anylist_id}
        )
        history = _index_history(anylist_items)
        keep_index, keep_dupes = _index_active(
            keep_items, {e.keep_id for e in shadow.values() if e.keep_id}
        )
        # A ticked Keep line is a purchase signal, so unlike the master's
        # crossed-off rows it still has to be looked at.
        for item in keep_items:
            item_key = normalise_key(item.name)
            if item.checked and item_key and item_key not in keep_index:
                keep_index[item_key] = item

        outcome = SyncOutcome(dry_run=self.dry_run)

        if not shadow and self.store.get_meta(_BOOTSTRAPPED) is None:
            plans = self._plan_bootstrap(keep_items, active)
            outcome.bootstrapped = True
            active_dupes = []
            keep_dupes = []
        else:
            plans = [
                self._plan_key(
                    item_key,
                    keep_index.get(item_key),
                    active.get(item_key),
                    history.get(item_key),
                    shadow.get(item_key),
                )
                for item_key in sorted(set(keep_index) | set(active) | set(shadow))
            ]

        # Collapsing same-side duplicates is not a purge, so it sits outside
        # the guard -- and only unchecked items ever reach here.
        dedupe_actions = [
            Action(side, "dedupe", normalise_key(item.name), item_id=item.id)
            for side, dupes in ((KEEP, keep_dupes), (ANYLIST, active_dupes))
            for item in dupes
        ]

        outcome.actions = [a for p in plans for a in p.actions] + dedupe_actions

        reason = self._check_guard(plans, shadow, len(keep_items), len(active))
        if reason is not None:
            outcome.guard_tripped = True
            outcome.guard_reason = reason
            outcome.actions = []
            log.error("guard tripped, skipping cycle: %s", reason)
            return outcome

        if not outcome.actions:
            # Still rewrite the shadow: ids can change under us even when the
            # visible contents have not.
            self.store.replace_all([p.shadow for p in plans if p.shadow])
            if outcome.bootstrapped and not self.dry_run:
                self.store.set_meta(_BOOTSTRAPPED, "1")
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
        if outcome.bootstrapped:
            self.store.set_meta(_BOOTSTRAPPED, "1")
        outcome.applied = True
        return outcome


def voice_additions(actions: list[Action]) -> list[str]:
    """Display names of things a Keep line just put on the active list.

    Two shapes, one event.  Creating a row and reviving a crossed-off one are
    both "somebody asked for this and it is on the list now" -- and with a
    catalogue of this size the revive is the common case, so reporting only the
    former would report almost nothing.

    Deliberately excludes items that became active in the AnyList app: those
    produce a projection into the note, not an addition to the master.
    """
    return [
        action.name or action.key
        for action in actions
        if action.side == ANYLIST
        and (action.kind == "add" or (action.kind == "check" and not action.checked))
    ]
