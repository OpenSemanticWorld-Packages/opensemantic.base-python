"""Configuration models for the DataTool dashboard UI."""

from enum import Enum
from typing import List

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


class DownsampleConfig(BaseModel):
    """Server-side downsampling configuration.

    When enabled and the backend supports it (PostgREST/TimescaleDB), the
    plot requests about ``max_points`` points per channel instead of the
    full series. ``average`` and ``minmax`` aggregate the bare stored
    numbers and are only correct for unit-normalized data; ``auto`` picks
    ``minmax`` for numeric channels and ``sample`` for text channels.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "title": "DownsampleConfig",
            "defaultProperties": [
                "enabled",
                "max_points",
                "method",
                "edge_anchors",
            ],
        }
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
    """Wunderbaum TreeGrid configuration."""

    model_config = ConfigDict(
        json_schema_extra={"title": "TreeConfig", "defaultProperties": []}
    )


class PlotConfig(BaseModel):
    """Time series plot configuration."""

    model_config = ConfigDict(
        json_schema_extra={
            "title": "PlotConfig",
            "defaultProperties": [
                "grouping",
                "auto_fetch",
                "row_limit",
                "cache_enabled",
                "downsample",
            ],
        }
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


class LiveConfig(BaseModel):
    """Live subscription configuration."""

    model_config = ConfigDict(
        json_schema_extra={
            "title": "LiveConfig",
            "defaultProperties": [
                "buffer_size",
                "update_interval_ms",
                "history_seconds",
            ],
        }
    )

    buffer_size: int = Field(
        1000,
        title="Buffer size",
        ge=10,
        json_schema_extra={"title*": {"de": "Puffergroesse"}},
    )
    update_interval_ms: int = Field(
        500,
        title="Update interval (ms)",
        ge=50,
        json_schema_extra={"title*": {"de": "Aktualisierungsintervall (ms)"}},
    )
    history_seconds: float = Field(
        10.0,
        title="History window (s)",
        gt=0,
        json_schema_extra={"title*": {"de": "Verlaufsfenster (s)"}},
    )


class DashboardConfig(BaseModel):
    """Root dashboard configuration."""

    model_config = ConfigDict(
        json_schema_extra={
            "title": "DashboardConfig",
            "defaultProperties": ["controllers", "lang", "tree", "plot"],
        }
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
    tree: TreeConfig = Field(default_factory=TreeConfig, title="Tree")
    plot: PlotConfig = Field(default_factory=PlotConfig, title="Plot")


class LiveDashboardConfig(DashboardConfig):
    """Dashboard configuration with live subscription support."""

    model_config = ConfigDict(
        json_schema_extra={
            "title": "LiveDashboardConfig",
            "defaultProperties": ["controllers", "lang", "tree", "plot", "live"],
        }
    )

    live: LiveConfig = Field(default_factory=LiveConfig, title="Live")
