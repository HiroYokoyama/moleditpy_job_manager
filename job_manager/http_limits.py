"""Bound resource usage before a local HTTP client is authenticated."""

import threading


class BoundedServerMixin:
    # A loopback client can stall before authentication by sending no headers
    # or an incomplete body. Bound both idle time and concurrent handler threads.
    request_timeout = 15.0
    max_clients = 32

    def __init__(self, *args, **kwargs):
        self._client_slots = threading.BoundedSemaphore(self.max_clients)
        super().__init__(*args, **kwargs)

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(self.request_timeout)
        return request, address

    def process_request(self, request, client_address):
        if not self._client_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._client_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._client_slots.release()
