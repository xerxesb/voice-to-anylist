"""Alert suppression: loud enough to notice, quiet enough to keep noticing."""

import pytest

from voice_to_anylist.alerts import ActivityNotifier, Alerter


@pytest.fixture
def alerter(monkeypatch):
    """An Alerter with a webhook that records instead of posting."""
    sent = []
    instance = Alerter(webhook_url="https://ntfy.example/topic", repeat_after=3600)
    monkeypatch.setattr(
        "voice_to_anylist.alerts.httpx.post",
        lambda url, **kwargs: sent.append(kwargs["content"].decode()),
    )
    instance.sent = sent
    return instance


def test_repeated_alerts_are_suppressed(alerter):
    for _ in range(5):
        alerter.send("auth", "token expired")

    assert alerter.sent == ["token expired"]


def test_a_cleared_alert_is_reported_again(alerter):
    alerter.send("auth", "token expired")
    alerter.clear("auth")
    alerter.send("auth", "token expired")

    assert alerter.sent == ["token expired"] * 2


def test_distinct_alerts_do_not_suppress_each_other(alerter):
    alerter.send("auth", "token expired")
    alerter.send("guard", "mass delete")

    assert alerter.sent == ["token expired", "mass delete"]


def test_a_failing_webhook_does_not_propagate(alerter, monkeypatch):
    """Losing an alert must never take the sync loop down with it."""
    import httpx

    monkeypatch.setattr(
        "voice_to_anylist.alerts.httpx.post",
        lambda url, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )

    alerter.send("auth", "token expired")  # must not raise


def test_nothing_is_posted_when_no_webhook_is_configured(alerter):
    alerter.webhook_url = ""

    alerter.send("auth", "token expired")

    assert alerter.sent == []


# -- activity, which is a different kind of message ---------------------------


@pytest.fixture
def notifier(monkeypatch):
    posted = []
    instance = ActivityNotifier(webhook_url="https://ntfy.example/activity")
    monkeypatch.setattr(
        "voice_to_anylist.alerts.httpx.post",
        lambda url, **kwargs: posted.append((url, kwargs["content"].decode(), kwargs["headers"])),
    )
    instance.posted = posted
    return instance


def test_an_addition_is_announced(notifier):
    notifier.added(["strawberries"])

    assert len(notifier.posted) == 1
    assert "strawberries" in notifier.posted[0][1]


def test_a_cycle_of_additions_is_one_message_not_several(notifier):
    """A bootstrap or a busy morning should not fan out into ten pings."""
    notifier.added(["milk", "bread", "2 lemons"])

    assert len(notifier.posted) == 1
    body = notifier.posted[0][1]
    assert "milk" in body and "bread" in body and "2 lemons" in body


def test_activity_is_never_suppressed(notifier):
    """Unlike a fault, the same item added twice is two real events."""
    notifier.added(["milk"])
    notifier.added(["milk"])

    assert len(notifier.posted) == 2


def test_nothing_is_posted_for_an_empty_cycle(notifier):
    notifier.added([])

    assert notifier.posted == []


def test_nothing_is_posted_without_a_webhook(notifier):
    notifier.webhook_url = ""

    notifier.added(["milk"])

    assert notifier.posted == []


def test_it_is_marked_low_priority_so_it_does_not_read_as_a_fault(notifier):
    notifier.added(["milk"])

    headers = notifier.posted[0][2]
    assert headers["Priority"] == "low"
    assert headers["Title"] != "voice-to-anylist", "must be distinguishable from an alert"


def test_a_failing_webhook_never_reaches_the_sync_loop(notifier, monkeypatch):
    import httpx

    monkeypatch.setattr(
        "voice_to_anylist.alerts.httpx.post",
        lambda url, **kwargs: (_ for _ in ()).throw(httpx.ConnectError("down")),
    )

    notifier.added(["milk"])  # must not raise
