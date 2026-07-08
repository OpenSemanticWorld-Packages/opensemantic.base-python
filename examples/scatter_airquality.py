"""Example: Air quality scatter / correlation analysis.

Simulates a 24-hour air-quality monitoring station with seven channels
(temperature, humidity, pressure, particle density, CO2, NOx, ozone)
that have realistic inter-correlations, then opens a ScatterView for
interactive scatter-plot exploration.

Run with::

    panel serve examples/scatter_airquality.py --dev
"""

import asyncio
import datetime as dt
import math
import random
from typing import List, Optional
from uuid import NAMESPACE_URL, uuid5

import nest_asyncio
import panel as pn

from opensemantic import compute_scoped_uuid
from opensemantic.base import StoreChannelDataBulkParams, StoreChannelSeriesParams
from opensemantic.base._demo_data import already_seeded
from opensemantic.base.v1 import (
    Database,
    DataChannel,
    DataTool,
    DataToolController,
)
from opensemantic.base.view._config import PlotConfig
from opensemantic.characteristics.quantitative.v1 import (
    Characteristic,
    ForcePerAreaUnit,
    Pressure,
    RelativeHumidity,
    Temperature,
    TemperatureUnit,
)
from opensemantic.core.v1 import Label

from scatter.scatter_view import ScatterConfig, ScatterDashboardConfig, ScatterView

pn.extension()

# ---------------------------------------------------------------------------
# Custom characteristics (unitless scalars for this example)
# ---------------------------------------------------------------------------


class ParticleDensity(Characteristic):
    class Config:
        schema_extra = {
            "uuid": "10000000-0000-0000-0000-000000000001",
            "title": "ParticleDensity",
        }

    type: Optional[list] = ["Category:OSW10000000000000000000000000000001"]
    value: Optional[float] = None


class CO2Concentration(Characteristic):
    class Config:
        schema_extra = {
            "uuid": "10000000-0000-0000-0000-000000000002",
            "title": "CO2Concentration",
        }

    type: Optional[list] = ["Category:OSW10000000000000000000000000000002"]
    value: Optional[float] = None


class NOxConcentration(Characteristic):
    class Config:
        schema_extra = {
            "uuid": "10000000-0000-0000-0000-000000000003",
            "title": "NOxConcentration",
        }

    type: Optional[list] = ["Category:OSW10000000000000000000000000000003"]
    value: Optional[float] = None


class OzoneConcentration(Characteristic):
    class Config:
        schema_extra = {
            "uuid": "10000000-0000-0000-0000-000000000004",
            "title": "OzoneConcentration",
        }

    type: Optional[list] = ["Category:OSW10000000000000000000000000000004"]
    value: Optional[float] = None


# Register custom types so the view can resolve them
try:
    from oold.model import _types

    _types[ParticleDensity.get_cls_iri()] = ParticleDensity
    _types[CO2Concentration.get_cls_iri()] = CO2Concentration
    _types[NOxConcentration.get_cls_iri()] = NOxConcentration
    _types[OzoneConcentration.get_cls_iri()] = OzoneConcentration
except ImportError:
    pass


# ---------------------------------------------------------------------------
# Simulated data generator
# ---------------------------------------------------------------------------

N_POINTS = 1440  # 24 hours at 1-minute intervals
TWO_PI = 2.0 * math.pi


def _generate_air_quality_values(
    n_points: int,
) -> dict:
    """Pre-compute correlated air quality values for all channels.

    Returns a dict mapping channel name -> list of Characteristic values.
    """
    temps: List = []
    hums: List = []
    pres_vals: List = []
    particles: List = []
    co2s: List = []
    noxs: List = []
    ozones: List = []

    for i in range(n_points):
        hour = i / 60.0

        temp = 20.0 + 8.0 * math.sin(TWO_PI * (hour - 6) / 24) + random.gauss(0, 0.3)
        hum = max(
            10.0,
            min(100.0, 75.0 - 1.8 * (temp - 20.0) + random.gauss(0, 3.0)),
        )
        pres = 1013.0 + 4.0 * math.sin(TWO_PI * hour / 48) + random.gauss(0, 0.5)
        part = max(
            0.0,
            25.0 + 0.8 * (temp - 15.0) - 0.15 * hum + random.gauss(0, 5.0),
        )
        co2 = max(
            350.0,
            410.0
            + 40.0 * math.sin(TWO_PI * (hour - 8) / 24)
            + 1.5 * temp
            + random.gauss(0, 10.0),
        )
        nox = max(0.0, 15.0 + 0.25 * (co2 - 410.0) + random.gauss(0, 4.0))
        ozone = max(
            0.0,
            50.0
            + 15.0 * math.sin(TWO_PI * (hour - 14) / 24)
            - 0.4 * nox
            + random.gauss(0, 3.0),
        )

        temps.append(Temperature(value=temp, unit=TemperatureUnit.Celsius))
        hums.append(RelativeHumidity(value=hum))
        pres_vals.append(Pressure(value=pres, unit=ForcePerAreaUnit.hecto_pascal))
        particles.append(ParticleDensity(value=part))
        co2s.append(CO2Concentration(value=co2))
        noxs.append(NOxConcentration(value=nox))
        ozones.append(OzoneConcentration(value=ozone))

    return {
        "temperature": temps,
        "humidity": hums,
        "pressure": pres_vals,
        "particle_density": particles,
        "co2": co2s,
        "nox": noxs,
        "ozone": ozones,
    }


# ---------------------------------------------------------------------------
# Data setup
# ---------------------------------------------------------------------------


async def setup_data():
    """Create an air quality station DataTool and populate with 24h of data."""

    parent = uuid5(NAMESPACE_URL, "AirQuality-Station-1")
    tool = DataTool(
        uuid=parent,
        name="AirQualityStation",
        label=[
            Label(text="Air Quality Station", lang="en"),
            Label(text="Luftqualitaetsstation", lang="de"),
        ],
        data_channels=[
            DataChannel(
                uuid=str(compute_scoped_uuid(parent, "temp")),
                osw_id="placeholder",
                name="temperature",
                label=[
                    Label(text="Temperature", lang="en"),
                    Label(text="Temperatur", lang="de"),
                ],
                characteristic=Temperature.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent, "humidity")),
                osw_id="placeholder",
                name="humidity",
                label=[
                    Label(text="Humidity", lang="en"),
                    Label(text="Luftfeuchtigkeit", lang="de"),
                ],
                characteristic=RelativeHumidity.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent, "pressure")),
                osw_id="placeholder",
                name="pressure",
                label=[
                    Label(text="Pressure", lang="en"),
                    Label(text="Druck", lang="de"),
                ],
                characteristic=Pressure.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent, "particles")),
                osw_id="placeholder",
                name="particle_density",
                label=[
                    Label(text="Particle Density (ug/m3)", lang="en"),
                    Label(text="Feinstaubdichte (ug/m3)", lang="de"),
                ],
                characteristic=ParticleDensity.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent, "co2")),
                osw_id="placeholder",
                name="co2",
                label=[
                    Label(text="CO2 (ppm)", lang="en"),
                    Label(text="CO2 (ppm)", lang="de"),
                ],
                characteristic=CO2Concentration.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent, "nox")),
                osw_id="placeholder",
                name="nox",
                label=[
                    Label(text="NOx (ppb)", lang="en"),
                    Label(text="NOx (ppb)", lang="de"),
                ],
                characteristic=NOxConcentration.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent, "ozone")),
                osw_id="placeholder",
                name="ozone",
                label=[
                    Label(text="Ozone (ppb)", lang="en"),
                    Label(text="Ozon (ppb)", lang="de"),
                ],
                characteristic=OzoneConcentration.get_cls_iri(),
            ),
        ],
        storage_locations=[
            Database(name="airquality_db", label=[Label(text="AirQuality DB")]),
        ],
    )

    ctrl = DataToolController(tool, auto_archive=True)

    # Skip data generation if already populated (fast re-serve)
    if await already_seeded(ctrl):
        return ctrl

    now = dt.datetime.now(dt.timezone.utc)
    base_ts = now - dt.timedelta(minutes=N_POINTS)
    timestamps = [base_ts + dt.timedelta(minutes=i) for i in range(N_POINTS)]

    values = _generate_air_quality_values(N_POINTS)

    series = [
        StoreChannelSeriesParams(
            channel=ch_name,
            timestamps=timestamps,
            values=ch_values,
        )
        for ch_name, ch_values in values.items()
    ]
    await ctrl.store_channel_data_bulk(
        StoreChannelDataBulkParams(series=series, ensure_tool=True)
    )

    return ctrl


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

nest_asyncio.apply()
controller = asyncio.run(setup_data())

config = ScatterDashboardConfig(
    lang="en",
    plot=PlotConfig(auto_fetch=True, row_limit=10000),
    scatter=ScatterConfig(
        interp_method="linear",
        grid_method="union",
        # Try adding computed columns in the UI, for example:
        #   thermal_stress = Temperature * Humidity / 100
    ),
)

view = ScatterView(
    controllers=[controller],
    config=config,
    title="Air Quality Scatter Analysis",
)

view.servable()
