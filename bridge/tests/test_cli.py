"""CLI behaviour that the runbook depends on.

Two things matter here.  A host with no credentials yet must not look like
broken software, and `doctor` must make the AnyList list name checkable rather
than guessable -- pointing at the wrong list is what turns a first sync into a
proposal to delete everything.
"""

import pytest

from voice_to_anylist import cli
from voice_to_anylist.clients.base import ListClientError, ListItem
from voice_to_anylist.config import Settings


class FakeAnyList:
    name = "anylist"

    def __init__(self, lists=("Grocery",), items=(), error=None):
        self._lists = list(lists)
        self._items = list(items)
        self._error = error
        self.closed = False

    def lists(self):
        if self._error:
            raise self._error
        return list(self._lists)

    def fetch(self):
        if self._error:
            raise self._error
        return list(self._items)

    def close(self):
        self.closed = True


class FakeKeep:
    name = "keep"

    def __init__(self, items=()):
        self._items = list(items)

    def fetch(self):
        return list(self._items)


@pytest.fixture
def configured():
    return Settings(google_email="x@example.com", google_master_token="aas_et/fake")


# -- an unconfigured host stays up -------------------------------------------


def test_run_serves_the_reason_instead_of_exiting_when_unconfigured(monkeypatch):
    """launchd would throttle a crash loop to ten minutes and bury the reason."""
    served = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))

    exit_code = cli.cmd_run(None, Settings(google_email="", google_master_token=""))

    assert exit_code == 0
    routes = {r.path: r.endpoint for r in served["app"].routes if hasattr(r, "endpoint")}
    response = routes["/healthz"]()
    assert response.status_code == 503
    body = response.body.decode()
    assert "GOOGLE_EMAIL" in body
    assert "GOOGLE_MASTER_TOKEN" in body


def test_the_unconfigured_status_endpoint_names_what_is_missing(monkeypatch):
    served = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))

    cli.cmd_run(None, Settings(google_email="x@example.com", google_master_token=""))

    routes = {r.path: r.endpoint for r in served["app"].routes if hasattr(r, "endpoint")}
    assert routes["/status"]()["unconfigured"] == ["GOOGLE_MASTER_TOKEN"]


def test_run_starts_the_real_service_once_configured(monkeypatch, configured):
    served = {}
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: served.update(app=app, **kw))
    monkeypatch.setattr(cli, "BridgeService", lambda settings: object())
    monkeypatch.setattr(cli, "create_app", lambda service: "real-app")

    cli.cmd_run(None, configured)

    assert served["app"] == "real-app"
    assert served["host"] == "127.0.0.1"


# -- doctor names every list -------------------------------------------------


def _doctor(monkeypatch, settings, anylist, keep=None):
    monkeypatch.setattr(cli, "_build_clients", lambda s: (keep or FakeKeep(), anylist))
    return cli.cmd_doctor(None, settings)


def test_doctor_prints_every_list_so_the_spelling_can_be_copied(
    monkeypatch, capsys, configured
):
    anylist = FakeAnyList(lists=["Costco", "Grocery", "Hardware"])

    _doctor(monkeypatch, configured, anylist)

    out = capsys.readouterr().out
    assert "Costco" in out
    assert "Grocery" in out
    assert "Hardware" in out


def test_doctor_marks_the_configured_list(monkeypatch, capsys, configured):
    _doctor(monkeypatch, configured, FakeAnyList(lists=["Costco", "Grocery"]))

    marked = [line for line in capsys.readouterr().out.splitlines() if "->" in line]
    assert len(marked) == 1
    assert "Grocery" in marked[0]


def test_doctor_fails_when_the_configured_list_is_not_on_the_account(
    monkeypatch, capsys, configured
):
    """The wrong list name is the mistake that makes a dry run propose deletions."""
    anylist = FakeAnyList(lists=["Costco", "Hardware"])

    exit_code = _doctor(monkeypatch, configured, anylist)

    assert exit_code == 1
    assert "Grocery" in capsys.readouterr().out


def test_doctor_quotes_names_so_stray_whitespace_is_visible(
    monkeypatch, capsys, configured
):
    _doctor(monkeypatch, configured, FakeAnyList(lists=["Grocery ", "Grocery"]))

    assert "'Grocery '" in capsys.readouterr().out


def test_doctor_reports_an_unreachable_sidecar_once(monkeypatch, capsys, configured):
    anylist = FakeAnyList(error=ListClientError("AnyList sidecar unreachable"))

    exit_code = _doctor(monkeypatch, configured, anylist)

    assert exit_code == 1
    assert "sidecar unreachable" in capsys.readouterr().out


def test_doctor_still_reports_item_counts(monkeypatch, capsys, configured):
    anylist = FakeAnyList(lists=["Grocery"], items=[ListItem(id="a1", name="milk")])
    keep = FakeKeep(
        items=[
            ListItem(id="k1", name="milk"),
            ListItem(id="k2", name="eggs", checked=True),
        ]
    )

    exit_code = _doctor(monkeypatch, configured, anylist, keep)

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "1 item" in out
    assert "2 items (1 ticked)" in out
