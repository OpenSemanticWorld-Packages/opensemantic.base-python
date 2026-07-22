"""Shared building blocks for archive views.

``BaseDataView`` is a mixin (no ``__init__``) holding the parts that are
identical across the channel-centered :class:`DataToolView` and the
process/object-centered :class:`ProcessObjectView`: unit-switch controls, the
plot/log/config cards, the value→numeric unit conversion, and the
servable/panel helpers. Subclasses provide the data model and the
view-specific bits (trees, controls, ``_build_figure`` /
``_update_log_console`` / ``_load_and_plot``).

The mixin only reads attributes the subclass sets in its own ``__init__`` /
build steps (``_config``, ``_groups``, ``_unit_selections``,
``_unit_controls``, ``_plot_col``/``_plot_card``, ``_app`` ...), so it composes
cleanly without participating in ``__init__`` and without disturbing existing
subclasses (e.g. ``LiveDataToolView``).
"""

import asyncio
import io
import logging
from typing import Any, List, Tuple

import panel as pn
from bokeh.palettes import Category10_10
from panelini.panels.jsoneditor import JsonEditor

from opensemantic.base.view._channel_utils import (
    _get_unit_symbol_map,
    _t,
    get_available_units,
    get_unit_enum,
    resolve_characteristic_class,
    resolve_characteristic_label,
    resolve_value_type,
)

_logger = logging.getLogger(__name__)

COLORS = Category10_10

# Row cap for data export (uniform np.linspace downsampling above this).
EXPORT_MAX_ROWS = 1_000_000

# Optional dependencies for unit-aware data export (opensemantic.base[export]).
try:
    import pandas as _pd  # noqa: F401
    import pint_pandas as _pint_pandas  # noqa: F401

    _EXPORT_DEPS_OK = True
except Exception:  # pragma: no cover - optional
    _EXPORT_DEPS_OK = False


def _series_to_dataframe(series: List[dict], max_rows: int):
    """Tidy series records -> a DataFrame (one column per series).

    Each record ``{"label", "x", "y", "x_kind", "unit"}`` becomes a column,
    aligned (outer join) on the x index. Numeric records with a unit get a
    ``pint[<unit>]`` dtype; a record with no unit (e.g. a text-log channel) or
    non-numeric values becomes a plain object column, so all checked channels -
    not just the plotted numeric ones - are exportable. Rows are capped at
    ``max_rows`` via uniform np.linspace downsampling (logged). Returns ``None``
    if pandas/pint-pandas are missing or there is nothing to export.
    """
    try:
        import numpy as np
        import pandas as pd
        import pint_pandas  # noqa: F401
    except ImportError:
        _logger.warning(
            "Data export needs the 'export' extra (pandas, pint-pandas, openpyxl)."
        )
        return None
    if not series:
        return None

    parts = []
    seen = {}
    for s in series:
        idx = pd.Index(s["x"], name=s.get("x_kind") or "x")
        unit = s.get("unit")
        col = None
        if unit:
            try:
                col = pd.Series(s["y"], index=idx, dtype=f"pint[{unit}]")
            except Exception:
                col = None
        if col is None:
            # No unit (text/log) or an unparseable one: numerics fall back to a
            # dimensionless pint column, text values to a plain object column.
            try:
                col = pd.Series(s["y"], index=idx, dtype="pint[dimensionless]")
            except Exception:
                col = pd.Series(s["y"], index=idx, dtype="object")
        # A well-defined outer join needs a unique index per column.
        col = col[~col.index.duplicated(keep="last")]
        # Unique column names: pandas returns a DataFrame (not a Series) for a
        # duplicated label, which would break the per-column dequantify.
        label = s["label"]
        if label in seen:
            seen[label] += 1
            label = f"{label} ({seen[label]})"
        else:
            seen[label] = 0
        col.name = label
        parts.append(col)
    if not parts:
        return None

    df = pd.concat(parts, axis=1).sort_index()
    n = len(df)
    if n > max_rows:
        sel = np.linspace(0, n - 1, max_rows).astype(int)
        df = df.iloc[sel]
        _logger.info("Data export capped from %d to %d rows.", n, max_rows)
    return df


def _dequantify(df):
    """Like ``df.pint.dequantify()`` but tolerant of non-pint (text) columns.

    Produces a two-level column header ``(label, unit)`` so a dedicated unit
    header row is written on export and the column keys stay unit-free. Pint
    columns contribute their magnitude and unit; plain (text) columns keep their
    values under an empty unit.
    """
    import pandas as pd

    tuples = []
    mags = []
    for name in df.columns:
        s = df[name]
        if str(s.dtype).startswith("pint"):
            tuples.append((str(name), str(s.pint.units)))
            mags.append(s.pint.magnitude)
        else:
            tuples.append((str(name), ""))
            mags.append(s)
    out = pd.concat(mags, axis=1)
    out.columns = pd.MultiIndex.from_tuples(tuples, names=[None, "unit"])
    return out


class BaseDataView:
    """Mixin with the UI/plot pieces shared by the archive views."""

    @property
    def lang(self) -> str:
        return self._config.lang

    # -- Unit-switch controls (one dropdown per quantity group) --

    def _update_unit_controls(self):
        self._unit_controls.clear()
        for group_key, channels in self._groups.items():
            if not channels:
                continue
            sample_ch = channels[0][1]
            if resolve_value_type(sample_ch) != "quantity":
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

    # -- Plot / log / config cards --

    def _build_plot(self):
        # No height cap / inner scroll here: the plots stack at their natural
        # height and the main content area (.content) scrolls as one. A fixed
        # max_height + scroll=True would add a second, nested scrollbar.
        self._plot_col = pn.Column(sizing_mode="stretch_width")
        self._plot_card = pn.Card(
            self._plot_col,
            title=_t("time_series", self.lang),
            sizing_mode="stretch_width",
        )

    # -- Export (data + plot) --

    _figures: List[Any] = []

    @property
    def figures(self) -> List[Any]:
        """The Bokeh figures currently rendered (updated by _build_figure).

        Exposed so host apps can add their own annotations; also used by the
        HTML plot export. Empty when nothing is plotted.
        """
        return list(getattr(self, "_figures", []) or [])

    def _build_export_toolbar(self):
        """Compact collapsed "Export" card of FileDownload buttons.

        A dashboard-level action (data CSV/XLSX + plot HTML), so it lives in the
        Plot Controls sidebar rather than inside the Time Series group. Returns
        the card and stores it on ``self._export_box`` for host embedding.
        """
        deps_note = "" if _EXPORT_DEPS_OK else " (needs opensemantic.base[export])"
        self._download_csv = pn.widgets.FileDownload(
            label=_t("download_csv", self.lang) + deps_note,
            filename="dashboard_data.csv",
            callback=lambda: self._build_data_export("csv"),
            button_type="default",
            disabled=True,
            sizing_mode="stretch_width",
        )
        self._download_xlsx = pn.widgets.FileDownload(
            label=_t("download_xlsx", self.lang) + deps_note,
            filename="dashboard_data.xlsx",
            callback=lambda: self._build_data_export("xlsx"),
            button_type="default",
            disabled=True,
            sizing_mode="stretch_width",
        )
        self._download_html = pn.widgets.FileDownload(
            label=_t("download_plot", self.lang),
            filename="dashboard_plot.html",
            callback=self._build_plot_html,
            button_type="default",
            disabled=True,
            sizing_mode="stretch_width",
        )
        self._export_box = pn.Card(
            pn.Column(
                self._download_csv,
                self._download_xlsx,
                self._download_html,
                sizing_mode="stretch_width",
            ),
            title=_t("export", self.lang),
            collapsed=True,
            sizing_mode="stretch_width",
            # Separate this tile from the controls above and the next section.
            margin=(10, 5),
        )
        return self._export_box

    def _has_export_log(self) -> bool:
        """Whether the log console currently has content to include in HTML."""
        pane = getattr(self, "_log_pane", None)
        card = getattr(self, "_log_card", None)
        return bool(getattr(pane, "object", "")) and bool(
            getattr(card, "visible", False)
        )

    def _update_export_state(self):
        """Enable/disable export buttons based on what is available.

        Data (CSV/XLSX) covers every checked channel, so it keys off
        ``export_series`` (numeric + text). The plot HTML covers the rendered
        figures and the log console, so it keys off either being present.
        """
        has_data = bool(self.export_series())
        has_html = bool(self.figures) or self._has_export_log()
        for w in ("_download_csv", "_download_xlsx"):
            btn = getattr(self, w, None)
            if btn is not None:
                btn.disabled = not (has_data and _EXPORT_DEPS_OK)
        if getattr(self, "_download_html", None) is not None:
            self._download_html.disabled = not has_html

    def _build_data_export(self, fmt: str) -> io.BytesIO:
        """Build a unit-aware CSV/XLSX of the plotted series via pint-pandas.

        Serialized with the pint-pandas ``dequantify`` accessor so a dedicated
        unit header row is written and the column keys stay unit-free. Returns a
        (possibly empty) BytesIO.
        """
        buf = io.BytesIO()
        cap = int(getattr(self, "EXPORT_MAX_ROWS", EXPORT_MAX_ROWS))
        df = _series_to_dataframe(self.export_series(), cap)
        if df is None or df.empty:
            return buf
        import pandas as pd

        dequantified = _dequantify(df)
        if fmt == "xlsx":
            try:
                with pd.ExcelWriter(buf, engine="openpyxl") as writer:
                    dequantified.to_excel(writer)
            except ImportError:
                _logger.warning("XLSX export needs openpyxl (base[export]).")
                return io.BytesIO()
        else:
            buf.write(dequantified.to_csv().encode("utf-8"))
        buf.seek(0)
        return buf

    def _export_figures(self) -> List[Any]:
        """Figures to serialize for HTML export.

        Defaults to the live ``figures``; views whose figures are attached to a
        Panel document override this to return a fresh, unattached copy, since
        ``file_html`` requires models that belong to no other document.
        """
        return self.figures

    #: Fixed frame size for HTML-exported figures.
    EXPORT_PLOT_WIDTH = 1000
    EXPORT_PLOT_HEIGHT = 350

    def _export_extra_models(self) -> List[Any]:
        """Extra Bokeh models to append below the figures in the HTML export.

        Includes the log console (as an HTML ``Div``) when it has content, so a
        dashboard with text-log channels carries them into the export too.
        """
        models: List[Any] = []
        if self._has_export_log():
            from bokeh.models import Div

            # Scrollable inner container so a long log does not stretch the page;
            # the heading stays fixed above it.
            width = self.EXPORT_PLOT_WIDTH
            models.append(
                Div(
                    text=(
                        f"<h3 style='margin:0 0 4px'>{_t('log_console', self.lang)}"
                        f"</h3>"
                        # Explicit width on the box: Bokeh's Div content wrapper
                        # is content-sized, so match the fixed plot width here.
                        f"<div style='width:{width}px;box-sizing:border-box;"
                        f"max-height:300px;overflow-y:auto;"
                        f"border:1px solid #ccc;padding:4px'>"
                        f"{self._log_pane.object}</div>"
                    ),
                    width=width,
                    sizing_mode="fixed",
                )
            )
        return models

    def _build_plot_html(self) -> io.BytesIO:
        """Standalone interactive HTML of the figures + log console."""
        buf = io.BytesIO()
        figs = self._export_figures()
        extras = self._export_extra_models()
        if not figs and not extras:
            return buf
        from bokeh.embed import file_html
        from bokeh.layouts import column as bk_column
        from bokeh.resources import CDN

        # The live figures are stretch_width, but a standalone HTML has no
        # container width to stretch into, so the plot frame collapses to zero
        # width. Pin an explicit size on the export copies so they render.
        for fig in figs:
            try:
                fig.sizing_mode = "fixed"
                fig.width = self.EXPORT_PLOT_WIDTH
                fig.height = self.EXPORT_PLOT_HEIGHT
            except Exception:  # pragma: no cover - non-figure layout
                pass

        roots = list(figs) + list(extras)
        root = roots[0] if len(roots) == 1 else bk_column(*roots)
        html = file_html(root, CDN, getattr(self, "_title", "Plot"))
        buf.write(html.encode("utf-8"))
        buf.seek(0)
        return buf

    def _build_log_console(self):
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

    # -- Plot helpers --

    def _refresh_plot(self):
        """Rebuild plot and log console from the subclass's loaded data."""
        self._build_figure()
        self._update_log_console()
        self._update_export_state()

    def _numeric(self, value: Any, channel: Any, target_unit_name: Any) -> Any:
        """Convert a value to its display unit and return the numeric scalar.

        Handles typed Characteristic instances (with ``to_unit``), raw dicts
        and bare scalars. Returns ``None`` when no value can be extracted.
        """
        if target_unit_name and hasattr(value, "to_unit"):
            unit_enum = get_unit_enum(channel)
            if unit_enum is not None and target_unit_name in unit_enum.__members__:
                try:
                    value = value.to_unit(unit_enum[target_unit_name])
                except Exception:
                    pass
        if hasattr(value, "value"):
            return value.value
        if isinstance(value, dict):
            return value.get("value")
        return value

    def _pint_unit(self, value: Any, target_unit_name: Any) -> str:
        """Pint-parseable unit string for a typed value in its display unit.

        Converts the value to ``target_unit_name`` (like the plot) and reads the
        pint unit off ``to_pint()``. Falls back to ``"dimensionless"`` for raw /
        untyped values so the export column is always a valid pint dtype.
        """
        if value is None or not hasattr(value, "to_pint"):
            return "dimensionless"
        try:
            if target_unit_name and hasattr(value, "to_unit"):
                unit = getattr(value, "unit", None)
                enum = type(unit) if unit is not None else None
                if enum is not None and hasattr(enum, "__members__"):
                    if target_unit_name in enum.__members__:
                        value = value.to_unit(enum[target_unit_name])
            return str(value.to_pint().units)
        except Exception:
            return "dimensionless"

    def _get_axis_label(self, group_key: str) -> str:
        """Build y-axis label: characteristic name [unit symbol]."""
        channels = self._groups.get(group_key, [])
        if not channels:
            return ""
        sample_ch = channels[0][1]
        char_label = resolve_characteristic_label(sample_ch, self.lang)

        unit_name = self._unit_selections.get(group_key)
        if unit_name:
            for u in get_available_units(sample_ch):
                if u["name"] == unit_name:
                    return f"{char_label} [{u['symbol']}]"

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

    # -- Data loading entry point (async-context aware) --

    def _trigger_load(self):
        """Start data loading, handling sync vs running-loop contexts."""
        try:
            asyncio.get_running_loop()
            asyncio.ensure_future(self._load_and_plot())
        except RuntimeError:
            asyncio.run(self._load_and_plot())

    # -- Serving --

    def servable(self, **kwargs):
        if self._app is None:
            raise RuntimeError(
                f"{type(self).__name__}(embeddable=True) has no servable app; "
                "place sidebar_cards / main_cards into a host app instead."
            )
        return self._app.servable(**kwargs)

    def panel(self):
        """Return the Panelini app for embedding (None when embeddable)."""
        return self._app

    # -- Subclass contract (implemented by each view) --

    def _build_figure(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def export_series(self) -> List[dict]:  # pragma: no cover - overridden
        """Return the currently plotted series as tidy records.

        Each record: ``{"label", "x", "y", "x_kind": "datetime"|"seconds",
        "unit"}`` where ``unit`` is a pint-parseable string. Built by reusing
        the same trace extraction as ``_build_figure`` so the export matches
        exactly what is plotted (unit conversion + normalization).
        """
        raise NotImplementedError

    def _update_log_console(self):  # pragma: no cover - overridden
        raise NotImplementedError

    async def _load_and_plot(self):  # pragma: no cover - overridden
        raise NotImplementedError

    def _on_config_editor_change(self, event):  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def sidebar_cards(self) -> List[Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    @property
    def main_cards(self) -> List[Any]:  # pragma: no cover - overridden
        raise NotImplementedError


# Re-exported helper type for subclasses that annotate group maps.
GroupList = List[Tuple[Any, Any]]
