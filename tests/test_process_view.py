"""Tests for the process/object-centered dashboard logic (_process_utils).

Pure-logic tests (no Panel/Bokeh): build in-memory objects, controllers and
processes, then exercise concrete-tree building, aggregation, and resolution.
All relations are matched offline by IRI.
"""

import datetime as dt
import logging
from uuid import NAMESPACE_URL, uuid5

import pytest

from opensemantic import compute_scoped_uuid
from opensemantic.base.v1 import (
    Database,
    DataChannel,
    DataTool,
    DataToolController,
    Process,
)
from opensemantic.base.view._process_utils import (
    build_concrete_tree,
    build_object_tree_source,
    build_process_tree_source,
    derive_aggregated_channels,
    entity_iri,
    get_selected_keys,
    iri_refs,
    resolve_aggregated_channel,
)
from opensemantic.characteristics.quantitative.v1 import Pressure, Temperature
from opensemantic.core.v1 import Item, Label

START = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
END = dt.datetime(2024, 1, 1, 1, tzinfo=dt.timezone.utc)

# Two distinct process-type IRIs for grouping tests.
EVAC_TYPE = "Category:OSW000000000000000000000000000000e1"
HEAT_TYPE = "Category:OSW000000000000000000000000000000e2"


# -- Fixtures --


def _make_tool(name, channels):
    u = uuid5(NAMESPACE_URL, name)
    return DataTool(
        uuid=u,
        name=name,
        label=[Label(text=name)],
        data_channels=[
            DataChannel(
                uuid=str(compute_scoped_uuid(u, cn)),
                osw_id="placeholder",
                name=cn,
                label=[Label(text=cn)],
                characteristic=char.get_cls_iri(),
            )
            for cn, char in channels
        ],
        storage_locations=[Database(name=name + "db", label=[Label(text="db")])],
    )


@pytest.fixture
def controllers():
    # Two DataTools of the same (default) datatool type. Both have a `temp`
    # (Temperature) channel; pressures differ in name (pressure_x / pressure_y).
    t1 = _make_tool("ToolA", [("temp", Temperature), ("pressure_x", Pressure)])
    t2 = _make_tool("ToolB", [("temp", Temperature), ("pressure_y", Pressure)])
    return [
        DataToolController(t1, auto_archive=True),
        DataToolController(t2, auto_archive=True),
    ]


@pytest.fixture
def objects():
    return [
        Item(uuid=uuid5(NAMESPACE_URL, "S1"), label=[Label(text="Sample 1")]),
        Item(uuid=uuid5(NAMESPACE_URL, "S2"), label=[Label(text="Sample 2")]),
    ]


def _make_process(name, sample, tools, type_iri=EVAC_TYPE, start=START, end=END):
    return Process(
        uuid=uuid5(NAMESPACE_URL, name),
        label=[Label(text=name)],
        type=[type_iri],
        input=[sample],
        tool=list(tools),
        start_date_time=start,
        end_date_time=end,
    )


# -- IRI matching --


class TestIriMatching:
    def test_process_input_and_tool_refs(self, objects, controllers):
        s1 = objects[0]
        proc = _make_process("P1", s1, [controllers[0]])
        assert entity_iri(s1) in iri_refs(proc, "input")
        assert entity_iri(controllers[0]) in iri_refs(proc, "tool")

    def test_iri_refs_empty_for_missing(self, objects):
        s1 = objects[0]
        assert iri_refs(s1, "tool") == []


# -- Concrete tree --


class TestConcreteTree:
    def test_basic_qualification(self, objects, controllers):
        s1, s2 = objects
        procs = [
            _make_process("P1", s1, controllers),
            _make_process("P2", s2, [controllers[0]]),
        ]
        tree = build_concrete_tree(objects, procs, controllers)
        assert len(tree[entity_iri(s1)]["processes"]) == 1
        assert len(tree[entity_iri(s2)]["processes"]) == 1
        # S1's process has both controllers attached
        assert len(tree[entity_iri(s1)]["processes"][0]["controllers"]) == 2

    def test_skip_missing_end_time(self, objects, controllers, caplog):
        s1 = objects[0]
        proc = _make_process("Pbad", s1, [controllers[0]], end=None)
        with caplog.at_level(logging.WARNING):
            tree = build_concrete_tree([s1], [proc], controllers)
        assert tree[entity_iri(s1)]["processes"] == []
        assert any("missing start/end" in r.message for r in caplog.records)

    def test_skip_missing_start_time(self, objects, controllers):
        s1 = objects[0]
        proc = _make_process("Pbad", s1, [controllers[0]], start=None)
        tree = build_concrete_tree([s1], [proc], controllers)
        assert tree[entity_iri(s1)]["processes"] == []

    def test_non_datatool_tool_skipped(self, objects, controllers, caplog):
        """A tool that is not among the provided controllers is skipped."""
        s1 = objects[0]
        stranger = Item(uuid=uuid5(NAMESPACE_URL, "Hammer"), label=[Label(text="H")])
        # Process tools = [non-controller, real controller]
        proc = _make_process("P1", s1, [stranger, controllers[0]])
        with caplog.at_level(logging.INFO):
            tree = build_concrete_tree([s1], [proc], controllers)
        pe = tree[entity_iri(s1)]["processes"]
        assert len(pe) == 1
        # Only the real controller remains
        assert pe[0]["controllers"] == [controllers[0]]
        assert any("not a DataTool controller" in r.message for r in caplog.records)

    def test_process_with_no_datatool_skipped(self, objects, controllers, caplog):
        s1 = objects[0]
        stranger = Item(uuid=uuid5(NAMESPACE_URL, "Hammer"), label=[Label(text="H")])
        proc = _make_process("P1", s1, [stranger])
        with caplog.at_level(logging.WARNING):
            tree = build_concrete_tree([s1], [proc], controllers)
        assert tree[entity_iri(s1)]["processes"] == []
        assert any("no DataTool attached" in r.message for r in caplog.records)

    def test_object_not_input_excluded(self, objects, controllers):
        s1, s2 = objects
        proc = _make_process("P1", s1, controllers)  # only s1 is input
        tree = build_concrete_tree(objects, [proc], controllers)
        assert len(tree[entity_iri(s1)]["processes"]) == 1
        assert len(tree[entity_iri(s2)]["processes"]) == 0


# -- Aggregation --


def _by_name(grp, name):
    return [a for a in grp["channels"].values() if a["channel_name"] == name]


class TestAggregation:
    def test_copresent_splits_into_per_instance(self, objects, controllers):
        """Two same-type datatools running together -> per-instance entries."""
        s1 = objects[0]
        proc = _make_process("P1", s1, controllers)  # both tools, both have temp
        tree = build_concrete_tree([s1], [proc], controllers)
        agg = derive_aggregated_channels(tree)
        grp = next(iter(agg.values()))
        temps = _by_name(grp, "temp")
        assert len(temps) == 2  # one entry per co-present datatool
        assert all(not t["aggregated"] for t in temps)
        assert all(t["n_channels"] == 1 for t in temps)
        # distinct datatool instances
        iris = {t["datatool_iris"][0] for t in temps}
        assert iris == {entity_iri(c) for c in controllers}

    def test_distinct_name_merges_free(self, objects, controllers):
        """A channel present on only one datatool stays a single merged entry."""
        s1 = objects[0]
        proc = _make_process("P1", s1, controllers)
        tree = build_concrete_tree([s1], [proc], controllers)
        agg = derive_aggregated_channels(tree)
        grp = next(iter(agg.values()))
        px = _by_name(grp, "pressure_x")
        assert len(px) == 1
        assert px[0]["aggregated"] is True
        assert px[0]["n_channels"] == 1

    def test_dropin_merge_across_runs(self, objects, controllers):
        """Two same-type datatools never run together -> one merged entry."""
        s1 = objects[0]
        procs = [
            _make_process("Run1", s1, [controllers[0]]),
            _make_process("Run2", s1, [controllers[1]]),
        ]
        tree = build_concrete_tree([s1], procs, controllers)
        agg = derive_aggregated_channels(tree)
        grp = next(iter(agg.values()))
        temps = _by_name(grp, "temp")
        assert len(temps) == 1  # merged drop-in entry
        merged = temps[0]
        assert merged["aggregated"] is True
        assert merged["n_channels"] == 2
        assert set(merged["datatool_iris"]) == {entity_iri(c) for c in controllers}
        assert "[2 channels]" in merged["label"]
        assert len(merged["aggregated_channels"]) == 2

    def test_grouped_by_process_type(self, objects, controllers):
        s1 = objects[0]
        procs = [
            _make_process("Pevac", s1, [controllers[0]], type_iri=EVAC_TYPE),
            _make_process("Pheat", s1, [controllers[0]], type_iri=HEAT_TYPE),
        ]
        tree = build_concrete_tree([s1], procs, controllers)
        agg = derive_aggregated_channels(tree)
        assert len(agg) == 2  # two process types

    def test_aggregated_channel_fields(self, objects, controllers):
        s1 = objects[0]
        proc = _make_process("P1", s1, [controllers[0]])
        tree = build_concrete_tree([s1], [proc], controllers)
        agg = derive_aggregated_channels(tree)
        grp = next(iter(agg.values()))
        temp = _by_name(grp, "temp")[0]
        assert temp["characteristic_iri"] == Temperature.get_cls_iri()
        assert temp["value_type"] == "quantity"
        assert "temp" in temp["label"]


# -- Resolution --


class TestResolution:
    def test_per_instance_resolves_only_its_datatool(self, objects, controllers):
        """A per-instance entry resolves only to its own datatool's channel."""
        s1 = objects[0]
        proc = _make_process("P1", s1, controllers)  # both tools co-present
        tree = build_concrete_tree([s1], [proc], controllers)
        agg = derive_aggregated_channels(tree)
        grp = next(iter(agg.values()))
        entry_a = [
            t
            for t in _by_name(grp, "temp")
            if t["datatool_iris"] == [entity_iri(controllers[0])]
        ][0]
        resolved = resolve_aggregated_channel(tree[entity_iri(s1)], entry_a)
        assert len(resolved) == 1
        ctrl, ch, _proc = resolved[0]
        assert entity_iri(ctrl) == entity_iri(controllers[0])
        assert ch.name == "temp"

    def test_per_instance_fans_out_across_runs(self, objects, controllers):
        """A co-present datatool used in two runs -> one trace per run."""
        s1 = objects[0]
        procs = [
            _make_process("Run1", s1, controllers),
            _make_process("Run2", s1, controllers),
        ]
        tree = build_concrete_tree([s1], procs, controllers)
        agg = derive_aggregated_channels(tree)
        grp = next(iter(agg.values()))
        entry_a = [
            t
            for t in _by_name(grp, "temp")
            if t["datatool_iris"] == [entity_iri(controllers[0])]
        ][0]
        resolved = resolve_aggregated_channel(tree[entity_iri(s1)], entry_a)
        assert len(resolved) == 2  # two runs, same datatool
        assert len({entity_iri(p) for _c, _ch, p in resolved}) == 2

    def test_merged_resolves_all_free_instances(self, objects, controllers):
        """A merged drop-in entry resolves to each datatool in its own run."""
        s1 = objects[0]
        procs = [
            _make_process("Run1", s1, [controllers[0]]),
            _make_process("Run2", s1, [controllers[1]]),
        ]
        tree = build_concrete_tree([s1], procs, controllers)
        agg = derive_aggregated_channels(tree)
        grp = next(iter(agg.values()))
        merged = _by_name(grp, "temp")[0]
        resolved = resolve_aggregated_channel(tree[entity_iri(s1)], merged)
        assert len(resolved) == 2
        assert {entity_iri(c) for c, _ch, _p in resolved} == {
            entity_iri(c) for c in controllers
        }

    def test_resolve_distinct_channel(self, objects, controllers):
        s1 = objects[0]
        proc = _make_process("P1", s1, controllers)
        tree = build_concrete_tree([s1], [proc], controllers)
        agg = derive_aggregated_channels(tree)
        grp = next(iter(agg.values()))
        px = _by_name(grp, "pressure_x")[0]
        resolved = resolve_aggregated_channel(tree[entity_iri(s1)], px)
        assert len(resolved) == 1  # only ToolA has pressure_x


# -- Tree sources --


class TestTreeSources:
    def test_object_tree_source(self, objects, controllers):
        s1 = objects[0]
        proc = _make_process("P1", s1, controllers)
        tree = build_concrete_tree(objects, [proc], controllers)
        source = build_object_tree_source(tree)
        assert len(source) == 2
        keys = [n["key"] for n in source]
        assert entity_iri(s1) in keys
        s1_node = [n for n in source if n["key"] == entity_iri(s1)][0]
        assert s1_node["processes"] == "1"
        assert s1_node["checkbox"] is True

    def test_process_tree_source(self, objects, controllers):
        s1 = objects[0]
        proc = _make_process("P1", s1, controllers)
        tree = build_concrete_tree([s1], [proc], controllers)
        agg = derive_aggregated_channels(tree)
        source = build_process_tree_source(agg)
        assert len(source) == 1  # one process type root
        root = source[0]
        # temp co-present -> 2 per-instance entries; pressure_x, pressure_y
        # each merged -> 4 children total
        assert len(root["children"]) == 4
        assert all(c["checkbox"] for c in root["children"])

    def test_get_selected_keys(self, objects, controllers):
        s1 = objects[0]
        proc = _make_process("P1", s1, controllers)
        tree = build_concrete_tree([s1], [proc], controllers)
        agg = derive_aggregated_channels(tree)
        proc_source = build_process_tree_source(agg)
        # Select first aggregated channel
        proc_source[0]["children"][0]["selected"] = True
        keys = get_selected_keys(proc_source)
        assert len(keys) == 1
        assert keys[0] == proc_source[0]["children"][0]["key"]

        # Object tree (flat, childless nodes)
        obj_source = build_object_tree_source(tree)
        obj_source[0]["selected"] = True
        obj_keys = get_selected_keys(obj_source)
        assert obj_keys == [obj_source[0]["key"]]
