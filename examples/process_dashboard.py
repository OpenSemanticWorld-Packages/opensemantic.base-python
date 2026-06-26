"""Example: Process/object-centered archive view.

The aggregated "virtual" channel in tree 2 is only a treeview collapse - it
merges the same channel across datatool instances of the same type so you
select it once. When plotted, that single selection fans out to **every real
channel each selected sample has data on**, across two axes:

- multiple process runs of the same type (e.g. a sample evacuated twice), and
- multiple datatools of the same type within a run (e.g. two probes).

Each resulting line is normalized so its first data point is t=0, and gets a
distinct legend entry (object / process / datatool / channel).

Treeview aggregation is co-presence aware: two same-type datatools that run
together in a process are kept as separate entries; ones that only ever appear
in different runs (drop-in replacements) are merged into one entry. Here:
- Evacuation always runs both probes together -> separate per-instance entries;
- Heating swaps the probe between runs -> one merged "DataTool/... [2 channels]"
  entry (tooltip lists the actual channels).

Run with: panel serve examples/process_dashboard.py --dev

Check a sample (tree 1) and a channel under a process type (tree 2): one line
appears per (run x datatool) the sample has data on, all from t=0.
"""

import asyncio
import datetime as dt
import random
from typing import Optional
from uuid import NAMESPACE_URL, uuid5

import nest_asyncio
import panel as pn

from opensemantic import compute_scoped_uuid
from opensemantic.base.v1 import (
    Database,
    DataChannel,
    DataTool,
    DataToolController,
    Process,
)
from opensemantic.base.view import ProcessObjectView
from opensemantic.base.view._config import DashboardConfig, PlotConfig
from opensemantic.characteristics.quantitative.v1 import (
    ForcePerAreaUnit,
    Pressure,
    Temperature,
    TemperatureUnit,
)
from opensemantic.core.v1 import Item, Label

pn.extension()

# -- Process-type marker classes (for readable tree-2 group labels) ----------
# In a real wiki these are proper Category pages; here we register lightweight
# subclasses in the oold type registry so the view can resolve a nice label.

EVAC_TYPE = "Category:OSW000000000000000000000000000000e1"
HEAT_TYPE = "Category:OSW000000000000000000000000000000e2"


class EvacuationProcess(Process):
    class Config:
        schema_extra = {"title": "Evacuation"}

    type: Optional[list] = [EVAC_TYPE]


class HeatingProcess(Process):
    class Config:
        schema_extra = {"title": "Heating"}

    type: Optional[list] = [HEAT_TYPE]


try:
    from oold.model import _types as _types_v2
    from oold.model.v1 import _types as _types_v1

    for _reg in (_types_v1, _types_v2):
        _reg[EVAC_TYPE] = EvacuationProcess
        _reg[HEAT_TYPE] = HeatingProcess
except ImportError:
    pass


# -- Build DataTool probes + controllers -------------------------------------
# Two probes of the same datatool type (same channel names temp/pressure), so
# their channels collapse to one aggregated entry per channel in the treeview.


def _make_probe(name):
    u = uuid5(NAMESPACE_URL, name)
    tool = DataTool(
        uuid=u,
        name=name,
        label=[Label(text=name)],
        data_channels=[
            DataChannel(
                uuid=str(compute_scoped_uuid(u, "temp")),
                osw_id="placeholder",
                name="temp",
                label=[Label(text="Temperature")],
                characteristic=Temperature.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(u, "pressure")),
                osw_id="placeholder",
                name="pressure",
                label=[Label(text="Pressure")],
                characteristic=Pressure.get_cls_iri(),
            ),
        ],
        storage_locations=[Database(name=name + "_db", label=[Label(text="db")])],
    )
    return DataToolController(tool, auto_archive=True)


BASE = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)


async def setup():
    probe_a = _make_probe("FurnaceProbe-A")
    probe_b = _make_probe("FurnaceProbe-B")

    # Store ~130 min of 1-minute data on both probes so every process window has
    # data. Values ramp with absolute time, so different windows differ but
    # share a shape (clear after normalizing each run to t=0).
    for probe in (probe_a, probe_b):
        for minute in range(130):
            ts = BASE + dt.timedelta(minutes=minute)
            await probe.store_channel_data(
                DataToolController.StoreChannelDataParams(
                    channel="temp",
                    value=Temperature(
                        value=20.0 + 0.15 * minute + random.uniform(-0.3, 0.3),
                        unit=TemperatureUnit.Celsius,
                    ),
                    timestamp=ts,
                )
            )
            await probe.store_channel_data(
                DataToolController.StoreChannelDataParams(
                    channel="pressure",
                    value=Pressure(
                        value=1013.0 - 0.2 * minute + random.uniform(-1, 1),
                        unit=ForcePerAreaUnit.hecto_pascal,
                    ),
                    timestamp=ts,
                )
            )

    return probe_a, probe_b


# Panel serve runs inside Tornado's event loop, so use nest_asyncio.
nest_asyncio.apply()
probe_a, probe_b = asyncio.run(setup())
controllers = [probe_a, probe_b]

# -- Sample objects and their process runs -----------------------------------

sample1 = Item(uuid=uuid5(NAMESPACE_URL, "Sample-1"), label=[Label(text="Sample 1")])
sample2 = Item(uuid=uuid5(NAMESPACE_URL, "Sample-2"), label=[Label(text="Sample 2")])
objects = [sample1, sample2]


def _run(cls, name, sample, tools, start_min, dur_min=30):
    start = BASE + dt.timedelta(minutes=start_min)
    return cls(
        uuid=uuid5(NAMESPACE_URL, name),
        label=[Label(text=name)],
        input=[sample],
        tool=list(tools),
        start_date_time=start,
        end_date_time=start + dt.timedelta(minutes=dur_min),
    )


# Two contrasting cases:
# - Evacuation always runs BOTH probes together (co-present) -> the tree keeps
#   them as separate per-instance entries (FurnaceProbe-A/temp, .../B/temp).
# - Heating swaps the probe between runs (A for Sample 1, B for Sample 2, never
#   together) -> the tree merges them into one drop-in entry
#   (DataTool/temp [2 channels]) whose tooltip lists the actual channels.
processes = [
    _run(EvacuationProcess, "S1 Evacuation #1", sample1, [probe_a, probe_b], 0),
    _run(EvacuationProcess, "S1 Evacuation #2", sample1, [probe_a, probe_b], 40),
    _run(HeatingProcess, "S1 Heating", sample1, [probe_a], 80),
    _run(EvacuationProcess, "S2 Evacuation", sample2, [probe_a, probe_b], 50),
    _run(HeatingProcess, "S2 Heating", sample2, [probe_b], 90),
]

# -- Launch ------------------------------------------------------------------

config = DashboardConfig(lang="en", plot=PlotConfig(auto_fetch=True, row_limit=10000))

view = ProcessObjectView(
    objects=objects,
    processes=processes,
    controllers=controllers,
    config=config,
    title="Process / Object Archive View",
)

view.servable()


# -- Loading from OpenSemanticLab instead of building locally ----------------
#
# from osw.express import OswExpress
# from osw.wiki_tools import SearchParam
#
# osw = OswExpress(domain="your-domain.org", cred_filepath="accounts.pwd.yaml")
# object_titles = ["Item:OSW<sample-1>", "Item:OSW<sample-2>"]
# objects = osw.load_entity(object_titles)
#
# # processes that consumed any of these objects
# process_titles = []
# for obj in objects:
#     process_titles += osw.site.semantic_search(
#         SearchParam(query=f"[[HasInput::{obj.get_iri()}]]")
#     )
# processes = osw.load_entity(list(set(process_titles)))
#
# # build a DataToolController per datatool referenced by the processes,
# # wiring each controller's archive_database from its storage_locations
# # (LocalTimeSeriesDatabaseController / PostgrestTimeSeriesDatabaseController).
# controllers = [...]
#
# view = ProcessObjectView(objects, processes, controllers)
# view.servable()
