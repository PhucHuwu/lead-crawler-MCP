"""The suite-wide ban on real outbound network calls.

``respx`` already fails a mocked test that requests an unregistered URL, but it
only covers requests that go *through* a mocked router. A test that reached the
network outside a respx context — a future edit that called ``crawl()`` directly,
say — would go straight to the live Apollo API, spend real credits and pull real
contact data into an assertion.

The autouse fixture in :mod:`tests.conftest` closes that hole by blocking the
socket layer. These tests prove it is actually closed: a guard that has never
been seen to fire is indistinguishable from one that does not work.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from tests.conftest import NetworkAccessAttempted


class TestNetworkIsForbidden:
    def test_dns_resolution_is_blocked(self) -> None:
        with pytest.raises(NetworkAccessAttempted, match="DNS lookup"):
            socket.getaddrinfo("api.apollo.io", 443)

    def test_opening_a_socket_is_blocked(self) -> None:
        with pytest.raises(NetworkAccessAttempted, match="connection to"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.connect(("127.0.0.1", 9))
            finally:
                sock.close()

    def test_the_high_level_helper_is_blocked(self) -> None:
        with pytest.raises(NetworkAccessAttempted, match="connection to"):
            socket.create_connection(("api.apollo.io", 443), timeout=1)

    def test_an_unmocked_httpx_request_cannot_escape(self) -> None:
        # The case the ban exists for: a genuine client call with no respx
        # router in scope. Without the guard this would reach the real host.
        with pytest.raises((NetworkAccessAttempted, httpx.ConnectError)):
            httpx.get("https://api.apollo.io/v1/contacts/search", timeout=1)

    def test_the_failure_names_the_remedy(self) -> None:
        # The message is the whole value of the guard: it has to say what to do,
        # not merely that something went wrong.
        with pytest.raises(NetworkAccessAttempted) as caught:
            socket.getaddrinfo("api.apollo.io", 443)
        assert "respx" in str(caught.value)
        assert "allow_network" in str(caught.value)


@pytest.mark.allow_network
class TestOptOut:
    """``allow_network`` lifts the ban, so a test needing a real socket can run."""

    def test_dns_resolution_is_permitted(self) -> None:
        # Resolving a name leaves the machine, so this is deliberately not a
        # connection: the opt-out is asserted without depending on any host
        # being reachable or on the network being up. ``getaddrinfo`` raises
        # ``gaierror`` when offline, which is still proof it was reached.
        try:
            socket.getaddrinfo("localhost", 80)
        except socket.gaierror:
            pytest.skip("no resolver available")

    def test_a_loopback_connection_is_permitted(self) -> None:
        # Nothing is listening on this port; the point is that the attempt gets
        # as far as a refusal from the OS rather than being blocked by us.
        with pytest.raises(OSError) as caught:
            socket.create_connection(("127.0.0.1", 9), timeout=1)
        assert not isinstance(caught.value, NetworkAccessAttempted)
