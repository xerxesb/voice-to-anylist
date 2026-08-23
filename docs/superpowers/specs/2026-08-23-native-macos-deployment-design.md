# Native macOS deployment

Replace the Docker Compose deployment with one that runs natively on a Mac
mini, and close the outstanding work that stood between the built service and
its first real run.

## Why

The target host is `xerxes@192.168.99.160`: a **Mac mini 6,2 (Late 2012),
Intel x86_64, 16 GB, macOS 10.15.7 Catalina**. Catalina is the ceiling for
that hardware.

Docker was never viable there. Docker Desktop dropped Catalina years ago, and
nothing else on the box supports it either:

| Constraint | Consequence |
| --- | --- |
| macOS 10.15, unupgradeable | Docker Desktop unavailable |
| No passwordless `sudo` | No `LaunchDaemon`, no `.pkg` installs, no writes to `/Library` |
| Homebrew 4.4.11, core tap frozen Dec 2024, Catalina untiered | No bottles; every formula source-builds. Unusable. |
| System Python 3.8.2 | Below the project's `requires-python = ">=3.11"` |

Three things were verified on the host before this design was written, because
each would have changed it:

- **Node 22 darwin-x64 runs.** Tested 18.20.8, 20.20.2 and 22.23.2; all three
  execute. No version compromise is needed.
- **`uv` runs, and installs a standalone CPython 3.12.14 in 2.3s** with
  working `ssl` (OpenSSL 3.5.7), `sqlite3` and `lzma`. No compiler, no sudo.
- **Egress is open** to `keep.google.com`, `www.anylist.com`, `nodejs.org` and
  `python.org`.

So the whole stack runs natively with **zero sudo and zero Homebrew**. The
host is also already a decent server: sleep disabled, wake-on-LAN and
auto-restart on, 51 days uptime, and a precedent for headless user agents in
`~/Library/LaunchAgents/homebrew.mxcl.syncthing.plist`.

## Architecture

One container running supervisord becomes two launchd user agents:

```
com.xerxesb.voice-to-anylist.sidecar   node server.js         -> 127.0.0.1:3000
com.xerxesb.voice-to-anylist.bridge    voice-to-anylist run   -> 127.0.0.1:8080
```

Two jobs rather than one Python parent supervising a Node child: launchd
already provides `KeepAlive` and throttled restart per job, and the bridge
already treats an unreachable sidecar as an ordinary transient failure with
backoff. A parent-child arrangement would reimplement both.

Both plists carry `LimitLoadToSessionType` including `Background` and
`System`, copying the syncthing agent, so they load without a GUI session.

### Layout on the host

| Path | Contents |
| --- | --- |
| `~/srv/voice-to-anylist` | Git clone. The directory a human `cd`s into. |
| `~/Library/Application Support/voice-to-anylist/` | `.env` (0600), `state.sqlite`, `keep_state.json`, `.anylist_credentials`, `logs/` |
| `~/Library/Application Support/voice-to-anylist/runtime/` | Pinned toolchain: `uv`, CPython 3.12 venv, unpacked Node 22 |

The toolchain is pinned and self-contained under the state directory. The
checkout stays clean, and no macOS or Homebrew change can move the interpreter
out from under a running service.

Secrets live only in `.env`, outside the checkout, mode 0600.

### Configuration reaches each process differently

The bridge is Python and reads `.env` through `pydantic-settings`, which
resolves it relative to the working directory — so both plists set
`WorkingDirectory` to the state directory.

The sidecar is Node and reads nothing on its own, so it is launched as
`node --env-file=<state>/.env server.js`. Node 22 parses the same
`KEY=VALUE`-with-comments format.

One `.env`, two readers, no duplication and no secrets in a plist.

## Deployment mechanism

`deploy/macos/install.sh`, idempotent and re-runnable, replacing
`docker compose up -d`:

1. Fetch a pinned `uv` into the runtime directory.
2. `uv python install 3.12`; create the venv; `uv pip install ./bridge`.
3. Fetch and unpack the pinned Node 22 darwin-x64 tarball.
4. `npm install --omit=dev` in `anylist-api/`.
5. Create state directories; seed `.env` from `.env.example` if absent, 0600.
6. Render both plists with absolute paths; `launchctl bootstrap gui/$UID`.

Alongside it: `deploy/macos/uninstall.sh`, and a `deploy/macos/vta` shim that
runs the CLI with the right interpreter and working directory, so
`vta doctor`, `vta sync --dry-run` and `vta bootstrap` need no venv activation.

Redeploying is `git pull && deploy/macos/install.sh`.

## Code changes

### 1. Unconfigured is not the same as broken

`cmd_run` calls `Settings.require_credentials()`, which raises `SystemExit`;
`anylist-api/server.js` calls `process.exit(1)` when `ANYLIST_EMAIL` or
`ANYLIST_PASSWORD` is unset. Under launchd both crash-loop until throttled to
a ten-minute retry interval, and the message explaining why scrolls away.

Misconfiguration and runtime failure want opposite handling. Missing
credentials need a human, so exiting accomplishes nothing and buries the
reason. Both sides change to stay up, log one line naming what is missing, and
report it on their health endpoint:

- `Settings.missing_credentials() -> list[str]` beside the existing
  `require_credentials()`.
- `cmd_run` branches on it; `Status` gains an `unconfigured` reason that
  `/healthz` (503) and `/status` surface.
- `server.js` serves `/health` with `ok: false` and the reason, instead of
  exiting.

Staying up is also what makes the `vta` shim usable for bootstrapping.

### 2. `doctor` prints every AnyList list

Pointing at the wrong list is the mistake most likely to make a dry run
propose deletions. The sidecar already exposes `GET /lists`; the client does
not call it. Add `AnyListClient.lists()` and have `cmd_doctor` print every
list name with the configured one marked, so the exact spelling can be copied
rather than guessed.

### 3. Configuration defaults leave the container world

`state_path`, `keep_state_path` and `ANYLIST_CREDENTIALS_FILE` default to
`/data/...`, which exists only inside the image. They move to a platform
default: `~/Library/Application Support/voice-to-anylist` on macOS,
`$XDG_STATE_HOME` or `~/.local/state/voice-to-anylist` elsewhere, and always
overridable by environment variable so tests and CI stay hermetic.

`http_host` changes from `0.0.0.0` to `127.0.0.1`. On a LAN box, binding every
interface publishes `/status` to the household network for no benefit.

### 4. The test suite must find Node wherever it is

`bridge/tests/conftest.py` passes `PATH=/usr/bin:/bin:/usr/local/bin` to the
sidecar subprocess, so `node` is resolved against that PATH rather than the
caller's. On any machine where Node lives elsewhere — an Apple Silicon
Homebrew at `/opt/homebrew/bin`, or the pinned runtime this design installs —
all 16 sidecar and end-to-end tests error with `FileNotFoundError` instead of
skipping or passing. Resolve the interpreter with `shutil.which("node")` and
pass the absolute path.

### 5. `bootstrap` stops printing Fly.io instructions

It prints `fly secrets set ...`. It should print where to put the values in
`.env` on this host.

## CI

`.github/workflows/ci.yml` runs `pytest` and `ruff` with Node installed, so
the sidecar contract and end-to-end tests actually execute rather than
skipping. The multi-arch image build planned in the previous handover is
dropped along with Docker. A `shellcheck` job covers `install.sh` instead —
that script is now the artifact that can break a deployment.

## Removed

`Dockerfile`, `docker-compose.yml`, `.dockerignore`, `fly.toml`,
`deploy/supervisord.conf`. None were ever built or run; keeping unverified
deployment code would mean documenting two paths and testing neither.

`README.md`, `docs/SETUP.md` and `docs/DEPLOYMENT.md` are rewritten around the
native path.

## Testing

The existing 124 tests keep passing, with the 16 currently erroring ones fixed
by change 4. New tests cover:

- `Settings.missing_credentials()` for each combination of absent credentials.
- `cmd_run` starting and serving `/healthz` 503 with an `unconfigured` reason
  when credentials are absent, rather than exiting.
- `/status` reporting `unconfigured`.
- `AnyListClient.lists()` against the stubbed sidecar, and `cmd_doctor`'s
  output marking the configured list.
- Platform defaults for the state paths, and that the environment overrides
  them.

The suite continues to need no credentials and touch no network.

## Verification

1. `pytest` and `ruff` green locally and in CI, including the 16 restored
   tests.
2. `install.sh` run on the mini, twice, to prove it is idempotent.
3. Both launchd jobs report running via `launchctl print gui/$UID/...`.
4. With an empty `.env`, `curl http://127.0.0.1:8080/healthz` returns 503
   naming the missing settings, and neither job crash-loops.
5. `vta doctor` reaches both services once credentials are in place, and lists
   the AnyList lists.
6. `vta sync --dry-run` prints a plan that is read before anything is written.
7. The real thing: "Hey Google, add strawberries to the shopping list" appears
   in AnyList within about 20 seconds.

## Out of scope

Credentials cannot be obtained programmatically. `bootstrap` requires an
interactive Google sign-in to copy an `oauth_token` cookie, and the AnyList
password is the user's to enter. Installation, startup and the unconfigured
health path are verifiable without them; the live sync is verified after the
user completes a short runbook.

## Known risk

A user LaunchAgent requires the box to be logged in. This one has been for 51
days, but a power cut that leaves it at the login window would stop the bridge
until someone logs in. The runbook carries the `LaunchDaemon` upgrade — one
`sudo` command, plists in `/Library/LaunchDaemons` — as the remedy if that
ever happens.
