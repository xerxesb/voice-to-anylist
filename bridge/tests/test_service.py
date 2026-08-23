"""How the bridge behaves when a side misbehaves.

The loop has to outlive every failure an unofficial API can produce, while
still making a genuine outage visible.
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from voice_to_anylist.clients.base import AuthenticationError, ListClientError, ListItem
from voice_to_anylist.config import Settings
from voice_to_anylist.service import BridgeService


@pytest.fixture
def settings():
    return Settings(
        google_email="x@example.com",
        google_master_token="aas_et/fake",
        poll_interval_seconds=20.0,
        alert_webhook_url="",
        dry_run=False,
    )


@pytest.fixture
def service(settings, keep, anylist, store):
    svc = BridgeService(settings, keep=keep, anylist=anylist, store=store)
    sent = []
    svc.alerter.send = lambda key, message: sent.append((key, message))  # type: ignore[method-assign]
    svc.alerts = sent
    return svc


def item(item_id, name, checked=False):
    return ListItem(id=item_id, name=name, checked=checked)


def test_a_successful_cycle_records_progress(service, keep):
    keep.replace_items([item("k1", "milk")])

    service.run_cycle()

    assert service.status.cycles == 1
    assert service.status.last_success is not None
    assert service.status.consecutive_failures == 0
    assert service.healthy()


def test_an_auth_failure_alerts_immediately(service, keep):
    keep.fail_on_fetch = AuthenticationError("token revoked")

    service.run_cycle()

    assert service.status.consecutive_failures == 1
    assert [key for key, _ in service.alerts] == ["auth"]


def test_a_single_blip_does_not_alert(service, keep):
    """Unofficial APIs hiccup; one failure is not news."""
    keep.fail_on_fetch = ListClientError("Keep sync failed")

    service.run_cycle()

    assert service.alerts == []
    assert service.status.consecutive_failures == 1


def test_a_persistent_outage_eventually_alerts(service, keep):
    keep.fail_on_fetch = ListClientError("Keep sync failed")

    for _ in range(5):
        service.run_cycle()

    assert [key for key, _ in service.alerts] == ["sync"]


def test_an_unexpected_bug_is_caught_so_the_loop_survives(service, keep):
    keep.fail_on_fetch = ZeroDivisionError("bug in the engine")

    service.run_cycle()  # must not raise

    assert service.status.consecutive_failures == 1
    assert [key for key, _ in service.alerts] == ["bug"]


def test_recovery_clears_the_failure_count(service, keep):
    keep.fail_on_fetch = ListClientError("down")
    service.run_cycle()
    keep.fail_on_fetch = None

    service.run_cycle()

    assert service.status.consecutive_failures == 0
    assert service.status.last_error is None


def test_backoff_grows_while_failing_and_resets_on_success(service, keep):
    baseline = service._delay()
    keep.fail_on_fetch = ListClientError("down")

    service.run_cycle()
    backed_off = service._delay()

    keep.fail_on_fetch = None
    service.run_cycle()

    assert backed_off > baseline
    assert service._delay() < backed_off


def test_a_guard_trip_is_alerted_and_counted(service, keep, anylist):
    names = ["milk", "bread", "eggs", "jam"]
    keep.replace_items([item(f"k{i}", n) for i, n in enumerate(names)])
    anylist.replace_items([item(f"a{i}", n) for i, n in enumerate(names)])
    service.run_cycle()

    keep.replace_items([])
    service.run_cycle()

    assert service.status.guard_trips == 1
    assert [key for key, _ in service.alerts] == ["guard"]
    assert len(anylist.fetch()) == 4


def test_health_goes_bad_when_syncing_has_silently_stopped(service, keep):
    """A container that answers while nothing syncs is the failure to catch."""
    keep.replace_items([item("k1", "milk")])
    service.run_cycle()
    assert service.healthy()

    service.status.last_success = datetime.now(UTC) - timedelta(hours=2)

    assert not service.healthy()


def test_health_tolerates_a_slow_first_cycle(service):
    assert service.healthy()


def test_a_cycle_can_run_on_a_worker_thread(service, keep):
    """run_forever dispatches each cycle with asyncio.to_thread.

    The shadow store's SQLite connection therefore outlives the thread that
    opened it, and would refuse to be used from another one unless it is
    explicitly built to allow it.
    """
    keep.replace_items([item("k1", "milk")])

    asyncio.run(asyncio.to_thread(service.run_cycle))

    assert service.status.consecutive_failures == 0, service.status.last_error
    assert service.status.last_success is not None


def test_consecutive_cycles_on_different_threads_stay_consistent(service, keep):
    keep.replace_items([item("k1", "milk")])

    async def two_cycles():
        await asyncio.to_thread(service.run_cycle)
        await asyncio.to_thread(service.run_cycle)

    asyncio.run(two_cycles())

    assert service.status.consecutive_failures == 0, service.status.last_error
    assert service.status.cycles == 2


def test_status_and_health_endpoints_report_the_service(service, keep):
    """The operator-facing surface: is it working, and if not, why."""
    from voice_to_anylist.service import create_app

    keep.replace_items([item("k1", "milk")])
    service.run_cycle()

    routes = {r.path: r.endpoint for r in create_app(service).routes if hasattr(r, "endpoint")}

    assert routes["/healthz"]().status_code == 200

    status = routes["/status"]()
    assert status["config"]["anylist_list"] == "Grocery"
    assert status["cycles"] == 1
    assert status["last_success"] is not None


def test_health_endpoint_reports_503_once_syncing_has_stopped(service):
    from voice_to_anylist.service import create_app

    service.status.last_success = datetime.now(UTC) - timedelta(hours=2)
    routes = {r.path: r.endpoint for r in create_app(service).routes if hasattr(r, "endpoint")}

    assert routes["/healthz"]().status_code == 503


def test_a_configured_service_reports_nothing_missing_on_status(service, keep):
    """The key is always present, so a caller tests one shape rather than two."""
    from voice_to_anylist.service import create_app

    routes = {r.path: r.endpoint for r in create_app(service).routes if hasattr(r, "endpoint")}

    assert routes["/status"]()["unconfigured"] == []
