"""Logs how long each HTTP request takes as the server sees it."""

import logging
import time
from urllib.parse import quote

logger = logging.getLogger("pii-shield")

_QUIET_PATHS = frozenset(("/health",))  # probes hit it every few seconds


class RequestTimingMiddleware:
    """Pure ASGI middleware: logs ``<METHOD> <path> -> <status> in <ms> ms`` for every request it sees.

    The time runs from the moment the app is called until the last byte of the response was handed to the server,
    so it is a little more than the ``total`` of the ``Server-Timing`` header, which ends when the handler is done
    and the response body is built. Errors are logged too (a request that raises before it answers counts as the
    500 the client gets). It only logs: ``app.timing`` owns the ``Server-Timing`` header.

    Register it before ``FastAPIInstrumentor.instrument_app``: the instrumentation then wraps it, the line is
    logged inside the request's server span and carries its trace id.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500  # what the client gets if the app raises before it answers

        async def send_noting_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_noting_status)
        finally:
            if scope["path"] not in _QUIET_PATHS:
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                # The path is the decoded one: quote it so that a request cannot forge a log line with %0a.
                logger.info("%s %s -> %d in %.1f ms", scope["method"], quote(scope["path"]), status, elapsed_ms)
