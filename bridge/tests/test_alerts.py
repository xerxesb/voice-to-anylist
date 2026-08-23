"""Alert suppression: loud enough to notice, quiet enough to keep noticing."""

import pytest

from voice_to_anylist.alerts import Alerter


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
