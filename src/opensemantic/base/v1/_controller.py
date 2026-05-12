"""v1 controller classes for opensemantic.base.

Composes mixin methods with v1 Database and DataTool models.
Database controllers use driver composition (not mixin inheritance).
"""

from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Union

from pydantic import ConfigDict, PrivateAttr

from opensemantic.base._controller_mixin import (
    DataToolMixin,
    TSDCMixin,
)
from opensemantic.base.v1._model import Database as _Database
from opensemantic.base.v1._model import DataTool as _DataTool


class DataToolController(DataToolMixin, _DataTool):
    """Generic DataTool controller (v1)."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    subdevices: Optional[List["DataToolController"]] = []
    archive_database: Optional[Any] = None
    auto_archive: bool = False
    credential_manager: Optional[Any] = None

    _channel_dict: Dict = PrivateAttr(default_factory=dict)
    _channel_datachange_notification_callback: Optional[
        Callable[..., Awaitable[Any]]
    ] = PrivateAttr(default=None)


class TimeSeriesDatabaseController(TSDCMixin, _Database):
    """Time series database controller extending the v1 Database model."""

    pass


try:
    import aiosqlite  # noqa: F401

    from opensemantic.base._drivers import LocalDriver

    class LocalTimeSeriesDatabaseController(TimeSeriesDatabaseController):
        """SQLite-based local time series database controller (v1)."""

        db_path: Union[str, Path]
        buffered: bool = False
        buffer_batch_size: int = 100
        _driver: LocalDriver = PrivateAttr(default=None)

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._driver = LocalDriver(
                self.db_path,
                buffered=self.buffered,
                buffer_batch_size=self.buffer_batch_size,
            )

        async def create_tool(self, params: TSDCMixin.CreateToolParams):
            return await self._driver.create_tool(params.tool_osw_id)

        async def delete_tool(self, params: TSDCMixin.DeleteToolParams):
            return await self._driver.delete_tool(params.tool_osw_id)

        async def get_tools_list(self) -> List[str]:
            return await self._driver.get_tools_list()

        async def write_tool_channel_raw(
            self, params: TSDCMixin.WriteToolChannelRawParams
        ):
            return await self._driver.write(params.tool_osw_id, params.data)

        async def read_tool_channel_raw(
            self, params: TSDCMixin.ReadToolChannelRawParams
        ):
            filters = None
            if params.filter:
                filters = [
                    {
                        "column": (
                            f.column.value
                            if isinstance(f.column, TSDCMixin.FilterColumn)
                            else f.column
                        ),
                        "operator": f.operator.value,
                        "criteria": f.criteria,
                    }
                    for f in params.filter
                ]
            return await self._driver.read(
                tool_osw_id=params.tool_osw_id,
                channel_osw_id=params.channel_osw_id,
                start=params.start,
                end=params.end,
                filters=filters,
                limit=params.limit,
            )

        async def delete_by_ids(self, tool_osw_id: str, ids: List[int]):
            return await self._driver.delete_by_ids(tool_osw_id, ids)

        async def get_table_size(self, tool_osw_id: str) -> int:
            return await self._driver.get_table_size(tool_osw_id)

        async def flush_buffer(self, tool_osw_id: Optional[str] = None):
            return await self._driver.flush_buffer(tool_osw_id)

except ImportError:
    pass


try:
    from postgrest import AsyncPostgrestClient  # noqa: F401

    from opensemantic.base._drivers import PostgrestDriver

    class PostgrestTimeSeriesDatabaseController(TimeSeriesDatabaseController):
        """PostgREST-based remote time series database controller (v1)."""

        buffered: bool = True
        buffer_batch_size: int = 100
        buffer_offline_location: Optional[Path] = Path("buffered_data.sqlite")
        buffer_offline_batch_size: int = 500
        buffer_offline_sync_interval: float = 0.2
        _driver: PostgrestDriver = PrivateAttr(default=None)

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._driver = PostgrestDriver(
                buffered=self.buffered,
                buffer_batch_size=self.buffer_batch_size,
                buffer_offline_location=self.buffer_offline_location,
                buffer_offline_batch_size=self.buffer_offline_batch_size,
                buffer_offline_sync_interval=(self.buffer_offline_sync_interval),
            )

        def set_client(self, client):
            self._driver.set_client(client)

        async def create_tool(self, params: TSDCMixin.CreateToolParams):
            return await self._driver.create_tool(params.tool_osw_id)

        async def delete_tool(self, params: TSDCMixin.DeleteToolParams):
            return await self._driver.delete_tool(params.tool_osw_id)

        async def get_tools_list(self) -> List[str]:
            return await self._driver.get_tools_list()

        async def write_tool_channel_raw(
            self, params: TSDCMixin.WriteToolChannelRawParams
        ):
            return await self._driver.write(params.tool_osw_id, params.data)

        async def read_tool_channel_raw(
            self, params: TSDCMixin.ReadToolChannelRawParams
        ):
            filters = None
            if params.filter:
                filters = [
                    {
                        "column": (
                            f.column.value
                            if isinstance(f.column, TSDCMixin.FilterColumn)
                            else f.column
                        ),
                        "operator": f.operator.value,
                        "criteria": f.criteria,
                    }
                    for f in params.filter
                ]
            return await self._driver.read(
                tool_osw_id=params.tool_osw_id,
                channel_osw_id=params.channel_osw_id,
                start=params.start,
                end=params.end,
                filters=filters,
                limit=params.limit,
            )

        async def get_tool_config(self):
            return await self._driver.get_tool_config()

        async def flush_buffer(self, tool_osw_id=None):
            return await self._driver.flush_buffer(tool_osw_id)

        async def start_offline_sync(self):
            await self._driver.start_offline_sync()

except ImportError:
    pass
