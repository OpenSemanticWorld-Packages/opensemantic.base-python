"""Base and shared component configuration for the archive-view dashboards.

The config mirrors the panel composition: each UI component owns a config class
(``TreeConfig``, ``PlotControlsConfig``, ``DownsampleConfig``), each view owns a
``BaseViewConfig`` subclass that composes the component configs it shows, and a
host that renders several views composes their configs into an aggregate parent
model. Creating a new dashboard therefore means: subclass ``BaseDataView`` (the
view) and ``BaseViewConfig`` (its config, defined next to the view), and provide
the config->view ``_apply_config`` hook.

Only the base and the configs shared across views live here; each concrete
view's config lives in the view's own module (e.g. ``DataToolViewConfig`` in
``_datatool_dashboard``).

The tree components reuse Wunderbaum's own JSON serialization (its ``source``
list of node dicts, which already carries the ``selected`` state) rather than a
parallel selection model - ``TreeConfig.source`` is exactly what
``Wunderbaum.get_source()`` / ``set_source()`` round-trip.
"""

from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class LangCode(str, Enum):
    EN = "en"
    DE = "de"


class GroupingMode(str, Enum):
    NONE = "group-none"
    UNIQUE = "group-unique-characteristics"
    SUB = "group-subcharacteristics"


class DownsampleMethod(str, Enum):
    AUTO = "auto"
    SAMPLE = "sample"
    AVERAGE = "average"
    MINMAX = "minmax"


# -- Value objects ----------------------------------------------------------


class TimeRange(BaseModel):
    """Explicit plot time window. Naive datetimes are treated as UTC."""

    model_config = ConfigDict(
        json_schema_extra={
            "title": "TimeRange",
            "defaultProperties": ["start", "end"],
        }
    )

    start: Optional[datetime] = Field(
        None, title="Start", json_schema_extra={"title*": {"de": "Start"}}
    )
    end: Optional[datetime] = Field(
        None, title="End", json_schema_extra={"title*": {"de": "Ende"}}
    )


# -- Component configs (one per UI component) -------------------------------


class DownsampleConfig(BaseModel):
    """Server-side downsampling configuration.

    When enabled and the backend supports it (PostgREST/TimescaleDB), the
    plot requests about ``max_points`` points per channel instead of the
    full series. ``average`` and ``minmax`` aggregate the bare stored
    numbers and are only correct for unit-normalized data; ``auto`` picks
    ``minmax`` for numeric channels and ``sample`` for text channels.
    """

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "title": "DownsampleConfig",
            "defaultProperties": [
                "enabled",
                "max_points",
                "method",
                "edge_anchors",
            ],
        },
    )

    enabled: bool = Field(
        True,
        title="Downsample enabled",
        json_schema_extra={"title*": {"de": "Downsampling aktiviert"}},
    )
    max_points: int = Field(
        2000,
        title="Max points",
        description="Target number of points per channel",
        ge=2,
        json_schema_extra={
            "title*": {"de": "Maximale Punkte"},
            "description*": {"de": "Zielanzahl Punkte pro Kanal"},
        },
    )
    method: DownsampleMethod = Field(
        DownsampleMethod.AUTO,
        title="Method",
        json_schema_extra={"title*": {"de": "Methode"}},
    )
    edge_anchors: bool = Field(
        True,
        title="Edge anchors",
        description="Keep the window's first/last real datapoints as endpoints",
        json_schema_extra={
            "title*": {"de": "Randpunkte"},
            "description*": {
                "de": "Ersten/letzten realen Datenpunkt des Fensters behalten"
            },
        },
    )


class TreeConfig(BaseModel):
    """Config of a Wunderbaum tree component - its selection state.

    Stores only the ``selected`` node keys, not the full native source: the tree
    structure is rebuilt deterministically from the current data (controllers /
    objects), so the persisted state is just which keys are checked. This keeps
    the config compact and human-readable (a list of uuids) instead of dumping
    every node's title/tooltip/data, which would bloat a URL-synced config.
    """

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "title": "TreeConfig",
            "defaultProperties": ["selected"],
        },
    )

    selected: List[str] = Field(
        default_factory=list,
        title="Selected keys",
        description="Keys of the checked tree nodes",
        json_schema_extra={"title*": {"de": "Ausgewaehlte Schluessel"}},
    )


class PlotControlsConfig(BaseModel):
    """Config of the Plot Controls component (and the plot it drives)."""

    model_config = ConfigDict(
        extra="allow",
        json_schema_extra={
            "title": "PlotControlsConfig",
            "defaultProperties": [
                "grouping",
                "auto_fetch",
                "row_limit",
                "cache_enabled",
                "downsample",
                "unit_selections",
            ],
        },
    )

    grouping: GroupingMode = Field(
        GroupingMode.UNIQUE,
        title="Grouping mode",
        json_schema_extra={"title*": {"de": "Gruppierung"}},
    )
    auto_fetch: bool = Field(
        True,
        title="Auto-fetch",
        description="Fetch data on any config or selection change",
        json_schema_extra={
            "description*": {
                "de": "Daten bei jeder Konfigurations- oder Auswahlaenderung laden"
            }
        },
    )
    row_limit: int = Field(
        10000,
        title="Row limit",
        description="Maximum number of rows per request",
        ge=1,
        json_schema_extra={
            "title*": {"de": "Zeilenlimit"},
            "description*": {"de": "Maximale Anzahl Zeilen pro Anfrage"},
        },
    )
    cache_enabled: bool = Field(
        True,
        title="Cache enabled",
        json_schema_extra={"title*": {"de": "Cache aktiviert"}},
    )
    downsample: DownsampleConfig = Field(
        default_factory=DownsampleConfig,
        title="Downsample",
        json_schema_extra={"title*": {"de": "Downsampling"}},
    )
    unit_selections: Dict[str, str] = Field(
        default_factory=dict,
        title="Unit selections",
        description="Selected display unit per characteristic group",
        json_schema_extra={"title*": {"de": "Einheitenauswahl"}},
    )


# -- Base view config -------------------------------------------------------


class BaseViewConfig(BaseModel):
    """Base view configuration - the complete, JSON-serializable state common
    to every archive view.

    A view owns its config class: each concrete view pairs a ``BaseDataView``
    subclass with its own ``BaseViewConfig`` subclass (defined in the view's
    module). ``extra="allow"`` lets a subclass (or an unknown future field)
    survive a ``model_dump()`` / ``model_validate()`` round-trip, and
    ``validate_assignment`` keeps field mutation valid (needed for URL binding).
    """

    model_config = ConfigDict(
        extra="allow",
        validate_assignment=True,
        json_schema_extra={
            "title": "BaseViewConfig",
            "defaultProperties": ["controllers", "lang", "plot"],
        },
    )

    controllers: List[str] = Field(
        default_factory=list,
        title="DataTool IRIs",
        description="List of DataTool IRIs to display",
        json_schema_extra={
            "title*": {"de": "DataTool-IRIs"},
            "description*": {"de": "Liste der anzuzeigenden DataTool-IRIs"},
        },
    )
    lang: LangCode = Field(
        LangCode.EN,
        title="Language",
        json_schema_extra={"title*": {"de": "Sprache"}},
    )
    plot: PlotControlsConfig = Field(
        default_factory=PlotControlsConfig, title="Plot controls"
    )
