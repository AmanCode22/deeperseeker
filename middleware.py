"""Core-rails middleware for deeperseeker (Stage 0.1 + 0.2).

Three pure-ASGI middlewares, kept dependency-light (stdlib + starlette types
only) so they are trivially unit-testable without FastAPI or a running server:

- RealIPMiddleware     resolve the real client IP behind reverse proxies
                       (fail-closed X-Forwarded-For handling, TRUSTED_PROXIES)
- RequestIDMiddleware  per-request correlation id + access log line
- RecovererMiddleware  last-resort exception barrier: an unhandled error in any
                       handler becomes a logged JSON 500 instead of a raw crash

Registration order in app.py matters: with Starlette, the LAST middleware
registered via add_middleware() runs FIRST (outermost). Register as
RealIP -> RequestID -> Recoverer so the execution order on a request is
Recoverer -> RequestID -> RealIP -> (other middleware) -> routes, and an
exception raised anywhere below Recoverer is turned into a proper 500 that
still carries the X-Request-ID header.
"""

import ipaddress
import json
import logging
import os
import re
import time
import uuid

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("uvicorn.error")


# ==============================================================================
# Stage 0.2 — TRUSTED_PROXIES: fail-closed X-Forwarded-For resolution
#
# TRUSTED_PROXIES is a comma-separated list of CIDR ranges (e.g.
# "10.0.0.0/8,172.16.0.0/12,127.0.0.1/32") naming the proxies allowed to set
# X-Forwarded-For. Fail-closed rules:
#   * TRUSTED_PROXIES unset/empty  -> XFF is NEVER trusted; the direct socket
#     peer is the client (spoofed XFF from any client is ignored).
#   * The socket peer must itself be a trusted proxy before ANY hop of the XFF
#     chain is adopted; the walk stops at the first untrusted reporter.
#   * A malformed/non-IP entry in the chain stops the walk (never skipped over).
# ==============================================================================

def parse_trusted_proxies(raw):
    """Parse a comma-separated CIDR list. Invalid entries are skipped with a
    warning (they simply grant no trust — failing closed, never open)."""
    networks = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid TRUSTED_PROXIES entry: %r", part)
    return networks


TRUSTED_PROXIES = parse_trusted_proxies(os.getenv("TRUSTED_PROXIES", ""))

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _valid_ip(text):
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _is_trusted(ip):
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXIES)


def get_real_ip(scope):
    """Resolve the client IP for an ASGI scope.

    Returns the direct socket peer unless the peer is a trusted proxy, in which
    case the rightmost XFF hop reported by a trusted peer is adopted, walking
    right-to-left until an untrusted reporter is hit (standard proxy-protocol
    semantics). Malformed hops abort the walk — fail closed.
    """
    client = scope.get("client") or ("", 0)
    direct = (client[0] or "").split("%")[0]  # strip IPv6 zone id
    if not direct:
        return "unknown"

    xff = ""
    for key, value in scope.get("headers") or ():
        if key == b"x-forwarded-for":
            xff = value.decode("latin-1")
            break
    if not xff:
        return direct

    # Fail-closed: without a trusted-proxy allowlist the chain is worthless.
    if not TRUSTED_PROXIES:
        return direct
    # Fail-closed: the peer that produced this XFF header is not ours to trust.
    if not _is_trusted(direct):
        return direct

    current = direct
    for hop in reversed([h.strip() for h in xff.split(",")]):
        if not _is_trusted(current):
            break
        hop_ip = _valid_ip(hop)
        if hop_ip is None:
            break  # garbage entry: stop, don't skip
        current = hop_ip
    return current


class RealIPMiddleware:
    """Store the resolved client IP in scope["state"]["real_ip"] for handlers,
    log correlation, and (later) per-IP rate limiting (Stage 8)."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] == "http":
            scope.setdefault("state", {})["real_ip"] = get_real_ip(scope)
        await self.app(scope, receive, send)


# ==============================================================================
# Stage 0.1 — RequestID: correlation id + access log
# ==============================================================================

class RequestIDMiddleware:
    """Attach a per-request id, echo it back as X-Request-ID, and emit one
    access-log line per request: request id, method, path, status, duration,
    resolved client ip. An inbound X-Request-ID (from a trusted front end) is
    honored when syntactically sane so traces correlate across hops."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        inbound = ""
        for key, value in scope.get("headers") or ():
            if key == b"x-request-id":
                inbound = value.decode("latin-1").strip()
                break
        request_id = inbound if _REQUEST_ID_RE.match(inbound or "") else uuid.uuid4().hex
        state["request_id"] = request_id

        started = time.perf_counter()
        status_holder = {"status": None}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message.setdefault("headers", []).append((b"x-request-id", request_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = (time.perf_counter() - started) * 1000.0
            logger.info(
                '%s "%s %s" %s %.1fms ip=%s',
                request_id,
                scope.get("method", "-"),
                scope.get("path", "-"),
                status_holder["status"] if status_holder["status"] is not None else "ERR",
                duration_ms,
                state.get("real_ip", "-"),
            )


# ==============================================================================
# Stage 0.1 — Recoverer: last-resort exception barrier
# ==============================================================================

class RecovererMiddleware:
    """Catch any unhandled exception escaping the route stack, log it with the
    request id + traceback, and return a JSON 500 carrying X-Request-ID.

    If the response has already started (mid-stream failure) a second response
    cannot be sent; the exception is re-raised so the server closes the
    connection — the error is still logged here first."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        response_started = {"v": False}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                response_started["v"] = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            state = scope.get("state") or {}
            request_id = state.get("request_id", "-")
            logger.exception(
                "Unhandled exception (request_id=%s %s %s ip=%s)",
                request_id,
                scope.get("method", "-"),
                scope.get("path", "-"),
                state.get("real_ip", "-"),
            )
            if response_started["v"]:
                raise
            body = json.dumps(
                {"error": {"message": "Internal server error", "type": "internal_error", "request_id": request_id}}
            ).encode("utf-8")
            headers = [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("latin-1")),
                (b"x-request-id", request_id.encode("latin-1")),
            ]
            await send({"type": "http.response.start", "status": 500, "headers": headers})
            await send({"type": "http.response.body", "body": body})
