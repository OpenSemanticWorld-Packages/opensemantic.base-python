"""Benchmark the server-side downsampling RPC against a live pgstack.

Seeds escalating series sizes for a scalar and a composite channel, then
times a full-resolution read against each downsampling strategy and reports
wall time, rows returned and approximate payload size, so the speedup vs
full-resolution and the relative cost of the deep aggregates are visible.

Gated on the same env vars as the integration tests; prints a skip notice
otherwise:

    TEST_PGRST_URL, TEST_PGRST_JWT_SECRET  (required)
    TEST_PGRST_JWT_ROLE, TEST_PGRST_SCHEMA (optional)
    BENCH_SIZES   comma-separated point counts (default "10000,100000")
    BENCH_MAXPTS  target points per downsampled read (default 2000)
    BENCH_REPEAT  timed repeats per case (default 3)
    TEST_PG_DSN   optional libpq DSN; if psycopg is installed an
                  EXPLAIN ANALYZE of one RPC call is printed.

Run:  python benchmarks/bench_downsample.py
"""

import asyncio
import datetime as dt
import json
import os
import statistics
import sys
import time
from pathlib import Path

# Load tests/.env if present, mirroring tests/conftest.py, so the benchmark
# can be run with the same configuration as the integration tests.
_env_path = Path(__file__).resolve().parent.parent / "tests" / ".env"
if _env_path.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(_env_path)
    except ImportError:
        pass

_URL = os.environ.get("TEST_PGRST_URL")
_SECRET = os.environ.get("TEST_PGRST_JWT_SECRET")
_ROLE = os.environ.get("TEST_PGRST_JWT_ROLE", "api_user")
_SCHEMA = os.environ.get("TEST_PGRST_SCHEMA", "api")
_SIZES = [int(s) for s in os.environ.get("BENCH_SIZES", "10000,100000").split(",")]
_MAXPTS = int(os.environ.get("BENCH_MAXPTS", "2000"))
_REPEAT = int(os.environ.get("BENCH_REPEAT", "3"))
_PG_DSN = os.environ.get("TEST_PG_DSN")

WRITE_CHUNK = 5000


def _make_db():
    import jwt
    from postgrest import AsyncPostgrestClient

    from opensemantic.base import PostgrestTimeSeriesDatabaseController
    from opensemantic.core import Label

    token = jwt.encode({"role": _ROLE}, _SECRET, algorithm="HS256")
    client = AsyncPostgrestClient(
        base_url=_URL,
        schema=_SCHEMA,
        headers={"Authorization": f"Bearer {token}"},
    )
    db = PostgrestTimeSeriesDatabaseController(
        name="bench", label=[Label(text="Bench")], buffered=False
    )
    db.set_client(client)
    return db


def _make_controller(db):
    """A DataToolController with a scalar and a composite channel, bound to db."""
    from uuid import uuid4

    from opensemantic import compute_scoped_uuid
    from opensemantic.base.v1 import DataChannel, DataToolController
    from opensemantic.core.v1 import Label as LabelV1

    parent = uuid4()
    ctrl = DataToolController(
        uuid=str(parent),
        name="Bench",
        label=[LabelV1(text="Bench")],
        data_channels=[
            DataChannel(
                uuid=str(compute_scoped_uuid(parent, "scalar")),
                osw_id="placeholder",
                name="scalar",
                label=[LabelV1(text="scalar")],
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent, "composite")),
                osw_id="placeholder",
                name="composite",
                label=[LabelV1(text="composite")],
            ),
        ],
    )
    ctrl.archive_database = db
    return ctrl


async def _timed_read(db, tool_id, ch, start, end, method):
    from opensemantic.base import DownsampleParams, ReadToolChannelRawParams

    ds = None
    if method != "raw":
        ds = DownsampleParams(max_points=_MAXPTS, method=method)
    times = []
    rows = []
    for _ in range(_REPEAT):
        t0 = time.perf_counter()
        rows = await db.read_tool_channel_raw(
            ReadToolChannelRawParams(
                tool_osw_id=tool_id,
                channel_osw_id=ch,
                start=start,
                end=end,
                downsample=ds,
            )
        )
        times.append((time.perf_counter() - t0) * 1000.0)
    payload_kb = len(json.dumps(rows)) / 1024.0
    return statistics.median(times), len(rows), payload_kb


def _explain(tool_id, ch, start, end):
    if not _PG_DSN:
        return
    try:
        import psycopg
    except Exception:
        print("\n(psycopg not installed; skipping EXPLAIN ANALYZE)")
        return
    sql = (
        "EXPLAIN (ANALYZE, BUFFERS) "
        "SELECT * FROM api.downsample_tool_channel(%s, %s, %s, %s, %s, NULL, 'minmax')"
    )
    print("\nEXPLAIN ANALYZE (minmax):")
    try:
        with psycopg.connect(_PG_DSN) as conn, conn.cursor() as cur:
            cur.execute(sql, (tool_id, ch, start, end, _MAXPTS))
            for (line,) in cur.fetchall():
                print("  " + line)
    except Exception as e:
        print(f"  EXPLAIN failed: {e}")


async def _run():
    from opensemantic.base import DeleteToolParams
    from opensemantic.base._demo_data import seed_channel_series

    db = _make_db()
    header = (
        f"{'size':>9} {'channel':>9} {'method':>8} {'rows':>8} {'ms':>9} {'KB':>10}"
    )
    print(header)
    print("-" * len(header))
    base = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
    for idx, n in enumerate(_SIZES):
        is_last = idx == len(_SIZES) - 1
        ctrl = _make_controller(db)
        tool_id = ctrl.get_osw_id()
        ch_scalar = ctrl.get_channel_by_name("scalar").get_osw_id().split("#")[-1]
        ch_comp = ctrl.get_channel_by_name("composite").get_osw_id().split("#")[-1]

        def _value(channel, i, _n=n):
            if channel.name == "composite":
                return {
                    "temperature": {"value": float(i)},
                    "humidity": {"value": float(_n - i)},
                }
            return {"value": float(i)}

        try:
            await seed_channel_series(
                ctrl, n_points=n, base_ts=base, value_fn=_value, chunk_size=WRITE_CHUNK
            )
            await asyncio.sleep(1.0)  # let PostgREST settle after the writes
            start, end = base, base + dt.timedelta(seconds=n - 1)
            for label, ch in (("scalar", ch_scalar), ("composite", ch_comp)):
                for method in ("raw", "sample", "average", "minmax"):
                    ms, rows, kb = await _timed_read(
                        db, tool_id, ch, start, end, method
                    )
                    print(
                        f"{n:>9} {label:>9} {method:>8} {rows:>8} "
                        f"{ms:>9.1f} {kb:>10.1f}"
                    )
            if is_last:
                # EXPLAIN while the tool still exists (largest size).
                _explain(tool_id, ch_scalar, start, end)
        finally:
            try:
                await db.delete_tool(DeleteToolParams(tool_osw_id=tool_id))
            except Exception:
                pass


def main():
    if not (_URL and _SECRET):
        print(
            "Skipping benchmark: set TEST_PGRST_URL and TEST_PGRST_JWT_SECRET "
            "to run against a live pgstack."
        )
        return 0
    try:
        asyncio.run(_run())
    except Exception as e:
        print(f"Benchmark failed (is pgstack up and the RPC applied?): {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
