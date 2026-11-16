import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from voice_to_anylist.clients.base import FakeListClient, ListItem
from voice_to_anylist.engine import SyncEngine
from voice_to_anylist.store import ShadowStore


@pytest.fixture
def store():
    s = ShadowStore(":memory:")
    yield s
    s.close()


@pytest.fixture
def keep():
    return FakeListClient("keep")


@pytest.fixture
def anylist():
    return FakeListClient("anylist")


@pytest.fixture
def engine(keep, anylist, store):
    return SyncEngine(keep, anylist, store)


def item(item_id, name, quantity=None, checked=False):
    return ListItem(id=item_id, name=name, quantity=quantity, checked=checked)


@pytest.fixture
def make_item():
    return item


@pytest.fixture
def settled(keep, anylist, engine):
    """A mirror that has already synced, so the shadow is populated.

    Most interesting behaviour only shows up on the *second* cycle, once the
    engine has a baseline to compare against.
    """

    def _settle(keep_items, anylist_items):
        keep.replace_items(keep_items)
        anylist.replace_items(anylist_items)
        engine.run_once()
        keep.calls.clear()
        anylist.calls.clear()

    return _settle


# -- the Node sidecar, running against an in-memory stub of AnyList ----------

REPO_ROOT = Path(__file__).resolve().parents[2]
STUB_SERVER = REPO_ROOT / "anylist-api" / "test" / "serve-stub.js"



def _require_node() -> str:
    """The node interpreter, or skip -- except in CI, where skipping is a lie.

    A `pytestmark` in a conftest does not apply to other modules, so the
    previous guard here never actually skipped anything; the tests errored
    instead.  Doing it in the fixture puts the check where the dependency is.
    """
    node = shutil.which("node")
    if node and (REPO_ROOT / "anylist-api" / "node_modules").exists():
        return node
    reason = "needs node and `npm install --prefix anylist-api`"
    # Silently skipping the Python/Node seam in CI defeats the point of having
    # a test for it: that seam is the likeliest place for a silent mismatch.
    if os.environ.get("CI"):
        pytest.fail(reason)
    pytest.skip(reason)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _start_sidecar(port: int, **env) -> subprocess.Popen:
    # Resolve node against the caller's PATH and pass it absolutely.  A
    # hardcoded PATH here is resolved *instead of* the caller's, so any
    # interpreter outside /usr/bin -- Homebrew on Apple Silicon, or the pinned
    # runtime the macOS install script lays down -- fails the whole module.
    return subprocess.Popen(
        [_require_node(), str(STUB_SERVER)],
        env={"PATH": os.environ.get("PATH", ""), "ANYLIST_API_PORT": str(port), **env},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


@pytest.fixture(scope="module")
def unconfigured_sidecar():
    """The sidecar as it starts on a freshly installed host: no credentials.

    It has to stay up and say why.  Exiting earns a launchd restart loop
    throttled to ten minutes, with the explanation scrolled out of the log.
    """
    port = _free_port()
    process = _start_sidecar(port, ANYLIST_EMAIL="", ANYLIST_PASSWORD="")
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = (process.stdout.read() or b"").decode()
                pytest.fail(f"sidecar exited instead of staying up:\n{output}")
            try:
                httpx.get(f"{base_url}/health", timeout=1)
                break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("unconfigured sidecar never answered")
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=10)


def _serve_healthy(**env):
    """Run a configured stub sidecar until the test module is done with it."""
    port = _free_port()
    process = _start_sidecar(
        port, ANYLIST_EMAIL="test@example.com", ANYLIST_PASSWORD="test", **env
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = (process.stdout.read() or b"").decode()
                pytest.fail(f"sidecar exited early:\n{output}")
            try:
                if httpx.get(f"{base_url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            pytest.fail("sidecar did not become healthy")
        yield base_url
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture(scope="module")
def sidecar():
    yield from _serve_healthy()


@pytest.fixture(scope="module")
def limited_sidecar():
    """The sidecar with its production download limits.

    Reset before each test through the stub's control routes, so each test
    starts with nothing downloaded and the whole budget unspent.
    """
    yield from _serve_healthy()


@pytest.fixture(scope="module")
def eager_sidecar():
    """No floor between downloads, so a push is visible on the very next read.

    The disconnected backstop is zero too, so a dropped websocket shows on the
    next read instead of five minutes later.
    """
    yield from _serve_healthy(
        ANYLIST_MIN_REFRESH_SECONDS="0", ANYLIST_DISCONNECTED_MAX_AGE_SECONDS="0"
    )


