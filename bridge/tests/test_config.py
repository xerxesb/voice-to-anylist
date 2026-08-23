"""Configuration: what counts as missing, and where state lands by default.

The defaults used to be container paths under /data.  Running natively, a
wrong default is silent -- the service starts and writes its shadow somewhere
nobody looks -- so they are pinned here.
"""

import sys

import pytest

from voice_to_anylist.config import Settings, default_state_dir


def test_both_google_settings_are_reported_when_neither_is_set():
    settings = Settings(google_email="", google_master_token="")

    assert settings.missing_credentials() == ["GOOGLE_EMAIL", "GOOGLE_MASTER_TOKEN"]


def test_only_the_absent_one_is_reported():
    settings = Settings(google_email="x@example.com", google_master_token="")

    assert settings.missing_credentials() == ["GOOGLE_MASTER_TOKEN"]


def test_nothing_is_missing_once_both_are_set():
    settings = Settings(google_email="x@example.com", google_master_token="aas_et/fake")

    assert settings.missing_credentials() == []


def test_require_credentials_still_exits_naming_what_is_missing():
    settings = Settings(google_email="", google_master_token="")

    with pytest.raises(SystemExit) as raised:
        settings.require_credentials()

    assert "GOOGLE_EMAIL" in str(raised.value)
    assert "GOOGLE_MASTER_TOKEN" in str(raised.value)


def test_require_credentials_passes_when_configured():
    Settings(
        google_email="x@example.com", google_master_token="aas_et/fake"
    ).require_credentials()


# -- where state lives -------------------------------------------------------


def test_the_state_directory_is_overridable(monkeypatch, tmp_path):
    monkeypatch.setenv("VTA_STATE_DIR", str(tmp_path))

    assert default_state_dir() == tmp_path


def test_state_paths_default_underneath_the_state_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("VTA_STATE_DIR", str(tmp_path))

    settings = Settings()

    assert settings.state_path == str(tmp_path / "state.sqlite")
    assert settings.keep_state_path == str(tmp_path / "keep_state.json")


def test_an_explicit_state_path_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("VTA_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", "/somewhere/else.sqlite")

    assert Settings().state_path == "/somewhere/else.sqlite"


def test_nothing_defaults_into_the_container_only_data_directory(monkeypatch):
    monkeypatch.delenv("VTA_STATE_DIR", raising=False)

    settings = Settings()

    assert not settings.state_path.startswith("/data")
    assert not settings.keep_state_path.startswith("/data")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS layout")
def test_macos_uses_application_support(monkeypatch):
    monkeypatch.delenv("VTA_STATE_DIR", raising=False)

    assert default_state_dir().parts[-3:] == (
        "Library",
        "Application Support",
        "voice-to-anylist",
    )


@pytest.mark.skipif(sys.platform == "darwin", reason="non-macOS layout")
def test_elsewhere_honours_xdg_state_home(monkeypatch, tmp_path):
    monkeypatch.delenv("VTA_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert default_state_dir() == tmp_path / "voice-to-anylist"


# -- the health endpoint binds to loopback -----------------------------------


def test_the_http_endpoint_binds_loopback_by_default():
    """It reports state and needs no audience beyond the host it runs on."""
    assert Settings().http_host == "127.0.0.1"
