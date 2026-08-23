"""The long-running bridge: poll, merge, report.

Everything interesting lives in the engine; this is the part that keeps it
running, decides how hard to retry, and exposes enough state that a failure is
visible without reading logs.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from .alerts import Alerter
from .clients.anylist import AnyListClient
from .clients.base import AuthenticationError, ListClientError
from .clients.keep import KeepClient
from .config import Settings
from .engine import GuardConfig, SyncEngine
from .store import ShadowStore

log = logging.getLogger(__name__)

# Backoff bounds for a side that is failing.  The ceiling is well under an
# hour so a transient Google outage recovers on its own overnight.
MAX_BACKOFF_SECONDS = 600.0


@dataclass
class Status:
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_success: datetime | None = None
    last_failure: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    cycles: int = 0
    actions_applied: int = 0
    guard_trips: int = 0
    guard_reason: str | None = None
    keep_items: int = 0
    anylist_items: int = 0

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at.isoformat(),
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_failure": self.last_failure.isoformat() if self.last_failure else None,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "cycles": self.cycles,
            "actions_applied": self.actions_applied,
            "guard_trips": self.guard_trips,
            "guard_reason": self.guard_reason,
            "keep_items": self.keep_items,
            "anylist_items": self.anylist_items,
            # Always present so a caller can test one key rather than two
            # shapes; a configured service simply reports nothing missing.
            "unconfigured": [],
        }


class BridgeService:
    def __init__(
        self,
        settings: Settings,
        *,
        keep=None,
        anylist=None,
        store: ShadowStore | None = None,
    ):
        self.settings = settings
        self.status = Status()
        self.alerter = Alerter(settings.alert_webhook_url)

        self.store = store or ShadowStore(settings.state_path)
        self.keep = keep or KeepClient(
            email=settings.google_email,
            master_token=settings.google_master_token,
            note_title=settings.keep_note_title,
            state_path=settings.keep_state_path,
        )
        self.anylist = anylist or AnyListClient(
            base_url=settings.anylist_api_url,
            list_name=settings.anylist_list,
            token=settings.anylist_api_token,
        )
        self.engine = SyncEngine(
            self.keep,
            self.anylist,
            self.store,
            dry_run=settings.dry_run,
            guard=GuardConfig(
                min_deletes=settings.guard_min_deletes,
                max_ratio=settings.guard_max_ratio,
                empty_side_min_shadow=settings.guard_empty_side_min_shadow,
            ),
        )

    # -- one turn of the loop ----------------------------------------------

    def run_cycle(self) -> None:
        """Run a single sync, recording the result. Never raises."""
        self.status.cycles += 1
        try:
            outcome = self.engine.run_once()
        except AuthenticationError as error:
            self._record_failure(error)
            self.alerter.send("auth", f"voice-to-anylist cannot authenticate: {error}")
            return
        except ListClientError as error:
            self._record_failure(error)
            # A single blip is normal for an unofficial API; only escalate once
            # it has clearly stopped being a blip.
            if self.status.consecutive_failures >= 5:
                self.alerter.send(
                    "sync",
                    f"voice-to-anylist has failed {self.status.consecutive_failures} "
                    f"cycles in a row: {error}",
                )
            return
        except Exception as error:  # noqa: BLE001 - the loop must outlive any bug
            log.exception("unexpected error during sync")
            self._record_failure(error)
            self.alerter.send("bug", f"voice-to-anylist hit an unexpected error: {error}")
            return

        self.status.last_success = datetime.now(UTC)
        self.status.consecutive_failures = 0
        self.status.last_error = None
        self.alerter.clear("auth")
        self.alerter.clear("sync")
        self.alerter.clear("bug")

        if outcome.guard_tripped:
            self.status.guard_trips += 1
            self.status.guard_reason = outcome.guard_reason
            self.alerter.send(
                "guard",
                "voice-to-anylist paused a sync that looked destructive: "
                f"{outcome.guard_reason}. It will proceed if the same change is "
                "still there next cycle.",
            )
        else:
            self.status.guard_reason = None
            self.alerter.clear("guard")

        if outcome.applied:
            self.status.actions_applied += len(outcome.actions)

    def _record_failure(self, error: BaseException) -> None:
        self.status.consecutive_failures += 1
        self.status.last_failure = datetime.now(UTC)
        self.status.last_error = f"{type(error).__name__}: {error}"
        log.warning("sync cycle failed (%d in a row): %s", self.status.consecutive_failures, error)

    def _delay(self) -> float:
        """Poll interval, backing off while a side is unhealthy.

        Jitter keeps a restart loop across several deploys from synchronising
        into a thundering herd against the same unofficial endpoints.
        """
        base = self.settings.poll_interval_seconds
        if self.status.consecutive_failures:
            base = min(base * 2**self.status.consecutive_failures, MAX_BACKOFF_SECONDS)
        return base * random.uniform(0.85, 1.15)

    # -- loop ---------------------------------------------------------------

    async def run_forever(self) -> None:
        log.info(
            'mirroring Keep note "%s" <-> AnyList list "%s" every %.0fs%s',
            self.settings.keep_note_title,
            self.settings.anylist_list,
            self.settings.poll_interval_seconds,
            " (dry run)" if self.settings.dry_run else "",
        )
        while True:
            started = time.monotonic()
            await asyncio.to_thread(self.run_cycle)
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(self._delay() - elapsed, 1.0))

    def close(self) -> None:
        if hasattr(self.anylist, "close"):
            self.anylist.close()
        self.store.close()

    # -- reporting ----------------------------------------------------------

    def healthy(self) -> bool:
        """Healthy means recently successful, not merely alive.

        A container that keeps answering while nothing has synced for an hour
        is exactly the failure this is meant to surface.
        """
        if self.status.last_success is None:
            # Allow for a slow first sync after a cold start.
            return self.status.consecutive_failures < 3
        stale_after = max(self.settings.poll_interval_seconds * 10, 600)
        age = (datetime.now(UTC) - self.status.last_success).total_seconds()
        return age < stale_after


def create_app(service: BridgeService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(service.run_forever())
        try:
            yield
        finally:
            task.cancel()
            service.close()

    app = FastAPI(
        title="voice-to-anylist", docs_url=None, redoc_url=None, lifespan=lifespan
    )

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        ok = service.healthy()
        return JSONResponse(
            {"ok": ok, "last_error": service.status.last_error},
            status_code=200 if ok else 503,
        )

    @app.get("/status")
    def status() -> dict:
        return {
            "config": {
                "keep_note": service.settings.keep_note_title,
                "anylist_list": service.settings.anylist_list,
                "poll_interval_seconds": service.settings.poll_interval_seconds,
                "dry_run": service.settings.dry_run,
            },
            **service.status.as_dict(),
        }

    return app


def create_unconfigured_app(missing: list[str]) -> FastAPI:
    """Serve the reason rather than exiting.

    Missing credentials and a runtime failure want opposite handling.  A
    runtime failure should take the process down so launchd replaces it, but
    missing credentials need a human, and exiting just earns a restart loop
    throttled to ten minutes with the explanation scrolled off the top of the
    log.  Staying up puts it somewhere a curl can read, and keeps the CLI
    usable for the bootstrap that fixes it.
    """
    reason = f"not configured: {', '.join(missing)} must be set"
    app = FastAPI(
        title="voice-to-anylist (unconfigured)", docs_url=None, redoc_url=None
    )

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        return JSONResponse(
            {"ok": False, "unconfigured": missing, "last_error": reason},
            status_code=503,
        )

    @app.get("/status")
    def status() -> dict:
        return {"unconfigured": missing, "last_error": reason}

    return app
