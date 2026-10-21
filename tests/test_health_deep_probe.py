"""Regression tests for the deep health probe (P0-9, Day 26).

The shallow /health check stays public; deep mode (?deep=1) performs one
real end-to-end upstream round trip (PoW -> chat session create) and is
therefore AUTH-REQUIRED. Locked behavior:

  1. an unauthenticated deep request is refused before any upstream traffic
     (the endpoint must never work as a free relay tester)
  2. a healthy probe reports upstream_reachable / pow_ok / latency, and no
     upstream session id (account material) in the body
  3. a failing probe flips the status to degraded + 503, with a redacted
     reason

Run:  python tests/test_health_deep_probe.py   (pytest-compatible)
"""

import asyncio
import os
import sys
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module  # noqa: E402


def test_deep_probe_requires_auth_before_upstream():
    """No key -> 401, and create_new_chat must never be reached."""
    calls = {"create": 0}

    async def fail_if_called(token):
        calls["create"] += 1
        return "chat-should-not-exist"

    with mock.patch.object(app_module, "create_new_chat", fail_if_called):
        result = asyncio.run(app_module._deep_health_probe_guarded(authorized=False))
    assert calls["create"] == 0, "unauthenticated deep probing must not touch upstream"
    assert result.status_code == 401, result


def test_deep_probe_healthy_reports_shape_without_secrets():
    async def ok_create(token):
        return "chat-session-id-1234"

    with (
        mock.patch.object(app_module, "create_new_chat", ok_create),
        # Review fix (PR #62): the probe rides the state-aware pick
        # (pick_token -> get_token) instead of the state-blind first row.
        mock.patch.object(app_module, "pick_token", lambda: 1),
        mock.patch.object(
            app_module,
            "get_token",
            lambda tid: {
                "id": tid,
                "alias": None,
                "token": "secret-token-material",
                "status": "ACTIVE",
            },
        ),
    ):
        payload = asyncio.run(app_module._deep_health_probe())
    assert payload["upstream_reachable"] is True
    assert payload["pow_ok"] is True
    assert isinstance(payload["latency_ms"], (int, float))
    assert payload["chat_created"] is True
    body = str(payload)
    assert "chat-session-id-1234" not in body, "no upstream session id may leak"
    assert "secret-token-material" not in body, "no token material may leak"


def test_deep_probe_failure_is_degraded_and_redacted():
    from functions import UpstreamError

    async def boom(token):
        raise UpstreamError(
            401, "upstream said no with token aaaa1111bbbb2222cccc3333dddd4444"
        )

    with (
        mock.patch.object(app_module, "create_new_chat", boom),
        mock.patch.object(app_module, "pick_token", lambda: 1),
        mock.patch.object(
            app_module,
            "get_token",
            lambda tid: {"id": tid, "alias": None, "token": "tok", "status": "ACTIVE"},
        ),
    ):
        payload = asyncio.run(app_module._deep_health_probe())
    assert payload["upstream_reachable"] is False
    assert payload["pow_ok"] is False
    assert "aaaa1111bbbb2222cccc3333dddd4444" not in str(payload), (
        "credential-shaped runs must be scrubbed from the reason"
    )


def test_deep_probe_without_pool_token_is_honest():
    with mock.patch.object(app_module, "pick_token", lambda: None):
        payload = asyncio.run(app_module._deep_health_probe())
    assert payload["upstream_reachable"] is False
    assert payload["reason"] == "no ACTIVE token in pool"


def test_deep_probe_skips_quarantined_pool():
    """A pool whose only row is QUARANTINED must NOT be probed: pick_token()
    returns None (quarantined rows are never candidates) and the probe
    reports honestly instead of re-arming a known-dead credential."""
    calls = {"create": 0}

    async def fail_if_called(token):
        calls["create"] += 1
        return "chat-should-not-exist"

    with (
        mock.patch.object(app_module, "create_new_chat", fail_if_called),
        mock.patch.object(app_module, "pick_token", lambda: None),
        # even a state-blind get_token would find this dead row — the probe
        # must never reach it because pick_token already refused
        mock.patch.object(
            app_module,
            "get_token",
            lambda tid: {
                "id": tid,
                "alias": None,
                "token": "dead-credential",
                "status": "QUARANTINED",
            },
        ),
    ):
        payload = asyncio.run(app_module._deep_health_probe())
    assert calls["create"] == 0, "a quarantined pool must not be probed upstream"
    assert payload["upstream_reachable"] is False
    assert payload["reason"] == "no ACTIVE token in pool"


def test_health_route_refuses_unauthenticated_deep_before_upstream():
    """Route-level contract (review fix, PR #62 Low): GET /health?deep=1
    without a key returns 401 and never reaches create_new_chat — the
    ordering the shallow route used to swallow."""
    from fastapi.testclient import TestClient

    calls = {"create": 0}

    async def fail_if_called(token):
        calls["create"] += 1
        return "chat-should-not-exist"

    with mock.patch.object(app_module, "create_new_chat", fail_if_called):
        client = TestClient(app_module.app)
        resp = client.get("/health?deep=1")
    assert calls["create"] == 0, "unauthenticated deep probing must not touch upstream"
    assert resp.status_code == 401, resp.text
    assert "Invalid API key" in resp.text


def test_health_route_shallow_stays_public():
    """The shallow check stays public: no key and no deep param is answered
    (200/503 by pool state), never 401."""
    from fastapi.testclient import TestClient

    client = TestClient(app_module.app)
    resp = client.get("/health")
    assert resp.status_code in (200, 503), resp.text
    assert "Invalid API key" not in resp.text


TESTS = [
    test_deep_probe_requires_auth_before_upstream,
    test_deep_probe_healthy_reports_shape_without_secrets,
    test_deep_probe_failure_is_degraded_and_redacted,
    test_deep_probe_without_pool_token_is_honest,
    test_deep_probe_skips_quarantined_pool,
    test_health_route_refuses_unauthenticated_deep_before_upstream,
    test_health_route_shallow_stays_public,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            import traceback

            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
            traceback.print_exc()
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()
