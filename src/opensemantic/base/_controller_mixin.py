"""Mixin classes with all controller methods and inner types.

These mixins are composed with the appropriate v1 or v2 model base class
in _controller.py and v1/_controller.py respectively.
No model imports here - only stdlib, pydantic BaseModel, and _controller_logic.
"""

import asyncio
import datetime as dt
import json
import logging
from abc import abstractmethod
from copy import deepcopy
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from opensemantic.base._controller_logic import (
    build_sqlite_read_query,
    check_buffer_duplicates,
    parse_sqlite_rows,
)

_logger = logging.getLogger(__name__)


class DataToolMixin:
    """Generic controller mixin for DataTool models.

    Provides: identity, channel/subdevice traversal, archiving, data change handling.
    Compose with a DataTool model class:
        class DataToolController(DataToolMixin, DataTool): pass
    """

    def __init__(self, **data):
        super().__init__(**data)
        self._compute_subobject_ids()
        if not isinstance(getattr(self, "_channel_dict", None), dict):
            object.__setattr__(self, "_channel_dict", {})
        for channel in self.get_all_channels():
            self._channel_dict[channel.node_id] = channel

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            super().__setattr__(name, value)

    # get_osw_id() and get_iri() are inherited from OswBaseModel
    # via the model base class (DataTool -> Entity -> OswBaseModel)

    # TODO: Consider moving _compute_subobject_ids to OswBaseModel
    def _compute_subobject_ids(self, parent_chain=None):
        """Compute composite osw_ids for inline subobject children.

        For each field without 'range' in json_schema_extra (i.e. not a wiki
        reference), prefix the child's osw_id with the parent's chain.

        Called automatically in __init__. For mutations after construction,
        call this method manually to recompute.
        """
        my_uuid = self.get_uuid()
        if my_uuid is None:
            return
        base_id = f"OSW{str(my_uuid).replace('-', '')}"

        if parent_chain:
            self.osw_id = f"{parent_chain}#{base_id}"

        my_osw_id = getattr(self, "osw_id", None) or base_id

        fields = {}
        if hasattr(self, "model_fields"):
            fields = self.model_fields
        elif hasattr(self, "__fields__"):
            fields = self.__fields__

        for field_name, field_info in fields.items():
            # Check for 'range' in field metadata
            # v2: json_schema_extra, v1: field_info.extra
            extra = getattr(field_info, "json_schema_extra", None) or {}
            if not extra and hasattr(field_info, "field_info"):
                extra = getattr(field_info.field_info, "extra", {}) or {}
            if "range" in extra:
                continue

            value = getattr(self, field_name, None)
            if value is None:
                continue

            children = []
            items = value if isinstance(value, list) else [value]
            for item in items:
                if hasattr(item, "get_uuid") and hasattr(item, "osw_id"):
                    children.append(item)

            for child in children:
                child_uuid = child.get_uuid()
                if child_uuid is None:
                    continue
                child_base_id = f"OSW{str(child_uuid).replace('-', '')}"
                new_osw_id = f"{my_osw_id}#{child_base_id}"
                if child.osw_id != new_osw_id:
                    child.osw_id = new_osw_id
                # Recurse if child also has _compute_subobject_ids
                if hasattr(child, "_compute_subobject_ids"):
                    child._compute_subobject_ids(parent_chain=new_osw_id)

    def get_credential(self, iri: str):
        """Look up a credential for the given IRI.

        Uses the instance's credential_manager if set, otherwise falls back
        to the global oold.backend.auth.get_credential store.

        Parameters
        ----------
        iri
            The IRI to look up credentials for.
        """
        from oold.backend.auth import get_credential as _global_get_credential

        if getattr(self, "credential_manager", None) is not None:
            from oold.backend.auth import CredentialManager

            config = CredentialManager.CredentialConfig(iri=iri)
            return self.credential_manager.get_credential(config)
        return _global_get_credential(iri)

    def get_subdevices(self) -> list:
        if self.subdevices is None:
            return []
        result = list(self.subdevices)
        for sub in self.subdevices:
            result.extend(sub.get_subdevices())
        return result

    def get_all_channels(self) -> list:
        channels = list(self.data_channels or [])
        for sub in self.subdevices or []:
            channels.extend(sub.get_all_channels())
        return channels

    def get_channel_owner(self, channel):
        own_uuids = [c.uuid for c in (self.data_channels or [])]
        if channel.uuid in own_uuids:
            return self
        for sub in self.subdevices or []:
            try:
                return sub.get_channel_owner(channel)
            except ValueError:
                continue
        raise ValueError(
            f"Channel {channel.name} with uuid {channel.uuid} "
            f"not found in any device controller"
        )

    # -- Inner param/result classes --

    class ChannelDataChangeNotificationParams(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        channel: Any = None
        value: Any = None
        timestamp: Optional[dt.datetime] = None

    class ReadArchiveDataParams(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        channel: Any = None
        start: Optional[dt.datetime] = None
        end: Optional[dt.datetime] = None
        max_rows: Optional[int] = 1000

    class ReadArchiveDataResultRow(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        timestamp: dt.datetime
        channel: Any = None
        data: Dict[str, Any] = {}

    class ReadArchiveDataResult(BaseModel):
        data: List[Any] = []

    class AutoArchiveParams(BaseModel):
        enable: bool = True

    # -- Async methods --

    async def _handle_data_change(self, params):
        if not hasattr(self, "_last_values"):
            self._last_values = {}
        if params.channel.uuid in self._last_values:
            last = self._last_values[params.channel.uuid]
            if last.value == params.value and last.timestamp == params.timestamp:
                _logger.warning(
                    "Duplicate data change for %s, ignoring", params.channel.name
                )
                return
        self._last_values[params.channel.uuid] = params

        if self.auto_archive and self.archive_database is not None:
            owner = self.get_channel_owner(params.channel)
            if owner.auto_archive:
                try:
                    tool_osw_id = owner.get_osw_id()
                    if isinstance(params.value, dict):
                        value = json.loads(json.dumps(params.value, default=str))
                    else:
                        value = {"value": params.value}
                    # Use just the channel's own ID (child part of subobject ID)
                    ch_osw_id = params.channel.get_osw_id()
                    if "#" in ch_osw_id:
                        ch_osw_id = ch_osw_id.split("#", 1)[1]
                    offline_before = getattr(self.archive_database, "_offline", False)
                    await self.archive_database.write_tool_channel_raw(
                        TSDCMixin.WriteToolChannelRawParams(
                            tool_osw_id=tool_osw_id,
                            data=[
                                {
                                    "ts": params.timestamp.isoformat(),
                                    "ch": ch_osw_id,
                                    "data": value,
                                }
                            ],
                        )
                    )
                    if not offline_before and getattr(
                        self.archive_database, "_offline", False
                    ):
                        _logger.warning("Database went offline")
                        self._on_archive_error()
                except Exception as e:
                    _logger.error("Error archiving data change: %s", e)

        if self._channel_datachange_notification_callback is not None:
            try:
                await self._channel_datachange_notification_callback(
                    type(self).ChannelDataChangeNotificationParams(
                        channel=params.channel,
                        value=params.value,
                        timestamp=params.timestamp,
                    )
                )
            except Exception as e:
                _logger.error("Error in data change callback: %s", e)
                import traceback

                _logger.error(traceback.format_exc())

    def _on_archive_error(self):
        """Called when the archive DB goes offline. Override in subclasses."""
        pass

    async def configure_auto_archive(self, params):
        if params.enable and self.archive_database is None:
            raise ValueError("Auto archive enabled but no archive database set")
        self.auto_archive = params.enable
        if params.enable:
            _logger.warning("Auto archive enabled")
        else:
            _logger.warning("Auto archive disabled")
        if params.enable and self.archive_database is not None:
            existing_tools = await self.archive_database.get_tools_list()
            required_tools = [self.get_osw_id()]
            for device in self.get_subdevices():
                required_tools.append(device.get_osw_id())
            for osw_id in required_tools:
                if osw_id not in existing_tools:
                    try:
                        await self.archive_database.create_tool(
                            TSDCMixin.CreateToolParams(tool_osw_id=osw_id)
                        )
                    except Exception as e:
                        _logger.error("Error creating tool %s: %s", osw_id, e)
            await asyncio.sleep(1)

    async def read_archive_data(self, params=None):
        if self.archive_database is None:
            raise ValueError("No archive database configured")
        if params is None:
            params = type(self).ReadArchiveDataParams()
        _params = TSDCMixin.ReadToolChannelRawParams(
            tool_osw_id=self.get_osw_id(),
            channel_osw_id=params.channel.get_osw_id() if params.channel else None,
            start=params.start,
            end=params.end,
            limit=params.max_rows,
        )
        results = await self.archive_database.read_tool_channel_raw(_params)
        ch_dict = {ch.get_osw_id(): ch for ch in self.get_all_channels()}
        return type(self).ReadArchiveDataResult(
            data=[
                type(self).ReadArchiveDataResultRow(
                    timestamp=row["ts"],
                    channel=(
                        params.channel if params.channel else ch_dict.get(row["ch"])
                    ),
                    data=row["data"],
                )
                for row in results
                if row["ch"] in ch_dict
            ]
        )

    def read_archive_data_sync(self, params=None):
        return asyncio.run(self.read_archive_data(params=params))

    async def stop(self):
        _logger.warning("Stopping")
        if self.archive_database is not None:
            await self.archive_database.flush_buffer()


class TSDCMixin:
    """Mixin providing TimeSeriesDatabaseController methods and inner types.

    Compose with a Database model class to create a concrete controller:
        class TimeSeriesDatabaseController(TSDCMixin, Database): pass
    """

    class CreateToolParams(BaseModel):
        tool_osw_id: str
        """OSW ID of the tool"""

    @abstractmethod
    async def create_tool(self, params: "TSDCMixin.CreateToolParams"):
        pass

    class DeleteToolParams(BaseModel):
        tool_osw_id: str
        """OSW ID of the tool"""

    @abstractmethod
    async def delete_tool(self, params: "TSDCMixin.DeleteToolParams"):
        pass

    @abstractmethod
    async def get_tools_list(self) -> List[str]:
        """Returns a list of all registered tools."""
        pass

    class WriteToolChannelRawParams(BaseModel):
        tool_osw_id: str
        """OSW ID of the tool"""
        data: list
        """List of data rows to store"""

    @abstractmethod
    async def write_tool_channel_raw(
        self, params: "TSDCMixin.WriteToolChannelRawParams"
    ):
        """Stores data for a tool with a predefined OSW ID."""
        pass

    def write_tool_channel_raw_sync(
        self, params: "TSDCMixin.WriteToolChannelRawParams"
    ):
        return asyncio.run(self.write_tool_channel_raw(params=params))

    class DataRow(BaseModel):
        ts: datetime
        """Timestamp of the data row"""
        ch: str
        """Channel OSW ID"""
        data: Any

    class StoreDataParams(BaseModel):
        tool_osw_id: str
        """OSW ID of the tool"""
        rows: List["TSDCMixin.DataRow"]
        """Data rows to store"""

    async def store_data(self, params: "TSDCMixin.StoreDataParams"):
        """Stores data for a tool with a predefined OSW ID."""
        rows = [row.model_dump(mode="json") for row in params.rows]
        return await self.write_tool_channel_raw(
            params=TSDCMixin.WriteToolChannelRawParams(
                tool_osw_id=params.tool_osw_id,
                data=rows,
            )
        )

    def store_data_sync(self, params: "TSDCMixin.StoreDataParams"):
        return asyncio.run(self.store_data(params=params))

    class FilterColumn(str, Enum):
        channel = "ch"
        timestamp = "ts"
        data = "data"

    class FilterOperator(str, Enum):
        eq = "eq"
        gt = "gt"
        gte = "gte"
        lt = "lt"
        lte = "lte"
        neq = "neq"
        like = "like"
        ilike = "ilike"
        match = "match"
        imatch = "imatch"
        in_ = "in"
        is_ = "is"
        isdistinct = "isdistinct"
        fts = "fts"
        plfts = "plfts"
        phfts = "phfts"
        wfts = "wfts"
        cs = "cs"
        cd = "cd"
        ov = "ov"
        sl = "sl"
        sr = "sr"
        nxr = "nxr"
        nxl = "nxl"
        adj = "adj"
        not_ = "not"
        or_ = "or"
        and_ = "and"
        all_ = "all"
        any_ = "any"

    class Filter(BaseModel):
        column: Union["TSDCMixin.FilterColumn", str]
        """Column name or column name + jsonb selector"""
        operator: "TSDCMixin.FilterOperator"
        """Filter operator"""
        criteria: Any
        """Criteria value for the filter"""

    class ReadToolChannelRawParams(BaseModel):
        tool_osw_id: str
        """OSW ID of the tool"""
        channel_osw_id: Optional[str] = None
        """OSW ID of the channel, all are read if None"""
        start: Optional[datetime] = None
        """Start time for reading data"""
        end: Optional[datetime] = None
        """End time for reading data"""
        filter: Optional[List["TSDCMixin.Filter"]] = None
        """Filters for reading data"""
        limit: Optional[int] = None
        """Limit the number of returned rows"""

    @abstractmethod
    async def read_tool_channel_raw(self, params: "TSDCMixin.ReadToolChannelRawParams"):
        """Retrieves data for a tool within a time range."""
        pass

    def read_tool_channel_raw_sync(self, params: "TSDCMixin.ReadToolChannelRawParams"):
        return asyncio.run(self.read_tool_channel_raw(params=params))

    async def flush_buffer(self, tool_osw_id: Optional[str] = None):
        """Flush any buffered data. No-op for non-buffered implementations."""
        pass


class LocalTSDCMixin:
    """Mixin providing LocalTimeSeriesDatabaseController methods.

    Compose with a TimeSeriesDatabaseController class:
        class LocalTSDC(LocalTSDCMixin, TSDC): pass
    """

    db_path: Union[str, Path]

    async def execute(self, query: str, params: tuple = ()):
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(query, params)
            await conn.commit()
        return cursor

    async def execute_many(self, query: str, params_list: List[tuple]):
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.cursor()
            await cursor.executemany(query, params_list)
            await conn.commit()
        return cursor

    async def fetchall(self, query: str, params: tuple = ()):
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(query, params)
            rows = await cursor.fetchall()
            await cursor.close()
        return rows

    async def fetchone(self, query: str, params: tuple = ()):
        import aiosqlite

        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(query, params)
            row = await cursor.fetchone()
            await cursor.close()
        return row

    async def create_tool(self, params: TSDCMixin.CreateToolParams):
        await self.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {params.tool_osw_id} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts DATETIME NOT NULL,
                ch TEXT NOT NULL,
                data JSONB NOT NULL
            );
            """
        )
        _logger.debug("Created table for tool %s.", params.tool_osw_id)

    async def delete_tool(self, params: TSDCMixin.DeleteToolParams):
        await self.execute(f"DROP TABLE IF EXISTS {params.tool_osw_id};")
        _logger.debug("Dropped table for tool %s.", params.tool_osw_id)

    async def get_tools_list(self) -> List[str]:
        rows = await self.fetchall(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        )
        return [row[0] for row in rows]

    async def write_tool_channel_raw(self, params: TSDCMixin.WriteToolChannelRawParams):
        data = [(row["ts"], row["ch"], json.dumps(row["data"])) for row in params.data]
        await self.execute_many(
            f"INSERT INTO {params.tool_osw_id} "
            f"(ts, ch, data) VALUES (datetime(?,'subsec'), ?, ?);",
            data,
        )

    async def read_tool_channel_raw(self, params: TSDCMixin.ReadToolChannelRawParams):
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

        query, query_params = build_sqlite_read_query(
            tool_osw_id=params.tool_osw_id,
            channel_osw_id=params.channel_osw_id,
            start=params.start,
            end=params.end,
            filters=filters,
            limit=params.limit,
        )

        _logger.debug("Executing query: %s with params: %s", query, query_params)
        rows = await self.fetchall(query, tuple(query_params))
        return parse_sqlite_rows(rows)

    async def delete_by_ids(self, tool_osw_id: str, ids: List[int]):
        """Delete rows by their IDs from the tool table."""
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        query = f"DELETE FROM {tool_osw_id} WHERE id IN ({placeholders});"
        res = await self.execute(query, tuple(ids))
        _logger.debug("Deleted %s rows from %s.", res.rowcount, tool_osw_id)

    async def get_table_size(self, tool_osw_id: str) -> int:
        """Get the number of rows in the tool table."""
        row = await self.fetchone(f"SELECT COUNT(*) FROM {tool_osw_id};")
        return row[0] if row else 0


class PostgrestTSDCMixin:
    """Mixin providing PostgrestTimeSeriesDatabaseController methods.

    Compose with a TimeSeriesDatabaseController class:
        class PostgrestTimeSeriesDatabaseController(
            PostgrestTSDCMixin, TimeSeriesDatabaseController
        ): pass
    """

    uuid: UUID = Field(default_factory=uuid4)
    buffered: bool = True
    buffer_batch_size: int = 100
    buffer_offline_location: Optional[Path] = Path("buffered_data.sqlite")
    buffer_offline_batch_size: int = 500
    buffer_offline_sync_interval: float = 0.2
    _offline: bool = False
    _client: Optional[Any] = None
    _buffer: Dict[str, List[Dict]] = {}
    _buffer_lock: asyncio.Lock = PrivateAttr(default_factory=asyncio.Lock)
    _emulate_offline: bool = False
    _local_db: Optional[Any] = None

    def set_client(self, client):
        """Set the PostgREST client connection."""
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            raise ValueError(
                "No PostgREST client configured. "
                "Call set_client() or pass a client in the constructor."
            )

    async def get_tools_list(self) -> List[str]:
        self._ensure_client()
        res = await self._client.table("tools").select("*").execute()
        tools = []
        if res.data:
            for tool in res.data:
                tools.append(tool["osw_tool"])
        return tools

    class ChannelConfig(BaseModel):
        osw_id: str

    class ToolConfig(BaseModel):
        osw_id: str
        channels: List["PostgrestTSDCMixin.ChannelConfig"]

    async def get_tool_config(self) -> List["PostgrestTSDCMixin.ToolConfig"]:
        """Returns a list of all tools with their channels."""
        self._ensure_client()
        res = await self._client.rpc("get_tool_config", {}).execute()
        if not res.data:
            return []
        return [
            {"osw_id": tool, "channels": [{"osw_id": ch} for ch in channels]}
            for tool, channels in res.data.items()
        ]

    async def create_tool(self, params: TSDCMixin.CreateToolParams):
        self._ensure_client()
        return await self._client.rpc(
            "create_tool", {"osw_tool": params.tool_osw_id}
        ).execute()

    async def delete_tool(self, params: TSDCMixin.DeleteToolParams):
        self._ensure_client()
        return await self._client.rpc(
            "delete_tool", {"osw_tool": params.tool_osw_id}
        ).execute()

    async def _flush_offline_buffer(self):
        """Background task that flushes offline-buffered data to the remote DB."""
        if not self.buffer_offline_location:
            return
        _logger.info(
            "Flushing offline buffered data from %s", self.buffer_offline_location
        )
        while True:
            try:
                tools = await self._local_db.get_tools_list()
                if len(tools) > 0:
                    remote_tools = await self.get_tools_list()
                    _logger.info("Currently stored offline data for tools: %s", tools)
                for tool in tools:
                    table_size = await self._local_db.get_table_size(tool)
                    _logger.info(
                        "Tool %s has %d rows in offline buffer", tool, table_size
                    )
                    rows = await self._local_db.read_tool_channel_raw(
                        TSDCMixin.ReadToolChannelRawParams(
                            tool_osw_id=tool, limit=self.buffer_offline_batch_size
                        )
                    )
                    if len(rows) > 0:
                        try:
                            _logger.info(
                                "Flushing %d rows for tool %s", len(rows), tool
                            )
                            _rows = [
                                {k: v for k, v in row.items() if k != "id"}
                                for row in rows
                            ]
                            if self._emulate_offline:
                                raise Exception("Emulated offline mode")
                            if tool not in remote_tools:
                                _logger.info("Tool %s not found, creating it", tool)
                                try:
                                    await self.create_tool(
                                        TSDCMixin.CreateToolParams(tool_osw_id=tool)
                                    )
                                    await asyncio.sleep(1)
                                except Exception as e:
                                    _logger.error("Error creating tool %s: %s", tool, e)
                                    continue
                            await self._client.table(tool).insert(_rows).execute()
                            ids = [row["id"] for row in rows]
                            await self._local_db.delete_by_ids(tool, ids)
                        except Exception as e:
                            _logger.error(
                                "Error flushing rows for tool %s: %s. Retrying.",
                                tool,
                                e,
                            )
                            await asyncio.sleep(5)
                    else:
                        _logger.info(
                            "No rows for tool %s - removing from offline db", tool
                        )
                        await self._local_db.delete_tool(
                            TSDCMixin.DeleteToolParams(tool_osw_id=tool)
                        )
                await asyncio.sleep(self.buffer_offline_sync_interval)
            except Exception as e:
                _logger.error("Error flushing offline buffer: %s. Retrying.", e)
                await asyncio.sleep(5)

    def _check_buffer(self):
        duplicates = check_buffer_duplicates(self._buffer)
        for tool_osw_id, dupes in duplicates.items():
            _logger.warning(
                "Duplicate entries in buffer for tool %s: %s", tool_osw_id, dupes
            )

    async def flush_buffer(self, tool_osw_id: Optional[str] = None):
        _logger.info(
            "Flushing buffer for tool %s",
            tool_osw_id if tool_osw_id else "all tools",
        )
        self._check_buffer()
        buffer_copy = deepcopy(self._buffer)
        if tool_osw_id:
            self._buffer[tool_osw_id] = []
        else:
            self._buffer = {}

        try:
            if self._emulate_offline:
                raise Exception("Emulated offline mode")
            self._ensure_client()
            if tool_osw_id:
                if tool_osw_id in buffer_copy and buffer_copy[tool_osw_id]:
                    data = buffer_copy[tool_osw_id]
                    _logger.info("Sending %d rows for tool %s", len(data), tool_osw_id)
                    res = await self._client.table(tool_osw_id).insert(data).execute()
                    self._offline = False
                    return res
            else:
                for tid, data in buffer_copy.items():
                    if data:
                        _logger.info("Sending %d rows for tool %s", len(data), tid)
                        await self._client.table(tid).insert(data).execute()
                self._offline = False
                return True
        except Exception as e:
            _logger.warning("Error flushing buffer: %s", e)
            self._offline = True
            if self.buffer_offline_location and self._local_db:
                try:
                    tools_to_flush = (
                        [tool_osw_id] if tool_osw_id else list(buffer_copy.keys())
                    )
                    for tid in tools_to_flush:
                        data = buffer_copy.get(tid, [])
                        if data:
                            await self._local_db.create_tool(
                                TSDCMixin.CreateToolParams(tool_osw_id=tid)
                            )
                            await self._local_db.write_tool_channel_raw(
                                TSDCMixin.WriteToolChannelRawParams(
                                    tool_osw_id=tid, data=data
                                )
                            )
                except Exception as e2:
                    _logger.error("Error storing to offline location: %s", e2)
            else:
                _logger.error("No offline location specified for buffered data")
            return False

    async def write_tool_channel_raw(self, params: TSDCMixin.WriteToolChannelRawParams):
        if self.buffered:
            async with self._buffer_lock:
                if params.tool_osw_id not in self._buffer:
                    self._buffer[params.tool_osw_id] = []
                self._buffer[params.tool_osw_id].extend(params.data)
                if len(self._buffer[params.tool_osw_id]) >= self.buffer_batch_size:
                    offline_before = self._offline
                    await self.flush_buffer(params.tool_osw_id)
                    if offline_before and not self._offline:
                        _logger.info("Database is back online")
        else:
            self._ensure_client()
            return (
                await self._client.table(params.tool_osw_id)
                .insert(params.data)
                .execute()
            )

    async def read_tool_channel_raw(self, params: TSDCMixin.ReadToolChannelRawParams):
        self._ensure_client()
        if params.channel_osw_id:
            query = (
                self._client.table(params.tool_osw_id)
                .select("*")
                .eq("ch", params.channel_osw_id)
                .order("ts", desc=False)
            )
        else:
            query = (
                self._client.table(params.tool_osw_id)
                .select("*")
                .order("ts", desc=False)
            )

        if params.start is not None:
            query = query.gte("ts", params.start.isoformat())
        if params.end is not None:
            query = query.lte("ts", params.end.isoformat())

        if params.filter is not None:
            for f in params.filter:
                criteria = f.criteria
                if not isinstance(criteria, str):
                    if isinstance(criteria, bool):
                        criteria = str(criteria).lower()
                    elif isinstance(criteria, datetime):
                        criteria = criteria.isoformat()
                    else:
                        criteria = str(criteria)
                column = (
                    f.column.value
                    if isinstance(f.column, TSDCMixin.FilterColumn)
                    else f.column
                )
                query = query.filter(column, f.operator.value, criteria)

        if params.limit is not None:
            query = query.limit(params.limit)

        res = await query.execute()
        return res.data if res.data else []
