#!/usr/bin/env bash
#
# Remove the launchd agents and the pinned runtimes.
#
# State and credentials are left alone by default -- losing the shadow is safe
# but noisy, and losing the master token means another bootstrap.  Pass
# --purge to remove those too.

set -euo pipefail

LABEL_PREFIX="com.xerxesb.voice-to-anylist"
STATE_DIR="${VTA_STATE_DIR:-$HOME/Library/Application Support/voice-to-anylist}"
AGENTS_DIR="$HOME/Library/LaunchAgents"

for label in "${LABEL_PREFIX}.bridge" "${LABEL_PREFIX}.sidecar"; do
  launchctl bootout "gui/$UID/$label" 2>/dev/null || true
  rm -f "$AGENTS_DIR/$label.plist"
  printf 'removed %s\n' "$label"
done

rm -f "$HOME/.local/bin/vta"
rm -rf "$STATE_DIR/runtime"
printf 'removed the pinned runtimes\n'

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$STATE_DIR"
  printf 'removed %s, credentials and all\n' "$STATE_DIR"
else
  printf 'kept %s -- pass --purge to remove state and credentials too\n' "$STATE_DIR"
fi
