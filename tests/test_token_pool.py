"""Regression tests for the token pool scheduler v2 (Stage 1 audit, B3).

The old pick_token() selected ACTIVE tokens with ORDER BY RANDOM() and fell
back to the first token by id when everything was limited: a rate-limited
token never recovered while any other stayed ACTIVE (issue #32), a
single-token pool had no backoff, and concurrent requests stampeded onto one
account. The scheduler now provides cooldown auto-recovery, least-in-flight
selection, an idle-oldest tie-break, a soft per-token concurrency cap and an
exclude set for retry rotation (B10).

P0-3 (Day 7) adds the lifecycle tests at the bottom: a 401 must quarantine the
token instead of cooling it for 60s, repeated 403s must escalate, and a
quarantined token must return only by manual action. Those three are RED on
today's tree by design — see the before-matrix in the branch's Day 6 commit.

Run:  python tests/test_token_pool.py   (pytest-compatible)
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functions  # noqa: E402


def _fresh_pool(tmpdir, tokens):
    """Point functions at an isolated DB, (re)build it, seed the pool."""
    functions._db = os.path.join(str(tmpdir), "pool.db")
    if os.path.exists(functions._db):
        os.remove(functions._db)
    functions._in_flight.clear()
    functions.init_db()
    for alias in tokens:
        functions.add_token(f"tok-{alias}", alias)
    return functions


def test_cooldown_auto_recovery(tmpdir=None):
    import tempfile

    tmpdir = tmpdir or tempfile.mkdtemp()
    fns = _fresh_pool(tmpdir, ["a", "b"])
    # Both limited; token 1 recovers first (sooner cooldown expiry).
    past = time.time() - 5
    future = time.time() + 3600
    conn = fns.get_db()
    conn.execute(
        "UPDATE tokens SET status='RATE_LIMITED', rate_limited_until=? WHERE id=1",
        (past,),
    )
    conn.execute(
        "UPDATE tokens SET status='RATE_LIMITED', rate_limited_until=? WHERE id=2",
        (future,),
    )
    conn.commit()
    conn.close()

    assert fns.pick_token() == 1, "the token whose cooldown expired must be picked"
    row = fns.get_token(1)
    assert row["status"] == "ACTIVE", (
        "picking a recovered token must flip it back to ACTIVE"
    )

    # Still-cooling tokens are never "available", but the soonest-to-recover
    # one is returned as a bounded-wait fallback (the old code returned the
    # first token by id with no backoff logic at all).
    conn = fns.get_db()
    conn.execute(
        "UPDATE tokens SET status='RATE_LIMITED', rate_limited_until=? WHERE id=1",
        (future,),
    )
    conn.commit()
    conn.close()
    assert fns.pick_token() == 1, (
        "with every token cooling, the soonest-to-recover token must be returned"
    )
    # An empty pool still yields None.
    fns.delete_token(1)
    fns.delete_token(2)
    assert fns.pick_token() is None, "an empty pool must return None"


def test_mark_limited_sets_and_clears_cooldown(tmpdir=None):
    import tempfile

    tmpdir = tmpdir or tempfile.mkdtemp()
    fns = _fresh_pool(tmpdir, ["a", "b"])
    fns.mark_limited(1)
    conn = fns.get_db()
    status, until = conn.execute(
        "SELECT status, rate_limited_until FROM tokens WHERE id=1"
    ).fetchone()
    conn.close()
    assert status == "RATE_LIMITED" and until is not None and until > time.time()

    fns.mark_active(1)
    conn = fns.get_db()
    status, until, last_used = conn.execute(
        "SELECT status, rate_limited_until, last_used FROM tokens WHERE id=1"
    ).fetchone()
    conn.close()
    assert status == "ACTIVE" and until is None
    assert last_used is not None and abs(last_used - time.time()) < 60, (
        "mark_active must refresh last_used"
    )


def test_least_in_flight_and_idle_oldest_selection(tmpdir=None):
    import tempfile

    tmpdir = tmpdir or tempfile.mkdtemp()
    fns = _fresh_pool(tmpdir, ["a", "b", "c"])

    # Equal in-flight, no usage: lowest id (idle-oldest) leads.
    assert fns.pick_token() == 1
    slot1 = fns.acquire_token_slot(1)  # the send reserves the slot after the pick
    # One in-flight on token 1 -> token 2 leads (least in-flight).
    assert fns.pick_token() == 2
    slot2 = fns.acquire_token_slot(2)
    # In-flight on 1 and 2 -> token 3 leads.
    assert fns.pick_token() == 3
    slot3 = fns.acquire_token_slot(3)
    slot1.release()
    slot2.release()
    slot3.release()

    # Idle-oldest tie-break: equal in-flight, token 2 holds the oldest
    # last_used (tokens 1 and 3 get a fresh stamp; never-used sorts as 0.0,
    # so every token under test needs an explicit stamp).
    conn = fns.get_db()
    conn.execute("UPDATE tokens SET last_used=? WHERE id=1", (time.time(),))
    conn.execute("UPDATE tokens SET last_used=? WHERE id=2", (time.time() - 1000,))
    conn.execute("UPDATE tokens SET last_used=? WHERE id=3", (time.time(),))
    conn.commit()
    conn.close()
    assert fns.pick_token() == 2, (
        "the least-recently-used token must lead on an in-flight tie"
    )


def test_soft_concurrency_cap_deprioritizes(tmpdir=None):
    import tempfile

    tmpdir = tmpdir or tempfile.mkdtemp()
    fns = _fresh_pool(tmpdir, ["a", "b"])
    # Saturate token 1 up to the cap; token 2 must take over.
    for _ in range(fns.TOKEN_CONCURRENCY_CAP):
        fns.acquire_token_slot(1)
    assert fns.pick_token() == 2, (
        "an at-cap token must only be used when nothing else is free"
    )
    fns._in_flight.clear()
    # No alternative: the capped token is still usable (soft cap, no starvation).
    fns.delete_token(2)
    assert fns.pick_token() == 1, "the cap must never starve the pool completely"


def test_exclude_rotates_away_from_failed_token(tmpdir=None):
    import tempfile

    tmpdir = tmpdir or tempfile.mkdtemp()
    fns = _fresh_pool(tmpdir, ["a", "b"])
    assert fns.pick_token(exclude={1}) == 2, "the excluded token must be skipped"
    assert fns.pick_token(exclude={1, 2}) in (1, 2), (
        "excluding everything must still return a token (fallback)"
    )


def test_token_slot_release_is_once_only(tmpdir=None):
    import tempfile

    tmpdir = tmpdir or tempfile.mkdtemp()
    fns = _fresh_pool(tmpdir, ["a"])
    slot = fns.acquire_token_slot(1)
    assert fns.token_in_flight(1) == 1
    slot.release()
    assert fns.token_in_flight(1) == 0
    slot.release()  # idempotent: a double release must not go negative
    assert fns.token_in_flight(1) == 0


# ---------------------------------------------------------------------------
# P0-3 (Day 7) — token lifecycle: quarantine on auth failure. RED by design on
# today's tree; see the before-matrix in the branch's Day 6 commit.
# ---------------------------------------------------------------------------


def _report(fns, token_id, code):
    """Feed an upstream status through the pool's failure door.

    Today app.py maps 401, 403 and 429 onto the same mark_limited(token_id)
    call; Day 8 makes that door status-aware. The shim makes these tests fail
    on BEHAVIOR (what the pool does to a dead token) rather than on a
    TypeError, so the red run is honest evidence of the before-matrix.
    """
    try:
        fns.mark_limited(token_id, code)
    except TypeError:
        fns.mark_limited(token_id)


def _force_expired_cooldown(fns, token_id):
    """Put rate_limited_until in the past: 'the cooldown has elapsed'."""
    conn = fns.get_db()
    conn.execute(
        "UPDATE tokens SET rate_limited_until=? WHERE id=?",
        (time.time() - 1, token_id),
    )
    conn.commit()
    conn.close()


def _status_row(fns, token_id):
    conn = fns.get_db()
    row = conn.execute(
        "SELECT status, rate_limited_until, consecutive_403 FROM tokens WHERE id=?",
        (token_id,),
    ).fetchone()
    conn.close()
    return row


def test_401_quarantines_and_makes_the_token_unselectable(tmpdir=None):
    """401 -> QUARANTINED, never selected, never auto-recovered (P0-3)."""
    import tempfile

    tmpdir = tmpdir or tempfile.mkdtemp()
    fns = _fresh_pool(tmpdir, ["a", "b"])

    _report(fns, 1, 401)

    assert fns.get_token(1)["status"] == "QUARANTINED", (
        "a 401 is an auth failure, not a rate limit: today the dead token "
        "just cools for 60s and then silently returns to rotation"
    )
    assert fns.pick_token() == 2, "a quarantined token must never be selected"

    # Elapsed cooldown must not bring it back. pick_token() flips an expired
    # RATE_LIMITED row to ACTIVE as landed behavior; a quarantined row may not
    # ride that path.
    _force_expired_cooldown(fns, 1)
    for _ in range(5):
        assert fns.pick_token() == 2, (
            "quarantine is terminal: cooldown expiry must not re-select it"
        )
    assert fns.get_token(1)["status"] == "QUARANTINED"


def test_repeated_403_escalates_to_quarantine(tmpdir=None):
    """N consecutive 403s escalate; a success resets the streak (P0-3)."""
    import tempfile

    tmpdir = tmpdir or tempfile.mkdtemp()
    fns = _fresh_pool(tmpdir, ["a", "b"])
    threshold = getattr(fns, "QUARANTINE_AFTER_403", 3)
    assert threshold >= 2, "the escalation knob needs a real streak to count"

    for _ in range(threshold - 1):
        _report(fns, 1, 403)
    assert fns.get_token(1)["status"] != "QUARANTINED", (
        "below the threshold a 403 stays the transient case it is today"
    )

    _report(fns, 1, 403)
    assert fns.get_token(1)["status"] == "QUARANTINED", (
        f"{threshold} consecutive 403s must escalate to quarantine, "
        f"got {fns.get_token(1)['status']}"
    )

    # "Consecutive" means without an intervening success: a healthy turn resets
    # the counter, so the same count spread across successes is not a ban sign.
    fns.restore_token(1)
    for _ in range(threshold - 1):
        _report(fns, 1, 403)
        fns.mark_active(1)  # a success between every failure
    assert fns.get_token(1)["status"] != "QUARANTINED", (
        "a successful turn must reset the 403 streak"
    )

    # The knob only governs 403 escalation; a 401 stays unconditional.
    fns.QUARANTINE_AFTER_403 = 0
    try:
        for _ in range(threshold + 2):
            _report(fns, 1, 403)
        assert fns.get_token(1)["status"] != "QUARANTINED", (
            "DEEPSEEKER_QUARANTINE_AFTER_403=0 must keep 403s transient"
        )
        _report(fns, 1, 401)
        assert fns.get_token(1)["status"] == "QUARANTINED", (
            "401 quarantine is unconditional, even with the 403 knob off"
        )
    finally:
        fns.QUARANTINE_AFTER_403 = threshold


def test_quarantine_returns_only_by_manual_action(tmpdir=None):
    """The one way out of quarantine is the explicit restore (P0-3)."""
    import tempfile

    tmpdir = tmpdir or tempfile.mkdtemp()
    fns = _fresh_pool(tmpdir, ["a", "b"])

    _report(fns, 1, 401)
    _force_expired_cooldown(fns, 1)
    assert fns.get_token(1)["status"] == "QUARANTINED", (
        "an elapsed cooldown must not have touched the quarantine"
    )

    # Every token dead: the soonest-to-recover fallback may not quietly hand
    # back a quarantined token — the pool has to report "no token" instead.
    _report(fns, 2, 401)
    _force_expired_cooldown(fns, 2)
    assert fns.pick_token() is None, (
        "with every token quarantined the pool must return no token at all"
    )

    fns.restore_token(1)
    status, until, streak = _status_row(fns, 1)
    assert status == "ACTIVE", "restore_token is the only exit from quarantine"
    assert until is None, "restore must clear the cooldown stamp as well"
    assert streak == 0, "restore must clear the 403 streak"
    assert fns.pick_token() == 1, (
        "a restored token re-enters rotation, the quarantined one stays out"
    )


TESTS = [
    test_cooldown_auto_recovery,
    test_mark_limited_sets_and_clears_cooldown,
    test_least_in_flight_and_idle_oldest_selection,
    test_soft_concurrency_cap_deprioritizes,
    test_exclude_rotates_away_from_failed_token,
    test_token_slot_release_is_once_only,
    test_401_quarantines_and_makes_the_token_unselectable,
    test_repeated_403_escalates_to_quarantine,
    test_quarantine_returns_only_by_manual_action,
]


def main():
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(TESTS)} tests passed")


if __name__ == "__main__":
    main()
