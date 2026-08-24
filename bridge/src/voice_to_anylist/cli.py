"""Command line entry points: bootstrap, doctor, sync, run."""

from __future__ import annotations

import argparse
import json
import logging
import sys

import uvicorn

from . import bootstrap as bootstrap_mod
from .clients.anylist import AnyListClient
from .clients.base import ListClientError
from .clients.keep import KeepClient
from .config import Settings, default_state_dir, load_settings
from .engine import GuardConfig, SyncEngine
from .service import BridgeService, create_app, create_unconfigured_app
from .store import ShadowStore

log = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def cmd_bootstrap(_args: argparse.Namespace, settings: Settings) -> int:
    print(bootstrap_mod.INSTRUCTIONS)
    email = settings.google_email or input("Google account email: ").strip()
    oauth_token = input("oauth_token cookie value: ").strip()
    if not oauth_token.startswith("oauth2_4/"):
        print("That does not look like an oauth_token (expected it to start with "
              "'oauth2_4/').", file=sys.stderr)
        return 1

    android_id = bootstrap_mod.generate_android_id()
    try:
        token = bootstrap_mod.exchange(email, oauth_token, android_id)
    except RuntimeError as error:
        print(f"\n{error}", file=sys.stderr)
        return 1

    print("\nSuccess. Put these in your .env, and keep them secret:\n")
    print(f"  GOOGLE_EMAIL={email}")
    print(f"  GOOGLE_MASTER_TOKEN={token}")
    print(f"\n.env lives at: {default_state_dir() / '.env'}")
    print("Then restart the bridge:  vta restart")
    return 0


def _build_clients(settings: Settings) -> tuple[KeepClient, AnyListClient]:
    keep = KeepClient(
        email=settings.google_email,
        master_token=settings.google_master_token,
        note_title=settings.keep_note_title,
        state_path=settings.keep_state_path,
    )
    anylist = AnyListClient(
        base_url=settings.anylist_api_url,
        list_name=settings.anylist_list,
        token=settings.anylist_api_token,
    )
    return keep, anylist


def cmd_doctor(_args: argparse.Namespace, settings: Settings) -> int:
    """Check every moving part separately, so a failure names its own cause."""
    settings.require_credentials()
    keep, anylist = _build_clients(settings)
    failures = 0

    try:
        names = anylist.lists()
    except ListClientError as error:
        print(f"  FAIL  AnyList: {error}")
        failures += 1
    else:
        print(f"  ok    AnyList: {len(names)} list(s) on the account")
        for name in names:
            # Quoted, so a trailing space in a list name is visible rather than
            # being the thing that mysteriously fails to match.
            marker = "->" if name == settings.anylist_list else "  "
            print(f"          {marker} {name!r}")
        if settings.anylist_list not in names:
            print(
                f"  FAIL  ANYLIST_LIST is {settings.anylist_list!r}, which is not "
                "one of those. Copy the spelling above."
            )
            failures += 1
        else:
            try:
                items = anylist.fetch()
                print(
                    f"  ok    AnyList list {settings.anylist_list!r}: "
                    f"{len(items)} item{'' if len(items) == 1 else 's'}"
                )
            except ListClientError as error:
                print(f"  FAIL  AnyList: {error}")
                failures += 1

    try:
        items = keep.fetch()
        checked = sum(1 for i in items if i.checked)
        print(
            f"  ok    Keep note {settings.keep_note_title!r}: "
            f"{len(items)} item{'' if len(items) == 1 else 's'} ({checked} ticked)"
        )
    except ListClientError as error:
        print(f"  FAIL  Google Keep: {error}")
        failures += 1

    if hasattr(anylist, "close"):
        anylist.close()
    print("\nAll checks passed." if not failures else f"\n{failures} check(s) failed.")
    return 1 if failures else 0


def cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    """Run exactly one cycle and print the plan. The way to rehearse safely."""
    settings.require_credentials()
    dry_run = settings.dry_run or args.dry_run
    keep, anylist = _build_clients(settings)
    store = ShadowStore(settings.state_path)
    engine = SyncEngine(
        keep,
        anylist,
        store,
        dry_run=dry_run,
        allow_empty_note=getattr(args, "allow_empty_note", False),
        guard=GuardConfig(
            min_deletes=settings.guard_min_deletes,
            max_ratio=settings.guard_max_ratio,
            empty_side_min_shadow=settings.guard_empty_side_min_shadow,
        ),
    )

    try:
        outcome = engine.run_once()
    except ListClientError as error:
        print(f"sync failed: {error}", file=sys.stderr)
        return 1
    finally:
        anylist.close()
        store.close()

    if outcome.guard_tripped:
        print(f"guard tripped: {outcome.guard_reason}")
        if "empty" not in (outcome.guard_reason or ""):
            print("Re-run to confirm; the same change twice is treated as deliberate.")
        return 2
    if not outcome.actions:
        print("Already in sync; nothing to do.")
        return 0
    prefix = "would " if dry_run else ""
    for action in outcome.actions:
        print(f"  {prefix}{action.describe()}")
    return 0


def cmd_run(_args: argparse.Namespace, settings: Settings) -> int:
    missing = settings.missing_credentials()
    if missing:
        # Not an error to die of.  A crash loop under launchd gets throttled to
        # a ten-minute retry and scrolls this line out of the log, so serve the
        # reason instead and let a human fix it.
        log.error(
            "not configured: %s must be set in %s. "
            "Run `vta bootstrap` for a Google master token. "
            "Serving /healthz 503 until then.",
            ", ".join(missing),
            default_state_dir() / ".env",
        )
        app = create_unconfigured_app(missing)
    else:
        app = create_app(BridgeService(settings))
    uvicorn.run(app, host=settings.http_host, port=settings.http_port, log_config=None)
    return 0


def cmd_config(_args: argparse.Namespace, settings: Settings) -> int:
    """Show effective settings with secrets redacted."""
    data = settings.model_dump()
    for secret in ("google_master_token", "anylist_api_token"):
        if data.get(secret):
            data[secret] = f"<set, {len(data[secret])} chars>"
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="voice-to-anylist",
        description="Mirror a Google Keep shopping list into AnyList.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bootstrap", help="obtain a Google master token").set_defaults(
        func=cmd_bootstrap
    )
    sub.add_parser("doctor", help="check credentials and that both lists resolve").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("config", help="print effective configuration").set_defaults(func=cmd_config)

    sync = sub.add_parser("sync", help="run a single cycle")
    sync.add_argument("--dry-run", action="store_true", help="plan without writing")
    sync.add_argument(
        "--allow-empty-note",
        action="store_true",
        help=(
            "accept an empty Keep note as deliberate, marking every active item "
            "purchased. The service never does this on its own"
        ),
    )
    sync.set_defaults(func=cmd_sync)

    sub.add_parser("run", help="run the bridge continuously").set_defaults(func=cmd_run)

    args = parser.parse_args(argv)
    settings = load_settings()
    _configure_logging(settings.log_level)
    return args.func(args, settings)


if __name__ == "__main__":
    raise SystemExit(main())
