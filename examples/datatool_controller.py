"""Example: DataToolController with local SQLite archiving.

Demonstrates the generic DataTool controller without any protocol
dependency (no OPC UA, MQTT, etc.):
1. Create a DataTool with channels and a storage location
2. Wrap in DataToolController (auto-inits archive DB)
3. Store channel data by name and by instance (raw + typed)
4. Load channel data back (raw + typed)
"""

import asyncio
import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from opensemantic import compute_scoped_uuid
from opensemantic.base.v1 import (
    Database,
    DataChannel,
    DataTool,
    DataToolController,
)
from opensemantic.characteristics.quantitative.v1 import (
    Temperature,
    TemperatureUnit,
)
from opensemantic.core.v1 import Label

DB_PATH = Path(__file__).parent / "example_archive.sqlite"
PARENT_UUID = uuid5(NAMESPACE_URL, "ExampleDataTool")


async def main():
    # -- 1. Create a pure data model --
    data_tool = DataTool(
        uuid=PARENT_UUID,
        name="ExampleDataTool",
        label=[Label(text="Example Data Tool")],
        data_channels=[
            DataChannel(
                uuid=str(compute_scoped_uuid(PARENT_UUID, "temperature")),
                osw_id="placeholder",
                name="temperature",
                label=[Label(text="Temperature Sensor")],
                characteristic=Temperature.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(PARENT_UUID, "pressure")),
                osw_id="placeholder",
                name="pressure",
                label=[Label(text="Pressure Sensor")],
            ),
        ],
        storage_locations=[
            Database(name="archive", label=[Label(text="Archive")]),
        ],
    )

    # -- 2. Wrap in controller --
    # DB is auto-initialized from storage_locations[0]
    controller = DataToolController(data_tool, auto_archive=True)

    print(f"Controller: {controller.name}")
    print(f"  Channels: {[ch.name for ch in controller.get_all_channels()]}")
    print(f"  Archive: {type(controller.archive_database).__name__}")

    now = datetime.datetime.now(datetime.timezone.utc)

    # -- 3. Store data by channel name --
    print("\n--- Store by name ---")
    await controller.store_channel_data(
        DataToolController.StoreChannelDataParams(
            channel="temperature",
            value=Temperature(value=295.0, unit=TemperatureUnit.kelvin),
            timestamp=now,
        )
    )
    await controller.store_channel_data(
        DataToolController.StoreChannelDataParams(
            channel="temperature",
            value=Temperature(value=296.0, unit=TemperatureUnit.kelvin),
            timestamp=now + datetime.timedelta(seconds=1),
        )
    )
    print("Stored 2 Temperature values")

    # -- 4. Store raw data by channel instance --
    print("\n--- Store raw by instance ---")
    pressure_ch = controller.get_channel_by_name("pressure")
    await controller.store_channel_data(
        DataToolController.StoreChannelDataParams(
            channel=pressure_ch,
            value={"value": 1013.25, "unit": "hPa"},
            timestamp=now,
        )
    )
    print("Stored 1 raw pressure value")

    # -- 5. Load typed data (auto-resolved from channel characteristic) --
    print("\n--- Load typed (auto) ---")
    results = await controller.load_channel_data(
        DataToolController.LoadChannelDataParams(
            channel="temperature",
            # No target_schema needed - resolved from channel's characteristic IRI
        )
    )
    for t in results:
        print(f"  {type(t).__name__}: {t.value} {t.unit}")

    # -- 6. Load typed with explicit target_schema (override) --
    print("\n--- Load typed (explicit) ---")
    results = await controller.load_channel_data(
        DataToolController.LoadChannelDataParams(
            channel="temperature",
            target_schema=Temperature,
        )
    )
    for t in results:
        print(f"  Temperature: {t.value} {t.unit}")

    # -- 7. Load raw data --
    print("\n--- Load raw ---")
    raw = await controller.load_channel_data(
        DataToolController.LoadChannelDataParams(channel="pressure")
    )
    for d in raw:
        print(f"  Pressure: {d}")

    # -- Cleanup --
    await controller.stop()
    db_path = controller.archive_database.db_path
    if Path(db_path).exists():
        Path(db_path).unlink()
    print(f"\nCleaned up {db_path}")


if __name__ == "__main__":
    asyncio.run(main())
