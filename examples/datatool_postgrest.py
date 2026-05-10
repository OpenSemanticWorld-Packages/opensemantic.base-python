"""Example: DataToolController with PostgREST backend (auto-initialized).

The archive database is auto-initialized from the Database entity's
server.url field. Credentials are looked up from the oold credential
store. No manual PostgREST client setup needed.

Requires a running pgstack instance:
  https://github.com/opensemanticworld/pgstack

Setup:
  1. Start pgstack:
     docker compose -f docker-compose.yml \
       -f docker-compose.example-tsdb.override.yml up -d
  2. Run this example:
     python examples/datatool_postgrest.py
"""

import asyncio
import datetime
import os
from uuid import NAMESPACE_URL, uuid5

import jwt
from pydantic import SecretStr

from opensemantic import compute_scoped_uuid
from opensemantic.base.v1 import (
    Database,
    DatabaseServer,
    DataChannel,
    DataTool,
    DataToolController,
    PostgrestTimeSeriesDatabaseController,
)
from opensemantic.characteristics.quantitative.v1 import (
    Temperature,
    TemperatureUnit,
)
from opensemantic.core.v1 import Label

PGRST_URL = os.environ.get("TEST_PGRST_URL", "http://localhost:3000")
PGRST_SECRET = os.environ.get(
    "TEST_PGRST_JWT_SECRET", "reallyreallyreallyreallyverysafe"
)


async def main():
    from oold.backend.auth import TokenCredential, set_credential

    # -- 1. Register credentials for the PostgREST server --
    token = jwt.encode({"role": "api_user"}, PGRST_SECRET, algorithm="HS256")
    set_credential(TokenCredential(iri=PGRST_URL, token=SecretStr(token)))

    # -- 2. Create controller (PostgREST auto-initialized) --
    # Uses deterministic UUID so tool table persists across runs
    parent = uuid5(NAMESPACE_URL, "postgrest-example")
    ctrl = DataToolController(
        DataTool(
            uuid=parent,
            name="PostgRESTExample",
            label=[Label(text="PostgREST Example")],
            data_channels=[
                DataChannel(
                    uuid=str(compute_scoped_uuid(parent, "temp")),
                    osw_id="placeholder",
                    name="temperature",
                    label=[Label(text="Temperature")],
                    characteristic=Temperature.get_cls_iri(),
                    unit=TemperatureUnit.Celsius.value,
                ),
            ],
            storage_locations=[
                Database(
                    name="remote_archive",
                    label=[Label(text="Remote Archive")],
                    server=DatabaseServer(
                        name="pgstack",
                        label=[Label(text="pgstack")],
                        url=PGRST_URL,
                    ),
                )
            ],
        ),
        auto_archive=True,
    )

    print(f"Controller: {ctrl.name}")
    print(f"  Archive: {type(ctrl.archive_database).__name__}")

    # -- 3. Store data (tool table auto-created on first write) --
    now = datetime.datetime.now(datetime.timezone.utc)
    for i in range(5):
        await ctrl.store_channel_data(
            ctrl.StoreChannelDataParams(
                channel="temperature",
                value=Temperature(value=20.0 + i, unit=TemperatureUnit.Celsius),
                timestamp=now + datetime.timedelta(seconds=i),
            )
        )
    await ctrl.archive_database.flush_buffer()
    print("  Stored 5 values")

    # -- 4. Load (auto-typed, in Celsius) --
    print("\n--- Load from PostgREST ---")
    results = await ctrl.load_channel_data(
        ctrl.LoadChannelDataParams(channel="temperature")
    )
    for t in results:
        print(f"  {type(t).__name__}: {t.value:.1f} {t.unit}")

    # -- 5. Cleanup --
    tool_id = ctrl.get_osw_id()
    await ctrl.archive_database.delete_tool(
        PostgrestTimeSeriesDatabaseController.DeleteToolParams(tool_osw_id=tool_id)
    )
    print(f"\nCleaned up tool {tool_id}")


if __name__ == "__main__":
    asyncio.run(main())
