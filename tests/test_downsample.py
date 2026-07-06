"""Unit tests for server-side downsampling param threading and fallback.

These tests use no live database. The PostgREST path is exercised with a
fake client; the SQLite path uses a temporary file. End-to-end behaviour
against a real TimescaleDB is covered (and gated) in
``test_downsample_integration.py``.
"""

import asyncio
import datetime as dt
import os
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import opensemantic.base.view._channel_utils as cu
from opensemantic.base._drivers import (
    LocalDatabaseDriver,
    PostgrestDatabaseDriver,
    _stride_decimate,
)
from opensemantic.base.view._data_cache import ChannelDataCache

# -- Fake PostgREST client -------------------------------------------------


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeRpc:
    def __init__(self, result, error):
        self._result = result
        self._error = error

    async def execute(self):
        if self._error is not None:
            raise self._error
        return _FakeResult(self._result)


class _FakeQuery:
    """Chainable stand-in for the table().select()... builder."""

    def __init__(self, rows):
        self._rows = rows

    def select(self, *a, **k):
        return self

    def eq(self, *a, **k):
        return self

    def order(self, *a, **k):
        return self

    def gte(self, *a, **k):
        return self

    def lte(self, *a, **k):
        return self

    def filter(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    async def execute(self):
        return _FakeResult(self._rows)


class _FakeClient:
    def __init__(self, rpc_result=None, rpc_error=None, table_rows=None):
        self.rpc_result = rpc_result
        self.rpc_error = rpc_error
        self.table_rows = table_rows or []
        self.rpc_calls = []

    def rpc(self, name, params):
        self.rpc_calls.append((name, params))
        return _FakeRpc(self.rpc_result, self.rpc_error)

    def table(self, name):
        return _FakeQuery(self.table_rows)


# -- _stride_decimate ------------------------------------------------------


def test_stride_decimate_bounds_and_endpoints():
    rows = list(range(1000))
    out = _stride_decimate(rows, 100)
    assert len(out) == 100
    assert out[0] == 0
    assert out[-1] == 999


def test_stride_decimate_noop_when_small():
    rows = list(range(10))
    assert _stride_decimate(rows, 100) == rows


# -- PostgREST driver routing ----------------------------------------------


def test_postgrest_read_routes_to_rpc():
    rpc_rows = [{"ts": "2023-01-01T00:00:00+00:00", "ch": "ch1", "data": {"value": 1}}]
    client = _FakeClient(rpc_result=rpc_rows)
    drv = PostgrestDatabaseDriver()
    drv.set_client(client)

    start = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(2023, 1, 2, tzinfo=dt.timezone.utc)
    rows = asyncio.run(
        drv.read(
            "OSWtool",
            channel_osw_id="ch1",
            start=start,
            end=end,
            max_points=500,
            downsample_method="minmax",
            edge_anchors=True,
        )
    )

    assert rows == rpc_rows
    assert len(client.rpc_calls) == 1
    name, params = client.rpc_calls[0]
    assert name == "downsample_tool_channel"
    assert params["osw_tool"] == "OSWtool"
    assert params["ch_id"] == "ch1"
    assert params["ts_start"] == start.isoformat()
    assert params["ts_end"] == end.isoformat()
    assert params["max_points"] == 500
    assert params["method"] == "minmax"
    assert params["edge_anchors"] is True


def test_postgrest_read_falls_back_on_rpc_error():
    table_rows = [
        {"ts": "2023-01-01T00:00:00+00:00", "ch": "ch1", "data": {"value": 2}}
    ]
    client = _FakeClient(
        rpc_error=RuntimeError("no such function"), table_rows=table_rows
    )
    drv = PostgrestDatabaseDriver()
    drv.set_client(client)

    rows = asyncio.run(
        drv.read(
            "OSWtool", channel_osw_id="ch1", max_points=500, downsample_method="minmax"
        )
    )

    # RPC failed -> the plain table read result is returned instead.
    assert rows == table_rows
    assert len(client.rpc_calls) == 1


def test_postgrest_read_without_downsample_skips_rpc():
    table_rows = [{"ts": "2023-01-01T00:00:00+00:00", "ch": "ch1", "data": {}}]
    client = _FakeClient(table_rows=table_rows)
    drv = PostgrestDatabaseDriver()
    drv.set_client(client)

    rows = asyncio.run(drv.read("OSWtool", channel_osw_id="ch1"))

    assert rows == table_rows
    assert client.rpc_calls == []


# -- SQLite stride fallback ------------------------------------------------


def test_local_read_decimates():
    path = tempfile.mktemp(suffix=".sqlite")
    try:
        drv = LocalDatabaseDriver(path)
        base = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
        data = [
            {
                "ts": (base + dt.timedelta(seconds=i)).isoformat(),
                "ch": "ch1",
                "data": {"value": i},
            }
            for i in range(1000)
        ]
        asyncio.run(drv.write("OSWtool", data))

        rows = asyncio.run(drv.read("OSWtool", max_points=100))
        assert len(rows) == 100
        values = {r["data"]["value"] for r in rows}
        assert 0 in values and 999 in values

        # Without max_points the full series is returned.
        full = asyncio.run(drv.read("OSWtool"))
        assert len(full) == 1000
    finally:
        if os.path.exists(path):
            os.remove(path)


# -- load_channel_data forwarding ------------------------------------------


class _RecordingDriver:
    """Driver stub that records the kwargs of read()."""

    def __init__(self):
        self.read_kwargs = None

    async def read(self, **kwargs):
        self.read_kwargs = kwargs
        return []


def test_load_channel_data_threads_downsample_params():
    from opensemantic.base import (
        DataToolController,
        LocalTimeSeriesDatabaseController,
    )
    from opensemantic.base._controller_mixin import DataToolMixin, DownsampleParams
    from opensemantic.base._model import DataChannel
    from opensemantic.core import Label

    ch = DataChannel(uuid=str(uuid4()), osw_id="temp", name="ch1")
    db = LocalTimeSeriesDatabaseController(
        name="rec",
        label=[Label(text="rec")],
        db_path=tempfile.mktemp(suffix=".sqlite"),
    )
    rec = _RecordingDriver()
    db._driver = rec

    tool = DataToolController(name="t", label=[Label(text="t")], data_channels=[ch])
    tool.archive_database = db

    asyncio.run(
        tool.load_channel_data(
            DataToolMixin.LoadChannelDataParams(
                channel="ch1",
                downsample=DownsampleParams(
                    max_points=750,
                    bin_size="5 seconds",
                    method="average",
                    edge_anchors=False,
                ),
            )
        )
    )

    # The mixin unpacks the downsample sub-object into the driver's kwargs.
    assert rec.read_kwargs is not None
    assert rec.read_kwargs["max_points"] == 750
    assert rec.read_kwargs["bin_size"] == "5 seconds"
    assert rec.read_kwargs["downsample_method"] == "average"
    assert rec.read_kwargs["edge_anchors"] is False


# -- Cache bypass ----------------------------------------------------------


class _RecordingController:
    def __init__(self):
        self.calls = []

    async def load_channel_data(self, params):
        self.calls.append(params)
        pt = SimpleNamespace(
            timestamp=dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc),
            channel=params.channel,
            value={"value": 1},
        )
        return [pt]


def test_cache_bypassed_when_downsampling():
    cache = ChannelDataCache(enabled=True)
    ctrl = _RecordingController()
    ch = SimpleNamespace(uuid="u1")
    start = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(2023, 1, 2, tzinfo=dt.timezone.utc)

    asyncio.run(
        cache.get_data(
            ctrl,
            ch,
            start,
            end,
            100,
            max_points=500,
            method="minmax",
            edge_anchors=True,
        )
    )
    asyncio.run(
        cache.get_data(
            ctrl,
            ch,
            start,
            end,
            100,
            max_points=500,
            method="minmax",
            edge_anchors=True,
        )
    )

    # Both calls hit the backend (no interval caching for downsampled reads).
    assert len(ctrl.calls) == 2
    assert cache._intervals == {}
    params = ctrl.calls[0]
    assert params.downsample.max_points == 500
    assert params.downsample.method == "minmax"
    assert params.downsample.edge_anchors is True


def test_cache_caches_full_resolution_reads():
    cache = ChannelDataCache(enabled=True)
    ctrl = _RecordingController()
    ch = SimpleNamespace(uuid="u2")
    start = dt.datetime(2023, 1, 1, tzinfo=dt.timezone.utc)
    end = dt.datetime(2023, 1, 2, tzinfo=dt.timezone.utc)

    asyncio.run(cache.get_data(ctrl, ch, start, end, 100))
    asyncio.run(cache.get_data(ctrl, ch, start, end, 100))

    # Second full-resolution read is served from cache.
    assert len(ctrl.calls) == 1
    assert cache._intervals.get("u2")


# -- Per-channel auto resolver ---------------------------------------------


def test_auto_resolver_picks_minmax_for_numeric(monkeypatch):
    for vt in ("quantity", "number", "composite"):
        monkeypatch.setattr(cu, "resolve_value_type", lambda ch, _vt=vt: _vt)
        assert cu.resolve_downsample_method(object(), "auto") == "minmax"


def test_auto_resolver_picks_sample_for_text(monkeypatch):
    for vt in ("text", "unknown"):
        monkeypatch.setattr(cu, "resolve_value_type", lambda ch, _vt=vt: _vt)
        assert cu.resolve_downsample_method(object(), "auto") == "sample"


def test_auto_resolver_explicit_passthrough(monkeypatch):
    monkeypatch.setattr(cu, "resolve_value_type", lambda ch: "text")
    # An explicit method ignores the channel type.
    assert cu.resolve_downsample_method(object(), "average") == "average"
    assert cu.resolve_downsample_method(object(), "minmax") == "minmax"
