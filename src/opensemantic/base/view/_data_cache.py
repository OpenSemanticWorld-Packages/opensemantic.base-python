"""Time-slice-aware data cache for DataTool channel data.

Stores fetched time series data per channel and only fetches
uncached time slices on subsequent requests. Uses
controller.load_channel_data() for typed deserialization.
"""

import bisect
import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Tuple

_logger = logging.getLogger(__name__)


class ChannelDataCache:
    """Caches time series data per channel, fetches only missing slices.

    Parameters
    ----------
    enabled
        If False, every get_data call fetches from the backend directly.
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        # channel_uuid -> sorted list of ChannelDataPoint
        self._data: Dict[str, List[Any]] = {}
        # channel_uuid -> list of (start, end) covered intervals
        self._intervals: Dict[str, List[Tuple[dt.datetime, dt.datetime]]] = {}

    async def get_data(
        self,
        controller: Any,
        channel: Any,
        start: dt.datetime,
        end: Optional[dt.datetime] = None,
        limit: int = 10000,
        typed: bool = True,
        max_points: Optional[int] = None,
        bin_size: Optional[str] = None,
        method: Optional[str] = None,
        edge_anchors: Optional[bool] = None,
    ) -> List[Any]:
        """Return ChannelDataPoint list for [start, end].

        Fetches only uncached slices via controller.load_channel_data().

        Parameters
        ----------
        controller
            DataToolController instance with archive_database.
        channel
            DataChannel instance.
        start
            Start of the requested time range.
        end
            End of the requested time range. Defaults to now(UTC).
        limit
            Maximum rows per backend request.
        typed
            If True, values are typed Characteristic instances.
        max_points, bin_size, method, edge_anchors
            Server-side downsampling parameters. When any of max_points /
            bin_size / method is set the gap-merge cache is bypassed (a
            downsampled read is resolution-dependent and must not poison the
            full-resolution cache) and the backend is queried directly.

        Returns
        -------
            Sorted list of ChannelDataPoint (timestamp, channel, value).
        """
        if end is None:
            end = dt.datetime.now(dt.timezone.utc)

        ch_uuid = channel.uuid

        downsampling = max_points is not None or bin_size is not None or bool(method)

        if not self.enabled or downsampling:
            return await self._fetch(
                controller,
                channel,
                start,
                end,
                limit,
                typed,
                max_points,
                bin_size,
                method,
                edge_anchors,
            )

        covered = self._intervals.get(ch_uuid, [])
        gaps = _compute_gaps(start, end, covered)

        for gap_start, gap_end in gaps:
            rows = await self._fetch(
                controller, channel, gap_start, gap_end, limit, typed
            )
            self._merge_data(ch_uuid, rows)
            # Only mark as covered if data was found, to allow re-fetch
            # when data arrives later (e.g., OPC UA client not yet connected)
            if rows:
                self._merge_interval(ch_uuid, gap_start, gap_end)

        return self._query_range(ch_uuid, start, end)

    async def _fetch(
        self,
        controller: Any,
        channel: Any,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
        typed: bool,
        max_points: Optional[int] = None,
        bin_size: Optional[str] = None,
        method: Optional[str] = None,
        edge_anchors: Optional[bool] = None,
    ) -> List[Any]:
        """Fetch data via controller.load_channel_data()."""
        from opensemantic.base._controller_mixin import (
            DataToolMixin,
            DownsampleParams,
        )

        downsample = None
        if max_points is not None or bin_size is not None or method:
            downsample = DownsampleParams(
                max_points=max_points,
                bin_size=bin_size,
                method=method,
                edge_anchors=edge_anchors,
            )
        points = await controller.load_channel_data(
            DataToolMixin.LoadChannelDataParams(
                channel=channel,
                start=start,
                end=end,
                limit=limit,
                typed=typed,
                downsample=downsample,
            )
        )
        # Normalize timestamps to UTC-aware and sort
        for pt in points:
            ts = pt.timestamp
            if isinstance(ts, str):
                ts = dt.datetime.fromisoformat(ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            pt.timestamp = ts
        points.sort(key=lambda p: p.timestamp)
        return points

    def _merge_data(self, ch_uuid: str, rows: List[Any]) -> None:
        """Merge new data points into the sorted cache for a channel."""
        if ch_uuid not in self._data:
            self._data[ch_uuid] = []
        existing = self._data[ch_uuid]
        existing_ts = {p.timestamp for p in existing}
        new_rows = [p for p in rows if p.timestamp not in existing_ts]
        existing.extend(new_rows)
        existing.sort(key=lambda p: p.timestamp)

    def _merge_interval(
        self, ch_uuid: str, start: dt.datetime, end: dt.datetime
    ) -> None:
        """Merge a new covered interval, coalescing overlaps."""
        if ch_uuid not in self._intervals:
            self._intervals[ch_uuid] = []
        intervals = self._intervals[ch_uuid]
        intervals.append((start, end))
        intervals.sort(key=lambda iv: iv[0])
        merged = [intervals[0]]
        for s, e in intervals[1:]:
            if s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        self._intervals[ch_uuid] = merged

    def _query_range(
        self, ch_uuid: str, start: dt.datetime, end: dt.datetime
    ) -> List[Any]:
        """Return cached data points within [start, end]."""
        data = self._data.get(ch_uuid, [])
        if not data:
            return []
        timestamps = [p.timestamp for p in data]
        lo = bisect.bisect_left(timestamps, start)
        hi = bisect.bisect_right(timestamps, end)
        return data[lo:hi]

    def clear_cache(self) -> None:
        """Delete all cached data."""
        self._data.clear()
        self._intervals.clear()

    def clear_channel(self, channel_uuid: str) -> None:
        """Delete cached data for a specific channel."""
        self._data.pop(channel_uuid, None)
        self._intervals.pop(channel_uuid, None)


def _compute_gaps(
    start: dt.datetime,
    end: dt.datetime,
    covered: List[Tuple[dt.datetime, dt.datetime]],
) -> List[Tuple[dt.datetime, dt.datetime]]:
    """Compute uncovered sub-intervals within [start, end].

    Parameters
    ----------
    start, end
        Requested range.
    covered
        Sorted, non-overlapping list of covered (start, end) intervals.

    Returns
    -------
        List of (gap_start, gap_end) intervals that need fetching.
    """
    if not covered:
        return [(start, end)]

    gaps = []
    cursor = start
    for cov_start, cov_end in covered:
        if cov_start > cursor:
            gap_end = min(cov_start, end)
            if gap_end > cursor:
                gaps.append((cursor, gap_end))
        cursor = max(cursor, cov_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return gaps
