"""Outbound notifications for the failures a human has to fix.

The master token dying is the expected long-run failure mode of this bridge,
and the symptom -- items quietly stopping -- is one nobody notices for a week.
So it gets said out loud.
"""

from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)

# Long enough that a persistent fault does not become a stream of pages, short
# enough that it is still nagging by the next shop.
REPEAT_AFTER_SECONDS = 3600.0


class Alerter:
    def __init__(self, webhook_url: str = "", repeat_after: float = REPEAT_AFTER_SECONDS):
        self.webhook_url = webhook_url
        self.repeat_after = repeat_after
        self._last_sent: dict[str, float] = {}

    def send(self, key: str, message: str) -> None:
        """Raise an alert, suppressing repeats of the same one for a while."""
        now = time.monotonic()
        previous = self._last_sent.get(key)
        if previous is not None and now - previous < self.repeat_after:
            return
        self._last_sent[key] = now

        log.error("ALERT [%s] %s", key, message)
        if not self.webhook_url:
            return
        try:
            httpx.post(
                self.webhook_url,
                content=message.encode("utf-8"),
                headers={"Title": "voice-to-anylist", "Content-Type": "text/plain"},
                timeout=10.0,
            )
        except httpx.HTTPError as error:
            log.warning("could not deliver alert: %s", error)

    def clear(self, key: str) -> None:
        """Forget a fault so its recovery-and-relapse is reported promptly."""
        self._last_sent.pop(key, None)
