"""ScatterView -- generic scatter/correlation view for multi-channel data.

Extends :class:`BaseDataView` with:

- Wunderbaum channel tree for selecting data channels
- Time-grid normalization (multiple interpolation methods)
- Computed columns via ``df.eval()``
- Bokeh scatter plot with user-selectable x / y / color / size mappings

Usage::

    from examples.scatter.scatter_view import ScatterView, ScatterDashboardConfig

    view = ScatterView(controllers=[ctrl], config=ScatterDashboardConfig())
    view.servable()
"""

import datetime as dt
import logging
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import panel as pn
from bokeh.models import (
    BoxZoomTool,
    ColorBar,
    ColumnDataSource,
    HoverTool,
    LinearColorMapper,
)
from bokeh.palettes import Viridis256
from bokeh.plotting import figure as bk_figure
from bokeh.transform import linear_cmap
from panelini import Panelini
from panelini.panels.wunderbaum import Wunderbaum
from pydantic import BaseModel, ConfigDict, Field

from opensemantic.base.view._base_view import COLORS, BaseDataView
from opensemantic.base.view._channel_utils import (
    _t,
    build_tree_source,
    flatten_composite_channels,
    get_display_label,
    get_selected_channels,
    group_channels_by_characteristic,
    resolve_downsample_method,
    resolve_value_type,
)
from opensemantic.base.view._config import DashboardConfig
from opensemantic.base.view._data_cache import ChannelDataCache

from .normalize import channels_to_dataframe

_logger = logging.getLogger(__name__)

_TREE_GRID_CSS = (
    ".tree-container.wunderbaum { width: 100% !important; }\n"
    ".wunderbaum-wrapper { overflow-x: hidden !important; }"
)

# ---------------------------------------------------------------------------
# i18n strings local to the scatter view
# ---------------------------------------------------------------------------

_SCATTER_STRINGS: Dict[str, Dict[str, str]] = {
    "scatter_plot": {"en": "Scatter Plot", "de": "Streudiagramm"},
    "scatter_controls": {
        "en": "Scatter Controls",
        "de": "Streudiagramm-Steuerung",
    },
    "x_axis": {"en": "X Axis", "de": "X-Achse"},
    "y_axis": {"en": "Y Axis", "de": "Y-Achse"},
    "color_axis": {"en": "Color", "de": "Farbe"},
    "size_axis": {"en": "Size", "de": "Groesse"},
    "none_option": {"en": "(none)", "de": "(keine)"},
    "interpolation": {"en": "Interpolation", "de": "Interpolation"},
    "grid_method": {"en": "Grid Method", "de": "Gittermethode"},
    "computed_column": {"en": "Computed Column", "de": "Berechnete Spalte"},
    "add_column": {"en": "Add Column", "de": "Spalte hinzufuegen"},
    "data_table": {"en": "Data Table", "de": "Datentabelle"},
    "normalize": {"en": "Normalize & Plot", "de": "Normalisieren & Plotten"},
}


def _st(key: str, lang: str = "en") -> str:
    """Translate a scatter-UI string, falling back to the core ``_t``."""
    entry = _SCATTER_STRINGS.get(key)
    if entry:
        return entry.get(lang, entry.get("en", key))
    return _t(key, lang)


# ---------------------------------------------------------------------------
# Config models
# ---------------------------------------------------------------------------


class InterpolationMethod(str, Enum):
    PREVIOUS = "previous"
    LINEAR = "linear"
    SPLINE = "spline"


class GridMethod(str, Enum):
    UNION = "union"
    FIXED_STEP = "fixed_step"


class ScatterConfig(BaseModel):
    """Scatter-plot-specific configuration."""

    model_config = ConfigDict(
        json_schema_extra={
            "title": "ScatterConfig",
            "defaultProperties": [
                "grid_method",
                "interp_method",
                "step_seconds",
                "computed_columns",
            ],
        }
    )

    grid_method: GridMethod = Field(
        GridMethod.UNION,
        title="Grid method",
        json_schema_extra={"title*": {"de": "Gittermethode"}},
    )
    interp_method: InterpolationMethod = Field(
        InterpolationMethod.LINEAR,
        title="Interpolation",
        json_schema_extra={"title*": {"de": "Interpolation"}},
    )
    step_seconds: Optional[float] = Field(
        None,
        title="Step (s)",
        description="Grid step for fixed_step mode",
        json_schema_extra={
            "title*": {"de": "Schritt (s)"},
            "description*": {"de": "Gitterschritt fuer fixed_step-Modus"},
        },
    )
    computed_columns: List[str] = Field(
        default_factory=list,
        title="Computed columns",
        description="Expressions like 'name = col_a * col_b'",
        json_schema_extra={
            "title*": {"de": "Berechnete Spalten"},
            "description*": {"de": "Ausdruecke wie 'name = spalte_a * spalte_b'"},
        },
    )


class ScatterDashboardConfig(DashboardConfig):
    """Dashboard config extended with scatter settings."""

    model_config = ConfigDict(
        json_schema_extra={
            "title": "ScatterDashboardConfig",
            "defaultProperties": [
                "controllers",
                "lang",
                "tree",
                "plot",
                "scatter",
            ],
        }
    )

    scatter: ScatterConfig = Field(
        default_factory=ScatterConfig,
        title="Scatter",
    )


# ---------------------------------------------------------------------------
# ScatterView
# ---------------------------------------------------------------------------

_NONE = "(none)"


class ScatterView(BaseDataView):
    """Generic scatter/correlation view for multi-channel data.

    Parameters
    ----------
    controllers
        List of DataToolController instances.
    config
        Dashboard + scatter configuration.
    title
        Window / tab title.
    embeddable
        When True, skip building the Panelini app so cards can be
        placed into a host layout via *sidebar_cards* / *main_cards*.
    """

    def __init__(
        self,
        controllers: Optional[List[Any]] = None,
        config: Optional[ScatterDashboardConfig] = None,
        title: str = "Scatter / Correlation View",
        embeddable: bool = False,
    ):
        self._controllers = controllers or []
        self._config: ScatterDashboardConfig = config or ScatterDashboardConfig()
        self._title = title
        self._embeddable = embeddable

        # Lookup maps
        self._channel_map: Dict[str, Any] = {}
        self._controller_map: Dict[str, Any] = {}
        self._build_lookup_maps()

        # Selection / grouping state (required by BaseDataView)
        self._selected: List[Tuple[Any, Any]] = []
        self._groups: Dict[str, List[Tuple[Any, Any]]] = {}
        self._unit_selections: Dict[str, str] = {}
        self._cached_data: Dict[str, List] = {}
        self._composite_parents: Dict[str, Tuple[Any, str]] = {}

        # Scatter state
        self._df: Optional[pd.DataFrame] = None
        self._column_names: List[str] = []

        # Cache
        self._cache = ChannelDataCache(enabled=self._config.plot.cache_enabled)

        # Build UI
        self._build_tree()
        self._build_controls()
        self._build_scatter_controls()
        self._build_plot()
        self._build_data_table()
        self._build_log_console()
        self._build_config_editor()
        self._build_layout()

    # -- lookup maps --------------------------------------------------------

    def _build_lookup_maps(self):
        for ctrl in self._controllers:
            for ch in ctrl.get_all_channels():
                self._channel_map[ch.uuid] = ch
                self._controller_map[ch.uuid] = ctrl

    # -- tree ---------------------------------------------------------------

    def _build_tree(self):
        source = build_tree_source(self._controllers, self.lang)
        self._tree = Wunderbaum(
            source=source,
            columns=[
                {"id": "*", "title": _st("channel", self.lang), "width": "190px"},
                {
                    "id": "characteristic",
                    "title": _st("characteristic", self.lang),
                    "width": "130px",
                },
            ],
            options={"checkbox": True, "selectMode": "hier"},
            stylesheets=[_TREE_GRID_CSS],
        )
        self._tree.param.watch(self._on_source_change, ["source"])
        self._tree_card = pn.Card(
            self._tree,
            title=_st("data_channels", self.lang),
            collapsed=False,
        )

    def _on_source_change(self, *args):
        try:
            self._update_selection()
            self._update_unit_controls()
            if self._config.plot.auto_fetch:
                self._trigger_load()
        except Exception as e:
            _logger.error("Error in _on_source_change: %s", e)

    def _update_selection(self):
        raw_selected = get_selected_channels(self._tree.source, self._controllers)
        self._selected = []
        self._composite_parents = {}
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

    # -- controls -----------------------------------------------------------

    def _build_controls(self):
        now = dt.datetime.now()
        self._start_picker = pn.widgets.DatetimePicker(
            name=_st("start", self.lang),
            value=now - dt.timedelta(hours=1),
        )
        self._end_picker = pn.widgets.DatetimePicker(
            name=_st("end", self.lang),
            value=now,
        )
        self._load_button = pn.widgets.Button(
            name=_st("load_data", self.lang),
            button_type="primary",
        )
        self._load_button.on_click(self._on_load_click)

        self._auto_fetch_cb = pn.widgets.Checkbox(
            name=_st("auto_fetch", self.lang),
            value=self._config.plot.auto_fetch,
        )
        self._auto_fetch_cb.param.watch(self._on_auto_fetch_change, ["value"])

        self._row_limit_input = pn.widgets.IntInput(
            name=_st("row_limit", self.lang),
            value=self._config.plot.row_limit,
            start=1,
            step=1000,
        )
        self._row_limit_input.param.watch(self._on_row_limit_change, ["value"])

        self._clear_cache_button = pn.widgets.Button(
            name=_st("clear_cache", self.lang),
            button_type="warning",
        )
        self._clear_cache_button.on_click(self._on_clear_cache)

        self._interp_select = pn.widgets.Select(
            name=_st("interpolation", self.lang),
            options={
                "Previous Value": "previous",
                "Linear": "linear",
                "Spline": "spline",
            },
            value=self._config.scatter.interp_method.value,
        )
        self._interp_select.param.watch(self._on_interp_change, ["value"])

        self._grid_select = pn.widgets.Select(
            name=_st("grid_method", self.lang),
            options={
                "Union": "union",
                "Fixed Step": "fixed_step",
            },
            value=self._config.scatter.grid_method.value,
        )
        self._grid_select.param.watch(self._on_grid_change, ["value"])

        self._unit_controls = pn.Column()

        self._start_picker.param.watch(self._on_time_change, ["value"])
        self._end_picker.param.watch(self._on_time_change, ["value"])

        self._controls_card = pn.Card(
            self._start_picker,
            self._end_picker,
            self._load_button,
            self._interp_select,
            self._grid_select,
            self._auto_fetch_cb,
            self._row_limit_input,
            self._clear_cache_button,
            self._unit_controls,
            title=_st("plot_controls", self.lang),
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

    def _on_interp_change(self, event):
        self._config.scatter.interp_method = InterpolationMethod(event.new)
        if self._cached_data:
            self._rebuild_dataframe()
            self._refresh_plot()

    def _on_grid_change(self, event):
        self._config.scatter.grid_method = GridMethod(event.new)
        if self._cached_data:
            self._rebuild_dataframe()
            self._refresh_plot()

    # -- scatter controls ---------------------------------------------------

    def _build_scatter_controls(self):
        self._x_select = pn.widgets.Select(
            name=_st("x_axis", self.lang), options=[], value=None
        )
        self._y_select = pn.widgets.Select(
            name=_st("y_axis", self.lang), options=[], value=None
        )
        self._color_select = pn.widgets.Select(
            name=_st("color_axis", self.lang), options=[_NONE], value=_NONE
        )
        self._size_select = pn.widgets.Select(
            name=_st("size_axis", self.lang), options=[_NONE], value=_NONE
        )

        for w in (self._x_select, self._y_select, self._color_select, self._size_select):
            w.param.watch(self._on_scatter_mapping_change, ["value"])

        self._computed_input = pn.widgets.TextInput(
            name=_st("computed_column", self.lang),
            placeholder="name = expression",
        )
        self._add_computed_btn = pn.widgets.Button(
            name=_st("add_column", self.lang),
            button_type="success",
        )
        self._add_computed_btn.on_click(self._on_add_computed)

        self._scatter_controls_card = pn.Card(
            self._x_select,
            self._y_select,
            self._color_select,
            self._size_select,
            pn.layout.Divider(),
            self._computed_input,
            self._add_computed_btn,
            title=_st("scatter_controls", self.lang),
        )

    def _on_scatter_mapping_change(self, event):
        self._build_figure()

    def _on_add_computed(self, event):
        expr = self._computed_input.value
        if not expr or "=" not in expr:
            return
        col_name, _, formula = expr.partition("=")
        col_name = col_name.strip()
        formula = formula.strip()
        if not col_name or not formula or self._df is None:
            return
        try:
            self._df[col_name] = self._df.eval(formula)
            if col_name not in self._column_names:
                self._column_names.append(col_name)
            self._update_scatter_dropdowns()
            self._update_table()
            self._build_figure()
            self._computed_input.value = ""
        except Exception as e:
            _logger.error("Computed column '%s' failed: %s", expr, e)

    # -- scatter dropdown helpers -------------------------------------------

    def _update_scatter_dropdowns(self):
        cols = list(self._column_names)
        none_cols = [_NONE] + cols

        prev_x = self._x_select.value
        prev_y = self._y_select.value
        prev_c = self._color_select.value
        prev_s = self._size_select.value

        self._x_select.options = cols
        self._y_select.options = cols
        self._color_select.options = none_cols
        self._size_select.options = none_cols

        self._x_select.value = prev_x if prev_x in cols else (cols[0] if cols else None)
        self._y_select.value = (
            prev_y
            if prev_y in cols
            else (cols[1] if len(cols) > 1 else cols[0] if cols else None)
        )
        self._color_select.value = prev_c if prev_c in none_cols else _NONE
        self._size_select.value = prev_s if prev_s in none_cols else _NONE

    # -- plot card ----------------------------------------------------------

    def _build_plot(self):
        self._plot_col = pn.Column(sizing_mode="stretch_width")
        self._plot_card = pn.Card(
            self._plot_col,
            title=_st("scatter_plot", self.lang),
            sizing_mode="stretch_width",
        )

    # -- data table card ----------------------------------------------------

    def _build_data_table(self):
        self._table_pane = pn.pane.DataFrame(
            pd.DataFrame(),
            sizing_mode="stretch_width",
            max_rows=20,
        )
        self._table_card = pn.Card(
            self._table_pane,
            title=_st("data_table", self.lang),
            collapsed=True,
            sizing_mode="stretch_width",
        )

    def _update_table(self):
        if self._df is not None and not self._df.empty:
            self._table_pane.object = self._df
        else:
            self._table_pane.object = pd.DataFrame()

    # -- data loading -------------------------------------------------------

    async def _load_and_plot(self):
        start = self._start_picker.value
        end = self._end_picker.value
        if start is None or end is None:
            return

        if start.tzinfo is None:
            start = start.astimezone(dt.timezone.utc)
        if end.tzinfo is None:
            end = end.astimezone(dt.timezone.utc)

        limit = self._config.plot.row_limit
        self._cached_data.clear()

        loaded_parents = set()
        for ctrl, ch in self._selected:
            try:
                if ch.uuid in self._composite_parents:
                    parent_ch, _ = self._composite_parents[ch.uuid]
                    if parent_ch.uuid in loaded_parents:
                        continue
                    loaded_parents.add(parent_ch.uuid)
                    mp, method, edge = self._downsample_for(parent_ch)
                    rows = await self._cache.get_data(
                        ctrl, parent_ch, start, end, limit,
                        max_points=mp, method=method, edge_anchors=edge,
                    )
                    self._cached_data[parent_ch.uuid] = rows
                else:
                    mp, method, edge = self._downsample_for(ch)
                    rows = await self._cache.get_data(
                        ctrl, ch, start, end, limit,
                        max_points=mp, method=method, edge_anchors=edge,
                    )
                    self._cached_data[ch.uuid] = rows
            except Exception as e:
                _logger.error("Error loading %s/%s: %s", ctrl.name, ch.name, e)

        self._rebuild_dataframe()
        self._refresh_plot()

    def _downsample_for(self, channel):
        ds = self._config.plot.downsample
        if not ds.enabled:
            return None, None, None
        method = resolve_downsample_method(channel, ds.method.value)
        return ds.max_points, method, ds.edge_anchors

    # -- DataFrame building -------------------------------------------------

    def _rebuild_dataframe(self):
        """Build the normalized DataFrame from cached channel data."""
        channel_series: Dict[str, Tuple[List[dt.datetime], List[float]]] = {}

        for ctrl, ch in self._selected:
            vtype = resolve_value_type(ch)
            if vtype == "text":
                continue

            label = self._make_column_label(ctrl, ch)
            timestamps, values = self._extract_channel_series(ch)
            if timestamps and values:
                channel_series[label] = (timestamps, values)

        if not channel_series:
            self._df = pd.DataFrame()
            self._column_names = []
            self._update_scatter_dropdowns()
            self._update_table()
            return

        self._df = channels_to_dataframe(
            channel_series,
            grid_method=self._config.scatter.grid_method.value,
            interp_method=self._config.scatter.interp_method.value,
            step_seconds=self._config.scatter.step_seconds,
        )

        # Apply persisted computed columns
        numeric_cols = [
            c for c in self._df.columns if c != "timestamp"
        ]
        for expr in self._config.scatter.computed_columns:
            if "=" not in expr:
                continue
            col_name, _, formula = expr.partition("=")
            col_name = col_name.strip()
            formula = formula.strip()
            if not col_name or not formula:
                continue
            try:
                self._df[col_name] = self._df.eval(formula)
                if col_name not in numeric_cols:
                    numeric_cols.append(col_name)
            except Exception as e:
                _logger.error("Computed column '%s' failed: %s", expr, e)

        self._column_names = [c for c in self._df.columns if c != "timestamp"]
        self._update_scatter_dropdowns()
        self._update_table()

    def _make_column_label(self, ctrl: Any, ch: Any) -> str:
        """Build a unique column name for a channel."""
        ch_label = get_display_label(ch, self.lang)
        if len(self._controllers) > 1:
            ctrl_label = get_display_label(ctrl, self.lang)
            return f"{ctrl_label} / {ch_label}"
        return ch_label

    def _extract_channel_series(
        self, ch: Any
    ) -> Tuple[List[dt.datetime], List[float]]:
        """Extract raw (timestamps, values) from cached data for one channel."""
        if ch.uuid in self._composite_parents:
            parent_ch, field_name = self._composite_parents[ch.uuid]
            return self._extract_composite_series(parent_ch, field_name)

        points = self._cached_data.get(ch.uuid, [])
        if not points:
            return [], []

        # Find the group key for unit conversion
        group_key = None
        for gk, channels in self._groups.items():
            for _, gch in channels:
                if gch.uuid == ch.uuid:
                    group_key = gk
                    break
            if group_key:
                break

        target_unit = self._unit_selections.get(group_key) if group_key else None
        timestamps = []
        values = []
        for pt in points:
            ts = pt.timestamp
            if isinstance(ts, str):
                ts = dt.datetime.fromisoformat(ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)
            v = self._numeric(pt.value, ch, target_unit)
            if v is not None:
                timestamps.append(ts)
                values.append(float(v))

        return timestamps, values

    def _extract_composite_series(
        self, parent_ch: Any, field_name: str
    ) -> Tuple[List[dt.datetime], List[float]]:
        """Extract a sub-field series from composite channel data."""
        points = self._cached_data.get(parent_ch.uuid, [])
        if not points:
            return [], []

        timestamps = []
        values = []
        for pt in points:
            ts = pt.timestamp
            if isinstance(ts, str):
                ts = dt.datetime.fromisoformat(ts)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=dt.timezone.utc)

            value = pt.value
            if isinstance(value, dict):
                sub = value.get(field_name)
            elif hasattr(value, field_name):
                sub = getattr(value, field_name)
            else:
                continue

            if sub is None:
                continue

            if isinstance(sub, dict):
                v = sub.get("value")
            elif hasattr(sub, "value"):
                v = sub.value
            elif isinstance(sub, (int, float)):
                v = sub
            else:
                continue

            if v is not None:
                timestamps.append(ts)
                values.append(float(v))

        return timestamps, values

    # -- figure building ----------------------------------------------------

    def _build_figure(self):
        self._plot_col.clear()

        if self._df is None or self._df.empty:
            return

        x_col = self._x_select.value
        y_col = self._y_select.value
        if not x_col or not y_col:
            return
        if x_col not in self._df.columns or y_col not in self._df.columns:
            return

        x_vals = self._df[x_col].values.astype(np.float64)
        y_vals = self._df[y_col].values.astype(np.float64)
        source_data: Dict[str, Any] = {"x": x_vals, "y": y_vals}

        # -- color mapping --
        color_col = self._color_select.value
        has_color = color_col and color_col != _NONE and color_col in self._df.columns
        if has_color:
            color_vals = self._df[color_col].values.astype(np.float64)
            source_data["color_val"] = color_vals
            c_min = float(np.nanmin(color_vals))
            c_max = float(np.nanmax(color_vals))
            if c_min == c_max:
                c_max = c_min + 1.0
            color_mapper = LinearColorMapper(
                palette=Viridis256, low=c_min, high=c_max
            )
            color_spec = linear_cmap("color_val", Viridis256, c_min, c_max)
        else:
            color_spec = COLORS[0]
            color_mapper = None

        # -- size mapping --
        size_col = self._size_select.value
        has_size = size_col and size_col != _NONE and size_col in self._df.columns
        if has_size:
            raw = self._df[size_col].values.astype(np.float64)
            s_min = float(np.nanmin(raw))
            s_max = float(np.nanmax(raw))
            denom = s_max - s_min if s_max != s_min else 1.0
            source_data["size_val"] = 4.0 + 16.0 * (raw - s_min) / denom
            size_spec: Any = "size_val"
        else:
            size_spec = 8

        src = ColumnDataSource(data=source_data)

        fig = bk_figure(
            height=500,
            sizing_mode="stretch_width",
            x_axis_label=x_col,
            y_axis_label=y_col,
            tools="pan,wheel_zoom,reset,save",
        )
        box_zoom = BoxZoomTool()
        fig.add_tools(box_zoom)
        fig.toolbar.active_drag = box_zoom

        fig.scatter(
            "x",
            "y",
            source=src,
            color=color_spec,
            size=size_spec,
            alpha=0.6,
        )

        # Color bar
        if color_mapper is not None:
            color_bar = ColorBar(
                color_mapper=color_mapper,
                title=color_col,
                location=(0, 0),
            )
            fig.add_layout(color_bar, "right")

        # Hover tool
        tooltips = [
            (x_col, "@x{0.00}"),
            (y_col, "@y{0.00}"),
        ]
        if has_color:
            tooltips.append((color_col, "@color_val{0.00}"))
        if has_size:
            tooltips.append((size_col, "@size_val{0.00}"))
        fig.add_tools(HoverTool(tooltips=tooltips))

        self._plot_col.append(pn.pane.Bokeh(fig, sizing_mode="stretch_width"))

    def _update_log_console(self):
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

    # -- config editor ------------------------------------------------------

    def _on_config_editor_change(self, event):
        if not event.new or not isinstance(event.new, dict):
            return
        try:
            new_config = ScatterDashboardConfig.model_validate(event.new)
        except Exception as e:
            _logger.debug("Incomplete config value, skipping: %s", e)
            return
        old_config = self._config
        self._config = new_config

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
        self._interp_select.value = new_config.scatter.interp_method.value
        self._grid_select.value = new_config.scatter.grid_method.value

    def _rebuild_ui_labels(self):
        self._tree_card.title = _st("data_channels", self.lang)
        self._controls_card.title = _st("plot_controls", self.lang)
        self._plot_card.title = _st("scatter_plot", self.lang)
        self._log_card.title = _st("log_console", self.lang)
        self._config_card.title = _st("config", self.lang)
        self._scatter_controls_card.title = _st("scatter_controls", self.lang)
        self._table_card.title = _st("data_table", self.lang)
        self._load_button.name = _st("load_data", self.lang)
        self._auto_fetch_cb.name = _st("auto_fetch", self.lang)
        self._row_limit_input.name = _st("row_limit", self.lang)
        self._clear_cache_button.name = _st("clear_cache", self.lang)
        self._start_picker.name = _st("start", self.lang)
        self._end_picker.name = _st("end", self.lang)
        self._interp_select.name = _st("interpolation", self.lang)
        self._grid_select.name = _st("grid_method", self.lang)
        self._x_select.name = _st("x_axis", self.lang)
        self._y_select.name = _st("y_axis", self.lang)
        self._color_select.name = _st("color_axis", self.lang)
        self._size_select.name = _st("size_axis", self.lang)
        self._computed_input.name = _st("computed_column", self.lang)
        self._add_computed_btn.name = _st("add_column", self.lang)
        source = build_tree_source(self._controllers, self.lang)
        self._tree.set_source(source)
        self._update_unit_controls()

    # -- layout -------------------------------------------------------------

    def _build_layout(self):
        if self._embeddable:
            self._app = None
            return
        self._app = Panelini(
            title=self._title,
            sidebar_enabled=True,
            sidebars_max_width=400,
        )
        self._app.sidebar_set([
            self._tree_card,
            self._controls_card,
            self._scatter_controls_card,
            self._config_card,
        ])
        self._app.main_set([self._plot_card, self._table_card, self._log_card])

    @property
    def sidebar_cards(self):
        return [
            self._tree_card,
            self._controls_card,
            self._scatter_controls_card,
            self._config_card,
        ]

    @property
    def main_cards(self):
        return [self._plot_card, self._table_card, self._log_card]
