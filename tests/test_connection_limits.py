import socket
import socketserver
import threading

import pytest


def load_server_class(surface):
    if surface == "api":
        pytest.importorskip("PyQt6.QtCore")
        from job_manager.api_server import _Server
    else:
        from job_manager.web_monitor import _Server
    return _Server


@pytest.mark.parametrize("surface", ["api", "monitor"])
def test_accepted_socket_has_finite_idle_timeout(surface):
    server_cls = load_server_class(surface)
    server = server_cls(("127.0.0.1", 0), socketserver.BaseRequestHandler)
    client = socket.create_connection(server.server_address, timeout=2)
    accepted = None
    try:
        accepted, _ = server.get_request()
        assert accepted.gettimeout() == 15.0
    finally:
        if accepted is not None:
            accepted.close()
        client.close()
        server.server_close()


@pytest.mark.parametrize("surface", ["api", "monitor"])
def test_connection_limit_and_slot_recovery(surface):
    server_cls = load_server_class(surface)
    started = threading.Event()

    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            started.set()
            if self.request.recv(1):
                self.request.sendall(b"ok")

    class SmallServer(server_cls):
        max_clients = 1
        request_timeout = 0.2

        def handle_error(self, request, client_address):
            pass  # the deliberately stalled first socket times out

    server = SmallServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with socket.create_connection(server.server_address, timeout=2) as first:
            assert started.wait(2)
            with socket.create_connection(server.server_address, timeout=2) as excess:
                assert excess.recv(1) == b""
            assert first.recv(1) == b""
        # The slot is released when the timed-out handler shuts down.
        assert server._client_slots.acquire(timeout=2)
        server._client_slots.release()
        with socket.create_connection(server.server_address, timeout=2) as recovered:
            recovered.sendall(b"x")
            assert recovered.recv(2) == b"ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
