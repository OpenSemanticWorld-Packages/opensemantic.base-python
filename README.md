[![PyPI-Server](https://img.shields.io/pypi/v/opensemantic.base.svg)](https://pypi.org/project/opensemantic.base/)
[![Coveralls](https://img.shields.io/coveralls/github/OpenSemanticWorld-Packages/opensemantic.base/main.svg)](https://coveralls.io/r/OpenSemanticWorld-Packages/opensemantic.base)

# opensemantic.base

Python models and controllers for the `world.opensemantic.base` page package.

Builds on [oold-python](https://github.com/OpenSemanticWorld/oold-python) (`BaseController`, `LinkedBaseModel`, `cast()`, `_types` registry) and [opensemantic](https://github.com/OpenSemanticWorld-Packages/opensemantic-python) (`OswBaseModel`, `compute_scoped_uuid`).

## Overview

- **Auto-generated Pydantic models** (v1 and v2): Database, WebService, DataTool, DataChannel, Person, Organization, etc.
- **DataToolController** - generic controller for any DataTool
- **TimeSeriesDatabaseController** - SQLite and PostgREST backends for time series storage

## Architecture

```
opensemantic.base/
  _model.py            # auto-generated v2 Pydantic models (DO NOT EDIT)
  _controller_mixin.py # DataToolMixin, TimeSeriesDatabaseController mixins
  _controller.py       # v2 controllers
  __init__.py           # re-exports model + controller classes
  v1/                   # same structure for Pydantic v1
```

### Data model

A `DataTool` instance is typed by the `DataTool` category, carries its data
channels (each typed by a characteristic), and references its storage:

```mermaid
graph LR
    probeA["probeA<br/>(DataTool instance)"]
    probeA -- HasType --> DataTool["DataTool"]
    probeA -- HasStorageLocation --> archive["archive<br/>(Database)"]
    probeA -- HasDataChannel --> temp[":temp"]
    probeA -- HasDataChannel --> pressure[":pressure"]
    temp -- HasCharacteristic --> Temperature["Temperature"]
    pressure -- HasCharacteristic --> Pressure["Pressure"]
```

## DataToolController

Extends DataTool with channel management, subdevice traversal, and data archiving.

```python
from opensemantic.base import DataToolController

tool = DataToolController(
    name="sensor",
    label=[...],
    data_channels=[ch1, ch2],
    storage_locations=[db],
    auto_archive=True,  # auto-creates archive DB from storage_locations[0]
)

tool.get_all_channels()       # recursive across subdevices
tool.get_channel_owner(ch)    # find which device owns a channel
tool.to_json()                # only model fields (controller fields stripped)
```

### Auto-archive from storage_locations

When `auto_archive=True` and no explicit `archive_database` is set, the controller auto-creates a `LocalTimeSeriesDatabaseController` from the first `storage_locations` entry (resolved via oold backend).

### Typed read/write

Channels with a `characteristic` IRI enable typed serialization:

```python
# Write: converts to base unit, strips defaults for compact storage
await tool.store_typed_data(DataToolMixin.StoreTypedDataParams(
    tool_osw_id=tool.get_osw_id(),
    rows=[DataToolMixin.TypedDataRow(ts=now, channel=ch, value=Temperature(value=300.0))],
))

# Read: resolves characteristic class via oold's _types registry
results = await tool.read_typed_data(DataToolMixin.ReadTypedDataParams(
    tool_osw_id=tool.get_osw_id(), channel=ch,
))
# results[0] is a Temperature instance with defaults restored
```

### Subobject ID auto-computation

Inline subobject `osw_id` fields are auto-prefixed with the parent's osw_id:

```
Parent:  OSW<parent_uuid>
Channel: OSW<parent_uuid>#OSW<channel_uuid>
```

Fields with `range` in json_schema_extra are references to separate entities and are not prefixed.

### Unloaded characteristic warning

On init, DataToolController checks if channel characteristic IRIs are present in oold's `_types` registry. Missing entries produce a warning with guidance to import the corresponding package.

## TimeSeriesDatabaseController

Abstract base for time series storage, with SQLite and PostgREST implementations.

```python
from opensemantic.base import LocalTimeSeriesDatabaseController

db = LocalTimeSeriesDatabaseController(name="archive", label=[...], db_path="./data.sqlite")
await db.create_tool(params)
await db.write_tool_channel_raw(params)
await db.read_tool_channel_raw(params)
```

## DataToolView (Dashboard UI)

Interactive dashboard for visualizing archived time series data from DataToolControllers.

Features:
- Wunderbaum TreeGrid for channel selection with characteristic metadata
- Stacked Bokeh plots grouped by characteristic (temperature, pressure, etc.)
- Unit conversion via dropdown (e.g. K to C, Pa to hPa)
- Composite channel splitting (e.g. AirQuality into temperature + pressure sub-plots)
- Text channel log console with timestamped entries
- Configurable via JsonEditor (grouping, auto-fetch, row limit, cache)

![Archive Demo](docs/archive_demo.gif)

*Interactive demo: channel selection, plotting, unit switching*

![Archive Dashboard](docs/screenshot_archive_plots.png)

*Channel selection with stacked plots grouped by characteristic*

![Unit Switching](docs/screenshot_unit_switch.png)

*Unit conversion via dropdown (K to C)*

![Log Console](docs/screenshot_log_console.png)

*Text channel log console with timestamped entries*

```python
from opensemantic.base.view import DataToolView
from opensemantic.base.view._config import DashboardConfig, PlotConfig

view = DataToolView(
    controllers=[ctrl],
    config=DashboardConfig(lang="en", plot=PlotConfig(auto_fetch=True)),
    title="My Dashboard",
)
view.servable()  # for panel serve
```

See [examples/datatool_dashboard.py](examples/datatool_dashboard.py) for a full working example.

To regenerate the screenshots after UI changes, see [docs/generate_screenshots.py](docs/generate_screenshots.py).

### Export

A toolbar at the top of the plot card exports the currently plotted data and
plot (requires the `export` extra):

- **Download CSV / XLSX** - a unit-aware [pint-pandas](https://github.com/hgrecco/pint-pandas)
  table (one column per series, its unit written in a dedicated header row via
  `dequantify()`), matching exactly what is plotted (unit conversion +
  normalization). Rows are capped at `EXPORT_MAX_ROWS` (default 1,000,000) via
  uniform downsampling.
- **Download plot (HTML)** - a standalone interactive Bokeh document
  (`bokeh.embed.file_html`). PNG is available from the Bokeh toolbar's save tool.

The rendered figures are exposed via `view.figures` so a host app can add its
own annotations; `view.export_series()` returns the plotted series as tidy
records.

## Server-side downsampling

Large time series are downsampled on the server so the plot only transports the
points the current zoom level can show. This is driven by `PlotConfig.downsample`:

```python
from opensemantic.base.view._config import DashboardConfig, PlotConfig, DownsampleConfig

config = DashboardConfig(
    plot=PlotConfig(
        downsample=DownsampleConfig(
            enabled=True,      # downsample when the backend supports it
            max_points=2000,   # target points per channel
            method="auto",     # auto | sample | average | minmax
            edge_anchors=True, # keep the window's first/last real datapoints
        )
    )
)
```

It only engages on a PostgREST/TimescaleDB backend (the `downsample_tool_channel`
RPC). On a SQLite/local backend, the RPC being absent, or any error, the read
silently falls back to the full-resolution path - downsampling never breaks a
read. The `DataToolView` plot also reloads at a finer resolution when you zoom in.

Strategies (N = number of buckets):

- `sample` (default): one real datapoint nearest each bucket center. Schema
  agnostic; works for scalar and composite channels. N rows.
- `average`: structure-preserving deep average per bucket (every numeric leaf
  averaged, non-numeric keys carried), bucket-center timestamp. N rows.
- `minmax`: the real min and max datapoint of every numeric sub-characteristic
  per bucket. Scalar: 2N real rows; composite: the real per-leaf extremes. Best
  at preserving spikes.
- `auto`: `minmax` for numeric channels, `sample` for text channels.

`average`/`minmax` skip non-numeric leaves (comments, labels) and fall back to
`sample` when a channel has no numeric leaf at all.

**Unit normalization caveat:** `average` and `minmax` compare and combine the
bare stored numbers per leaf, so they are only correct when all stored values of
a leaf share the same unit. The archive stores base-unit-normalized values, but
data ingested without normalization (mixed units in one channel) will produce
wrong `average`/`minmax` results. `sample` returns whole real rows and is
unaffected.

The RPC lives in pgstack's `postgres/config/optional/100_init_tsdb_schema.sql`
(uses only core, Apache-2 TimescaleDB; no toolkit dependency). It is created at
database init; apply it manually (`psql -f` / pgAdmin) on an already-running
cluster. To measure the speedup, see
[benchmarks/bench_downsample.py](benchmarks/bench_downsample.py).

[examples/downsample_demo.py](examples/downsample_demo.py) is an interactive
demo: four channels carry the same 100k-point signal (with narrow spikes), one
per strategy - the channels are named `raw`, `sample`, `average`, `minmax`.
Selecting `raw` loads slowly with full detail; `sample`/`average` load fast but
drop the spikes; `minmax` keeps them. Box-zoom into a flat stretch and click
"Load current range" to re-fetch that window at finer resolution - the hidden
spikes reappear on `sample`/`average`. Needs a running pgstack with the RPC
applied; seed once with `python examples/downsample_demo.py`, then
`panel serve examples/downsample_demo.py`.

![Downsampling demo](docs/downsample_demo.gif)

![Strategy comparison](docs/screenshot_downsample_raw.png)

*Full window, all four channels: `raw` and `minmax` keep the spikes; `sample`
and `average` smooth them away at the coarse full-window resolution.*

![Zoomed, before reload](docs/screenshot_downsample_zoom_select.png)

*Box-zoomed around a spike on the `sample` channel: only the coarse full-window
points are shown, so the spike is still hidden.*

![Zoom reveals the peak](docs/screenshot_downsample_zoom.png)

*After "Load current range": the window is re-fetched at finer buckets and the
hidden spike reappears. The toolbar reset returns to the full window.*

To regenerate these, see
[docs/generate_downsample_screenshots.py](docs/generate_downsample_screenshots.py).

## ProcessObjectView (Process/Object Dashboard UI)

Where `DataToolView` is centered on data tools, `ProcessObjectView` is centered
on the **objects** (samples, specimens, ... - any `Item`) that pass through
processes. It answers *"how did measurement X compare across the runs my objects
went through?"* by overlaying repeated runs on a common, time-normalized axis.

The view walks these relations - an object is a process *input*, a process
*uses* data tools, and a data tool *has* channels:

```mermaid
graph LR
    process[":process"]
    process -- HasInput --> sample["sample (Item)<br/>the 'object'"]
    process -- HasTool --> probeA["probeA<br/>(DataTool)"]
    process -- HasStartDateAndTime --> startTime["start_date_time"]
    process -- HasEndDateAndTime --> endTime["end_date_time"]
    probeA -- HasDataChannel --> channels[":temp, :pressure"]
```

`start_date_time` / `end_date_time` define each run's plot window; the view
normalizes every run to its first data point (t=0).

![Process Demo](docs/process_demo.gif)

*Pick objects (tree 1) and a channel under a process type (tree 2); each run is
overlaid from t=0.*

Two trees drive the plot:

1. **Objects** - the `Item` instances to compare.
2. **Process types -> channels** - for each process type the objects went
   through, the channels of the data tools used in those runs.

Tree 2 is **aggregated for selection only** (tick once instead of ticking the
same channel on every run and tool). The aggregation is **co-presence aware**:

- data tools of the same type that run **together** in a process stay as
  **separate** entries (distinct measurement points);
- data tools that only ever appear in **different** runs are treated as drop-in
  replacements and **merged** into one `… [n channels]` entry (the actual
  channels are listed in its tooltip).

![Process trees](docs/screenshot_process_trees.png)

*Evacuation ran both probes together (separate per-instance entries); Heating
swapped the probe between runs (merged `DataTool/… [2 channels]` entries).*

Selecting an object + a channel entry plots **every real channel that object has
data on** - fanning out across process runs and co-present tools. Each line is
normalized to its own run (first data point at t=0, x-axis in seconds), grouped
by characteristic, and gets a distinct legend entry
(`object / process / data tool / channel`).

![Process overlay](docs/screenshot_process_overlay.png)

*One channel selection fans out to two runs of the same sample, overlaid from
t=0 for comparison.*

![Heating drop-in](docs/screenshot_process_heating.png)

*Adding `Heating/DataTool/temp` and `…/pressure` on top of the Evacuation
selection overlays both processes, grouped into separate Temperature and Pressure
plots; the merged Heating entries resolve to probe A for Sample 1 and probe B for
Sample 2 (drop-in replacements compared across objects).*

```python
from opensemantic.base.view import ProcessObjectView
from opensemantic.base.view._config import DashboardConfig

view = ProcessObjectView(
    objects=objects,        # list[Item]
    processes=processes,    # list[Process] (filtered to those with start+end
                            # time and >=1 DataTool whose data you can load)
    controllers=controllers,  # list[DataToolController], matched to process tools
    config=DashboardConfig(lang="en"),
    title="Process / Object Archive View",
)
view.servable()  # for panel serve
```

`DataToolView` and `ProcessObjectView` share their plot/unit/config machinery via
`BaseDataView`, and both support `embeddable=True` (exposing `sidebar_cards` /
`main_cards`) so a host app can combine them.

See [examples/process_dashboard.py](examples/process_dashboard.py) for a full
working example, and
[docs/generate_process_screenshots.py](docs/generate_process_screenshots.py) to
regenerate these screenshots.

## Installation

```bash
pip install opensemantic.base            # models only
pip install opensemantic.base[controller] # + aiosqlite, postgrest
pip install opensemantic.base[view]       # + panel, bokeh, panelini, pint
pip install opensemantic.base[export]     # + pandas, pint-pandas, openpyxl (data/plot export)
```

### Running the examples

```bash
git clone https://github.com/OpenSemanticWorld-Packages/opensemantic.base-python.git
cd opensemantic.base-python
pip install -e .
pip install opensemantic.base[controller] opensemantic.base[view] opensemantic.base[export]
pip install opensemantic.characteristics.quantitative   # typed characteristics
pip install nest-asyncio                                 # examples/datatool_dashboard.py
pip install PyJWT                                        # examples/downsample_demo.py (needs a database)

panel serve examples/datatool_dashboard.py --dev
```

## Testing

```bash
pytest tests/test_controller.py
```

PostgREST integration tests require a running [pgstack](https://github.com/opensemanticworld/pgstack) instance. To enable them:

1. Start pgstack: `docker compose -f docker-compose.yml -f docker-compose.example-tsdb.override.yml up -d`
2. Copy `tests/.env.example` to `tests/.env` and fill in `TEST_PGRST_URL` and `TEST_PGRST_JWT_SECRET`
3. Run tests - PostgREST tests are skipped unless both env vars are set
