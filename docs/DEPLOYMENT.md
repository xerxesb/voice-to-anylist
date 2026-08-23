# Deployment

How this runs on a Mac, what it puts where, and what to do when it breaks.
For first-time credential setup, see [SETUP.md](SETUP.md).

## Shape

Two launchd **user agents**, no container and no supervisor:

| Label | Process | Listens on |
| --- | --- | --- |
| `com.xerxesb.voice-to-anylist.sidecar` | `node anylist-api/server.js` | `127.0.0.1:3000` |
| `com.xerxesb.voice-to-anylist.bridge` | `voice-to-anylist run` | `127.0.0.1:8080` |

Two jobs rather than one process supervising the other: launchd already
provides `KeepAlive` and throttled restart per job, and the bridge already
treats an unreachable sidecar as an ordinary transient failure with backoff.

Both are user agents rather than system daemons, and nothing is installed
outside `$HOME`. That is deliberate — see [Why not Docker](#why-not-docker).

## Host requirements

- macOS 10.15 or later, Intel or Apple Silicon.
- Outbound HTTPS. Nothing needs to be reachable from outside the machine.
- The box stays logged in — see [Known limitation](#known-limitation-a-user-agent-needs-a-login).
- **No sudo, no Homebrew, and no system Python or Node.** The installer pins
  and unpacks its own.

Turn off sleep on the machine, or it will not poll:

```bash
sudo pmset -a sleep 0 disksleep 10 womp 1 autorestart 1
```

## Installing

```bash
git clone -b claude/anylist-voice-assistant-j3419q \
  https://github.com/xerxesb/voice-to-anylist ~/srv/voice-to-anylist
~/srv/voice-to-anylist/deploy/macos/install.sh
```

The installer is idempotent, and re-running it is how you deploy:

```bash
cd ~/srv/voice-to-anylist && git pull && ./deploy/macos/install.sh
```

It fetches a pinned `uv`, a standalone CPython 3.12 and a pinned Node 22,
verifying the checksum of each; builds the virtualenv; installs the sidecar's
npm dependencies; seeds `.env` if absent; renders and loads both agents; and
links `vta` into `~/.local/bin`.

Add `~/.local/bin` to your `PATH` if it is not there already.

## Layout

| Path | Contents |
| --- | --- |
| `~/srv/voice-to-anylist` | The checkout. Nothing secret, nothing generated. |
| `~/Library/Application Support/voice-to-anylist/.env` | Every credential, mode 0600 |
| `…/state.sqlite` | The shadow of the last agreed state |
| `…/keep_state.json`, `…/.anylist_credentials` | Cached sessions |
| `…/logs/{bridge,sidecar}.log` | Both processes |
| `…/runtime/` | Pinned CPython, Node and the virtualenv |
| `~/Library/LaunchAgents/com.xerxesb.voice-to-anylist.*.plist` | The two agents |

Secrets live outside the checkout, so `git clean` cannot touch them and a
stray `git add -A` cannot commit them. State is separate from the runtime, so
reinstalling never risks the shadow.

Override the whole state location with `VTA_STATE_DIR` if you want it
elsewhere; both processes and `vta` honour it.

### Configuration reaches the two processes differently

One `.env`, two readers. The bridge is Python and finds it through
`pydantic-settings`, which resolves `.env` relative to the working directory —
so both agents set `WorkingDirectory` to the state directory. The sidecar is
Node and is launched with `--env-file-if-exists`. Neither plist contains a
secret.

## Operating

```bash
vta status | health | logs | jobs | restart
vta doctor | sync --dry-run | bootstrap | config
```

Anything `vta` does not recognise is passed through to the CLI.

## Updating

```bash
cd ~/srv/voice-to-anylist && git pull && ./deploy/macos/install.sh
```

Both agents are reloaded against the new plists. State and credentials are
untouched. To move to a newer Python or Node, bump the pins at the top of
`install.sh` and re-run it — the virtualenv is rebuilt when the pinned Python
no longer matches.

## Uninstalling

```bash
~/srv/voice-to-anylist/deploy/macos/uninstall.sh
```

Removes the agents, the runtimes and the `vta` link, and **keeps** state and
credentials. Add `--purge` to remove those too.

## When something breaks

Start here:

```bash
vta jobs      # is launchd running them at all
vta health    # 503 means it has stopped syncing, or was never configured
vta status    # last success, consecutive failures, guard state
vta logs
```

**Both jobs running, `/healthz` 503 naming settings.** Not a fault — it has
not been configured yet. Work through [SETUP.md](SETUP.md).

**A job is not running.** `launchctl print gui/$UID/com.xerxesb.voice-to-anylist.bridge`
gives its state, run count and last exit code. A `runs` count climbing by
itself is a crash loop; the reason will be in `vta logs`.

**Nothing since a reboot.** See below.

## Known limitation: a user agent needs a login

A LaunchAgent loads when the user session does. The agents carry
`LimitLoadToSessionType` including `Background` and `System`, so they run
headless, but a machine sitting at the login window after a power cut will not
start them until somebody logs in.

If that ever happens, promote them to system daemons. This is the one step
that needs a password:

```bash
sudo cp ~/Library/LaunchAgents/com.xerxesb.voice-to-anylist.*.plist /Library/LaunchDaemons/
sudo chown root:wheel /Library/LaunchDaemons/com.xerxesb.voice-to-anylist.*.plist
# Daemons have no user context, so each plist needs a UserName key adding.
sudo launchctl bootstrap system /Library/LaunchDaemons/com.xerxesb.voice-to-anylist.sidecar.plist
sudo launchctl bootstrap system /Library/LaunchDaemons/com.xerxesb.voice-to-anylist.bridge.plist
```

Enabling automatic login is the lighter alternative, and is usually enough.

## Why not Docker

The original plan was Docker Compose, and before that Fly.io. Both were
dropped once the target host was known: a **Mac mini 6,2 (Late 2012)**, which
cannot go past **macOS 10.15**.

| Constraint | Consequence |
| --- | --- |
| macOS 10.15, unupgradeable | Docker Desktop dropped Catalina years ago |
| No passwordless sudo | No system daemon, no `.pkg`, no writes to `/Library` |
| Homebrew untiered on Catalina | No bottles; every formula source-builds |
| System Python 3.8.2 | Below the project's `requires-python = ">=3.11"` |

A standalone CPython via `uv` and an unpacked Node tarball clear all four at
once, and need no privileges. The container image had never been built, so
keeping it would have meant documenting two paths and testing neither.
