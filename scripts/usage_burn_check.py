"""Day 30 burn/performance check (P0-6): seed a month of synthetic records,
verify exact numbers, and time the aggregation the dashboard page runs.

Run:  python scripts/usage_burn_check.py   (not part of the test suite)
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import functions  # noqa: E402

NOW = time.time()
KEY_A = "dsk-aaaaaaaaaaaaaaaaaaaa1111"
KEY_B = "dsk-bbbbbbbbbbbbbbbbbbbb2222"

tmp = tempfile.mkdtemp()
functions._db = os.path.join(tmp, "deeperseeker.db")
functions.init_db()

# Burn: key A -> 123 requests (mix of surfaces), key B -> 47 requests, all
# "today" (day bucket = NOW's UTC date). Known totals:
IN_A, OUT_A = 123 * 1000, 123 * 250
IN_B, OUT_B = 47 * 500, 47 * 100
for i in range(123):
    surface = "openai" if i % 2 else "anthropic"
    functions.record_usage(KEY_A, surface, "v4.1flash", 1000, 250, ts=NOW)
for _ in range(47):
    functions.record_usage(KEY_B, "openai", "v4.1flash", 500, 100, ts=NOW)

rows = functions.usage_by_day_key()
by = {r["key"]: r for r in rows}
a = by[functions.mask_api_key(KEY_A)]
b = by[functions.mask_api_key(KEY_B)]
assert a["requests"] == 123, a
assert a["input_tokens"] == IN_A and a["output_tokens"] == OUT_A, a
assert b["requests"] == 47, b
assert b["input_tokens"] == IN_B and b["output_tokens"] == OUT_B, b
print(f"burn check: exact match — A={a['requests']} req, B={b['requests']} req")

# Cross-check one day x key slice against a direct store query.
conn = functions.get_db()
direct = conn.execute(
    "SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM usage "
    "WHERE key_alias = ? AND day = ?",
    (functions.mask_api_key(KEY_B), time.strftime("%Y-%m-%d", time.gmtime(NOW))),
).fetchone()
conn.close()
assert tuple(direct) == (47, IN_B, OUT_B), tuple(direct)
print(f"cross-check vs direct store query: {direct} == (47, {IN_B}, {OUT_B})")

# Performance: a month of synthetic data at ~1000 turns/day (~30k rows),
# page aggregation timed end-to-end.
t0 = time.perf_counter()
for d in range(30):
    ts = NOW - (30 - d) * 86400
    for r in range(1000):
        functions.record_usage(
            KEY_A if r % 3 else KEY_B, "openai", "v4.1flash", 1200, 300, ts=ts
        )
seed_s = time.perf_counter() - t0
t0 = time.perf_counter()
rows = functions.usage_by_day_key()
agg_s = time.perf_counter() - t0
total_rows = sum(r["requests"] for r in rows)
print(
    f"perf: seeded 30000 rows in {seed_s:.2f}s; "
    f"aggregation returned {len(rows)} day-key rows "
    f"({total_rows} requests) in {agg_s * 1000:.1f} ms"
)
assert agg_s < 1.0, "page query must stay under a second at a month of data"
print("OK")
