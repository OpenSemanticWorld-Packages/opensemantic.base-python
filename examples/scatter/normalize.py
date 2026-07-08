"""Time-grid normalization utilities.

Aligns multi-channel time-series data onto a common time grid so that
each row has values for all channels simultaneously.  The result is a
pandas DataFrame suitable for scatter-plot analysis.
"""

import datetime as dt
import logging
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_logger = logging.getLogger(__name__)


def _dt_to_epoch(timestamps: List[dt.datetime]) -> np.ndarray:
    """Convert datetime list to float64 epoch-seconds array."""
    return np.array([t.timestamp() for t in timestamps], dtype=np.float64)


def _epoch_to_dt(epochs: np.ndarray) -> List[dt.datetime]:
    """Convert float64 epoch-seconds array back to UTC datetimes."""
    return [dt.datetime.fromtimestamp(e, tz=dt.timezone.utc) for e in epochs]


def build_common_time_grid(
    channel_series: Dict[str, Tuple[List[dt.datetime], List[float]]],
    method: str = "union",
    step_seconds: Optional[float] = None,
) -> np.ndarray:
    """Build a sorted epoch-seconds array forming the common time grid.

    Parameters
    ----------
    channel_series
        ``{column_name: (timestamps, values)}`` for each channel.
    method
        ``"union"`` -- merge all unique timestamps from every channel.
        ``"fixed_step"`` -- evenly spaced from earliest to latest at
        *step_seconds* intervals.
    step_seconds
        Required when *method* is ``"fixed_step"``.

    Returns
    -------
    np.ndarray
        Sorted float64 epoch-seconds grid.
    """
    all_epochs: List[np.ndarray] = []
    for ts_list, _ in channel_series.values():
        if ts_list:
            all_epochs.append(_dt_to_epoch(ts_list))

    if not all_epochs:
        return np.array([], dtype=np.float64)

    if method == "fixed_step":
        if step_seconds is None or step_seconds <= 0:
            raise ValueError("step_seconds must be > 0 for fixed_step grid")
        combined = np.concatenate(all_epochs)
        start, end = combined.min(), combined.max()
        return np.arange(start, end + step_seconds, step_seconds)

    # "union" (default)
    combined = np.concatenate(all_epochs)
    return np.unique(combined)


def interpolate_to_grid(
    timestamps: np.ndarray,
    values: np.ndarray,
    grid: np.ndarray,
    method: str = "linear",
) -> np.ndarray:
    """Interpolate one channel's values onto the common time grid.

    Parameters
    ----------
    timestamps
        Sorted epoch-seconds for this channel's samples.
    values
        Corresponding numeric values.
    grid
        The common time grid (epoch-seconds, sorted).
    method
        ``"previous"`` -- zero-order hold (last known value).
        ``"linear"`` -- linear interpolation.
        ``"spline"`` -- cubic spline (falls back to linear if scipy is
        unavailable).

    Returns
    -------
    np.ndarray
        Interpolated values on *grid*.  ``NaN`` for grid points outside
        the channel's data range.
    """
    if len(timestamps) == 0:
        return np.full(len(grid), np.nan)

    if method == "previous":
        idx = np.searchsorted(timestamps, grid, side="right") - 1
        result = np.full(len(grid), np.nan)
        valid = idx >= 0
        result[valid] = values[idx[valid]]
        # Mark points after the last sample as NaN only if they exceed
        # the last timestamp (searchsorted already handles this).
        beyond = grid > timestamps[-1]
        result[beyond] = np.nan
        return result

    if method == "spline":
        try:
            from scipy.interpolate import CubicSpline

            if len(timestamps) >= 2:
                cs = CubicSpline(timestamps, values, extrapolate=False)
                return cs(grid)
        except ImportError:
            _logger.warning(
                "scipy not available, falling back to linear interpolation"
            )

    # "linear" (default and spline fallback)
    result = np.interp(grid, timestamps, values, left=np.nan, right=np.nan)
    return result


def channels_to_dataframe(
    channel_series: Dict[str, Tuple[List[dt.datetime], List[float]]],
    grid_method: str = "union",
    interp_method: str = "linear",
    step_seconds: Optional[float] = None,
) -> pd.DataFrame:
    """Normalize multi-channel data into a single DataFrame.

    Parameters
    ----------
    channel_series
        ``{column_name: (timestamps, values)}`` -- each channel's raw
        time-series as parallel lists.
    grid_method
        How to build the common time grid (``"union"`` or
        ``"fixed_step"``).
    interp_method
        How to interpolate each channel onto the grid (``"previous"``,
        ``"linear"``, or ``"spline"``).
    step_seconds
        Grid spacing for ``"fixed_step"`` mode.

    Returns
    -------
    pd.DataFrame
        Columns: ``"timestamp"`` (UTC datetime) plus one column per
        channel.  Rows with any ``NaN`` are dropped.
    """
    if not channel_series:
        return pd.DataFrame()

    grid = build_common_time_grid(channel_series, grid_method, step_seconds)
    if len(grid) == 0:
        return pd.DataFrame()

    data: Dict[str, np.ndarray] = {}
    for col_name, (ts_list, val_list) in channel_series.items():
        ts_arr = _dt_to_epoch(ts_list)
        val_arr = np.asarray(val_list, dtype=np.float64)
        # Sort by timestamp (channels may not be pre-sorted)
        order = np.argsort(ts_arr)
        ts_arr = ts_arr[order]
        val_arr = val_arr[order]
        data[col_name] = interpolate_to_grid(ts_arr, val_arr, grid, interp_method)

    df = pd.DataFrame(data)
    df.insert(0, "timestamp", _epoch_to_dt(grid))
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df
