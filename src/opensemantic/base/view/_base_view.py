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
        self._plot_col = pn.Column(
            sizing_mode="stretch_width", scroll=True, max_height=600
        )
        self._plot_card = pn.Card(
            self._plot_col,
            title=_t("time_series", self.lang),
            sizing_mode="stretch_width",
        )

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
