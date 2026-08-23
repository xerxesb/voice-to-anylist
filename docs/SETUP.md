# Setup

Roughly half an hour, most of it in Google's UI rather than here. Work through
it in order: the Keep note has to exist before anything else will do anything.

If you have not installed it yet, start with
[DEPLOYMENT.md](DEPLOYMENT.md#installing).

## 1. The Keep note (the part that will bite you)

Google's speakers do not talk to third parties any more. What they still do is
write the shopping list into a **Google Keep** note, and that is what this
bridge reads.

1. In Google Keep, create a note titled exactly **`Shopping list`**.
2. Open its ⋮ menu and turn on **Show checkboxes**. A plain note will not do —
   the bridge refuses to run against one rather than guess.
3. **Share the note with every member of the household** (⋮ → Collaborator).
4. Each person opens Assistant settings on their phone and sets Keep as their
   **Notes & Lists** provider.

Step 3 is what makes one bridge serve everybody. Assistant writes into the
sharee's own view of a shared note, so everyone's "add strawberries" lands in
the same place and one poller sees all of it.

Two things worth knowing before you debug a missing item:

- **Voice Match decides whose list it is.** A recognised voice writes to that
  person's Keep. An unrecognised or guest voice writes to whichever account the
  speaker is linked to. If somebody's items never show up, their voice profile
  or their Notes & Lists provider is the first thing to check.
- **Say "shopping list", not "grocery list".** Assistant matches the list by
  name. If it hears a name that does not exist it will happily create a second
  one, and the bridge will not be watching it.

**Verify before continuing.** Say *"Hey Google, add test item to the shopping
list"* and confirm it appears in the Keep note. If it does not, nothing
downstream will work.

## 2. The Google master token

Keep has no usable official API for personal accounts — the documented one is
Workspace-only — so the bridge authenticates the way a phone does, with a
master token.

```bash
vta bootstrap
```

It sends you to Google's embedded-setup page, you copy one cookie value back,
and it prints the token.

The token behaves like a logged-in device: it lasts indefinitely, but is
revoked when the account password changes or the device is removed from the
account. It also grants access to **every** note in that account's Keep — its
scope cannot be narrowed. Treat it as a password.

That expiry is the single most likely reason this stops working in a year,
which is why `ALERT_WEBHOOK_URL` in step 3 is worth setting.

## 3. Configuration

Everything lives in one file, outside the checkout:

```bash
$EDITOR ~/Library/Application\ Support/voice-to-anylist/.env
```

Fill in the two Google values from step 2, your AnyList login, and the name of
the AnyList list to mirror. Set `ALERT_WEBHOOK_URL` to an
[ntfy.sh](https://ntfy.sh) topic while you are there — the master token dying
is the expected long-run failure, and the symptom (items quietly not arriving)
otherwise goes unnoticed for a week.

Then pick the new settings up:

```bash
vta restart
```

## 4. Check it before you let it write

```bash
vta doctor
```

`doctor` checks each side separately, so a failure names its own cause. It
prints **every list on your AnyList account**, quoted, with the configured one
marked:

```
  ok    AnyList: 3 list(s) on the account
             'Costco'
          -> 'Grocery'
             'Hardware'
  ok    AnyList list 'Grocery': 12 items
  ok    Keep note 'Shopping list': 4 items (1 ticked)
```

Copy the spelling from there rather than guessing it. Pointing at the wrong
list is the mistake most likely to make the next step propose deletions.

## 5. Rehearse

```bash
vta sync --dry-run
```

It prints every change it *would* make and writes nothing. **Read that list.**
If it proposes deleting things you wanted to keep, stop and work out why before
continuing — most often the wrong `ANYLIST_LIST`.

## 6. Go live

The service is already running; it just has not been given anything to do yet.
Confirm both sides are healthy and let it work:

```bash
vta health     # expect HTTP 200
vta status     # last sync, item counts on both sides, guard state
```

Then say *"Hey Google, add strawberries to the shopping list"* and watch it
land in AnyList within about 20 seconds.

## Operating it

```bash
vta status     # last successful sync, failures, item counts, guard state
vta health     # 200 while syncing, 503 once it has silently stopped
vta logs       # both processes, followed
vta jobs       # whether launchd has them running
vta restart
```

`/healthz` returns 503 once syncing has stopped, which is the failure worth
catching — a process that keeps answering while nothing syncs is exactly what
a plain liveness check misses.

### "The guard paused a sync that looked destructive"

The bridge will not propagate a mass deletion the first time it sees one,
because an unofficial API returning an empty list looks exactly like a user
clearing their list. If the same change is still there on the next cycle it
goes through. So: if you really did clear the list, wait one cycle. If you did
not, look at `vta status` — something is failing.

### Rotating the token

Re-run `vta bootstrap`, update `GOOGLE_MASTER_TOKEN`, `vta restart`. Nothing
else needs to change; the shadow state stays valid.

### If Google breaks the Keep API

It might; it is unofficial and unendorsed. Both sides sit behind the same
narrow client interface (`bridge/src/voice_to_anylist/clients/base.py`), so a
replacement input — an Alexa skill, a webhook, a Home Assistant `todo` entity —
is a new client class and no change to the merge engine.
