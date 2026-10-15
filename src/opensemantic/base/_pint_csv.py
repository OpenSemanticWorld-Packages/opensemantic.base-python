"""CSV with a unit header, for tables whose columns are not all quantities.

pint-pandas writes units into a second header row, which is what makes a CSV
of typed values round-trip. Its own ``dequantify`` / ``quantify`` require
*every* column to carry a unit, and a table row carries plain members too - a
label, a count, a timestamp. The two functions here keep the unit row for the
columns that have one and pass the rest through unchanged.

The form written is::

    ,temperature,city
    unit,kelvin,
    0,300.0,Cambridge

so the second row names the units, empty where a column has none.
"""

import io
from typing import Any

UNIT_ROW_MARKER = "unit"
"""First cell of the unit row, and how a reader recognizes this form."""


def dequantify(frame: Any) -> Any:
    """Move each column's unit into a second header level.

    A column without a unit keeps its values under an empty unit, where
    ``frame.pint.dequantify()`` raises.
    """
    import pandas as pd

    columns = []
    values = []
    for name in frame.columns:
        series = frame[name]
        if str(series.dtype).startswith("pint"):
            columns.append((str(name), str(series.pint.units)))
            values.append(series.pint.magnitude)
        else:
            columns.append((str(name), ""))
            values.append(series)
    out = pd.concat(values, axis=1) if values else frame.copy()
    out.columns = pd.MultiIndex.from_tuples(columns, names=[None, UNIT_ROW_MARKER])
    return out


def quantify(frame: Any) -> Any:
    """Turn the second header level back into column units.

    The inverse of :func:`dequantify`: a column whose unit cell is empty stays
    as it is, which is what ``frame.pint.quantify()`` cannot express.
    """
    import pandas as pd

    out = {}
    for name, unit in frame.columns:
        series = frame[(name, unit)]
        text = "" if unit is None else str(unit).strip()
        if text and not text.lower().startswith("unnamed"):
            out[name] = pd.Series(series.to_numpy(), dtype=f"pint[{text}]", name=name)
        else:
            out[name] = pd.Series(series.to_numpy(), name=name)
    return pd.DataFrame(out)


def has_unit_row(payload: bytes) -> bool:
    """Whether a CSV carries the unit header this module writes."""
    lines = payload.splitlines()
    if len(lines) < 2:
        return False
    first_cell = lines[1].split(b",")[0].strip().strip(b'"')
    return first_cell.decode("utf-8", "replace").lower() == UNIT_ROW_MARKER


def read_csv(payload: bytes) -> Any:
    """A CSV as a frame, with or without the unit header.

    A CSV from anywhere else has one header row and no units; reading it as
    the two-row form would silently turn its first data row into part of the
    header.
    """
    import pandas as pd

    if has_unit_row(payload):
        return quantify(pd.read_csv(io.BytesIO(payload), header=[0, 1], index_col=0))
    return pd.read_csv(io.BytesIO(payload))


def to_csv(frame: Any) -> bytes:
    """A frame as CSV bytes, with the unit header."""
    return dequantify(frame).to_csv().encode("utf-8")
