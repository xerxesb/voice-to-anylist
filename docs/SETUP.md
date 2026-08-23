# Setup

Roughly half an hour, most of it in Google's UI rather than here. Work through
it in order: the Keep note has to exist before anything else will do anything.

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

Verify before continuing: say *"Hey Google, add test item to the shopping
list"* and confirm it appears in the Keep note.

## 2. The Google master token

Keep has no usable official API for personal accounts — the documented one is
Workspace-only — so the bridge authenticates the way a phone does, with a
master token.

```bash
voice-to-anylist bootstrap
```

Follow the prompts. It sends you to Google's embedded-setup page, you copy one
cookie value back, and it prints the token.

The token behaves like a logged-in device: it lasts indefinitely, but is
revoked when the account password changes or the device is removed from the
account. That is the single most likely reason this will stop working in a
year, which is why `ALERT_WEBHOOK_URL` is worth setting.

## 3. Configuration

```bash
cp .env.example .env
```

Fill in the Google values from step 2, your AnyList login, and the name of the
AnyList list to mirror (`ANYLIST_LIST`, default `Grocery`).

## 4. Check it before you let it write

```bash
docker compose up -d
docker compose exec voice-to-anylist voice-to-anylist doctor
```

`doctor` checks each side separately, so a failure names its own cause: which
list it found, how many items are on it, or exactly which credential was
rejected.

Then rehearse a real cycle without writing anything:

```bash
docker compose exec voice-to-anylist voice-to-anylist sync --dry-run
```

It prints every change it *would* make. Read that list. If it proposes
deleting things you wanted to keep, stop and work out why before continuing —
most often the wrong `ANYLIST_LIST`.

When it looks right, set `DRY_RUN=false` and restart.

## 5. Deploying to Fly.io

```bash
fly launch --no-deploy
fly volumes create state --size 1
fly secrets set \
  GOOGLE_EMAIL="you@gmail.com" \
  GOOGLE_MASTER_TOKEN="aas_et/..." \
  ANYLIST_EMAIL="you@example.com" \
  ANYLIST_PASSWORD="..." \
  ALERT_WEBHOOK_URL="https://ntfy.sh/your-secret-topic"
fly deploy
```

The volume matters. It holds the shadow state and the cached credentials; lose
it and the next start treats both lists as brand new, which is safe — it unions
them rather than deleting — but noisy.

`auto_stop_machines` is off deliberately. The bridge polls Google; a machine
that sleeps when no one is looking at its HTTP port syncs nothing.

## Operating it

```bash
curl https://your-app.fly.dev/status
```

Reports the last successful sync, consecutive failures, item counts on both
sides, and whether the guard has tripped. `/healthz` returns 503 once syncing
has silently stopped, which is what the platform health check watches — a
container that keeps answering while nothing syncs is the failure worth
catching.

### "The guard paused a sync that looked destructive"

The bridge will not propagate a mass deletion the first time it sees one,
because an unofficial API returning an empty list looks exactly like a user
clearing their list. If the same change is still there on the next cycle it
goes through. So: if you really did clear the list, wait one cycle. If you did
not, look at `/status` — something is failing.

### Rotating the token

Re-run `voice-to-anylist bootstrap` and update `GOOGLE_MASTER_TOKEN`. Nothing
else needs to change; the shadow state stays valid.

### If Google breaks the Keep API

It might; it is unofficial and unendorsed. Both sides sit behind the same
narrow client interface, so a replacement input — an Alexa skill, a webhook, a
Home Assistant `todo` entity — is a new client class and no change to the merge
engine.
