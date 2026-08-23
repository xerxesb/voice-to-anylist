# Deployment handover

The bridge is built and tested but **has never been run against real
accounts**. This document is the handover for whoever picks that up — read it
before touching anything, because it records what is verified, what is not, and
which sharp edges are known.

## Status

**Done and verified** (124 tests, no credentials, no network):

- Item normaliser — quantity parsing and the matching key.
- Three-way merge engine — adds, deletes, ticks, echo prevention, same-side
  dedupe, and the mass-deletion guard.
- Service loop — backoff, alerting, health reporting, thread safety.
- The Python↔Node seam, tested against the real Express server running on an
  in-memory stub of the `anylist` package.
- End-to-end user stories, with only Google itself simulated.

**Not verified:**

- **The Docker image has never been built.** This is the most likely thing to
  fail on first run. See task 3 below.
- Google Keep authentication and sync, against a real account.
- AnyList authentication and writes, against a real account.

The work was done in a Claude Code web session whose egress policy refused
every host involved — `keep.google.com`, `www.anylist.com`,
`android.clients.google.com`, `api.fly.io`, and Docker Hub's blob CDN — all
403 on CONNECT. Nothing could be built, deployed or exercised against a live
service from there. That is why these gaps exist; they are not oversights.

## Decisions already made

| Decision | Choice |
| --- | --- |
| Where it runs | Home Assistant box, via Docker Compose |
| Google account | The user's main account |
| AnyList list to mirror | `Grocery` |
| Keep note title | `Shopping list` |
| Sync model | Two-way mirror |

Cloud (Fly.io) was the original plan and `fly.toml` still works, but Compose on
the always-on HA box won: no third-party account, no volume to provision, and
credentials never leave the house. It only needs outbound HTTPS.

## Outstanding work

Four changes, in rough priority order. None are large.

### 1. A missing token must not crash-loop the container

`bridge/src/voice_to_anylist/cli.py::cmd_run` calls
`Settings.require_credentials()`, which raises `SystemExit`. Supervisor
restarts it, gives up, and the `quit_on_fatal` listener in
`deploy/supervisord.conf` then kills the container. So the very first
`docker compose up -d` — before a token exists — crash-loops and exits, which
reads as broken software rather than "not configured yet".

Misconfiguration and runtime failure want opposite handling:

- **Misconfiguration** (no token yet): stay up, log one clear line naming what
  is missing and how to get it, and serve `/healthz` 503 with that reason. It
  needs a human, so crash-looping accomplishes nothing and buries the message.
- **Runtime failure** (configured, then something broke): leave as-is. The
  current behaviour is correct and is what the health check exists for.

Suggested shape: add `Settings.missing_credentials() -> list[str]` next to the
existing `require_credentials()` in `bridge/src/voice_to_anylist/config.py`,
branch on it in `cmd_run`, and give `Status` in
`bridge/src/voice_to_anylist/service.py` an `unconfigured` reason that
`/healthz` and `/status` surface.

Staying up is also what makes `docker compose exec` usable for bootstrapping.

### 2. `doctor` should print every AnyList list name

Pointing at the wrong list is the mistake most likely to make the dry run
propose deletions. The sidecar already exposes `GET /lists`
(`anylist-api/server.js`) — `AnyListClient` simply does not call it.

Add `AnyListClient.lists()` in `bridge/src/voice_to_anylist/clients/anylist.py`
and have `cmd_doctor` print every list name with the configured one marked, so
the exact spelling can be copied rather than guessed.

### 3. CI that builds the image

The Dockerfile is unverified. A workflow at `.github/workflows/ci.yml` should:

- run `pytest` and `ruff` — note the contract and end-to-end tests need
  `npm install` in `anylist-api/`, and skip silently without it;
- build for **`linux/amd64` and `linux/arm64`** and push to GHCR.

arm64 matters: if the HA box is a Raspberry Pi, building locally is slow, and a
published image turns setup into `docker compose pull`. It also gets the build
verified by something other than the user's first attempt.

Then have `docker-compose.yml` prefer the published image:

```yaml
image: ghcr.io/xerxesb/voice-to-anylist:latest
build: .          # used by `docker compose build`, ignored by `pull`
```

### 4. Docs should lead with Compose, not Fly

`README.md` and `docs/SETUP.md` present Fly.io as the deployment path. Demote
it to an appendix and lead with the quickstart below. Keep `fly.toml`.

## Setup runbook

About half an hour, most of it in Google's UI.

### Step 1 — the Keep note (the step that silently breaks everything)

Google's speakers do not talk to third parties any more. What they still do is
write the shopping list into a Google Keep note, and that is what this bridge
reads.

1. In Google Keep, create a note titled exactly **`Shopping list`**.
2. ⋮ menu → **Show checkboxes**. A plain note will not do; the bridge refuses
   to run against one rather than guess.
3. **Share it with every member of the household** (⋮ → Collaborator).
4. Each person sets Keep as their **Notes & Lists** provider in Assistant
   settings.

Step 3 is what makes one bridge serve everybody: Assistant writes into each
person's own view of the shared note, so one poller sees all of it.

**Verify before continuing.** Say *"Hey Google, add test item to the shopping
list"* and confirm it appears in the note. If it does not, nothing downstream
will work.

Two things worth knowing before debugging a missing item:

- **Voice Match decides whose list it is.** A recognised voice writes to that
  person's Keep; an unrecognised or guest voice writes to whichever account the
  speaker is linked to.
- **Say "shopping list", not "grocery list".** Assistant matches by name, and
  will happily create a second list the bridge is not watching.

### Step 2 — clone and configure

```bash
git clone -b claude/anylist-voice-assistant-j3419q \
  https://github.com/xerxesb/voice-to-anylist && cd voice-to-anylist
cp .env.example .env
```

### Step 3 — the Google master token

```bash
docker compose run --rm voice-to-anylist voice-to-anylist bootstrap
```

`compose run` starts a throwaway container, so this works before the service is
configured — and before task 1 above is done.

It walks through signing in at Google's embedded-setup page and copying one
cookie value back, then prints the token. Put it and the account email in
`.env`.

The token behaves like a logged-in device: it lasts indefinitely, but is
revoked when the account password changes or the device is removed from the
account. It also grants access to **every** note in that account's Keep — its
scope cannot be narrowed.

### Step 4 — fill in the rest of `.env`

AnyList email and password, and `ANYLIST_LIST=Grocery`.

### Step 5 — check before letting it write

```bash
docker compose up -d
docker compose exec voice-to-anylist voice-to-anylist doctor
```

`doctor` checks each side separately, so a failure names its own cause: which
list it found, how many items are on it, or exactly which credential was
rejected. Confirm `Grocery` is spelled the way AnyList spells it.

### Step 6 — rehearse

```bash
docker compose exec voice-to-anylist voice-to-anylist sync --dry-run
```

Prints every change it *would* make and writes nothing. **Read that list.** If
it proposes deleting things worth keeping, stop — almost always the wrong
`ANYLIST_LIST`.

### Step 7 — go live

Set `DRY_RUN=false` in `.env`, then `docker compose up -d` again.

### Step 8 — alerting (recommended)

Set `ALERT_WEBHOOK_URL` to an [ntfy.sh](https://ntfy.sh) topic. The master
token dying is the expected long-run failure, and the symptom — items quietly
not arriving — otherwise goes unnoticed for a week.

## Credentials

All of these live in `.env` on the Docker host and nowhere else. **Do not paste
them into a chat session, a commit, or an issue.** The Google master token in
particular is equivalent to a logged-in device on that account.

| Setting | Where it comes from |
| --- | --- |
| `GOOGLE_EMAIL` | The account whose Keep note Assistant writes to |
| `GOOGLE_MASTER_TOKEN` | Printed by step 3 |
| `ANYLIST_EMAIL` / `ANYLIST_PASSWORD` | The AnyList login |
| `ANYLIST_LIST` | `Grocery` |
| `ALERT_WEBHOOK_URL` | Optional ntfy topic |

`.env` is already in `.gitignore`. Keep it that way.

## Verification

1. `cd bridge && pytest` — the suite passes, including new tests for the
   unconfigured-startup path and `doctor`'s list output.
2. CI green and the multi-arch image published — this is what finally proves
   the Dockerfile builds.
3. `docker compose up -d` **with an empty `.env`** stays up and reports the
   missing settings on `/healthz` instead of crash-looping.
4. The runbook above, ending with the real thing:
   - Say *"Hey Google, add strawberries to the shopping list"* → appears in
     AnyList within ~20 seconds.
   - Say *"Hey Google, add two lemons to the shopping list"* → AnyList holds an
     item named `lemons` with quantity `2`, not one called `2 lemons`.
   - Check `strawberries` off in the AnyList app → it clears from the Keep note
     on the next cycle.
   - Ask *"Hey Google, what's on my shopping list?"* → reads back items added
     in AnyList.
   - Have a second household member add an item by voice → it arrives. This is
     what validates the shared-note setup from step 1.

## If something goes wrong

`curl http://<host>:8080/status` reports the last successful sync, consecutive
failures, item counts on both sides, and whether the guard has tripped.

**"The guard paused a sync that looked destructive."** The bridge will not
propagate a mass deletion the first time it sees one, because an unofficial API
returning an empty list looks exactly like a user clearing their list. If the
same change is still there next cycle, it goes through. So if the list really
was cleared, wait one cycle; if it was not, check `/status` — something is
failing.

**Keep authentication stopped working.** Re-run `bootstrap` and update
`GOOGLE_MASTER_TOKEN`. Nothing else changes; the shadow state stays valid.

**Google breaks the Keep API entirely.** It might — it is unofficial and
unendorsed, and the official Keep API is Workspace-only. Both sides sit behind
the same narrow client interface (`bridge/src/voice_to_anylist/clients/base.py`),
so a replacement input — an Alexa skill, a webhook, a Home Assistant `todo`
entity — is a new client class and no change to the merge engine.
