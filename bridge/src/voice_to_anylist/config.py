"""Runtime configuration, all of it environment-driven for container deploys."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    state_path: str = "/data/state.sqlite"
    keep_state_path: str = "/data/keep_state.json"

    # -- Guard rails --------------------------------------------------------
    guard_min_deletes: int = 5
    guard_max_ratio: float = 0.5
    guard_empty_side_min_shadow: int = 3

    # -- Operations ---------------------------------------------------------
    http_host: str = "0.0.0.0"  # noqa: S104 - the health endpoint is the container's probe
    http_port: int = 8080
    alert_webhook_url: str = Field(
        default="",
        description="POSTed a plain-text body when auth fails or the guard trips",
    )
    log_level: str = "INFO"

    def require_credentials(self) -> None:
        missing = [
            name
            for name, value in (
                ("GOOGLE_EMAIL", self.google_email),
                ("GOOGLE_MASTER_TOKEN", self.google_master_token),
            )
            if not value
        ]
        if missing:
            raise SystemExit(
                f"Missing required configuration: {', '.join(missing)}.\n"
                "Run `voice-to-anylist bootstrap` to obtain a Google master token."
            )


def load_settings() -> Settings:
    return Settings()
