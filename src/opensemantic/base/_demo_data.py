"""Seeding helpers for demos, benchmarks and tests.

Generate a synthetic time series and store it through the high-level
``DataToolController`` bulk API (``store_channel_data_bulk``), so demo, bench
and test seeding share one code path instead of each duplicating the low-level
``create_tool`` + ``write_tool_channel_raw`` loop.
"""

import datetime as dt
from typing import Any, Callable, List, Optional

from opensemantic.base import (
    LoadChannelDataParams,
    StoreChannelDataBulkParams,
    StoreChannelSeriesParams,
)


async def seed_channel_series(
    controller: Any,
    *,
    n_points: int,
    base_ts: dt.datetime,
    value_fn: Callable[[Any, int], Any],
    channels: Optional[List[Any]] = None,
    seconds_step: float = 1.0,
    chunk_size: int = 5000,
) -> int:
    """Seed ``n_points`` per channel via the high-level bulk store.

    ``value_fn(channel, i)`` returns the value stored for ``channel`` at index
    ``i`` (a raw dict/scalar or a Characteristic instance); the timestamp is
    ``base_ts + i * seconds_step`` seconds. ``channels`` defaults to all of the
    controller's channels. The tool table is created if missing. Returns the
    number of points written.
    """
    if channels is None:
        channels = controller.get_all_channels()
    timestamps = [
        base_ts + dt.timedelta(seconds=i * seconds_step) for i in range(n_points)
    ]
    series = [
        StoreChannelSeriesParams(
            channel=ch,
            timestamps=timestamps,
            values=[value_fn(ch, i) for i in range(n_points)],
        )
        for ch in channels
    ]
    return await controller.store_channel_data_bulk(
        StoreChannelDataBulkParams(series=series, chunk_size=chunk_size)
    )


async def already_seeded(controller: Any) -> bool:
    """True if the controller's tool exists and has at least one stored row."""
    try:
        tools = await controller.archive_database.get_tools_list()
    except Exception:
        return False
    if controller.get_osw_id() not in tools:
        return False
    rows = await controller.load_channel_data(
        LoadChannelDataParams(limit=1, typed=False)
    )
    return bool(rows)
