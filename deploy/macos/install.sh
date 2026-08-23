#!/usr/bin/env bash
#
# Install and start voice-to-anylist as two launchd user agents.
#
# The target is a Mac mini on macOS 10.15, where Docker Desktop is unavailable,
# Homebrew has no bottles, and there is no passwordless sudo.  So this touches
# nothing outside $HOME: the Python and Node runtimes are pinned, downloaded,
# checksummed and unpacked under the state directory, and the jobs are user
# agents rather than system daemons.
#
# Idempotent.  Re-running it is how you deploy a new version:
#
#     git pull && deploy/macos/install.sh
#
# Catalina ships bash 3.2, so nothing here uses bash 4 syntax.

set -euo pipefail

NODE_VERSION="22.23.2"
UV_VERSION="0.12.5"
PYTHON_VERSION="3.12"

LABEL_PREFIX="com.xerxesb.voice-to-anylist"
BRIDGE_LABEL="${LABEL_PREFIX}.bridge"
SIDECAR_LABEL="${LABEL_PREFIX}.sidecar"

REPO_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
STATE_DIR="${VTA_STATE_DIR:-$HOME/Library/Application Support/voice-to-anylist}"
RUNTIME_DIR="$STATE_DIR/runtime"
LOG_DIR="$STATE_DIR/logs"
ENV_FILE="$STATE_DIR/.env"
AGENTS_DIR="$HOME/Library/LaunchAgents"
VENV_DIR="$RUNTIME_DIR/venv"
NODE_DIR="$RUNTIME_DIR/node-v${NODE_VERSION}"
UV="$RUNTIME_DIR/bin/uv"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

case "$(uname -s)" in
  Darwin) ;;
  *) die "this installer is for macOS; see docs/DEPLOYMENT.md" ;;
esac

case "$(uname -m)" in
  x86_64) UV_TARGET="x86_64-apple-darwin"; NODE_TARGET="darwin-x64" ;;
  arm64)  UV_TARGET="aarch64-apple-darwin"; NODE_TARGET="darwin-arm64" ;;
  *) die "unsupported architecture: $(uname -m)" ;;
esac

mkdir -p "$RUNTIME_DIR/bin" "$LOG_DIR" "$AGENTS_DIR"

# ---------------------------------------------------------------------------
# Downloads are verified.  Over HTTPS a checksum mostly catches a truncated or
# corrupted transfer, which otherwise surfaces much later as an inscrutable
# runtime error rather than a failed install.
# ---------------------------------------------------------------------------

verify_sha256() {
  local file="$1" expected="$2" actual
  actual="$(shasum -a 256 "$file" | awk '{print $1}')"
  [ "$actual" = "$expected" ] || die "checksum mismatch for $file
  expected $expected
  got      $actual"
}

# -- uv ---------------------------------------------------------------------

if [ ! -x "$UV" ] || [ "$("$UV" --version 2>/dev/null | awk '{print $2}')" != "$UV_VERSION" ]; then
  say "installing uv $UV_VERSION"
  tmp="$(mktemp -d)"
  base="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_TARGET}.tar.gz"
  curl -fsSL --retry 3 -o "$tmp/uv.tar.gz" "$base"
  curl -fsSL --retry 3 -o "$tmp/uv.sha256" "${base}.sha256"
  verify_sha256 "$tmp/uv.tar.gz" "$(awk '{print $1}' "$tmp/uv.sha256")"
  tar xzf "$tmp/uv.tar.gz" -C "$tmp"
  mv "$(find "$tmp" -type f -name uv -perm -u+x | head -1)" "$UV"
  chmod +x "$UV"
  rm -rf "$tmp"
else
  say "uv $UV_VERSION already installed"
fi

# -- Python -----------------------------------------------------------------
#
# A standalone CPython, not the system 3.8 (too old) and not Homebrew (no
# Catalina bottles, so every formula source-builds).  It brings its own
# OpenSSL, which matters: a Python without a working ssl module cannot reach
# Google or AnyList at all.

export UV_PYTHON_INSTALL_DIR="$RUNTIME_DIR/python"
say "installing CPython $PYTHON_VERSION"
# --no-bin: everything here runs out of the venv or by absolute path, so the
# shims in ~/.local/bin would only be another thing to collide with.
"$UV" python install --no-bin "$PYTHON_VERSION"

# Reuse the virtualenv across deploys, but rebuild it if the pinned Python has
# moved underneath it -- otherwise a version bump silently keeps the old one.
if "$VENV_DIR/bin/python" -V 2>/dev/null | grep -q "^Python $PYTHON_VERSION\."; then
  say "reusing the virtualenv"
  "$UV" venv --python "$PYTHON_VERSION" --allow-existing "$VENV_DIR" >/dev/null
else
  say "building the virtualenv"
  "$UV" venv --python "$PYTHON_VERSION" --clear "$VENV_DIR" >/dev/null
fi
VIRTUAL_ENV="$VENV_DIR" "$UV" pip install --quiet "$REPO_DIR/bridge"

# -- Node -------------------------------------------------------------------

if [ ! -x "$NODE_DIR/bin/node" ]; then
  say "installing Node $NODE_VERSION"
  tmp="$(mktemp -d)"
  tarball="node-v${NODE_VERSION}-${NODE_TARGET}.tar.gz"
  curl -fsSL --retry 3 -o "$tmp/$tarball" "https://nodejs.org/dist/v${NODE_VERSION}/${tarball}"
  curl -fsSL --retry 3 -o "$tmp/SHASUMS256.txt" \
    "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"
  verify_sha256 "$tmp/$tarball" "$(awk -v f="$tarball" '$2 == f {print $1}' "$tmp/SHASUMS256.txt")"
  rm -rf "$NODE_DIR"
  mkdir -p "$NODE_DIR"
  tar xzf "$tmp/$tarball" -C "$NODE_DIR" --strip-components=1
  rm -rf "$tmp"
else
  say "Node $NODE_VERSION already installed"
fi

# Stable path for the plists and the shim, so a version bump does not require
# rewriting them.
ln -sfn "$NODE_DIR" "$RUNTIME_DIR/node"

say "installing sidecar dependencies"
PATH="$RUNTIME_DIR/node/bin:$PATH" \
  npm install --prefix "$REPO_DIR/anylist-api" --omit=dev --no-audit --no-fund --silent

# -- Configuration ----------------------------------------------------------
#
# Secrets live here, outside the checkout, so a `git clean` or a stray commit
# cannot touch them.

if [ ! -f "$ENV_FILE" ]; then
  say "seeding $ENV_FILE"
  cp "$REPO_DIR/.env.example" "$ENV_FILE"
fi
chmod 600 "$ENV_FILE"

# -- launchd ----------------------------------------------------------------

render() {
  sed -e "s|__REPO_DIR__|$REPO_DIR|g" \
      -e "s|__STATE_DIR__|$STATE_DIR|g" \
      -e "s|__RUNTIME_DIR__|$RUNTIME_DIR|g" \
      -e "s|__LOG_DIR__|$LOG_DIR|g" \
      "$1" > "$2"
}

reload() {
  label="$1"; plist="$2"; waited=0
  # bootout first: bootstrap on an already-loaded label is an error, and this
  # script's whole job is to be safe to re-run.
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  # bootout returns before the job has actually gone, so bootstrapping straight
  # after it fails with "service already loaded" and, under set -e, leaves a
  # redeploy half applied -- the old code still running under the new plist.
  while launchctl print "gui/$UID/$label" >/dev/null 2>&1; do
    waited=$((waited + 1))
    [ "$waited" -gt 50 ] && die "$label will not unload. Try: launchctl bootout gui/$UID/$label"
    sleep 0.2
  done
  launchctl bootstrap "gui/$UID" "$plist"
  launchctl enable "gui/$UID/$label"
}

say "installing launchd agents"
render "$REPO_DIR/deploy/macos/sidecar.plist.in" "$AGENTS_DIR/$SIDECAR_LABEL.plist"
render "$REPO_DIR/deploy/macos/bridge.plist.in" "$AGENTS_DIR/$BRIDGE_LABEL.plist"

# The sidecar first, so the bridge's opening cycle has something to talk to.
reload "$SIDECAR_LABEL" "$AGENTS_DIR/$SIDECAR_LABEL.plist"
reload "$BRIDGE_LABEL" "$AGENTS_DIR/$BRIDGE_LABEL.plist"

# -- The shim ---------------------------------------------------------------

mkdir -p "$HOME/.local/bin"
ln -sfn "$REPO_DIR/deploy/macos/vta" "$HOME/.local/bin/vta"

cat <<EOF

Installed.

  code      $REPO_DIR
  state     $STATE_DIR
  config    $ENV_FILE
  logs      $LOG_DIR
  control   vta  (from ~/.local/bin -- add it to PATH if it is not there)

Next:

  vta bootstrap        obtain a Google master token
  \$EDITOR "$ENV_FILE"  fill in the token and the AnyList login
  vta restart
  vta doctor           check both sides, and confirm the AnyList list name
  vta sync --dry-run   rehearse without writing anything

Until credentials are set, both jobs stay up and report why:

  curl -s http://127.0.0.1:8080/healthz
EOF
