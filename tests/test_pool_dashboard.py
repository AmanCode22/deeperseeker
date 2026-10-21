"""P0-3 (Day 10) — the dashboard pool view.

Operators need to see, at a glance, which token is in rotation, which is
cooling, and which is quarantined until someone restores it. The rule for this
page is the same one the access-log work (PR #25) established: the template
renders state and computes nothing, and no credential material reaches the page
source at any point.

The token table used to print the first 12 and last 4 characters of every auth
token. That is secret material in a page a browser can cache, a proxy can log
and a screenshot can carry away, so this file pins the opposite contract: the
seeded token string must not appear anywhere in the rendered HTML.

Run:  python tests/test_pool_dashboard.py   (pytest-compatible)
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import functions  # noqa: E402

# Distinctive on purpose: any leak of it into the HTML is unmistakable.
SECRET_A = "SECRETMATERIAL-alpha-never-render-this"
SECRET_B = "SECRETMATERIAL-beta-never-render-this"
SECRET_C = "SECRETMATERIAL-gamma-never-render-this"


def _seed_pool():
    """Three tokens: one in rotation, one cooling, one quarantined."""
    tmpdir = tempfile.mkdtemp()
    functions._db = os.path.join(tmpdir, "pool.db")
    if os.path.exists(functions._db):
        os.remove(functions._db)
    functions._in_flight.clear()
    functions.init_db()
    functions.add_token(SECRET_A, "personal")
    functions.add_token(SECRET_B, "work")
    functions.add_token(SECRET_C, None)

    # #2 is cooling for another minute; #3 is quarantined by an upstream 401.
    functions.mark_limited(2)
    conn = functions.get_db()
    conn.execute(
        "UPDATE tokens SET rate_limited_until=? WHERE id=2", (time.time() + 60,)
    )
    conn.commit()
    conn.close()
    functions.mark_limited(3, 401)
    return functions


def _render_dashboard():
    import app as app_module

    app_module.SESSIONS["pool-view-test"] = time.time()
    client = TestClient(app_module.app)
    client.cookies.set("session_id", "pool-view-test")
    try:
        return client.get("/dashboard")
    finally:
        app_module.SESSIONS.pop("pool-view-test", None)


def test_pool_table_shows_state_without_secrets():
    fns = _seed_pool()
    # One live request in flight on the healthy token, the way the dashboard
    # must report it while traffic is running.
    slot = fns.acquire_token_slot(1)
    try:
        resp = _render_dashboard()
    finally:
        slot.release()

    assert resp.status_code == 200, f"dashboard did not render: {resp.status_code}"
    html = resp.text

    for secret in (SECRET_A, SECRET_B, SECRET_C):
        assert secret not in html, "token material reached the page source"
        assert secret[:12] not in html, "a prefix of a token is still token material"

    # State, in-flight, last-used and cooldown-until are all on the page.
    for needle in ("QUARANTINED", "RATE_LIMITED", "ACTIVE", "In-flight", "Last used"):
        assert needle in html, f"pool view is missing {needle!r}"
    assert "personal" in html and "work" in html, "aliases must still identify rows"

    # A quarantined token needs the manual-return action next to it.
    assert "Restore" in html, "no operator path back into rotation"

    # Rendered states must match a direct store read, not a cached guess.
    direct = {row["id"]: row["status"] for row in _store_rows()}
    rendered = {row["id"]: row["status"] for row in fns.get_pool_view()}
    assert rendered == direct, f"view drifted from the store: {rendered}"
    assert rendered == {1: "ACTIVE", 2: "RATE_LIMITED", 3: "QUARANTINED"}


def _store_rows():
    conn = functions.get_db()
    rows = conn.execute("SELECT id, status FROM tokens ORDER BY id").fetchall()
    conn.close()
    return [{"id": r[0], "status": r[1]} for r in rows]


def test_pool_view_carries_no_credential_fields():
    fns = _seed_pool()
    view = fns.get_pool_view()
    assert len(view) == 3
    for row in view:
        assert "token" not in row, "the pool view must not even carry the token field"
    by_id = {row["id"]: row for row in view}
    assert by_id[3]["cooldown"] == "—", "quarantine has no timer to count down"
    assert by_id[2]["cooldown"] != "—", "a cooling token must show its countdown"
    assert "manual" in by_id[3]["state_note"].lower(), (
        "the quarantine note must tell the operator what returns the token"
    )


TESTS = [
    test_pool_table_shows_state_without_secrets,
    test_pool_view_carries_no_credential_fields,
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
