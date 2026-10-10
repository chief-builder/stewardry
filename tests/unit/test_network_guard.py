import socket
import urllib.request

import pytest


def test_socket_connect_is_blocked() -> None:
    with socket.socket() as s, pytest.raises(RuntimeError, match="network access is blocked"):
        s.connect(("192.0.2.1", 443))


def test_name_resolution_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="network access is blocked"):
        socket.getaddrinfo("example.com", 443)


def test_http_client_is_blocked() -> None:
    with pytest.raises(RuntimeError, match="network access is blocked"):
        urllib.request.urlopen("https://example.com", timeout=1)
