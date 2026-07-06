"""Database driver implementations for time series storage.

Plain Python classes (no Pydantic) that handle all backend I/O.
Controllers use these via composition (_driver attribute).
"""

import asyncio
import json
import logging
from abc import abstractmethod
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from opensemantic.base._controller_logic import (
    build_sqlite_read_query,
    check_buffer_duplicates,
    parse_sqlite_rows,
)

_logger = logging.getLogger(__name__)


def _stride_decimate(rows: list, max_points: int) -> list:
    """Reduce ``rows`` to about ``max_points`` by even striding.

    Always keeps the first and last row so the series still spans its
    full window. Used as the SQLite fallback when server-side
    downsampling is unavailable.
    """
    n = len(rows)
    if max_points <= 2 or n <= max_points:
        return rows
    step = n / float(max_points)
    picked = [rows[min(int(i * step), n - 1)] for i in range(max_points)]
    if picked[0] is not rows[0]:
        picked[0] = rows[0]
    if picked[-1] is not rows[-1]:
        picked[-1] = rows[-1]
    return picked


class DatabaseDriver:
    """Base class with shared buffer management.

    Subclasses implement _write_immediate() for their specific backend.
    Buffer accumulation, flushing, and locking are handled here.
    """

    def __init__(self, buffered: bool = False, buffer_batch_size: int = 100):
        self.buffered = buffered
        self.buffer_batch_size = buffer_batch_size
        self._buffer: Dict[str, List[Dict]] = {}
        self._buffer_lock: Optional[asyncio.Lock] = None

    @abstractmethod
    async def _write_immediate(self, tool_osw_id: str, data: list):
        """Write rows directly to the backend. No buffering."""

    @abstractmethod
    async def read(
        self,
        tool_osw_id: str,
        channel_osw_id: Optional[str] = None,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        filters: Optional[list] = None,
        limit: Optional[int] = None,
        max_points: Optional[int] = None,
        bin_size: Optional[str] = None,
        downsample_method: Optional[str] = None,
        edge_anchors: Optional[bool] = None,
    ) -> list:
        """Read rows from the backend.

        The trailing ``max_points`` / ``bin_size`` / ``downsample_method`` /
        ``edge_anchors`` parameters request server-side downsampling and are
        honored by backends that support it (PostgREST/TimescaleDB); other
        backends ignore or approximate them.
        """

    @abstractmethod
    async def create_tool(self, tool_osw_id: str):
        """Create storage for a tool (table, etc.)."""

    @abstractmethod
    async def delete_tool(self, tool_osw_id: str):
        """Delete storage for a tool."""

    @abstractmethod
    async def get_tools_list(self) -> List[str]:
        """List all registered tools."""

    async def write(self, tool_osw_id: str, data: list):
        """Write data, buffering if enabled."""
        if self.buffered:
            if self._buffer_lock is None:
                self._buffer_lock = asyncio.Lock()
            async with self._buffer_lock:
                if tool_osw_id not in self._buffer:
                    self._buffer[tool_osw_id] = []
                self._buffer[tool_osw_id].extend(data)
                if len(self._buffer[tool_osw_id]) >= self.buffer_batch_size:
                    await self.flush_buffer(tool_osw_id)
        else:
            await self._write_immediate(tool_osw_id, data)

    async def flush_buffer(self, tool_osw_id: Optional[str] = None):
        """Flush buffered writes to the backend."""
        if tool_osw_id:
            data = self._buffer.pop(tool_osw_id, [])
            if data:
                _logger.debug("Flushing %d rows for %s", len(data), tool_osw_id)
                await self._write_immediate(tool_osw_id, data)
        else:
            buffer_copy = self._buffer
            self._buffer = {}
            for tid, data in buffer_copy.items():
                if data:
                    _logger.debug("Flushing %d rows for %s", len(data), tid)
                    await self._write_immediate(tid, data)

    def _pending_count(self) -> int:
        """Return total number of rows pending in the buffer."""
        return sum(len(v) for v in self._buffer.values())


class LocalDatabaseDriver(DatabaseDriver):
    """SQLite backend via aiosqlite."""

    def __init__(
        self,
        db_path: Union[str, Path],
        buffered: bool = False,
        buffer_batch_size: int = 100,
    ):
        super().__init__(buffered=buffered, buffer_batch_size=buffer_batch_size)
        self.db_path = db_path
        if buffered:
            _logger.info(
                "LocalDatabaseDriver: buffered mode enabled (batch_size=%d). "
                "Call flush_buffer() when done to persist remaining data.",
                buffer_batch_size,
            )

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

    async def create_tool(self, tool_osw_id: str):
        await self.execute(
            f"CREATE TABLE IF NOT EXISTS {tool_osw_id} ("
            f"id INTEGER PRIMARY KEY AUTOINCREMENT,"
            f"ts DATETIME NOT NULL,"
            f"ch TEXT NOT NULL,"
            f"data JSONB NOT NULL);"
        )
        _logger.debug("Created table for tool %s.", tool_osw_id)

    async def delete_tool(self, tool_osw_id: str):
        await self.execute(f"DROP TABLE IF EXISTS {tool_osw_id};")
        _logger.debug("Dropped table for tool %s.", tool_osw_id)

    async def get_tools_list(self) -> List[str]:
        rows = await self.fetchall(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name NOT LIKE 'sqlite_%';"
        )
        return [row[0] for row in rows]

    async def _write_immediate(self, tool_osw_id: str, data: list):
        """Write rows directly to SQLite in a single transaction."""
        await self.create_tool(tool_osw_id)
        rows = [(row["ts"], row["ch"], json.dumps(row["data"])) for row in data]
        await self.execute_many(
            f"INSERT INTO {tool_osw_id} "
            f"(ts, ch, data) VALUES (datetime(?,'subsec'), ?, ?);",
            rows,
        )

    async def read(
        self,
        tool_osw_id: str,
        channel_osw_id: Optional[str] = None,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        filters: Optional[list] = None,
        limit: Optional[int] = None,
        max_points: Optional[int] = None,
        bin_size: Optional[str] = None,
        downsample_method: Optional[str] = None,
        edge_anchors: Optional[bool] = None,
    ) -> list:
        query, query_params = build_sqlite_read_query(
            tool_osw_id=tool_osw_id,
            channel_osw_id=channel_osw_id,
            start=start,
            end=end,
            filters=filters,
            limit=limit,
        )
        _logger.debug("Executing query: %s with params: %s", query, query_params)
        rows = await self.fetchall(query, tuple(query_params))
        parsed = parse_sqlite_rows(rows)
        # SQLite has no time_bucket; approximate downsampling with a simple
        # row-stride decimation that keeps the first and last point.
        if max_points and len(parsed) > max_points:
            parsed = _stride_decimate(parsed, max_points)
        return parsed

    async def delete_by_ids(self, tool_osw_id: str, ids: List[int]):
        if not ids:
            return
        placeholders = ",".join("?" for _ in ids)
        query = f"DELETE FROM {tool_osw_id} WHERE id IN ({placeholders});"
        res = await self.execute(query, tuple(ids))
        _logger.debug("Deleted %s rows from %s.", res.rowcount, tool_osw_id)

    async def get_table_size(self, tool_osw_id: str) -> int:
        row = await self.fetchone(f"SELECT COUNT(*) FROM {tool_osw_id};")
        return row[0] if row else 0


class PostgrestDatabaseDriver(DatabaseDriver):
    """PostgREST backend with in-memory buffering and offline fallback."""

    def __init__(
        self,
        client=None,
        buffered: bool = True,
        buffer_batch_size: int = 100,
        buffer_offline_location: Optional[Union[str, Path]] = None,
        buffer_offline_batch_size: int = 500,
        buffer_offline_sync_interval: float = 0.2,
    ):
        super().__init__(buffered=buffered, buffer_batch_size=buffer_batch_size)
        self.client = client
        self.buffer_offline_location = buffer_offline_location
        self.buffer_offline_batch_size = buffer_offline_batch_size
        self.buffer_offline_sync_interval = buffer_offline_sync_interval
        self._offline = False
        self._emulate_offline = False
        self._local_db: Optional[LocalDatabaseDriver] = None
        self._created_tools: set = set()

        if buffer_offline_location:
            self._local_db = LocalDatabaseDriver(db_path=buffer_offline_location)

    def set_client(self, client):
        self.client = client

    def _ensure_client(self):
        if self.client is None:
            raise ValueError("No PostgREST client configured. Call set_client().")

    async def get_tools_list(self) -> List[str]:
        self._ensure_client()
        res = await self.client.table("tools").select("*").execute()
        tools = []
        if res.data:
            for tool in res.data:
                tools.append(tool["osw_tool"])
        return tools

    async def get_tool_config(self) -> list:
        self._ensure_client()
        res = await self.client.rpc("get_tool_config", {}).execute()
        if not res.data:
            return []
        return [
            {
                "osw_id": tool,
                "channels": [{"osw_id": ch} for ch in channels],
            }
            for tool, channels in res.data.items()
        ]

    async def create_tool(self, tool_osw_id: str):
        self._ensure_client()
        return await self.client.rpc("create_tool", {"osw_tool": tool_osw_id}).execute()

    async def delete_tool(self, tool_osw_id: str):
        self._ensure_client()
        return await self.client.rpc("delete_tool", {"osw_tool": tool_osw_id}).execute()

    async def _write_immediate(self, tool_osw_id: str, data: list):
        """Write rows directly to PostgREST."""
        self._ensure_client()
        await self.client.table(tool_osw_id).insert(data).execute()

    async def write(self, tool_osw_id: str, data: list):
        """Write with auto-create tool on first write."""
        if tool_osw_id not in self._created_tools:
            try:
                await self.create_tool(tool_osw_id)
            except Exception:
                pass
            self._created_tools.add(tool_osw_id)
        if self.buffered:
            await super().write(tool_osw_id, data)
        else:
            await self._write_immediate(tool_osw_id, data)

    async def flush_buffer(self, tool_osw_id: Optional[str] = None):
        """Flush with auto-create tools and offline fallback."""
        _logger.info(
            "Flushing buffer for %s",
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
            tools_to_flush = [tool_osw_id] if tool_osw_id else list(buffer_copy.keys())
            for tid in tools_to_flush:
                if tid not in self._created_tools:
                    try:
                        await self.create_tool(tid)
                    except Exception:
                        pass
                    self._created_tools.add(tid)
            if tool_osw_id:
                if tool_osw_id in buffer_copy and buffer_copy[tool_osw_id]:
                    data = buffer_copy[tool_osw_id]
                    _logger.info("Sending %d rows for %s", len(data), tool_osw_id)
                    res = await self.client.table(tool_osw_id).insert(data).execute()
                    self._offline = False
                    return res
            else:
                for tid, data in buffer_copy.items():
                    if data:
                        _logger.info("Sending %d rows for %s", len(data), tid)
                        await self.client.table(tid).insert(data).execute()
                self._offline = False
                return True
        except Exception as e:
            _logger.warning("Error flushing buffer: %s", e)
            self._offline = True
            if self._local_db:
                try:
                    tools = [tool_osw_id] if tool_osw_id else list(buffer_copy.keys())
                    for tid in tools:
                        data = buffer_copy.get(tid, [])
                        if data:
                            await self._local_db.create_tool(tid)
                            await self._local_db.write(tid, data)
                except Exception as e2:
                    _logger.error("Error storing offline: %s", e2)
            else:
                _logger.error("No offline location for buffered data")
            return False

    def _check_buffer(self):
        duplicates = check_buffer_duplicates(self._buffer)
        for tool_osw_id, dupes in duplicates.items():
            _logger.warning("Duplicate entries for tool %s: %s", tool_osw_id, dupes)

    async def read(
        self,
        tool_osw_id: str,
        channel_osw_id: Optional[str] = None,
        start: Optional[Any] = None,
        end: Optional[Any] = None,
        filters: Optional[list] = None,
        limit: Optional[int] = None,
        max_points: Optional[int] = None,
        bin_size: Optional[str] = None,
        downsample_method: Optional[str] = None,
        edge_anchors: Optional[bool] = None,
    ) -> list:
        self._ensure_client()
        # Route to the server-side downsampling RPC when requested. Any
        # failure (RPC missing on an older pgstack, network error, ...)
        # falls through to the plain full-resolution read below.
        if max_points is not None or bin_size is not None or downsample_method:
            rows = await self._read_downsampled(
                tool_osw_id,
                channel_osw_id,
                start,
                end,
                max_points,
                bin_size,
                downsample_method,
                edge_anchors,
            )
            if rows is not None:
                return rows
        if channel_osw_id:
            query = (
                self.client.table(tool_osw_id)
                .select("*")
                .eq("ch", channel_osw_id)
                .order("ts", desc=False)
            )
        else:
            query = self.client.table(tool_osw_id).select("*").order("ts", desc=False)

        if start is not None:
            query = query.gte("ts", start.isoformat())
        if end is not None:
            query = query.lte("ts", end.isoformat())

        if filters is not None:
            for f in filters:
                criteria = f["criteria"]
                if not isinstance(criteria, str):
                    if isinstance(criteria, bool):
                        criteria = str(criteria).lower()
                    elif isinstance(criteria, datetime):
                        criteria = criteria.isoformat()
                    else:
                        criteria = str(criteria)
                query = query.filter(f["column"], f["operator"], criteria)

        if limit is not None:
            query = query.limit(limit)

        res = await query.execute()
        return res.data if res.data else []

    async def _read_downsampled(
        self,
        tool_osw_id: str,
        channel_osw_id: Optional[str],
        start: Optional[Any],
        end: Optional[Any],
        max_points: Optional[int],
        bin_size: Optional[str],
        downsample_method: Optional[str],
        edge_anchors: Optional[bool],
    ) -> Optional[list]:
        """Call the server-side downsampling RPC.

        Returns the bucketed rows (same ``{"ts","ch","data"}`` shape as the
        plain read), or ``None`` when the RPC is unavailable/failed so the
        caller can fall back to a full-resolution read.
        """
        params: Dict[str, Any] = {"osw_tool": tool_osw_id}
        if channel_osw_id:
            params["ch_id"] = channel_osw_id
        if start is not None:
            params["ts_start"] = start.isoformat()
        if end is not None:
            params["ts_end"] = end.isoformat()
        if max_points is not None:
            params["max_points"] = max_points
        if bin_size is not None:
            params["bin_size"] = bin_size
        if downsample_method:
            params["method"] = downsample_method
        if edge_anchors is not None:
            params["edge_anchors"] = edge_anchors
        try:
            res = await self.client.rpc("downsample_tool_channel", params).execute()
            return res.data if res.data else []
        except Exception as e:
            _logger.debug(
                "downsample RPC unavailable (%s); falling back to full read", e
            )
            return None

    async def _flush_offline_buffer(self):
        """Background task that syncs offline-buffered data to remote."""
        if not self._local_db:
            return
        _logger.info(
            "Flushing offline buffered data from %s",
            self.buffer_offline_location,
        )
        while True:
            try:
                tools = await self._local_db.get_tools_list()
                if tools:
                    remote_tools = await self.get_tools_list()
                    _logger.info("Offline data for tools: %s", tools)
                for tool in tools:
                    table_size = await self._local_db.get_table_size(tool)
                    _logger.info("Tool %s has %d rows offline", tool, table_size)
                    rows = await self._local_db.read(
                        tool, limit=self.buffer_offline_batch_size
                    )
                    if rows:
                        try:
                            _logger.info("Flushing %d rows for %s", len(rows), tool)
                            _rows = [
                                {k: v for k, v in row.items() if k != "id"}
                                for row in rows
                            ]
                            if self._emulate_offline:
                                raise Exception("Emulated offline mode")
                            if tool not in remote_tools:
                                _logger.info("Creating tool %s", tool)
                                try:
                                    await self.create_tool(tool)
                                    await asyncio.sleep(1)
                                except Exception as e:
                                    _logger.error("Error creating %s: %s", tool, e)
                                    continue
                            await self.client.table(tool).insert(_rows).execute()
                            ids = [row["id"] for row in rows]
                            await self._local_db.delete_by_ids(tool, ids)
                        except Exception as e:
                            _logger.error("Error flushing %s: %s. Retrying.", tool, e)
                            await asyncio.sleep(5)
                    else:
                        _logger.info("No rows for %s - removing", tool)
                        await self._local_db.delete_tool(tool)
                await asyncio.sleep(self.buffer_offline_sync_interval)
            except Exception as e:
                _logger.error("Error flushing offline buffer: %s. Retrying.", e)
                await asyncio.sleep(5)

    async def start_offline_sync(self):
        """Start the background offline sync task."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._flush_offline_buffer())
        except RuntimeError:
            pass
