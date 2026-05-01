"""v2 controller classes for opensemantic.base.

Composes mixin methods with v2 Database and DataTool models.
"""

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from pydantic import ConfigDict, PrivateAttr

from opensemantic.base._controller_mixin import (
    DataToolMixin,
    LocalTSDCMixin,
    PostgrestTSDCMixin,
    TSDCMixin,
)
from opensemantic.base._model import Database as _Database
from opensemantic.base._model import DataTool as _DataTool


class DataToolController(DataToolMixin, _DataTool):
    """Generic DataTool controller (v2).

    Provides channel management, subdevice traversal, archiving,
    and data change handling for any DataTool.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    subdevices: Optional[List["DataToolController"]] = []
    archive_database: Optional[Any] = None
    auto_archive: bool = False
    credential_manager: Optional[Any] = None
    """Optional CredentialManager instance. If set, get_credential() uses it
    instead of the global oold.backend.auth store."""

    _channel_dict: Dict = PrivateAttr(default_factory=dict)
    _channel_datachange_notification_callback: Optional[
        Callable[..., Awaitable[Any]]
    ] = PrivateAttr(default=None)


class TimeSeriesDatabaseController(TSDCMixin, _Database):
    """Time series database controller extending the v2 Database model."""

    pass


try:
    import aiosqlite  # noqa: F401

    class LocalTimeSeriesDatabaseController(
        LocalTSDCMixin, TimeSeriesDatabaseController
    ):
        """SQLite-based local time series database controller (v2)."""

        db_path: Union[str, Path]

except ImportError:
    pass


try:
    from postgrest import AsyncPostgrestClient  # noqa: F401

    class PostgrestTimeSeriesDatabaseController(
        PostgrestTSDCMixin, TimeSeriesDatabaseController
    ):
        """PostgREST-based remote time series database controller (v2)."""

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            if self._local_db is None and self.buffer_offline_location:
                self._local_db = LocalTimeSeriesDatabaseController(
                    name=self.name,
                    label=self.label,
                    db_path=self.buffer_offline_location,
                )
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._flush_offline_buffer())
            except RuntimeError:
                pass

except ImportError:
    pass
