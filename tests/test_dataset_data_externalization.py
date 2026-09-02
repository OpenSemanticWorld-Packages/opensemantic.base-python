"""Externalize a Dataset's inline ``data`` payload into a file and back.

Follows the data-bundling principle of
https://github.com/OO-LD/oold-schema/issues/109: the same logical content is
either carried inline on the dataset (mode A) or materialized into a file that
a ``Distribution`` describes (mode C). Converting between the two is lossless,
so a payload that is uploaded and later read back is unchanged.

``Dataset.data`` is typed by its schema ("the type of data IS the schema"), so
the generic ``Dataset`` does not accept an arbitrary payload; a dataset that
carries one narrows the field in a subclass, here to a plain dict.
"""

import json
from pathlib import Path
from typing import Optional

import pytest

import opensemantic.base
import opensemantic.base.v1
import opensemantic.core
import opensemantic.core.v1

PAYLOAD = {
    "rows": [
        {"test_time": {"value": 0.1}, "voltage": {"value": 3.7}},
        {"test_time": {"value": 0.2}, "voltage": {"value": 3.6}},
    ],
    "meta": {"cell": "cell42"},
}

MEDIA_TYPE = "application/json"

MODULES = [
    pytest.param(opensemantic.base, opensemantic.core, id="v2"),
    pytest.param(opensemantic.base.v1, opensemantic.core.v1, id="v1"),
]


def _rows_dataset_cls(base_mod):
    """A Dataset whose schema narrows ``data`` to a dict payload."""

    class RowsDataset(base_mod.Dataset):
        data: Optional[dict] = None

    return RowsDataset


def _dump(model) -> str:
    return (
        model.model_dump_json()
        if hasattr(model, "model_dump_json")
        else model.json()  # pydantic v1
    )


def _load(cls, raw: str):
    return (
        cls.model_validate_json(raw)
        if hasattr(cls, "model_validate_json")
        else cls.parse_raw(raw)  # pydantic v1
    )


def _externalize(dataset, base_mod, core_mod, target: Path):
    """Emulate an upload: write ``data`` to a file, attach it, clear ``data``.

    Returns the created file entity so a caller can put it into a store.
    """
    payload = json.dumps(dataset.data).encode("utf-8")
    target.write_bytes(payload)
    local_file = core_mod.LocalFile(
        label=[core_mod.Label(text=target.name)],
        local_path=str(target),
    )
    dataset.distributions = [
        base_mod.Distribution(
            file=local_file,
            media_type=MEDIA_TYPE,
            byte_size=len(payload),
        )
    ]
    dataset.data = None
    return local_file


def _materialize(dataset):
    """The inverse: read the attached file back into ``data``."""
    distribution = dataset.distributions[0]
    stored = Path(distribution.file.local_path)
    dataset.data = json.loads(stored.read_text(encoding="utf-8"))
    dataset.distributions = None


@pytest.mark.parametrize("base_mod, core_mod", MODULES)
def test_generic_dataset_rejects_a_dict_payload(base_mod, core_mod):
    """The payload type comes from the schema, not from the generic Dataset."""
    with pytest.raises(Exception):
        base_mod.Dataset(label=[core_mod.Label(text="Cell 42")], data=PAYLOAD)


@pytest.mark.parametrize("base_mod, core_mod", MODULES)
def test_data_is_externalized_into_an_attached_local_file(base_mod, core_mod, tmp_path):
    dataset = _rows_dataset_cls(base_mod)(
        label=[core_mod.Label(text="Cell 42 cycling")],
        data=PAYLOAD,
    )
    assert dataset.data == PAYLOAD
    assert not dataset.distributions

    _externalize(dataset, base_mod, core_mod, tmp_path / "cell42.data.json")

    # The payload no longer sits on the dataset; it lives in the file that the
    # distribution describes.
    assert dataset.data is None
    assert len(dataset.distributions) == 1

    distribution = dataset.distributions[0]
    assert isinstance(distribution.file, core_mod.LocalFile)
    assert distribution.media_type == MEDIA_TYPE

    stored = Path(distribution.file.local_path)
    assert stored.exists()
    assert distribution.byte_size == stored.stat().st_size
    assert json.loads(stored.read_text(encoding="utf-8")) == PAYLOAD


@pytest.mark.parametrize("base_mod, core_mod", MODULES)
def test_file_round_trips_back_into_data(base_mod, core_mod, tmp_path):
    """File -> Dataset.data restores the payload unchanged (lossless)."""
    dataset = _rows_dataset_cls(base_mod)(
        label=[core_mod.Label(text="Cell 42 cycling")],
        data=PAYLOAD,
    )
    _externalize(dataset, base_mod, core_mod, tmp_path / "cell42.data.json")
    assert dataset.data is None

    _materialize(dataset)

    assert dataset.data == PAYLOAD
    assert not dataset.distributions


@pytest.fixture
def resolver_registry():
    """Isolate the global oold resolver registry from other tests."""
    from oold.backend import interface

    saved = dict(interface._resolvers)
    yield
    interface._resolvers.clear()
    interface._resolvers.update(saved)


@pytest.mark.parametrize("base_mod, core_mod", MODULES)
def test_file_round_trips_back_into_data_across_serialization(
    base_mod, core_mod, tmp_path, resolver_registry
):
    """The payload is recoverable from a serialized dataset.

    The distribution keeps only a node reference to the file entity, so a
    loader has to dereference it before the payload can be read back.
    """
    from oold.backend.document_store import SimpleDictDocumentStore
    from oold.backend.interface import SetResolverParam, StoreParam, set_resolver

    cls = _rows_dataset_cls(base_mod)
    dataset = cls(
        label=[core_mod.Label(text="Cell 42 cycling")],
        data=PAYLOAD,
    )
    local_file = _externalize(
        dataset, base_mod, core_mod, tmp_path / "cell42.data.json"
    )

    # The file entity is stored on its own, like an uploaded file would be.
    store = SimpleDictDocumentStore()
    store.store(StoreParam(nodes={local_file.get_iri(): local_file}))
    set_resolver(SetResolverParam(iri="Item", resolver=store))

    # The serialized dataset references the file instead of inlining the payload.
    raw = _dump(dataset)
    assert "test_time" not in raw
    assert local_file.get_iri() in raw

    restored = _load(cls, raw)
    assert restored.data is None

    _materialize(restored)

    assert restored.data == PAYLOAD
