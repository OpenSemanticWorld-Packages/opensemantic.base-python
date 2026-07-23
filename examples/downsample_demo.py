"""Example: server-side downsampling, one strategy per channel.

Four channels carry the *same* 100,000-point signal (a slow baseline with a
few narrow spikes). Each channel is named after - and plotted with - a different
downsampling strategy, so the trade-offs are visible side by side:

    raw      no downsampling: full detail, slow to load
    sample   one real point per bucket: spikes often lost
    average  bucket mean: spikes smoothed away
    minmax   real per-bucket extremes: spikes preserved

What to try:
  - Check "raw": note the longer load (100k points transported).
  - Check the others: they load fast (~2000 points), but the spikes are missing
    on "sample" and "average", while "minmax" keeps them.
  - Box-zoom into a flat stretch (drag horizontally), then click "Load current
    range": the zoomed window is re-fetched at finer resolution and the hidden
    spikes reappear on "sample"/"average". Box-zoom alone is just visual; the
    toolbar "reset" returns to the full view.

Requires a running pgstack (TimescaleDB + PostgREST) with the downsampling RPC
applied. Configure via env vars (defaults match the local dev stack):

    DEMO_PGRST_URL          (default http://localhost:3000)
    DEMO_PGRST_JWT_SECRET   (default reallyreallyreallyreallyverysafe)
    DEMO_PGRST_JWT_ROLE     (default api_user)
    DEMO_PGRST_SCHEMA       (default api)

Seed the data once, then serve:
    python examples/downsample_demo.py          # seeds 100k x 4 channels
    panel serve examples/downsample_demo.py --dev
"""

import asyncio
import datetime as dt
import math
import os
from typing import Optional
from uuid import NAMESPACE_URL, uuid5

import panel as pn

from opensemantic import compute_scoped_uuid
from opensemantic.base._demo_data import already_seeded, seed_channel_series
from opensemantic.base.v1 import (
    DataChannel,
    DataToolController,
    PostgrestTimeSeriesDatabaseController,
)
from opensemantic.base.view import (
    DataToolPlotControlsConfig,
    DataToolView,
    DataToolViewConfig,
    DownsampleConfig,
    UrlConfigMode,
)
from opensemantic.characteristics.quantitative.v1 import Characteristic
from opensemantic.core.v1 import Label

pn.extension()

N_POINTS = 100_000
MAX_POINTS = 2000
# Narrow spikes (a few points wide): lost by sample/average at the full-window
# resolution (~50 s buckets), but revealed once you zoom in (finer buckets).
SPIKE_CENTERS = (12_000, 37_000, 62_000, 87_000)
SPIKE_WIDTH = 5
SPIKE_AMPLITUDE = 35.0

# Channels are named after the downsampling strategy used to plot them.
# "raw" means no downsampling (method=None).
METHOD_BY_CHANNEL = {
    "raw": None,
    "sample": "sample",
    "average": "average",
    "minmax": "minmax",
}

_URL = os.environ.get("DEMO_PGRST_URL", "http://localhost:3000")
_SECRET = os.environ.get("DEMO_PGRST_JWT_SECRET", "reallyreallyreallyreallyverysafe")
_ROLE = os.environ.get("DEMO_PGRST_JWT_ROLE", "api_user")
_SCHEMA = os.environ.get("DEMO_PGRST_SCHEMA", "api")

BASE_TS = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)


class _Signal(Characteristic):
    """Base for the demo's plain numeric characteristics."""

    value: Optional[float] = None


# One distinct characteristic per channel so each lands in its OWN plot
# (grouping is by characteristic). The underlying values are identical; only
# the downsampling strategy differs, so the per-plot effect is easy to see.
class RawSignal(_Signal):
    class Config:
        schema_extra = {"uuid": "00000000-0000-0000-0000-0000000000d1", "title": "raw"}

    type: Optional[list] = ["Category:OSW000000000000000000000000000000d1"]


class SampleSignal(_Signal):
    class Config:
        schema_extra = {
            "uuid": "00000000-0000-0000-0000-0000000000d2",
            "title": "sample",
        }

    type: Optional[list] = ["Category:OSW000000000000000000000000000000d2"]


class AverageSignal(_Signal):
    class Config:
        schema_extra = {
            "uuid": "00000000-0000-0000-0000-0000000000d3",
            "title": "average",
        }

    type: Optional[list] = ["Category:OSW000000000000000000000000000000d3"]


class MinmaxSignal(_Signal):
    class Config:
        schema_extra = {
            "uuid": "00000000-0000-0000-0000-0000000000d4",
            "title": "minmax",
        }

    type: Optional[list] = ["Category:OSW000000000000000000000000000000d4"]


_CHARACTERISTICS = {
    "raw": RawSignal,
    "sample": SampleSignal,
    "average": AverageSignal,
    "minmax": MinmaxSignal,
}


try:
    from oold.model import _types

    for _c in _CHARACTERISTICS.values():
        _types[_c.get_cls_iri()] = _c
except ImportError:
    pass


class DownsampleDemoView(DataToolView):
    """DataToolView that forces a fixed downsampling method per channel name."""

    def _downsample_for(self, channel):
        method = METHOD_BY_CHANNEL.get(getattr(channel, "name", ""), "sample")
        if method is None:
            return None, None, None  # raw / full resolution
        ds = self._config.plot.downsample
        return ds.max_points, method, ds.edge_anchors

    async def _load_and_plot(self):
        # Construct the PostgREST client on the server's event loop on first
        # use, then run the normal load. Serialize concurrent loads (a fast
        # selection plus the zoom-reload callback can otherwise overlap and
        # render a stale, collapsed figure); re-run once if one was requested
        # while loading.
        _ensure_client(self._controllers[0].archive_database)
        if getattr(self, "_loading", False):
            self._reload_pending = True
            return
        self._loading = True
        try:
            await super()._load_and_plot()
            while getattr(self, "_reload_pending", False):
                self._reload_pending = False
                await super()._load_and_plot()
        finally:
            self._loading = False


def _make_client():
    import jwt
    from postgrest import AsyncPostgrestClient

    token = jwt.encode({"role": _ROLE}, _SECRET, algorithm="HS256")
    return AsyncPostgrestClient(
        base_url=_URL,
        schema=_SCHEMA,
        headers={"Authorization": f"Bearer {token}"},
    )


def _make_db():
    # No client here: the async client must be constructed on the event loop
    # that will use it (see _ensure_client). Building it at import time, off any
    # running loop, makes its anyio primitives bind to the wrong loop and every
    # request fails with "Server disconnected" under panel serve.
    return PostgrestTimeSeriesDatabaseController(
        name="downsample_demo",
        label=[Label(text="Downsample Demo DB")],
        buffered=False,
    )


def _ensure_client(db):
    """Lazily attach the PostgREST client (call from the running loop)."""
    driver = getattr(db, "_driver", None)
    if driver is not None and driver.client is None:
        db.set_client(_make_client())


def _signal(i: int) -> float:
    """Slow baseline plus a narrow spike at a few index ranges."""
    v = 10.0 + 5.0 * math.sin(2.0 * math.pi * i / 8000.0)
    if any(c <= i < c + SPIKE_WIDTH for c in SPIKE_CENTERS):
        v += SPIKE_AMPLITUDE
    return v


def _build_controller():
    parent = uuid5(NAMESPACE_URL, "OSW-DownsampleDemoTool")
    channels = [
        DataChannel(
            uuid=str(compute_scoped_uuid(parent, name)),
            osw_id="placeholder",
            name=name,
            label=[Label(text=name)],
            characteristic=_CHARACTERISTICS[name].get_cls_iri(),
        )
        for name in METHOD_BY_CHANNEL
    ]
    ctrl = DataToolController(
        uuid=str(parent),
        name="DownsampleDemo",
        label=[Label(text="Downsample Demo Tool")],
        data_channels=channels,
        auto_archive=False,
    )
    return ctrl


async def _seed_if_needed(ctrl):
    _ensure_client(ctrl.archive_database)
    if await already_seeded(ctrl):
        print(f"Tool {ctrl.get_osw_id()} already seeded; skipping.")
        return
    print(f"Seeding {N_POINTS} points x {len(ctrl.get_all_channels())} channels...")
    written = await seed_channel_series(
        ctrl,
        n_points=N_POINTS,
        base_ts=BASE_TS,
        value_fn=lambda ch, i: {"value": _signal(i)},
    )
    print(f"Seeded {written} rows for {ctrl.get_osw_id()}")


# Build synchronously with NO network I/O at import time. ``panel serve``
# re-runs this module per session; doing async PostgREST I/O here (under
# nest_asyncio) is unreliable and would blank the app. The reads happen
# lazily on the server's own event loop when a channel is selected.
controller = _build_controller()
controller.archive_database = _make_db()

config = DataToolViewConfig(
    lang="en",
    plot=DataToolPlotControlsConfig(
        auto_fetch=True,
        row_limit=N_POINTS,
        downsample=DownsampleConfig(enabled=True, max_points=MAX_POINTS),
    ),
)

view = DownsampleDemoView(
    controllers=[controller],
    config=config,
    url_sync=True,
    url_mode=UrlConfigMode.JSON,  # readable JSON in the URL
    title="Downsampling Demo (raw / sample / average / minmax)",
)
# One plot per characteristic (channel). The plots stack at full height and the
# main content area scrolls, so all four strategies can be compared by scrolling.
# Time window. Defaults to the full series; DEMO_WINDOW="start_sec,end_sec"
# (seconds from BASE_TS) narrows it - used to capture the "zoomed in" view
# where sample/average reload at finer buckets and the spikes reappear.
_win = os.environ.get("DEMO_WINDOW")
if _win:
    _a, _b = (int(x) for x in _win.split(","))
    view.set_time_range(
        BASE_TS + dt.timedelta(seconds=_a),
        BASE_TS + dt.timedelta(seconds=_b),
        fetch=False,
    )
else:
    view.set_time_range(BASE_TS, BASE_TS + dt.timedelta(seconds=N_POINTS), fetch=False)

view.servable()


if __name__ == "__main__":
    # Seed the demo data once, before serving:
    #   python examples/downsample_demo.py
    #   panel serve examples/downsample_demo.py --dev
    asyncio.run(_seed_if_needed(controller))
    print("Seeding complete. Now run: panel serve examples/downsample_demo.py")
