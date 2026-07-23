"""Example: DataTool archive view.

Creates sample DataTools with channels, stores test data,
then launches a Panelini view for browsing and plotting.

Run with: panel serve examples/datatool_dashboard.py --dev
"""

import asyncio
import datetime as dt
import random
from pathlib import Path
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
)
from opensemantic.base.view import (
    DataToolPlotControlsConfig,
    DataToolView,
    DataToolViewConfig,
    UrlConfigMode,
)
from opensemantic.characteristics.quantitative.v1 import (
    Characteristic,
    ForcePerAreaUnit,
    Pressure,
    Temperature,
    TemperatureUnit,
)
from opensemantic.core.v1 import Label

pn.extension()

DB_PATH = Path(__file__).parent / "dashboard_example.sqlite"


# -- Custom characteristic for text (status messages) --


class StatusMessage(Characteristic):
    class Config:
        schema_extra = {
            "uuid": "00000000-0000-0000-0000-000000000001",
            "title": "StatusMessage",
        }

    type: Optional[list] = ["Category:OSW00000000000000000000000000000001"]
    value: Optional[str] = None


# -- Custom composite characteristic --


class AirQuality(Characteristic):
    class Config:
        schema_extra = {
            "uuid": "00000000-0000-0000-0000-000000000002",
            "title": "AirQuality",
        }

    type: Optional[list] = ["Category:OSW00000000000000000000000000000002"]
    temperature: Optional[Temperature] = None
    pressure: Optional[Pressure] = None


# Register custom types so resolve_characteristic_class can find them
try:
    from oold.model import _types

    _types[StatusMessage.get_cls_iri()] = StatusMessage
    _types[AirQuality.get_cls_iri()] = AirQuality
except ImportError:
    pass


async def setup_data():
    """Create sample DataTools and populate with test data."""

    parent1 = uuid5(NAMESPACE_URL, "Sensor-A")
    tool1 = DataTool(
        uuid=parent1,
        name="SensorA",
        label=[Label(text="Sensor A", lang="en"), Label(text="Sensor A", lang="de")],
        data_channels=[
            DataChannel(
                uuid=str(compute_scoped_uuid(parent1, "temp")),
                osw_id="placeholder",
                name="temperature",
                label=[
                    Label(text="Temperature", lang="en"),
                    Label(text="Temperatur", lang="de"),
                ],
                characteristic=Temperature.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent1, "pressure")),
                osw_id="placeholder",
                name="pressure",
                label=[
                    Label(text="Pressure", lang="en"),
                    Label(text="Druck", lang="de"),
                ],
                characteristic=Pressure.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent1, "status")),
                osw_id="placeholder",
                name="status",
                label=[
                    Label(text="Status", lang="en"),
                    Label(text="Status", lang="de"),
                ],
                characteristic=StatusMessage.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent1, "air")),
                osw_id="placeholder",
                name="air_quality",
                label=[
                    Label(text="Air Quality", lang="en"),
                    Label(text="Luftqualitaet", lang="de"),
                ],
                characteristic=AirQuality.get_cls_iri(),
            ),
        ],
        storage_locations=[
            Database(name="example_db", label=[Label(text="Example DB")]),
        ],
    )

    parent2 = uuid5(NAMESPACE_URL, "Sensor-B")
    tool2 = DataTool(
        uuid=parent2,
        name="SensorB",
        label=[Label(text="Sensor B", lang="en"), Label(text="Sensor B", lang="de")],
        data_channels=[
            DataChannel(
                uuid=str(compute_scoped_uuid(parent2, "temp")),
                osw_id="placeholder",
                name="temperature",
                label=[
                    Label(text="Temperature", lang="en"),
                    Label(text="Temperatur", lang="de"),
                ],
                characteristic=Temperature.get_cls_iri(),
            ),
        ],
        storage_locations=[
            Database(name="example_db2", label=[Label(text="Example DB 2")]),
        ],
    )

    ctrl1 = DataToolController(tool1, auto_archive=True)
    ctrl2 = DataToolController(tool2, auto_archive=True)

    # Store some sample data
    now = dt.datetime.now(dt.timezone.utc)
    for i in range(100):
        ts = now - dt.timedelta(minutes=100 - i)
        await ctrl1.store_channel_data(
            DataToolController.StoreChannelDataParams(
                channel="temperature",
                value=Temperature(
                    value=20.0 + 5.0 * (i / 100) + random.uniform(-0.5, 0.5),
                    unit=TemperatureUnit.Celsius,
                ),
                timestamp=ts,
            )
        )
        await ctrl1.store_channel_data(
            DataToolController.StoreChannelDataParams(
                channel="pressure",
                value=Pressure(
                    value=1013.0 + random.uniform(-2, 2),
                    unit=ForcePerAreaUnit.hecto_pascal,
                ),
                timestamp=ts,
            )
        )
        await ctrl2.store_channel_data(
            DataToolController.StoreChannelDataParams(
                channel="temperature",
                value=Temperature(
                    value=22.0 + 3.0 * (i / 100) + random.uniform(-0.3, 0.3),
                    unit=TemperatureUnit.Celsius,
                ),
                timestamp=ts,
            )
        )
        # Text channel: status messages every 3 points
        if i % 3 == 0:
            messages = [
                "OK",
                "Warning: high temp",
                "Calibrating",
                "Idle",
                "Error: sensor",
            ]
            await ctrl1.store_channel_data(
                DataToolController.StoreChannelDataParams(
                    channel="status",
                    value=StatusMessage(value=random.choice(messages)),
                    timestamp=ts,
                )
            )
        # Composite channel: air quality
        await ctrl1.store_channel_data(
            DataToolController.StoreChannelDataParams(
                channel="air_quality",
                value=AirQuality(
                    temperature=Temperature(
                        value=20.0 + 5.0 * (i / 100) + random.uniform(-0.2, 0.2),
                        unit=TemperatureUnit.Celsius,
                    ),
                    pressure=Pressure(
                        value=1013.0 + random.uniform(-1, 1),
                        unit=ForcePerAreaUnit.hecto_pascal,
                    ),
                ),
                timestamp=ts,
            )
        )

    return [ctrl1, ctrl2]


# Panel serve runs inside Tornado's event loop, so use nest_asyncio
nest_asyncio.apply()

controllers = asyncio.run(setup_data())

config = DataToolViewConfig(
    lang="en",
    plot=DataToolPlotControlsConfig(auto_fetch=True, row_limit=10000),
)

# Persist the full dashboard state in the URL. COMPRESSED_BASE64 keeps the URL
# short even though the native tree source is large (many channels); selection,
# units, time range and plot options all round-trip. (PLAIN_KEYS suits small
# configs - see the composed demo - not a big channel tree.)
view = DataToolView(
    controllers=controllers,
    config=config,
    url_sync=True,
    url_mode=UrlConfigMode.COMPRESSED_BASE64,
    title="DataTool Archive View",
)

view.servable()
