"""Controller for datasets that carry their rows inline or in a distribution.

A ``Dataset`` holds its rows in ``data`` and/or points at them through a
``Distribution``. This controller owns the conversions between those forms and
a pandas DataFrame:

* :meth:`to_df` / :meth:`from_df` - inline rows to and from a pint-pandas frame,
  one column per row field, each carrying the field's declared unit.
* :meth:`materialize_data` - fill ``data`` from a distribution's file.
* :meth:`externalize_data` - write ``data`` to a file, describe it as a
  distribution and drop the inline copy.

File access goes through the ``opensemantic.core`` file controllers, so a
distribution backed by a local file, an HTTP URL or a wiki page is read the same
way and this module needs no knowledge of the storage behind it.

The row values are typed characteristics; their unit handling is used through
duck typing (``to_pint``, ``get_pint_ureg_compatible_str``) so this module does
not depend on the characteristics package.
"""

import io
import json
import logging
import typing
from collections import abc
from typing import Any, List, Optional

from opensemantic.core import get_file_controller

_logger = logging.getLogger(__name__)

JSON_MEDIA_TYPE = "application/json"
CSV_MEDIA_TYPE = "text/csv"


def _fields_of(model_cls) -> dict:
    """Field map of a model, for either pydantic version."""
    return getattr(model_cls, "model_fields", None) or model_cls.__fields__


def _unwrap(annotation):
    """Strip Optional/List wrappers down to the contained model class.

    A mapping unwraps to its *value* type, and a union of more than one real
    member is left as it is: there is no single class behind it, and taking the
    first argument turned ``Optional[Dict[str, X]]`` into ``str``.
    """
    while True:
        origin = typing.get_origin(annotation)
        args = [
            a for a in typing.get_args(annotation) if a is not type(None)
        ]  # noqa: E721
        if not args:
            return annotation
        if isinstance(origin, type) and issubclass(origin, abc.Mapping):
            annotation = args[-1]
            continue
        if len(args) > 1:
            return annotation
        annotation = args[0]


def _field_type(model_cls, name):
    """The model class behind a field, for either pydantic version."""
    field = _fields_of(model_cls)[name]
    annotation = getattr(field, "annotation", None)
    if annotation is None:
        return getattr(field, "type_", None)
    return _unwrap(annotation)


def _is_model_cls(obj) -> bool:
    """Whether ``obj`` is a model class of either pydantic version."""
    return isinstance(obj, type) and (
        hasattr(obj, "model_fields") or hasattr(obj, "__fields__")
    )


def row_class_of(dataset_cls):
    """The row model behind a dataset's ``data`` field, or ``None``.

    ``data`` is a list of typed rows on a concrete dataset; on the generic
    ``Dataset`` it carries no row model, in which case there is nothing to
    convert.
    """
    fields = _fields_of(dataset_cls)
    if "data" not in fields:
        return None
    row_cls = _field_type(dataset_cls, "data")
    return row_cls if _is_model_cls(row_cls) else None


def _value_fields(row_cls) -> List[str]:
    """Data-column fields of a row model (all but the category marker)."""
    return [name for name in _fields_of(row_cls) if name != "type"]


def _declared_unit(row_cls, field: str) -> Optional[str]:
    """Pint-compatible unit string declared as the field's default, if any.

    A field that is not a model - ``timestamp: Optional[datetime]``, a plain
    string - carries no unit, which is what makes the unit-less column path
    reachable.
    """
    value_cls = _field_type(row_cls, field)
    if not _is_model_cls(value_cls):
        return None
    unit_field = _fields_of(value_cls).get("unit")
    default = getattr(unit_field, "default", None) if unit_field else None
    name = getattr(default, "name", None)
    if name is None:
        return None
    to_pint_str = getattr(value_cls, "get_pint_ureg_compatible_str", None)
    return to_pint_str(name) if callable(to_pint_str) else name


def _column_unit(row_cls, field: str, values: List[Any]) -> Optional[str]:
    """Unit for a column: the declared default, else the first value's own."""
    declared = _declared_unit(row_cls, field)
    if declared:
        return declared
    for value in values:
        if value is not None and hasattr(value, "to_pint"):
            return str(value.to_pint().units)
    return None


def _remote_file_cls(dataset):
    """``RemoteFile`` of the dataset's own pydantic layer.

    The v1 and v2 models are not interchangeable: handing v1 labels to the v2
    class fails validation, so the layer the dataset belongs to decides.
    """
    from pydantic.v1 import BaseModel as BaseModelV1

    if isinstance(dataset, BaseModelV1):
        from opensemantic.core.v1 import RemoteFile
    else:
        from opensemantic.core import RemoteFile
    return RemoteFile


def _row_value(dataset_cls, field: str, value_cls, magnitude, declared, unit):
    """One cell of a frame as the value a row field takes.

    With a declared unit the column was converted to it, so the magnitude alone
    is unambiguous. Without one the column's own unit has to travel with the
    value, or the dataset reads back in the model's default unit instead.
    """
    if _is_model_cls(value_cls) and isinstance(magnitude, value_cls):
        return magnitude
    try:
        number = float(magnitude)
    except (TypeError, ValueError) as error:
        raise TypeError(
            f"{dataset_cls.__name__}.{field}: cannot build a row value from "
            f"{magnitude!r} - the column holds neither a number nor a typed value"
        ) from error
    if declared is not None or unit is None:
        return {"value": number}
    from_pint = getattr(value_cls, "from_pint", None)
    if not callable(from_pint):
        raise TypeError(
            f"{dataset_cls.__name__}.{field}: the column carries {unit}, but "
            f"{getattr(value_cls, '__name__', value_cls)} declares no unit and "
            "cannot be built from a pint quantity"
        )
    import pint

    return from_pint(
        pint.get_application_registry().Quantity(number, unit),
        quantity_type=value_cls,
    )


class _NamedBytesIO(io.BytesIO):
    """A buffer that carries a file name.

    A controller may derive metadata from the stream it is handed - the wiki one
    takes the uploaded page's title from ``file.name`` - and a plain BytesIO has
    none.
    """

    def __init__(self, data: bytes, name: str):
        super().__init__(data)
        self.name = name


def _file_url(controller) -> Optional[str]:
    """The URL a file controller publishes, if it has one.

    A remote file carries its own address; a local one does not, and reading its
    ``url`` may need a live connection. A connection failure is reported and
    then treated as "no URL", so the distribution still describes the file.
    """
    for attribute in ("download_url", "url", "access_url"):
        try:
            value = getattr(controller, attribute, None)
        except (AttributeError, ValueError, OSError) as error:
            # a property that needs a connection, or a file that has no address
            _logger.info(
                "%s.%s is not readable: %s", type(controller).__name__, attribute, error
            )
            continue
        if isinstance(value, str) and value:
            return value
    return None


class DatasetControllerMixin:
    """Conversions between a dataset's inline rows, a DataFrame and files."""

    # -- DataFrame --------------------------------------------------------

    def to_df(self):
        """The dataset's rows as a pint-pandas DataFrame.

        One column per row field, converted to that field's declared unit;
        missing optional values become NA.
        """
        import pandas as pd

        row_cls = row_class_of(type(self))
        rows = self.data or []
        if row_cls is None:
            raise TypeError(
                f"{type(self).__name__}.data carries no row model to convert"
            )

        columns = {}
        for field in _value_fields(row_cls):
            values = [getattr(row, field, None) for row in rows]
            unit = _column_unit(row_cls, field, values)
            if unit is None:
                columns[field] = pd.Series(values, name=field, dtype="object")
                continue
            magnitudes = [
                value.to_pint().to(unit).magnitude if value is not None else None
                for value in values
            ]
            columns[field] = pd.Series(magnitudes, dtype=f"pint[{unit}]", name=field)
        return pd.DataFrame(columns)

    @classmethod
    def from_df(cls, df, **kwargs):
        """Build a dataset from a pint-pandas frame.

        Columns matching a row field are stored in that field's declared unit.
        A field without a declared unit keeps the column's own unit, so a
        ``to_df`` / ``from_df`` round trip does not reinterpret magnitudes.
        Unknown columns are ignored; remaining ``kwargs`` are passed to the
        dataset (``label`` is required by the model).
        """
        import pandas as pd

        row_cls = row_class_of(cls)
        if row_cls is None:
            raise TypeError(f"{cls.__name__}.data carries no row model to build")

        known = set(_value_fields(row_cls))
        columns = [name for name in df.columns if name in known]

        prepared = {}
        for field in columns:
            series = df[field]
            declared = _declared_unit(row_cls, field)
            is_pint = hasattr(series, "pint")
            if declared is not None and is_pint:
                series = series.pint.to(declared)
            unit = str(series.pint.units) if is_pint else None
            prepared[field] = (
                series.pint.magnitude if is_pint else series,
                _field_type(row_cls, field),
                declared,
                unit,
            )

        rows = []
        for i in range(len(df)):
            values = {}
            for field in columns:
                magnitudes, value_cls, declared, unit = prepared[field]
                magnitude = magnitudes.iloc[i]
                if magnitude is None or (
                    not isinstance(magnitude, str) and pd.isna(magnitude)
                ):
                    continue
                values[field] = _row_value(
                    cls, field, value_cls, magnitude, declared, unit
                )
            rows.append(row_cls(**values))
        return cls(data=rows, **kwargs)

    # -- Distributions ----------------------------------------------------

    def _distribution_controller(self, distribution, **controller_kwargs):
        """File controller for a distribution's file, or ``None``.

        ``controller_kwargs`` reach the controller, which is how a caller hands
        it what it needs to read the file - a wiki session, for instance. A
        controller that does not declare them ignores them.
        """
        file = getattr(distribution, "file", None)
        if file is not None:
            return get_file_controller(file, **controller_kwargs)
        url = getattr(distribution, "download_url", None) or getattr(
            distribution, "access_url", None
        )
        if not url:
            return None
        remote_cls = _remote_file_cls(self)
        return get_file_controller(
            remote_cls(label=self.label, download_url=url), **controller_kwargs
        )

    def materialize_data(self, distribution=None, **controller_kwargs):
        """Fill ``data`` from a distribution's file and return the rows.

        Without an explicit distribution the first one that yields rows is
        used: a distribution whose file cannot be read or parsed is reported
        and the next one is tried. The file is parsed by its ``media_type``
        (JSON or CSV).

        ``controller_kwargs`` are handed to the file controller, which is how
        a caller passes the session or credentials the file needs.
        """
        import pandas as pd

        distributions = (
            [distribution]
            if distribution is not None
            else list(getattr(self, "distributions", None) or [])
        )
        row_cls = row_class_of(type(self))
        if row_cls is None:
            raise TypeError(f"{type(self).__name__}.data carries no row model to fill")
        for dist in distributions:
            controller = self._distribution_controller(dist, **controller_kwargs)
            if controller is None:
                continue
            try:
                with controller.get() as handle:
                    payload = handle.read()
                if isinstance(payload, str):
                    payload = payload.encode("utf-8")

                media_type = getattr(dist, "media_type", None) or JSON_MEDIA_TYPE
                if media_type == CSV_MEDIA_TYPE:
                    frame = pd.read_csv(
                        io.BytesIO(payload), header=[0, 1], index_col=0
                    ).pint.quantify(level=-1)
                    self.data = type(self).from_df(frame, label=self.label).data
                else:
                    entries = json.loads(payload.decode("utf-8"))
                    if isinstance(entries, dict):
                        entries = entries.get("data", [])
                    self.data = [
                        entry if isinstance(entry, row_cls) else row_cls(**entry)
                        for entry in entries
                    ]
            except Exception as error:  # noqa: BLE001 - the next one may work
                _logger.warning(
                    "%s: distribution %s could not be read: %s",
                    type(self).__name__,
                    getattr(dist, "download_url", None) or dist,
                    error,
                )
                continue
            return self.data
        raise ValueError(
            f"{type(self).__name__} has no distribution with a readable file"
        )

    def externalize_data(self, file, fmt: str = "json", keep_data: bool = False):
        """Write ``data`` to ``file``, attach a distribution and clear ``data``.

        ``file`` is a file entity or file controller that accepts the bytes; the
        distribution records its media type and size, and the file's URL when it
        has one, so the rows stay reachable once the dataset is reloaded and the
        file is only a reference. Set ``keep_data`` to leave the inline rows in
        place.
        """
        payload = self.serialize_data(fmt)
        controller = get_file_controller(file)
        if controller is None:
            raise ValueError(f"No file controller for {type(file).__name__}")
        name = getattr(controller, "name", None) or f"data.{fmt}"
        if not str(name).endswith(f".{fmt}"):
            name = f"{name}.{fmt}"
        controller.put(_NamedBytesIO(payload, str(name)))

        distribution_cls = _field_type(type(self), "distributions")
        distribution = distribution_cls(
            file=controller,
            media_type=CSV_MEDIA_TYPE if fmt == "csv" else JSON_MEDIA_TYPE,
            byte_size=len(payload),
            download_url=_file_url(controller),
        )
        self.distributions = list(self.distributions or []) + [distribution]
        if not keep_data:
            self.data = None
        return distribution

    def serialize_data(self, fmt: str = "json") -> bytes:
        """The inline rows as JSON or CSV bytes.

        JSON keeps the typed values, so it round-trips without loss. CSV carries
        a two-row ``(field, unit)`` header written by pint-pandas.
        """
        if fmt == "csv":
            return self.to_df().pint.dequantify().to_csv().encode("utf-8")
        if fmt != "json":
            raise ValueError(f"Unsupported format: {fmt}")
        rows = self.data or []
        # v2 models carry both .model_dump() and a deprecated .dict(), so the
        # v2 form is tested first; its json mode is what keeps bytes, Decimal
        # and datetime lossless rather than leaving them to json's default=str
        dump = [
            (
                row.model_dump(exclude_none=True, mode="json")
                if hasattr(row, "model_dump")
                else row.dict(exclude_none=True)
            )
            for row in rows
        ]
        return json.dumps(dump, default=str).encode("utf-8")
