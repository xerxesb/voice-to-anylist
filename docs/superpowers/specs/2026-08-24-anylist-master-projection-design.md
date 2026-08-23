# AnyList as master, Keep as projection

Replace the symmetric two-way mirror with a one-master model, so a list that
doubles as a purchase catalogue is safe to sync.

## Why

The first live cycle against the real list did two things it should never do.
It planned to copy **1091 crossed-off items into the Keep note**, and it
deleted **19 crossed-off rows** as duplicates before it was stopped. Neither
was a bug in the code: both are what a symmetric mirror is supposed to do.

The mistake was the model. `Alexa Shopping List` holds 1432 rows, of which
**1422 are crossed off**. It is not a scratch list that happens to have some
items ticked — it is a catalogue of everything the household buys, where
ticked means "not currently needed". A mirror sees 1422 items to replicate and
354 duplicates to collapse. A catalogue sees history that must not be touched.

So the direction of authority becomes explicit: **AnyList is master. The Keep
note is a projection of AnyList's active items, plus an inbox for voice adds.**

## Rules

Let `A` be the AnyList row for a key and `K` the Keep line.

| State | Action |
| --- | --- |
| `K` only, no `A` | New voice add → create it in AnyList, unchecked |
| `K`, and `A` is crossed off | **Revive**: uncheck `A`. No new row. |
| `K` unticked, `A` active | In sync. Quantity reconciled, AnyList wins. |
| `K` ticked, `A` active | Purchased → check `A`, drop the line from Keep |
| `K` gone (was mapped), `A` active | Purchased → check `A` |
| `K` absent, no shadow, `A` active | Project → add to Keep |
| `K` absent, `A` crossed off | Correct. Nothing to do. |
| Neither side, shadow entry | Forget it |

Two invariants fall out, and they are the point of the change:

- **A crossed-off AnyList row is never copied to Keep, never deduped, never
  deleted, and never modified except by a revive.** History is off-limits.
- **The bridge never deletes an AnyList row.** The only removal it can emit on
  that side is collapsing a duplicate among *active* items.

### Revive, and what "history" then means

Asking for something already crossed off revives the existing row rather than
appending a second one. That makes the list a catalogue — "everything we buy,
ticked = not needed right now" — rather than a purchase log. It will not tell
you how many times ham was bought. This is a deliberate choice, made on the
understanding that the rows themselves are what must survive.

### First sync

When the shadow is empty there is no basis for deciding whether a Keep line is
a new voice add or a leftover. Rather than guess, bootstrap asserts the master:
**the Keep note is replaced with exactly AnyList's active set.** Nothing is
deleted from AnyList.

The caveat is that losing `state.sqlite` re-runs this, discarding any voice
adds sitting unsynced in the note. That is a narrow window and it costs at
worst a re-spoken item, where the alternative risks writing junk into the
master.

## Guard

The guard counted propagated *deletions*. Under this model AnyList deletions
are nearly extinct, and the destructive act is now marking rows purchased in
bulk — which is what a Keep fetch returning empty would trigger for every
active item at once. So it counts **destructive AnyList writes**: removals,
plus checks caused by a Keep line's absence. Dedupe of active items is
excluded, as same-side collapsing was before.

The existing empty-side and ratio rules and the confirm-on-repeat behaviour
carry over unchanged.

## Safety defaults

`DRY_RUN` ships **true**. The incident happened because a live service was
restarted against a real list before anyone had read a plan. Writing should be
something switched on deliberately, once a dry run has been read.

## Testing

`test_engine.py` is rewritten: its cases assert symmetric merge semantics that
no longer exist. New coverage, driven by what actually went wrong:

- A crossed-off AnyList row is never added to Keep — the 1091 case.
- A crossed-off row is never deduped, even with many sharing a key — the 19 case.
- No plan ever contains an AnyList `remove` except an active-item dedupe.
- Revive: a Keep line matching a crossed-off row unchecks it, adding nothing.
- Purchase: ticking or dropping a mapped Keep line checks the AnyList row.
- Projection: an active AnyList row missing from Keep is restored to Keep.
- Bootstrap: empty shadow replaces the note with AnyList's active set.
- The guard trips when an empty Keep fetch would mark every active row purchased.

## Out of scope

Item identity, quantity parsing and the Keep and AnyList clients are unchanged.
This is a change to the planner and the guard only.
