"""Outbound notifications.

Two kinds, deliberately kept apart.

*Alerts* are failures a human has to fix.  The master token dying is the
expected long-run failure mode, and the symptom -- items quietly stopping -- is
one nobody notices for a week, so it gets said out loud and repeated.

*Activity* is the bridge working: somebody asked for something and it landed.
It goes to its own webhook, at low priority.  Routing it into the alert channel
would train the reader to ignore that channel, and the one message that matters
is the one that arrives a month later at 2am.
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

    def _post(self, message: str, *, title: str, priority: str | None = None) -> None:
        headers = {"Title": title, "Content-Type": "text/plain"}
        if priority:
            headers["Priority"] = priority
        try:
            httpx.post(
                self.webhook_url,
                content=message.encode("utf-8"),
                headers=headers,
                timeout=10.0,
            )
        except httpx.HTTPError as error:
            # Losing a notification must never take the sync loop down with it.
            log.warning("could not deliver notification: %s", error)

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
        self._post(message, title="voice-to-anylist")

    def clear(self, key: str) -> None:
        """Forget a fault so its recovery-and-relapse is reported promptly."""
        self._last_sent.pop(key, None)


class ActivityNotifier(Alerter):
    """Says when something new reaches the shopping list.

    Inherits the posting and the swallow-every-error behaviour, but none of the
    suppression: the same item asked for twice in an afternoon is two real
    events, not a repeat of one fault.
    """

    TITLE = "Shopping list"

    def added(self, names: list[str]) -> None:
        """Announce one cycle's additions as a single message."""
        if not names:
            return
        log.info("added to the list: %s", ", ".join(names))
        if not self.webhook_url:
            return
        body = (
            f"Added: {names[0]}"
            if len(names) == 1
            else "Added {}:\n{}".format(
                f"{len(names)} items", "\n".join(f"\u2022 {n}" for n in names)
            )
        )
        self._post(body, title=self.TITLE, priority="low")
