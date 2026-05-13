"""DataTool View - archive time series visualization.

Provides a Panelini-based view with:
- Wunderbaum TreeGrid sidebar showing DataTools and their channels
- Plotly time series plot for selected (checked) channels
- Time range selection and unit switching controls
- JsonEditor for runtime config editing

Usage:
    from opensemantic.base.view import DataToolView

    view = DataToolView(controllers=[ctrl1, ctrl2])
    view.servable()
"""

import asyncio
import datetime as dt
import logging
from typing import Any, Dict, List, Optional, Tuple

import panel as pn
from bokeh.models import ColumnDataSource, DatetimeTickFormatter
from bokeh.palettes import Category10_10
from bokeh.plotting import figure as bk_figure
from panelini import Panelini
from panelini.panels.jsoneditor import JsonEditor
from panelini.panels.wunderbaum import Wunderbaum

from opensemantic.base.view._channel_utils import (
    _get_unit_symbol_map,
    _t,
    build_tree_source,
    flatten_composite_channels,
    get_available_units,
    get_display_label,
    get_selected_channels,
    get_unit_enum,
    group_channels_by_characteristic,
    resolve_characteristic_class,
    resolve_characteristic_label,
    resolve_value_type,
)
from opensemantic.base.view._config import DashboardConfig
from opensemantic.base.view._data_cache import ChannelDataCache


def get_unit_enum_from_value(value: Any) -> Any:
    """Get the UnitEnum type from a typed value instance."""
    unit = getattr(value, "unit", None)
    if unit is not None:
        return type(unit) if hasattr(type(unit), "__members__") else None
    return None


_logger = logging.getLogger(__name__)

COLORS = Category10_10


class DataToolView:
    """Archive-mode DataTool view.

    Displays multiple DataToolControllers in a TreeGrid sidebar.
    Users check channels to plot archived time series data.
    Channels sharing the same characteristic are grouped on shared y-axes.

    Parameters
    ----------
    controllers
        List of DataToolController instances to display.
    config
        Dashboard configuration. If None, uses defaults.
    title
        Dashboard title shown in the Panelini header.
    """

    def __init__(
        self,
        controllers: Optional[List[Any]] = None,
        config: Optional[DashboardConfig] = None,
        title: str = "DataTool Dashboard",
    ):
        self._controllers = controllers or []
        self._config = config or DashboardConfig()
        self._title = title

        # Build lookup maps
        self._channel_map: Dict[str, Any] = {}
        self._controller_map: Dict[str, Any] = {}
        self._build_lookup_maps()

        # State
        self._selected: List[Tuple[Any, Any]] = []
        self._groups: Dict[str, List[Tuple[Any, Any]]] = {}
        self._unit_selections: Dict[str, str] = {}
        self._cached_data: Dict[str, List] = {}
        self._composite_parents: Dict[str, Tuple[Any, str]] = {}

        # Cache
        self._cache = ChannelDataCache(enabled=self._config.plot.cache_enabled)

        # Build UI
        self._build_tree()
        self._build_controls()
        self._build_plot()
        self._build_log_console()
        self._build_config_editor()
        self._build_layout()

    @property
    def lang(self) -> str:
        return self._config.lang

    def _build_lookup_maps(self):
        for ctrl in self._controllers:
            for ch in ctrl.get_all_channels():
                self._channel_map[ch.uuid] = ch
                self._controller_map[ch.uuid] = ctrl

    # -- TreeGrid --

    def _build_tree(self):
        source = build_tree_source(self._controllers, self.lang)
        self._tree = Wunderbaum(
            source=source,
            columns=[
                {"id": "*", "title": _t("channel", self.lang), "width": "200px"},
                {
                    "id": "characteristic",
                    "title": _t("characteristic", self.lang),
                    "width": "150px",
                },
            ],
            options={"checkbox": True, "selectMode": "hier"},
        )
        # Watch the source param for checkbox changes (emitSource syncs selected state)
        self._tree.param.watch(self._on_source_change, ["source"])
        self._tree_card = pn.Card(
            self._tree,
            title=_t("data_channels", self.lang),
            collapsed=False,
        )

    def _on_source_change(self, *args):
        """Handle tree source changes (checkbox toggling)."""
        try:
            self._update_selection()
            self._update_unit_controls()
            if self._config.plot.auto_fetch:
                self._trigger_load()
        except Exception as e:
            _logger.error("Error in _on_source_change: %s", e)

    def _update_selection(self):
        raw_selected = get_selected_channels(self._tree.source, self._controllers)
        # Expand composite channels into leaf sub-channels for grouping
        self._selected = []
        self._composite_parents = {}  # sub_ch.uuid -> (parent_ch, field_name)
        for ctrl, ch in raw_selected:
            vtype = resolve_value_type(ch)
            if vtype == "composite":
                for sub_ctrl, sub_ch, path in flatten_composite_channels(ctrl, ch):
                    self._selected.append((ctrl, sub_ch))
                    self._composite_parents[sub_ch.uuid] = (ch, sub_ch.name)
            else:
                self._selected.append((ctrl, ch))
        self._groups = group_channels_by_characteristic(
            self._selected, self._config.plot.grouping
        )

    # -- Plot Controls --

    def _build_controls(self):
        now = dt.datetime.now()
        self._start_picker = pn.widgets.DatetimePicker(
            name=_t("start", self.lang),
            value=now - dt.timedelta(hours=1),
        )
        self._end_picker = pn.widgets.DatetimePicker(
            name=_t("end", self.lang),
            value=now,
        )
        self._load_button = pn.widgets.Button(
            name=_t("load_data", self.lang),
            button_type="primary",
        )
        self._load_button.on_click(self._on_load_click)

        self._auto_fetch_cb = pn.widgets.Checkbox(
            name=_t("auto_fetch", self.lang),
            value=self._config.plot.auto_fetch,
        )
        self._auto_fetch_cb.param.watch(self._on_auto_fetch_change, ["value"])

        self._row_limit_input = pn.widgets.IntInput(
            name=_t("row_limit", self.lang),
            value=self._config.plot.row_limit,
            start=1,
            step=1000,
        )
        self._row_limit_input.param.watch(self._on_row_limit_change, ["value"])

        self._clear_cache_button = pn.widgets.Button(
            name=_t("clear_cache", self.lang),
            button_type="warning",
        )
        self._clear_cache_button.on_click(self._on_clear_cache)

        self._unit_controls = pn.Column()

        self._start_picker.param.watch(self._on_time_change, ["value"])
        self._end_picker.param.watch(self._on_time_change, ["value"])

        self._controls_card = pn.Card(
            self._start_picker,
            self._end_picker,
            self._load_button,
            self._auto_fetch_cb,
            self._row_limit_input,
            self._clear_cache_button,
            self._unit_controls,
            title=_t("plot_controls", self.lang),
        )

    def _on_load_click(self, event):
        self._trigger_load()

    def _on_time_change(self, event):
        if self._config.plot.auto_fetch and self._selected:
            self._trigger_load()

    def _on_auto_fetch_change(self, event):
        self._config.plot.auto_fetch = event.new

    def _on_row_limit_change(self, event):
        self._config.plot.row_limit = event.new

    def _on_clear_cache(self, event):
        self._cache.clear_cache()
        self._cached_data.clear()

    def _update_unit_controls(self):
        self._unit_controls.clear()
        for group_key, channels in self._groups.items():
            if not channels:
                continue
            sample_ch = channels[0][1]
            vtype = resolve_value_type(sample_ch)
            if vtype != "quantity":
                continue
            units = get_available_units(sample_ch)
            if not units:
                continue
            char_label = resolve_characteristic_label(sample_ch, self.lang)
            current = self._unit_selections.get(group_key)
            options = {u["symbol"]: u["name"] for u in units}
            if current not in options.values():
                current = next(iter(options.values()))
                self._unit_selections[group_key] = current
            dropdown = pn.widgets.Select(
                name=f"{_t('unit', self.lang)}: {char_label}",
                options=options,
                value=current,
            )
            dropdown.param.watch(
                lambda event, _key=group_key: self._on_unit_change(_key, event),
                ["value"],
            )
            self._unit_controls.append(dropdown)

    def _on_unit_change(self, group_key: str, event):
        self._unit_selections[group_key] = event.new
        self._refresh_plot()

    # -- Plot --

    def _build_plot(self):
        self._plot_col = pn.Column(
            sizing_mode="stretch_width", scroll=True, max_height=600
        )
        self._plot_card = pn.Card(
            self._plot_col,
            title=_t("time_series", self.lang),
            sizing_mode="stretch_width",
        )

    def _build_log_console(self):
        self._log_data = []
        self._log_pane = pn.pane.HTML(
            "",
            sizing_mode="stretch_width",
            height=200,
            styles={"overflow-y": "auto"},
        )
        self._log_card = pn.Card(
            self._log_pane,
            title=_t("log_console", self.lang),
            collapsed=False,
            visible=False,
            sizing_mode="stretch_width",
        )

    # -- Config Editor --

    def _build_config_editor(self):
        schema = self._config.model_json_schema()
        self._config_editor = JsonEditor(
            value=self._config.model_dump(),
            options={
                "schema": schema,
                "no_additional_properties": True,
                "disable_edit_json": False,
            },
        )
        self._config_editor.param.watch(self._on_config_editor_change, ["value"])
        self._config_card = pn.Card(
            pn.Column(
                self._config_editor,
                sizing_mode="stretch_width",
                max_height=1000,
                scroll=True,
            ),
            title=_t("config", self.lang),
            collapsed=True,
        )

    def _on_config_editor_change(self, event):
        if not event.new or not isinstance(event.new, dict):
            return
        try:
            new_config = DashboardConfig.model_validate(event.new)
        except Exception as e:
            _logger.debug("Incomplete config value, skipping: %s", e)
            return
        if new_config is None:
            return
        old_config = self._config
        self._config = new_config

        # Determine what changed and rebuild accordingly
        if old_config.controllers != new_config.controllers:
            self._on_controllers_changed()
        if old_config.plot.grouping != new_config.plot.grouping:
            self._update_selection()
            self._update_unit_controls()
            self._refresh_plot()
        if old_config.plot.cache_enabled != new_config.plot.cache_enabled:
            self._cache.enabled = new_config.plot.cache_enabled
        if old_config.lang != new_config.lang:
            self._rebuild_ui_labels()
        self._auto_fetch_cb.value = new_config.plot.auto_fetch
        self._row_limit_input.value = new_config.plot.row_limit

    def _on_controllers_changed(self):
        """Handle controllers list change from config editor.

        Subclasses (LiveDashboard) can override to resolve IRIs
        and create new controller instances.
        """
        self._cache.clear_cache()
        self._cached_data.clear()
        self._build_lookup_maps()
        # Rebuild tree
        source = build_tree_source(self._controllers, self.lang)
        self._tree.set_source(source)

    def _rebuild_ui_labels(self):
        """Rebuild all UI labels after language change."""
        self._tree_card.title = _t("data_channels", self.lang)
        self._controls_card.title = _t("plot_controls", self.lang)
        self._plot_card.title = _t("time_series", self.lang)
        self._log_card.title = _t("log_console", self.lang)
        self._config_card.title = _t("config", self.lang)
        self._load_button.name = _t("load_data", self.lang)
        self._auto_fetch_cb.name = _t("auto_fetch", self.lang)
        self._row_limit_input.name = _t("row_limit", self.lang)
        self._clear_cache_button.name = _t("clear_cache", self.lang)
        self._start_picker.name = _t("start", self.lang)
        self._end_picker.name = _t("end", self.lang)
        # Rebuild tree source with new lang
        source = build_tree_source(self._controllers, self.lang)
        self._tree.set_source(source)
        # Rebuild unit controls
        self._update_unit_controls()

    # -- Data Loading --

    def _trigger_load(self):
        """Start data loading (handles async context)."""
        _logger.debug("_trigger_load called, %d selected", len(self._selected))
        try:
            asyncio.get_running_loop()
            asyncio.ensure_future(self._load_and_plot())
        except RuntimeError:
            asyncio.run(self._load_and_plot())

    async def _load_and_plot(self):
        """Load data for all selected channels and update plots."""
        start = self._start_picker.value
        # Use current time as end when auto-fetching so new data is included
        end = dt.datetime.now() if self._auto_fetch_cb.value else self._end_picker.value
        if start is None or end is None:
            return

        # DatetimePicker returns naive local time; convert to UTC
        if start.tzinfo is None:
            start = start.astimezone(dt.timezone.utc)
        if end.tzinfo is None:
            end = end.astimezone(dt.timezone.utc)

        limit = self._config.plot.row_limit
        self._cached_data.clear()

        # Load data for each channel. For composite sub-channels, load the parent.
        loaded_parents = set()
        for ctrl, ch in self._selected:
            try:
                if ch.uuid in self._composite_parents:
                    parent_ch, _ = self._composite_parents[ch.uuid]
                    if parent_ch.uuid in loaded_parents:
                        continue
                    loaded_parents.add(parent_ch.uuid)
                    rows = await self._cache.get_data(
                        ctrl, parent_ch, start, end, limit
                    )
                    self._cached_data[parent_ch.uuid] = rows
                else:
                    rows = await self._cache.get_data(ctrl, ch, start, end, limit)
                    self._cached_data[ch.uuid] = rows
            except Exception as e:
                _logger.error("Error loading %s/%s: %s", ctrl.name, ch.name, e)

        self._refresh_plot()

    def _refresh_plot(self):
        """Rebuild plot and log console from cached data."""
        self._build_figure()
        self._update_log_console()

    def _build_figure(self):
        """Build Bokeh figures - one per characteristic group."""
        self._plot_col.clear()

        plot_groups = []
        for group_key, channels in self._groups.items():
            if not channels:
                continue
            sample_ch = channels[0][1]
            vtype = resolve_value_type(sample_ch)
            if vtype == "text":
                continue
            plot_groups.append((group_key, channels, vtype))

        if not plot_groups:
            return

        color_idx = 0
        for group_key, channels, vtype in plot_groups:
            axis_label = self._get_axis_label(group_key)
            fig = bk_figure(
                height=250,
                sizing_mode="stretch_width",
                x_axis_type="datetime",
                y_axis_label=axis_label,
            )
            fig.xaxis.formatter = DatetimeTickFormatter(
                seconds="%H:%M:%S",
                minutes="%H:%M",
                hours="%H:%M",
            )

            for ctrl, ch in channels:
                timestamps, values = self._extract_trace_data(ch, group_key)
                if timestamps:
                    trace_name = (
                        f"{get_display_label(ctrl, self.lang)}/"
                        f"{get_display_label(ch, self.lang)}"
                    )
                    src = ColumnDataSource(data={"x": timestamps, "y": values})
                    fig.line(
                        "x",
                        "y",
                        source=src,
                        legend_label=trace_name,
                        color=COLORS[color_idx % len(COLORS)],
                        line_width=2,
                    )
                    color_idx += 1

            fig.legend.click_policy = "hide"
            self._plot_col.append(pn.pane.Bokeh(fig, sizing_mode="stretch_width"))

    def _extract_trace_data(self, ch: Any, group_key: str) -> Tuple[List, List]:
        """Extract timestamps and values from cached ChannelDataPoints.

        Handles regular channels and composite sub-channels.
        For composite sub-channels, reads from the parent's cache and extracts
        the named sub-field.
        """
        # Check if this is a composite sub-channel
        if ch.uuid in self._composite_parents:
            parent_ch, field_name = self._composite_parents[ch.uuid]
            return self._extract_composite_field(parent_ch, field_name, group_key)

        points = self._cached_data.get(ch.uuid, [])
        if not points:
            return [], []

        target_unit_name = self._unit_selections.get(group_key)
        timestamps = []
        values = []

        for pt in points:
            ts = pt.timestamp
            if isinstance(ts, str):
                ts = dt.datetime.fromisoformat(ts)
            if ts.tzinfo is not None:
                ts = ts.astimezone(tz=None).replace(tzinfo=None)
            timestamps.append(ts)

            value = pt.value
            # Convert to display unit if needed
            if target_unit_name and hasattr(value, "to_unit"):
                unit_enum = get_unit_enum(ch)
                if unit_enum is not None and target_unit_name in unit_enum.__members__:
                    try:
                        value = value.to_unit(unit_enum[target_unit_name])
                    except Exception:
                        pass
            # Extract numeric value
            if hasattr(value, "value"):
                values.append(value.value)
            elif isinstance(value, dict):
                values.append(value.get("value", 0))
            else:
                values.append(value)

        return timestamps, values

    def _extract_composite_field(
        self, parent_ch: Any, field_name: str, group_key: str
    ) -> Tuple[List, List]:
        """Extract a sub-field from composite ChannelDataPoints.

        Reads from the parent channel's cache and extracts the named field.
        Works with both typed objects and raw dicts (from typed=False loading).
        """
        points = self._cached_data.get(parent_ch.uuid, [])
        if not points:
            return [], []

        target_unit_name = self._unit_selections.get(group_key)
        timestamps = []
        values = []

        for pt in points:
            ts = pt.timestamp
            if isinstance(ts, str):
                ts = dt.datetime.fromisoformat(ts)
            if ts.tzinfo is not None:
                ts = ts.astimezone(tz=None).replace(tzinfo=None)

            value = pt.value
            # Extract sub-field from composite (dict or typed object)
            if isinstance(value, dict):
                sub = value.get(field_name)
            elif hasattr(value, field_name):
                sub = getattr(value, field_name)
            else:
                continue

            if sub is None:
                continue

            # Sub-field may be a dict (raw) or typed object
            if isinstance(sub, dict):
                v = sub.get("value")
                if v is not None:
                    timestamps.append(ts)
                    values.append(v)
            elif hasattr(sub, "value"):
                # Unit conversion on typed sub-field
                if target_unit_name and hasattr(sub, "to_unit"):
                    unit_enum = get_unit_enum_from_value(sub)
                    if unit_enum and target_unit_name in unit_enum.__members__:
                        try:
                            sub = sub.to_unit(unit_enum[target_unit_name])
                        except Exception:
                            pass
                timestamps.append(ts)
                values.append(sub.value)
            elif isinstance(sub, (int, float)):
                timestamps.append(ts)
                values.append(sub)

        return timestamps, values

    def _get_axis_label(self, group_key: str) -> str:
        """Build y-axis label: characteristic name [unit symbol].

        Uses the selected unit if set, otherwise the channel's configured unit.
        """
        channels = self._groups.get(group_key, [])
        if not channels:
            return ""
        sample_ch = channels[0][1]
        char_label = resolve_characteristic_label(sample_ch, self.lang)

        # Try selected unit first
        unit_name = self._unit_selections.get(group_key)
        if unit_name:
            units = get_available_units(sample_ch)
            for u in units:
                if u["name"] == unit_name:
                    return f"{char_label} [{u['symbol']}]"

        # Fall back to channel's configured unit
        ch_unit = getattr(sample_ch, "unit", None)
        if ch_unit:
            cls = resolve_characteristic_class(sample_ch)
            symbol_map = _get_unit_symbol_map(cls)
            unit_enum = get_unit_enum(sample_ch)
            if unit_enum:
                for member in unit_enum:
                    if member.value == ch_unit or member.name == ch_unit:
                        symbol = symbol_map.get(member.name, member.name)
                        return f"{char_label} [{symbol}]"

        return char_label

    def _update_log_console(self):
        """Update the log console with text-type channel data."""
        log_entries = []
        has_text = False

        for group_key, channels in self._groups.items():
            for ctrl, ch in channels:
                vtype = resolve_value_type(ch)
                if vtype != "text":
                    continue
                has_text = True
                rows = self._cached_data.get(ch.uuid, [])
                ch_label = get_display_label(ch, self.lang)
                for pt in rows:
                    val = pt.value
                    if hasattr(val, "value"):
                        text = str(val.value)
                    elif isinstance(val, dict):
                        text = str(val.get("value", val))
                    else:
                        text = str(val)
                    log_entries.append((pt.timestamp, ch_label, text))

        self._log_card.visible = has_text
        if not log_entries:
            return

        log_entries.sort(key=lambda x: x[0])
        html_lines = []
        for ts, ch_label, text in log_entries:
            if isinstance(ts, str):
                ts_str = ts
            elif hasattr(ts, "strftime"):
                local_ts = ts.astimezone(tz=None) if ts.tzinfo else ts
                ts_str = local_ts.strftime("%Y-%m-%d %H:%M:%S")
            else:
                ts_str = str(ts)
            html_lines.append(
                f"<div style='font-family:monospace; font-size:12px;'>"
                f"<span style='color:#888'>{ts_str}</span> "
                f"<span style='color:#1f77b4'>[{ch_label}]</span> {text}</div>"
            )
        self._log_pane.object = "\n".join(html_lines)

    # -- Layout --

    def _build_layout(self):
        self._app = Panelini(
            title=self._title,
            sidebar_enabled=True,
            sidebars_max_width=400,
        )
        self._app.sidebar_set(
            [
                self._tree_card,
                self._controls_card,
                self._config_card,
            ]
        )
        self._build_main_area()

    def _build_main_area(self):
        """Set main area content. Override in subclasses for tabs."""
        self._app.main_set([self._plot_card, self._log_card])

    def servable(self, **kwargs):
        """Make the dashboard servable."""
        return self._app.servable(**kwargs)

    def panel(self):
        """Return the Panelini app for embedding."""
        return self._app
