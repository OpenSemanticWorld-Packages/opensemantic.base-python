"""Configuration models for the DataTool dashboard UI."""

from enum import Enum
from typing import List

from pydantic import ConfigDict, Field

from opensemantic import OswBaseModel


class LangCode(str, Enum):
    EN = "en"
    DE = "de"


class GroupingMode(str, Enum):
    NONE = "group-none"
    UNIQUE = "group-unique-characteristics"
    SUB = "group-subcharacteristics"


class TreeConfig(OswBaseModel):
    """Wunderbaum TreeGrid configuration."""

    model_config = ConfigDict(
        json_schema_extra={"title": "TreeConfig", "defaultProperties": []}
    )


class PlotConfig(OswBaseModel):
    """Time series plot configuration."""

    model_config = ConfigDict(
        json_schema_extra={
            "title": "PlotConfig",
            "defaultProperties": [
                "grouping",
                "auto_fetch",
                "row_limit",
                "cache_enabled",
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


class LiveConfig(OswBaseModel):
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


class DashboardConfig(OswBaseModel):
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
