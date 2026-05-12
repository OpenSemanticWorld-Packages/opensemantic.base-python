"""Tests for DataTool view utilities and data cache."""

import asyncio
import datetime as dt
from uuid import NAMESPACE_URL, uuid5

import pytest

from opensemantic import compute_scoped_uuid
from opensemantic.base.v1 import (
    Database,
    DataChannel,
    DataTool,
    DataToolController,
)
from opensemantic.base.view._channel_utils import (
    build_tree_source,
    get_available_units,
    get_characteristic_iri,
    get_display_label,
    get_display_label_cls,
    get_selected_channels,
    group_channels_by_characteristic,
    resolve_characteristic_class,
    resolve_characteristic_label,
    resolve_value_type,
)
from opensemantic.base.view._config import DashboardConfig, GroupingMode, PlotConfig
from opensemantic.base.view._data_cache import ChannelDataCache, _compute_gaps
from opensemantic.characteristics.quantitative.v1 import (
    ForcePerAreaUnit,
    Pressure,
    Temperature,
    TemperatureUnit,
)
from opensemantic.core.v1 import Label

# -- Fixtures --


@pytest.fixture
def parent_uuid():
    return uuid5(NAMESPACE_URL, "TestSensor")


@pytest.fixture
def data_tool(parent_uuid):
    return DataTool(
        uuid=parent_uuid,
        name="TestSensor",
        label=[
            Label(text="Test Sensor", lang="en"),
            Label(text="Testsensor", lang="de"),
        ],
        data_channels=[
            DataChannel(
                uuid=str(compute_scoped_uuid(parent_uuid, "temp")),
                osw_id="placeholder",
                name="temperature",
                label=[
                    Label(text="Temperature", lang="en"),
                    Label(text="Temperatur", lang="de"),
                ],
                characteristic=Temperature.get_cls_iri(),
            ),
            DataChannel(
                uuid=str(compute_scoped_uuid(parent_uuid, "press")),
                osw_id="placeholder",
                name="pressure",
                label=[
                    Label(text="Pressure", lang="en"),
                    Label(text="Druck", lang="de"),
                ],
                characteristic=Pressure.get_cls_iri(),
            ),
        ],
        storage_locations=[
            Database(name="test_cache_db", label=[Label(text="Test DB")]),
        ],
    )


@pytest.fixture
def controller(data_tool):
    return DataToolController(data_tool, auto_archive=True)


@pytest.fixture
def controller_with_data(controller):
    now = dt.datetime.now(dt.timezone.utc)

    async def store():
        for i in range(20):
            ts = now - dt.timedelta(minutes=20 - i)
            await controller.store_channel_data(
                DataToolController.StoreChannelDataParams(
                    channel="temperature",
                    value=Temperature(
                        value=20.0 + i * 0.5, unit=TemperatureUnit.Celsius
                    ),
                    timestamp=ts,
                )
            )
            await controller.store_channel_data(
                DataToolController.StoreChannelDataParams(
                    channel="pressure",
                    value=Pressure(
                        value=1013.0 + i * 0.1, unit=ForcePerAreaUnit.hecto_pascal
                    ),
                    timestamp=ts,
                )
            )

    asyncio.run(store())
    return controller, now


# -- Display label tests --


class TestDisplayLabel:
    def test_get_display_label_en(self, data_tool):
        assert get_display_label(data_tool, "en") == "Test Sensor"

    def test_get_display_label_de(self, data_tool):
        assert get_display_label(data_tool, "de") == "Testsensor"

    def test_get_display_label_fallback_to_en(self, data_tool):
        assert get_display_label(data_tool, "fr") == "Test Sensor"

    def test_get_display_label_no_label(self):
        class NoLabel:
            name = "fallback_name"

        assert get_display_label(NoLabel(), "en") == "fallback_name"

    def test_get_display_label_cls(self):
        label = get_display_label_cls(Temperature, "en")
        assert label != ""  # Should return title or title*


# -- Characteristic resolution tests --


class TestCharacteristicResolution:
    def test_get_characteristic_iri(self, controller):
        ch = controller.get_channel_by_name("temperature")
        iri = get_characteristic_iri(ch)
        assert iri is not None
        assert "OSW" in iri

    def test_resolve_characteristic_class(self, controller):
        ch = controller.get_channel_by_name("temperature")
        cls = resolve_characteristic_class(ch)
        assert cls is not None

    def test_resolve_characteristic_label(self, controller):
        ch = controller.get_channel_by_name("temperature")
        label = resolve_characteristic_label(ch, "en")
        assert label != ""

    def test_resolve_value_type_quantity(self, controller):
        ch = controller.get_channel_by_name("temperature")
        vtype = resolve_value_type(ch)
        assert vtype == "quantity"

    def test_get_available_units(self, controller):
        ch = controller.get_channel_by_name("temperature")
        units = get_available_units(ch)
        assert len(units) > 0
        names = [u["name"] for u in units]
        assert (
            "kelvin" in names
            or "Kelvin" in names
            or any("kelvin" in n.lower() for n in names)
        )


# -- Tree source tests --


class TestTreeSource:
    def test_build_tree_source(self, controller):
        source = build_tree_source([controller], "en")
        assert len(source) == 1
        root = source[0]
        assert root["title"] == "Test Sensor"
        assert root["checkbox"] is True
        assert len(root["children"]) == 2

    def test_build_tree_source_de(self, controller):
        source = build_tree_source([controller], "de")
        root = source[0]
        assert root["title"] == "Testsensor"

    def test_channel_nodes_have_characteristic(self, controller):
        source = build_tree_source([controller], "en")
        ch_node = source[0]["children"][0]
        assert "characteristic" in ch_node
        assert ch_node["characteristic"] != ""

    def test_get_selected_channels_none(self, controller):
        source = build_tree_source([controller], "en")
        selected = get_selected_channels(source, [controller])
        assert len(selected) == 0

    def test_get_selected_channels_some(self, controller):
        source = build_tree_source([controller], "en")
        source[0]["children"][0]["selected"] = True
        selected = get_selected_channels(source, [controller])
        assert len(selected) == 1


# -- Grouping tests --


class TestGrouping:
    def test_group_none(self, controller):
        channels = controller.get_all_channels()
        selected = [(controller, ch) for ch in channels]
        groups = group_channels_by_characteristic(selected, GroupingMode.NONE)
        assert len(groups) == 2  # each channel is its own group

    def test_group_unique(self, controller):
        channels = controller.get_all_channels()
        selected = [(controller, ch) for ch in channels]
        groups = group_channels_by_characteristic(selected, GroupingMode.UNIQUE)
        assert len(groups) == 2  # temperature and pressure are different

    def test_group_unique_same_characteristic(self, controller, parent_uuid):
        """Two channels with same characteristic should group together."""
        parent2 = uuid5(NAMESPACE_URL, "TestSensor2")
        tool2 = DataTool(
            uuid=parent2,
            name="Sensor2",
            label=[Label(text="S2")],
            data_channels=[
                DataChannel(
                    uuid=str(compute_scoped_uuid(parent2, "t2")),
                    osw_id="placeholder",
                    name="temp2",
                    label=[Label(text="Temp 2")],
                    characteristic=Temperature.get_cls_iri(),
                ),
            ],
            storage_locations=[Database(name="s2db", label=[Label(text="S2")])],
        )
        ctrl2 = DataToolController(tool2, auto_archive=True)

        ch1 = controller.get_channel_by_name("temperature")
        ch2 = ctrl2.get_channel_by_name("temp2")
        selected = [(controller, ch1), (ctrl2, ch2)]
        groups = group_channels_by_characteristic(selected, GroupingMode.UNIQUE)
        # Both are Temperature, so should be 1 group
        assert len(groups) == 1
        assert len(list(groups.values())[0]) == 2


# -- Gap computation tests --


class TestComputeGaps:
    def test_no_coverage(self):
        start = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc)
        gaps = _compute_gaps(start, end, [])
        assert gaps == [(start, end)]

    def test_full_coverage(self):
        start = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc)
        gaps = _compute_gaps(start, end, [(start, end)])
        assert gaps == []

    def test_partial_coverage(self):
        start = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        mid = dt.datetime(2024, 1, 1, 12, tzinfo=dt.timezone.utc)
        end = dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc)
        gaps = _compute_gaps(start, end, [(start, mid)])
        assert len(gaps) == 1
        assert gaps[0] == (mid, end)

    def test_gap_in_middle(self):
        start = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
        end = dt.datetime(2024, 1, 3, tzinfo=dt.timezone.utc)
        covered = [
            (
                dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2024, 1, 1, 12, tzinfo=dt.timezone.utc),
            ),
            (
                dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc),
                dt.datetime(2024, 1, 3, tzinfo=dt.timezone.utc),
            ),
        ]
        gaps = _compute_gaps(start, end, covered)
        assert len(gaps) == 1
        assert gaps[0][0] == dt.datetime(2024, 1, 1, 12, tzinfo=dt.timezone.utc)
        assert gaps[0][1] == dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc)


# -- Cache tests --


class TestChannelDataCache:
    def test_cache_stores_data(self, controller_with_data):
        ctrl, now = controller_with_data
        ch = ctrl.get_channel_by_name("temperature")
        cache = ChannelDataCache(enabled=True)

        async def run():
            start = now - dt.timedelta(hours=1)
            end = now + dt.timedelta(minutes=1)
            rows = await cache.get_data(ctrl, ch, start, end, 100)
            return rows

        rows = asyncio.run(run())
        assert len(rows) >= 20

    def test_cache_returns_timestamps(self, controller_with_data):
        ctrl, now = controller_with_data
        ch = ctrl.get_channel_by_name("temperature")
        cache = ChannelDataCache(enabled=True)

        async def run():
            start = now - dt.timedelta(hours=1)
            end = now + dt.timedelta(minutes=1)
            rows = await cache.get_data(ctrl, ch, start, end, 100)
            return rows

        rows = asyncio.run(run())
        assert len(rows) > 0
        pt = rows[0]
        assert isinstance(pt.timestamp, (dt.datetime, str))
        assert pt.value is not None

    def test_cache_hit_no_refetch(self, controller_with_data):
        ctrl, now = controller_with_data
        ch = ctrl.get_channel_by_name("temperature")
        cache = ChannelDataCache(enabled=True)

        async def run():
            start = now - dt.timedelta(hours=1)
            end = now + dt.timedelta(minutes=1)
            rows1 = await cache.get_data(ctrl, ch, start, end, 100)
            # Second call should use cache
            rows2 = await cache.get_data(ctrl, ch, start, end, 100)
            return rows1, rows2

        rows1, rows2 = asyncio.run(run())
        assert len(rows1) == len(rows2)

    def test_cache_disabled(self, controller_with_data):
        ctrl, now = controller_with_data
        ch = ctrl.get_channel_by_name("temperature")
        cache = ChannelDataCache(enabled=False)

        async def run():
            start = now - dt.timedelta(hours=1)
            end = now + dt.timedelta(minutes=1)
            rows = await cache.get_data(ctrl, ch, start, end, 100)
            return rows

        rows = asyncio.run(run())
        assert len(rows) >= 20

    def test_clear_cache(self, controller_with_data):
        ctrl, now = controller_with_data
        ch = ctrl.get_channel_by_name("temperature")
        cache = ChannelDataCache(enabled=True)

        async def run():
            start = now - dt.timedelta(hours=1)
            end = now + dt.timedelta(minutes=1)
            await cache.get_data(ctrl, ch, start, end, 100)
            assert ch.uuid in cache._data
            cache.clear_cache()
            assert ch.uuid not in cache._data

        asyncio.run(run())

    def test_time_range_filter(self, controller_with_data):
        ctrl, now = controller_with_data
        ch = ctrl.get_channel_by_name("temperature")
        cache = ChannelDataCache(enabled=True)

        async def run():
            # Only last 10 minutes
            start = now - dt.timedelta(minutes=10)
            end = now + dt.timedelta(minutes=1)
            rows = await cache.get_data(ctrl, ch, start, end, 100)
            return rows

        rows = asyncio.run(run())
        # Should have data (exact count depends on test isolation)
        assert len(rows) >= 1


# -- Config tests --


class TestConfig:
    def test_default_config(self):
        config = DashboardConfig()
        assert config.lang == "en"
        assert config.plot.auto_fetch is True
        assert config.plot.row_limit == 10000
        assert config.plot.grouping == GroupingMode.UNIQUE

    def test_config_serialization(self):
        config = DashboardConfig(lang="de", plot=PlotConfig(row_limit=5000))
        d = config.model_dump()
        restored = DashboardConfig(**d)
        assert restored.lang == "de"
        assert restored.plot.row_limit == 5000
