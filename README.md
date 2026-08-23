# voice-to-anylist

*"Hey Google, add strawberries to the shopping list"* → it appears in AnyList.

No app to open, no phrase to learn, no per-person setup beyond sharing one
note. Anyone in the house can say it, and Google gives the spoken confirmation
itself.

## Why this shape

Both of AnyList's voice integrations have been broken by their platforms:

- **Google** stopped supporting third-party Notes & Lists integration in June
  2023. There is no longer any API by which a Nest speaker hands arbitrary text
  to a third party, so AnyList's integration cannot come back.
- **Amazon** shut down List skills and the List Management REST API in July
  2024, forcing AnyList onto `"Alexa, ask AnyList to..."` — a carrier phrase
  that has been failing widely since March 2026.

What Google *does* still do is write its shopping list into a Google Keep
checklist note. So this bridge lets Google do the part it is good at — hearing
the utterance, extracting the item, confirming out loud — and mirrors Keep
against AnyList behind it.

```
Nest speakers ──native──> Google Keep note ──gkeepapi──> bridge ──> AnyList
```

The mirror is two-way: *"what's on my shopping list?"* still reads back, and
ticking an item off in the AnyList app clears it from the note.

## What it handles

- **Quantities.** "add two lemons" becomes an AnyList item named `lemons` with
  a quantity of `2`, not an item called `2 lemons`.
- **Duplicates.** `Strawberries` and `strawberry` are one item. Asking for
  something that is already there but ticked off revives it instead of adding a
  second copy — including when Google appends a fresh line without noticing.
- **Its own writes.** Each side is compared against a stored shadow of the last
  agreed state, so nothing echoes back as a new item on the next cycle.
- **Failure.** A single blip is ignored; a run of them alerts. A mass deletion
  is not propagated until it has been seen twice, so an API returning nothing
  is never read as "the user cleared their list".
- **Not being set up yet.** A host with no credentials stays up and says so on
  its health endpoint, rather than restart-looping and looking broken.

## Getting started

It runs natively on a Mac, as two launchd user agents. No Docker, no
Homebrew, no sudo — it pins and unpacks its own Python and Node under your
home directory.

```bash
git clone -b claude/anylist-voice-assistant-j3419q \
  https://github.com/xerxesb/voice-to-anylist ~/srv/voice-to-anylist
~/srv/voice-to-anylist/deploy/macos/install.sh
```

Then work through **[docs/SETUP.md](docs/SETUP.md)**. The Keep note has to be
created and shared before anything else matters, and `vta sync --dry-run` is
worth reading before you let it write.

**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** covers the host layout, how to
update, and what to do when something breaks.

## Layout

| Path | What it is |
| --- | --- |
| `bridge/` | The Python service: merge engine, clients, CLI |
| `bridge/src/voice_to_anylist/engine.py` | Three-way merge and the safety guard |
| `bridge/src/voice_to_anylist/normalise.py` | Quantity parsing and item identity |
| `anylist-api/` | Loopback-only Node facade over the `anylist` package |
| `deploy/macos/` | `install.sh`, the two launchd agents, and the `vta` shim |

Two languages because the best-maintained client for each service is in a
different one: `gkeepapi` for Keep, `anylist` for AnyList. They talk over
loopback, so the AnyList facade never binds a public interface.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e 'bridge[dev]'
npm install --prefix anylist-api
(cd bridge && ../.venv/bin/python -m pytest)
```

The suite needs no credentials and touches no network. The engine is tested
against in-memory fakes, and the Python/Node seam against the real Express
server running on a stubbed AnyList. Those seam tests skip without `node` and
an installed `anylist-api` — except under `CI`, where they fail instead, since
quietly not testing the seam defeats the point of having a test for it.

## Caveats

`gkeepapi` is unofficial and unendorsed by Google; the official Keep API is
Workspace-only and unavailable to a personal account. Google could break this.
The guard rails, the dry-run mode and the alerting all exist because of that,
and both sides sit behind a narrow client interface so a different input can be
added without touching the merge engine.

Sync is polled, not pushed. Expect around 20 seconds, not instant.
