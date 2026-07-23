"""Tests for the bidirectional, composable, URL-syncable dashboard config.

Covers: the component-composed config round-trip (incl. an ``extra`` field and
the native Wunderbaum tree source), the generic ``UrlConfig`` in both modes,
the view-level binding (``set_config`` applies to the UI, user changes write
through and fire ``on_config_change``), the full URL<->UI round-trip, and the
host composition pattern (an aggregate parent config of per-view sub-configs).
"""

import types
from uuid import NAMESPACE_URL, uuid5

import pytest

pytest.importorskip("panel")
pytest.importorskip("panelini")

from pydantic import BaseModel  # noqa: E402

import opensemantic.base.view.url_config as urlmod  # noqa: E402
from opensemantic import compute_scoped_uuid  # noqa: E402
from opensemantic.base.v1 import (  # noqa: E402
    DataChannel,
    DataTool,
    DataToolController,
)
from opensemantic.base.view import (  # noqa: E402
    DataToolPlotControlsConfig,
    DataToolView,
    DataToolViewConfig,
    GroupingMode,
    ProcessObjectViewConfig,
    TimeRange,
)
from opensemantic.base.view.url_config import (  # noqa: E402
    UrlConfig,
    UrlConfigMode,
)
from opensemantic.characteristics.quantitative.v1 import Temperature  # noqa: E402
from opensemantic.core.v1 import Label  # noqa: E402

# -- Config model round-trip -------------------------------------------------


def test_config_roundtrip_component_shape_and_extra():
    cfg = DataToolViewConfig()
    cfg.tree.selected = ["chan-a", "chan-b"]
    cfg.plot.unit_selections = {"grp": "kelvin"}
    cfg.plot.time_range = TimeRange(start=__import__("datetime").datetime(2024, 1, 1))
    # extra="allow" -> an unknown/host field survives the round-trip.
    dumped = {**cfg.model_dump(mode="json"), "host_extra": 7}
    back = DataToolViewConfig.model_validate(dumped)
    assert back.tree.selected == ["chan-a", "chan-b"]
    assert back.plot.unit_selections == {"grp": "kelvin"}
    assert back.plot.time_range.start.year == 2024
    assert back.model_dump()["host_extra"] == 7
    # Process config composes two trees and no time_range.
    p = ProcessObjectViewConfig()
    assert "object_tree" in p.model_dump() and "process_tree" in p.model_dump()
    assert "time_range" not in p.plot.model_dump()


# -- UrlConfig modes ---------------------------------------------------------


class _FakeLoc:
    def __init__(self):
        self.search = ""


@pytest.fixture
def fake_location(monkeypatch):
    loc = _FakeLoc()
    monkeypatch.setattr(
        urlmod, "pn", types.SimpleNamespace(state=types.SimpleNamespace(location=loc))
    )
    return loc


@pytest.mark.parametrize(
    "mode",
    [
        UrlConfigMode.COMPRESSED_BASE64,
        UrlConfigMode.JSON,
        UrlConfigMode.PLAIN_KEYS,
    ],
)
def test_urlconfig_roundtrip_both_modes(fake_location, mode):
    # All modes round-trip typed fields and the compact tree selection.
    cfg = DataToolViewConfig()
    cfg.controllers = ["iri:a", "iri:b"]
    cfg.plot.unit_selections = {"g": "kelvin"}
    cfg.tree.selected = ["chan-x", "chan-y"]
    uc = UrlConfig(DataToolViewConfig, param_name="cfg")
    assert not uc.has_config()
    uc.set_config(cfg, mode)
    assert uc.has_config()
    back = uc.get_config()
    assert back.controllers == ["iri:a", "iri:b"]
    assert back.plot.unit_selections == {"g": "kelvin"}
    assert back.plot.grouping == cfg.plot.grouping
    assert back.tree.selected == ["chan-x", "chan-y"]
    uc.clear_config()
    assert not uc.has_config()


def test_urlconfig_full_fidelity_datetime_and_extra(fake_location):
    # Datetimes (ISO) and extra="allow" host fields round-trip too.
    import datetime as dt

    cfg = DataToolViewConfig()
    cfg.plot.time_range = TimeRange(start=dt.datetime(2024, 1, 1))
    cfg.host_extra = "keep"  # extra="allow"
    uc = UrlConfig(DataToolViewConfig, param_name="cfg")
    uc.set_config(cfg, UrlConfigMode.COMPRESSED_BASE64)
    back = uc.get_config()
    assert back.plot.time_range.start.year == 2024
    assert back.model_dump().get("host_extra") == "keep"


# -- View: build helper ------------------------------------------------------


def _view(config=None):
    parent = uuid5(NAMESPACE_URL, "CfgBind")
    ch_uuid = str(compute_scoped_uuid(parent, "temp"))
    tool = DataTool(
        uuid=parent,
        name="CfgTool",
        label=[Label(text="Cfg Tool")],
        data_channels=[
            DataChannel(
                uuid=ch_uuid,
                osw_id="placeholder",
                name="temperature",
                label=[Label(text="Temperature")],
                characteristic=Temperature.get_cls_iri(),
            ),
        ],
    )
    ctrl = DataToolController(tool)
    if config is None:
        config = DataToolViewConfig(plot=DataToolPlotControlsConfig(auto_fetch=False))
    view = DataToolView(controllers=[ctrl], config=config, embeddable=True)
    return view, ch_uuid


def _select_in_source(source, key):
    """Return a copy of the tree source with the child ``key`` checked."""
    import copy

    src = copy.deepcopy(source)
    for root in src:
        for child in root.get("children", []):
            child["selected"] = child.get("key") == key
    return src


# -- View: config -> UI (set_config) -----------------------------------------


def test_set_config_applies_selection_units_and_time(fake_location):
    view, ch_uuid = _view()
    cfg = DataToolViewConfig(plot=DataToolPlotControlsConfig(auto_fetch=False))
    cfg.tree.selected = [ch_uuid]
    cfg.plot.unit_selections = {}
    import datetime as dt

    cfg.plot.time_range = TimeRange(
        start=dt.datetime(2024, 1, 1, 0, 0, 0),
        end=dt.datetime(2024, 1, 1, 1, 0, 0),
    )
    view.set_config(cfg)
    # Selection applied: the channel is now in the view's selection.
    assert any(ch.uuid == ch_uuid for _c, ch in view._selected)
    # Time range applied to the pickers.
    assert view._start_picker.value.year == 2024
    # get_config returns the concrete class and round-trips.
    assert isinstance(view.get_config(), DataToolViewConfig)


# -- View: UI -> config (write-through + on_config_change) -------------------


def test_write_through_emits_config_change(fake_location):
    view, ch_uuid = _view()
    received = []
    view.on_config_change(received.append)
    # Simulate a user checking the channel in the tree.
    view._tree.source = _select_in_source(view._tree.source, ch_uuid)
    view._on_source_change()
    assert received, "on_config_change should fire on selection change"
    latest = received[-1]
    # The checked channel key is mirrored into the config's tree selection.
    assert ch_uuid in latest.tree.selected


def test_native_plot_controls_write_through_and_apply(fake_location):
    # Every PlotControlsConfig property has a native widget (no JSON editor).
    view, _ = _view()
    received = []
    view.on_config_change(received.append)

    # UI -> config: changing the native widgets writes through.
    view._grouping_select.value = GroupingMode.NONE.value
    view._ds_max_points.value = 500
    view._ds_enabled.value = False
    cfg = view.get_config()
    assert cfg.plot.grouping == GroupingMode.NONE
    assert cfg.plot.downsample.max_points == 500
    assert cfg.plot.downsample.enabled is False
    assert received  # each change emitted

    # config -> UI: set_config pushes values back into the widgets.
    new = DataToolViewConfig(plot=DataToolPlotControlsConfig(auto_fetch=False))
    new.plot.grouping = GroupingMode.SUB
    new.plot.downsample.max_points = 1234
    view.set_config(new)
    assert view._grouping_select.value == GroupingMode.SUB.value
    assert view._ds_max_points.value == 1234


def test_apply_config_is_reentrancy_guarded(fake_location):
    view, ch_uuid = _view()
    received = []
    view.on_config_change(received.append)
    cfg = view.get_config()
    cfg = cfg.model_copy(deep=True)
    cfg.tree.selected = [ch_uuid]
    view.set_config(cfg)
    # set_config notifies once; applying widgets must not re-emit in a loop.
    assert len(received) == 1


# -- Full URL <-> UI round-trip ----------------------------------------------


def test_url_to_ui_and_ui_to_url(fake_location):
    # UI -> URL: a change writes the config to the URL.
    view, ch_uuid = _view()
    uc = UrlConfig(type(view.get_config()), param_name="cfg")
    view.on_config_change(lambda c: uc.set_config(c, UrlConfigMode.COMPRESSED_BASE64))
    view._tree.source = _select_in_source(view._tree.source, ch_uuid)
    view._on_source_change()
    assert uc.has_config(), "view change should have populated the URL"

    # URL -> UI: a fresh view loads the config from the URL and reflects it.
    view2, _ = _view()
    loaded = uc.get_config()
    view2.set_config(loaded)
    assert any(ch.uuid == ch_uuid for _c, ch in view2._selected)


# -- Host composition (aggregate parent config of per-view sub-configs) ------


def test_composition_parent_config_roundtrip(fake_location):
    class AppConfig(BaseModel):
        title: str = "App"
        datatool: DataToolViewConfig = DataToolViewConfig()
        process: ProcessObjectViewConfig = ProcessObjectViewConfig()

    view, ch_uuid = _view()
    app = AppConfig()

    # Host keeps its parent slot in sync with the view's sub-config.
    def _update(cfg):
        app.datatool = cfg

    view.on_config_change(_update)
    view._tree.source = _select_in_source(view._tree.source, ch_uuid)
    view._on_source_change()
    assert app.datatool.tree.selected, "parent slot updated from the view"

    # The whole app config URL-syncs with the same generic tooling.
    uc = UrlConfig(AppConfig, param_name="app")
    uc.set_config(app, UrlConfigMode.COMPRESSED_BASE64)
    back = uc.get_config()
    assert back.title == "App"
    assert back.datatool.tree.selected == app.datatool.tree.selected
