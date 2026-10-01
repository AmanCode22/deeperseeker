"""Regression tests for LRU session pruning (B13, Stage 1 audit).

prune_sessions() used to keep the newest rows by rowid (= insertion order).
Long-running chats — the most valuable sessions — hold the OLDEST rows and
were pruned while active: silent context loss on exactly the workload the
bridge serves best. Pruning is now least-recently-USED first, with
find_session() touching last_used on every hit.

Run:  python tests/test_session_pruning.py   (pytest-compatible)
"""
import os
import sys
import tempfile
import time
import unittest.mock as mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functions  # noqa: E402


def _fresh_db():
    tmpdir = tempfile.mkdtemp()
    functions._db = os.path.join(tmpdir, "sessions.db")
    if os.path.exists(functions._db):
        os.remove(functions._db)
    functions.init_db()
    conn = functions.get_db()
    conn.execute("DELETE FROM sessions")
    conn.commit()
    conn.close()


def _sig(i):
    return f"sig-{i:04d}"


def _seed(n):
    for i in range(n):
        functions.save_session(_sig(i), 1, f"chat-{i}", 0)


def test_prune_keeps_most_recently_used():
    _fresh_db()
    _seed(6)
    # sig-0000 has the OLDEST rowid (a long-running chat saved on its first
    # turn) but was used JUST NOW; sig-0005 is the NEWEST rowid but went
    # stale an hour ago. The old rowid-DESC policy kept sig-0005 and evicted
    # sig-0000 — exactly the context-loss bug. LRU must do the opposite.
    now = time.time()
    stamps = {_sig(0): now, _sig(1): now - 10, _sig(2): now - 20, _sig(3): now - 30, _sig(4): now - 40, _sig(5): now - 3600}
    conn = functions.get_db()
    for sig, ts in stamps.items():
        conn.execute("UPDATE sessions SET last_used=? WHERE signature=?", (ts, sig))
    conn.commit()
    conn.close()

    with mock.patch.object(functions, "MAX_SESSIONS", 3):
        functions.prune_sessions()

    remaining = {r["session_id"] for r in (functions.find_session(_sig(i)) for i in range(6)) if r}
    assert "chat-0" in remaining and "chat-1" in remaining and "chat-2" in remaining, remaining
    assert "chat-5" not in remaining, "the stale newest-rowid session must be evicted"
    assert "chat-4" not in remaining and "chat-3" not in remaining, remaining


def test_find_session_touches_last_used():
    _fresh_db()
    functions.save_session(_sig(1), 1, "chat-1", 0)
    conn = functions.get_db()
    conn.execute("UPDATE sessions SET last_used=NULL WHERE signature=?", (_sig(1),))
    conn.commit()
    conn.close()

    assert functions.find_session(_sig(1)) is not None
    conn = functions.get_db()
    last_used = conn.execute("SELECT last_used FROM sessions WHERE signature=?", (_sig(1),)).fetchone()[0]
    conn.close()
    assert last_used is not None and abs(last_used - time.time()) < 60, \
        "find_session must touch last_used so pruning sees real recency"


def test_prune_never_grows_below_cap():
    _fresh_db()
    _seed(4)
    with mock.patch.object(functions, "MAX_SESSIONS", 10):
        functions.prune_sessions()
    conn = functions.get_db()
    total = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    assert total == 4, "pruning below the cap must delete nothing"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {type(e).__name__}: {e}")
    if failed:
        sys.exit(1)
    print(f"{len(tests)} tests passed")


if __name__ == "__main__":
    main()
