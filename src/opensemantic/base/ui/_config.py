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
        GroupingMode.UNIQUE, title="Grouping mode", title_={"de": "Gruppierung"}
    )
    auto_fetch: bool = Field(
        True,
        title="Auto-fetch",
        description="Fetch data on any config or selection change",
        description_={
            "de": "Daten bei jeder Konfigurations- oder Auswahlaenderung laden"
        },
    )
    row_limit: int = Field(
        10000,
        title="Row limit",
        title_={"de": "Zeilenlimit"},
        description="Maximum number of rows per request",
        description_={"de": "Maximale Anzahl Zeilen pro Anfrage"},
        ge=1,
    )
    cache_enabled: bool = Field(
        True,
        title="Cache enabled",
        title_={"de": "Cache aktiviert"},
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
        title_={"de": "Puffergroesse"},
        ge=10,
    )
    update_interval_ms: int = Field(
        500,
        title="Update interval (ms)",
        title_={"de": "Aktualisierungsintervall (ms)"},
        ge=50,
    )
    history_seconds: float = Field(
        10.0,
        title="History window (s)",
        title_={"de": "Verlaufsfenster (s)"},
        gt=0,
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
        title_={"de": "DataTool-IRIs"},
        description="List of DataTool IRIs to display",
        description_={"de": "Liste der anzuzeigenden DataTool-IRIs"},
    )
    lang: LangCode = Field(
        LangCode.EN,
        title="Language",
        title_={"de": "Sprache"},
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
