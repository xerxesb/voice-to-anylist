"""Runtime configuration, all of it environment-driven."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

STATE_DIR_ENV = "VTA_STATE_DIR"


def default_state_dir() -> Path:
    """Where the shadow state and cached credentials live.

    Deliberately a real per-user location rather than the old ``/data``, which
    existed only inside the container image.  Running natively, a path that
    does not exist is not an error -- SQLite happily creates one -- so a stale
    default would silently strand the shadow somewhere nobody looks.
    """
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "voice-to-anylist"
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return Path(xdg) / "voice-to-anylist"
    return Path.home() / ".local" / "state" / "voice-to-anylist"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # -- Google Keep --------------------------------------------------------
    google_email: str = Field(default="", description="Account whose Keep note is mirrored")
    google_master_token: str = Field(
        default="", description="Long-lived token from `voice-to-anylist bootstrap`"
    )
    keep_note_title: str = Field(
        default="Shopping list",
        description="Title of the Keep note Assistant writes into. Must have checkboxes on.",
    )

    # -- AnyList ------------------------------------------------------------
    anylist_api_url: str = "http://127.0.0.1:3000"
    anylist_api_token: str = ""
    anylist_list: str = Field(default="Grocery", description="AnyList list to mirror")

    # -- Behaviour ----------------------------------------------------------
    poll_interval_seconds: float = 20.0
    dry_run: bool = False
    state_path: str = Field(default_factory=lambda: str(default_state_dir() / "state.sqlite"))
    keep_state_path: str = Field(
        default_factory=lambda: str(default_state_dir() / "keep_state.json")
    )

    # -- Guard rails --------------------------------------------------------
    guard_min_deletes: int = 5
    guard_max_ratio: float = 0.5
    guard_empty_side_min_shadow: int = 3

    # -- Operations ---------------------------------------------------------
    # Loopback: this reports state, and has no audience beyond the host it runs
    # on.  Binding every interface would publish it to the household network.
    http_host: str = "127.0.0.1"
    http_port: int = 8080
    alert_webhook_url: str = Field(
        default="",
        description="POSTed a plain-text body when auth fails or the guard trips",
    )
    log_level: str = "INFO"

    def missing_credentials(self) -> list[str]:
        """The settings the bridge cannot run without, by environment name."""
        return [
            name
            for name, value in (
                ("GOOGLE_EMAIL", self.google_email),
                ("GOOGLE_MASTER_TOKEN", self.google_master_token),
            )
            if not value
        ]

    def require_credentials(self) -> None:
        missing = self.missing_credentials()
        if missing:
            raise SystemExit(
                f"Missing required configuration: {', '.join(missing)}.\n"
                f"Set them in {default_state_dir() / '.env'}.\n"
                "Run `vta bootstrap` to obtain a Google master token."
            )


def load_settings() -> Settings:
    return Settings()
