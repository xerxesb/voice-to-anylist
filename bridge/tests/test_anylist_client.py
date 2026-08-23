"""The AnyList client, exercised against a stubbed sidecar."""

import json

import httpx
import pytest

from voice_to_anylist.clients.anylist import AnyListClient
from voice_to_anylist.clients.base import AuthenticationError, ListClientError


def client_with(handler) -> AnyListClient:
    client = AnyListClient("http://sidecar", "Grocery", token="secret")
    client._http = httpx.Client(
        base_url="http://sidecar",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer secret"},
    )
    return client


def test_fetch_maps_items_and_normalises_missing_quantity():
    def handler(request):
        assert request.url.params["list"] == "Grocery"
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "a1", "name": "milk", "quantity": "2", "checked": False},
                    {"id": "a2", "name": "bread", "quantity": None, "checked": True},
                ]
            },
        )

    items = client_with(handler).fetch()

    assert [i.id for i in items] == ["a1", "a2"]
    assert items[0].quantity == "2"
    assert items[1].quantity is None and items[1].checked is True


def test_fetch_skips_items_with_no_usable_name():
    """An item with no identity cannot be matched, so it is left alone."""

    def handler(request):
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "a1", "name": "   ", "quantity": None, "checked": False},
                    {"id": "a2", "name": "milk", "quantity": None, "checked": False},
                ]
            },
        )

    assert [i.id for i in client_with(handler).fetch()] == ["a2"]


def test_add_sends_the_list_name_and_returns_the_new_id():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"id": "new-id"})

    assert client_with(handler).add("lemons", "2", False) == "new-id"
    assert seen["body"] == {
        "list": "Grocery",
        "name": "lemons",
        "quantity": "2",
        "checked": False,
    }


@pytest.mark.parametrize(
    ("kind", "method", "args"),
    [
        ("set_checked", "PATCH", ("a1", True)),
        ("set_quantity", "PATCH", ("a1", "3")),
        ("remove", "DELETE", ("a1",)),
    ],
)
def test_mutations_target_the_item_endpoint(kind, method, args):
    seen = {}

    def handler(request):
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"id": "a1", "name": "x", "quantity": None,
                                         "checked": False})

    getattr(client_with(handler), kind)(*args)

    assert seen["method"] == method
    assert seen["path"] == "/items/a1"


def test_unauthorised_is_reported_as_an_auth_failure():
    def handler(request):
        return httpx.Response(401, json={"error": "unauthorized"})

    with pytest.raises(AuthenticationError):
        client_with(handler).fetch()


def test_a_missing_list_is_a_client_error_not_a_crash():
    def handler(request):
        return httpx.Response(404, json={"error": 'No AnyList list named "Grocery"'})

    with pytest.raises(ListClientError, match="Grocery"):
        client_with(handler).fetch()


def test_an_unreachable_sidecar_is_a_client_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ListClientError, match="unreachable"):
        client_with(handler).fetch()
