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

## Installation

```bash
pip install opensemantic.base            # models only
pip install opensemantic.base[controller] # + aiosqlite, postgrest
```

## Testing

```bash
pytest tests/test_controller.py
```

PostgREST integration tests require a running [pgstack](https://github.com/opensemanticworld/pgstack) instance. To enable them:

1. Start pgstack: `docker compose -f docker-compose.yml -f docker-compose.example-tsdb.override.yml up -d`
2. Copy `tests/.env.example` to `tests/.env` and fill in `TEST_PGRST_URL` and `TEST_PGRST_JWT_SECRET`
3. Run tests - PostgREST tests are skipped unless both env vars are set
