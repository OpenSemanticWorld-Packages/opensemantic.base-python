"""Tests for ``DatasetControllerMixin`` itself.

The externalization test next door models the same workflow by hand, so the
controller that downstream packages actually call had no test of its own. This
drives it directly, on both pydantic versions: rows to a frame and back, the
JSON and CSV files a distribution points at, and the failure paths.
"""

import io
import json
from types import SimpleNamespace
from typing import List, Optional

import pytest

import opensemantic.base
import opensemantic.base.v1
import opensemantic.core
import opensemantic.core.v1
from opensemantic.base._dataset_controller_mixin import (
    _declared_unit,
    _file_url,
    _unwrap,
    row_class_of,
)


def _modules(version):
    if version == "v1":
        return opensemantic.base.v1, opensemantic.core.v1
    return opensemantic.base, opensemantic.core


def _quantities(version):
    if version == "v1":
        from opensemantic.characteristics.quantitative.v1 import (
            QuantityValue,
            Temperature,
            Time,
        )
    else:
        from opensemantic.characteristics.quantitative import (
            QuantityValue,
            Temperature,
            Time,
        )
    return QuantityValue, Temperature, Time


def _row_base(version):
    if version == "v1":
        from pydantic.v1 import BaseModel
    else:
        from pydantic import BaseModel
    return BaseModel


@pytest.fixture(params=["v2", "v1"])
def ctx(request):
    """The models of one pydantic version, plus a dataset built on them."""
    version = request.param
    base_mod, core_mod = _modules(version)
    quantity_value, temperature_cls, time_cls = _quantities(version)
    row_base = _row_base(version)

    class Row(row_base):
        time: Optional[time_cls] = None
        temperature: Optional[temperature_cls] = None

    class TempDataset(base_mod.Dataset):
        data: Optional[List[Row]] = None

    class TempDatasetController(base_mod.DatasetControllerMixin, TempDataset):
        pass

    return SimpleNamespace(
        version=version,
        base=base_mod,
        core=core_mod,
        QuantityValue=quantity_value,
        Temperature=temperature_cls,
        Time=time_cls,
        row_base=row_base,
        Row=Row,
        cls=TempDatasetController,
    )


@pytest.fixture
def dataset(ctx):
    return ctx.cls(
        label=[ctx.core.Label(text="Cell 42")],
        data=[
            ctx.Row(time=ctx.Time(value=0.0), temperature=ctx.Temperature(value=300.0)),
            ctx.Row(
                time=ctx.Time(value=60.0), temperature=ctx.Temperature(value=301.5)
            ),
        ],
    )


def _in_grams(ctx, magnitude):
    """A QuantityValue in a unit the model does not declare as its default."""
    import pint

    quantity = pint.get_application_registry().Quantity(magnitude, "gram")
    return ctx.QuantityValue.from_pint(quantity, quantity_type=ctx.QuantityValue)


def _local_file(ctx, path):
    return ctx.core.LocalFile(
        label=[ctx.core.Label(text=path.name)], local_path=str(path)
    )


# -- frame conversions --------------------------------------------------------


def test_to_df_carries_the_declared_unit(dataset):
    df = dataset.to_df()
    assert list(df.columns) == ["time", "temperature"]
    assert str(df["temperature"].pint.units) == "kelvin"
    assert df["temperature"].pint.magnitude.tolist() == [300.0, 301.5]


def test_from_df_round_trips_the_rows(dataset):
    restored = type(dataset).from_df(dataset.to_df(), label=dataset.label)
    assert len(restored.data) == 2
    assert restored.data[1].temperature.to_pint().magnitude == pytest.approx(301.5)


def test_from_df_keeps_a_unit_the_model_does_not_declare(ctx):
    """``QuantityValue`` has no default unit, so the column's unit must travel."""

    quantity_value = ctx.QuantityValue

    class GenericRow(ctx.row_base):
        reading: Optional[quantity_value] = None

    class GenericDataset(ctx.base.Dataset):
        data: Optional[List[GenericRow]] = None

    class GenericController(ctx.base.DatasetControllerMixin, GenericDataset):
        pass

    source = GenericController(
        label=[ctx.core.Label(text="Generic")],
        data=[GenericRow(reading=_in_grams(ctx, 5.0))],
    )
    df = source.to_df()
    assert str(df["reading"].pint.units) == "gram"

    restored = GenericController.from_df(df, label=source.label)

    assert restored.data[0].reading.to_pint().to("gram").magnitude == pytest.approx(5.0)


def test_from_df_ignores_unknown_columns(dataset):
    df = dataset.to_df()
    df["not_a_field"] = [1, 2]
    restored = type(dataset).from_df(df, label=dataset.label)
    assert len(restored.data) == 2


def test_from_df_reports_a_column_it_cannot_build(dataset):
    import pandas as pd

    df = pd.DataFrame({"temperature": ["warm", "cold"]})
    with pytest.raises(TypeError, match="temperature"):
        type(dataset).from_df(df, label=dataset.label)


def test_to_df_without_a_row_model():
    class Plain(opensemantic.base.DatasetControllerMixin, opensemantic.base.Dataset):
        pass

    plain = Plain(label=[opensemantic.core.Label(text="Plain")])
    with pytest.raises(TypeError, match="no row model"):
        plain.to_df()


# -- serialization ------------------------------------------------------------


def test_serialize_data_json(dataset):
    entries = json.loads(dataset.serialize_data("json").decode("utf-8"))
    assert len(entries) == 2
    assert entries[0]["temperature"]["value"] == 300.0


def test_serialize_data_csv_has_a_unit_header(dataset):
    header = dataset.serialize_data("csv").decode("utf-8").splitlines()[:2]
    assert "temperature" in header[0]
    assert "kelvin" in header[1]


def test_serialize_data_rejects_an_unknown_format(dataset):
    with pytest.raises(ValueError, match="Unsupported format"):
        dataset.serialize_data("parquet")


# -- distributions ------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_externalize_then_materialize(ctx, dataset, fmt, tmp_path):
    target = tmp_path / f"rows.{fmt}"
    expected = [row.temperature.to_pint() for row in dataset.data]

    distribution = dataset.externalize_data(_local_file(ctx, target), fmt=fmt)

    assert dataset.data is None
    assert target.exists()
    assert distribution.byte_size == target.stat().st_size
    assert distribution.media_type == (
        "text/csv" if fmt == "csv" else "application/json"
    )

    rows = dataset.materialize_data()

    assert len(rows) == 2
    for row, want in zip(rows, expected):
        assert row.temperature.to_pint().to(want.units).magnitude == pytest.approx(
            want.magnitude
        )


def test_externalize_can_keep_the_inline_rows(ctx, dataset, tmp_path):
    dataset.externalize_data(_local_file(ctx, tmp_path / "rows.json"), keep_data=True)
    assert dataset.data is not None


def test_externalize_rejects_a_file_without_a_controller(ctx, dataset):
    plain = ctx.core.File(label=[ctx.core.Label(text="No content access")])
    with pytest.raises(ValueError, match="No file controller"):
        dataset.externalize_data(plain)


def test_materialize_skips_a_distribution_it_cannot_read(
    ctx, dataset, tmp_path, caplog
):
    """The docstring promises the next distribution is tried."""
    dataset.externalize_data(_local_file(ctx, tmp_path / "rows.json"), keep_data=True)
    broken = ctx.base.Distribution(
        file=_local_file(ctx, tmp_path / "missing.json"),
        media_type="application/json",
    )
    dataset.distributions = [broken] + list(dataset.distributions)
    dataset.data = None

    rows = dataset.materialize_data()

    assert len(rows) == 2
    assert "could not be read" in caplog.text


def test_materialize_without_a_readable_distribution(dataset):
    dataset.distributions = None
    with pytest.raises(ValueError, match="no distribution"):
        dataset.materialize_data()


def test_materialize_from_a_url_distribution(ctx, dataset, monkeypatch):
    """A distribution that carries only a URL resolves on the dataset's layer."""
    payload = dataset.serialize_data("json")

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False

    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *a, **k: _Response(payload), raising=True
    )

    dataset.distributions = [
        ctx.base.Distribution(
            download_url="https://example.org/rows.json",
            media_type="application/json",
        )
    ]
    dataset.data = None

    assert len(dataset.materialize_data()) == 2


# -- helpers ------------------------------------------------------------------


def test_unwrap_keeps_a_union_of_several_members():
    from typing import Dict, Union

    assert _unwrap(Optional[List[int]]) is int
    assert _unwrap(Optional[Dict[str, float]]) is float
    assert _unwrap(Optional[Union[int, str]]) == Optional[Union[int, str]]


def test_declared_unit_of_a_field_that_is_not_a_model():
    from datetime import datetime

    class Row(_row_base("v2")):
        stamp: Optional[datetime] = None
        name: Optional[str] = None

    assert _declared_unit(Row, "stamp") is None
    assert _declared_unit(Row, "name") is None


def test_row_class_of_a_dataset_without_rows():
    assert row_class_of(opensemantic.base.Dataset) is None


def test_file_url_reports_an_unreadable_attribute(caplog):
    import logging

    class Broken:
        @property
        def download_url(self):
            raise OSError("not connected")

        url = "https://example.org/a.bin"

    with caplog.at_level(
        logging.INFO, logger="opensemantic.base._dataset_controller_mixin"
    ):
        assert _file_url(Broken()) == "https://example.org/a.bin"
    assert "not readable" in caplog.text


# -- rows that are not quantities ---------------------------------------------


@pytest.fixture
def mixed_ctx(ctx):
    """A dataset whose rows mix a quantity with plain fields."""
    from datetime import datetime

    temperature_cls = ctx.Temperature

    class MixedRow(ctx.row_base):
        temperature: Optional[temperature_cls] = None
        stamp: Optional[datetime] = None
        note: Optional[str] = None

    class MixedDataset(ctx.base.Dataset):
        data: Optional[List[MixedRow]] = None

    class MixedController(ctx.base.DatasetControllerMixin, MixedDataset):
        pass

    ctx.MixedRow = MixedRow
    ctx.MixedController = MixedController
    return ctx


def _mixed_dataset(ctx):
    from datetime import datetime

    return ctx.MixedController(
        label=[ctx.core.Label(text="Mixed")],
        data=[
            ctx.MixedRow(
                temperature=ctx.Temperature(value=300.0),
                stamp=datetime(2026, 1, 1, 12, 0, 0),
                note="start",
            ),
            ctx.MixedRow(
                temperature=ctx.Temperature(value=301.0),
                stamp=datetime(2026, 1, 1, 12, 1, 0),
                note="end",
            ),
        ],
    )


def test_from_df_keeps_a_field_that_is_not_a_quantity(mixed_ctx):
    dataset = _mixed_dataset(mixed_ctx)

    restored = type(dataset).from_df(dataset.to_df(), label=dataset.label)

    assert restored.data[0].note == "start"
    assert restored.data[1].stamp == dataset.data[1].stamp


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_mixed_rows_survive_a_file_round_trip(mixed_ctx, fmt, tmp_path):
    dataset = _mixed_dataset(mixed_ctx)
    target = tmp_path / f"mixed.{fmt}"

    dataset.externalize_data(_local_file(mixed_ctx, target), fmt=fmt)
    rows = dataset.materialize_data()

    assert [row.note for row in rows] == ["start", "end"]


def test_serialize_data_json_keeps_a_datetime(mixed_ctx):
    dataset = _mixed_dataset(mixed_ctx)

    entries = json.loads(dataset.serialize_data("json").decode("utf-8"))

    assert entries[0]["stamp"].startswith("2026-01-01")


# -- failures are reported with their cause -----------------------------------


def test_materialize_names_the_column_it_could_not_build(ctx, dataset, tmp_path):
    """The TypeError from from_df must not be hidden behind 'no readable file'."""
    target = tmp_path / "broken.csv"
    dataset.externalize_data(_local_file(ctx, target), fmt="csv")
    target.write_text("temperature\nkelvin\n0,warm\n", encoding="utf-8")

    with pytest.raises(ValueError, match="no distribution") as caught:
        dataset.materialize_data()

    assert caught.value.__cause__ is not None


def test_materialize_without_a_row_model():
    class Plain(opensemantic.base.DatasetControllerMixin, opensemantic.base.Dataset):
        pass

    plain = Plain(label=[opensemantic.core.Label(text="Plain")])
    with pytest.raises(TypeError, match="no row model"):
        plain.materialize_data()


def test_materialize_passes_controller_kwargs_on(ctx, dataset, tmp_path, monkeypatch):
    """The session a wiki file needs travels with the call."""
    seen = {}
    import opensemantic.base._dataset_controller_mixin as module

    original = module.get_file_controller

    def _spy(file, **kwargs):
        seen.update(kwargs)
        return original(file, **kwargs)

    monkeypatch.setattr(module, "get_file_controller", _spy)
    dataset.externalize_data(_local_file(ctx, tmp_path / "rows.json"))

    dataset.materialize_data(osw="SESSION")

    assert seen.get("osw") == "SESSION"


def test_unwrap_sees_through_an_annotated_type():
    from typing import Annotated

    assert _unwrap(Optional[Annotated[int, "metres"]]) is int


def test_file_url_survives_a_property_that_raises_anything(caplog):
    class Broken:
        @property
        def download_url(self):
            raise RuntimeError("the wiki said no")

        url = None
        access_url = None

    assert _file_url(Broken()) is None
    assert "the wiki said no" in caplog.text
