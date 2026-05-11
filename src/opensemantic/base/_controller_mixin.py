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
from datetime import datetime
from enum import Enum
from typing import Any, List, Optional, Union

from oold.model import BaseController
from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger(__name__)


class DataToolMixin(BaseController):
    """Generic controller mixin for DataTool models.

    Provides: identity, channel/subdevice traversal, archiving, data change handling.
    Compose with a DataTool model class:
        class DataToolController(DataToolMixin, DataTool): pass
    """

    def __init__(self, *args, **data):
        super().__init__(*args, **data)
        self._compute_subobject_ids()
        if not isinstance(getattr(self, "_channel_dict", None), dict):
            object.__setattr__(self, "_channel_dict", {})
        for channel in self.get_all_channels():
            # Use node_id if available (OPC UA), fall back to uuid
            key = getattr(channel, "node_id", None) or channel.uuid
            self._channel_dict[key] = channel
        # Warn about unloaded channel characteristics
        self._check_channel_characteristics()
        # TODO: update wiki OpcUaServer model to include endpoint/url field
        # so it survives serialization. Currently url is controller-only.

        # Auto-init archive database from storage_locations
        _archive_db = getattr(self, "archive_database", None)
        _needs_init = _archive_db is None or isinstance(_archive_db, dict)
        _storage = getattr(self, "storage_locations", None)
        if self.auto_archive and _needs_init and _storage:
            self.archive_database = self._init_archive_database(_storage[0])

    # __setattr__ for private attrs inherited from BaseController
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

    def _check_channel_characteristics(self):
        """Warn if any channel has an unresolvable characteristic IRI.

        A characteristic IRI that is not in oold's _types registry
        means typed read/write will fail for that channel unless
        target_schema is passed explicitly.
        """
        try:
            from oold.model import _types
        except ImportError:
            return
        for ch in self.get_all_channels():
            iris = getattr(ch, "__iris__", {}).get("characteristic", [])
            if isinstance(iris, str):
                iris = [iris]
            for iri in iris:
                if iri and iri not in _types:
                    _logger.warning(
                        "Channel '%s': characteristic IRI '%s' is not "
                        "in the type registry. Import the corresponding "
                        "package (e.g. opensemantic.characteristics."
                        "quantitative) to enable typed read/write.",
                        ch.name,
                        iri,
                    )

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

    def _init_archive_database(self, db):
        """Create a TimeSeriesDatabaseController from a Database entity.

        Uses oold's backend resolution to get the full Database instance
        (if db is an IRI string), then casts it to the appropriate
        controller class.

        Parameters
        ----------
        db
            A Database model instance or IRI string from storage_locations.

        Returns
        -------
            A TimeSeriesDatabaseController instance, or None.
        """
        # Already a controller - return as-is
        if isinstance(db, BaseController):
            return db

        # If db is a string IRI, it hasn't been resolved yet
        if isinstance(db, str):
            _logger.warning(
                "storage_locations[0] is an unresolved IRI: %s. "
                "Register a backend with set_backend() to enable "
                "auto-resolution.",
                db,
            )
            return None

        # Determine target controller class and extra kwargs.
        # Try inline object first, then IRI resolution via backend.
        server = db.__dict__.get("server")
        server_url = getattr(server, "url", None) if server else None
        if server_url is None:
            # Server may be an IRI in __iris__ - try to resolve or
            # use the IRI itself if it looks like a URL
            server_iri = getattr(db, "__iris__", {}).get("server")
            if server_iri and isinstance(server_iri, str):
                if server_iri.startswith("http"):
                    server_url = server_iri
                else:
                    try:
                        server = getattr(db, "server", None)
                        server_url = getattr(server, "url", None) if server else None
                    except (ValueError, ImportError):
                        pass

        # Build API URL from server fields
        if not server_url and server is not None:
            schema = getattr(server, "schema_", None) or "http"
            domain = getattr(server, "domain", None)
            ports = getattr(server, "network_port", None)
            port = ports[0] if ports else None
            path = getattr(server, "url_path", None) or ""
            if domain:
                server_url = f"{schema}://{domain}"
                if port:
                    server_url += f":{port}"
                server_url += f"/{path}".rstrip("/")

        if server_url:
            try:
                from postgrest import AsyncPostgrestClient

                # Look up credentials for this server
                cred = self.get_credential(server_url)
                headers = {}
                if cred is not None:
                    token = getattr(cred, "token", None)
                    if token is not None:
                        secret = (
                            token.get_secret_value()
                            if hasattr(token, "get_secret_value")
                            else str(token)
                        )
                        headers["Authorization"] = f"Bearer {secret}"

                client = AsyncPostgrestClient(
                    base_url=server_url,
                    schema="api",
                    headers=headers,
                )

                # Use version-matching controller
                db_module = type(db).__module__
                if ".v1." in db_module or db_module.endswith(".v1"):
                    from opensemantic.base.v1._controller import (
                        PostgrestTimeSeriesDatabaseController,
                    )
                else:
                    from opensemantic.base._controller import (
                        PostgrestTimeSeriesDatabaseController,
                    )

                controller = db.cast(
                    PostgrestTimeSeriesDatabaseController,
                    remove_extra=True,
                )
                controller.set_client(client)
                _logger.info(
                    "Auto-initialized PostgREST controller" " for %s at %s",
                    db.name,
                    server_url,
                )
                return controller
            except ImportError:
                _logger.warning(
                    "postgrest package not installed. " "Falling back to local SQLite."
                )
            except Exception as e:
                _logger.warning(
                    "Could not create PostgREST controller for %s: %s."
                    " Falling back to local SQLite.",
                    db.name,
                    e,
                )

        # Fall back to local SQLite
        try:
            # Use version-matching controller (v1 db -> v1 controller)
            db_module = type(db).__module__
            if ".v1." in db_module or db_module.endswith(".v1"):
                from opensemantic.base.v1._controller import (
                    LocalTimeSeriesDatabaseController,
                )
            else:
                from opensemantic.base._controller import (
                    LocalTimeSeriesDatabaseController,
                )
            db_path = f"./{db.name}.sqlite"
            controller = db.cast(
                LocalTimeSeriesDatabaseController,
                remove_extra=True,
                db_path=db_path,
            )
            _logger.info(
                "Auto-initialized LocalTimeSeriesDatabaseController" " for %s at %s",
                db.name,
                db_path,
            )
            return controller
        except ImportError:
            _logger.error(
                "Cannot auto-initialize archive database: "
                "install opensemantic.base[controller] "
                "(aiosqlite or postgrest)"
            )
            return None

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

    def get_channel_by_name(self, name: str):
        """Look up a channel by name across self and all subdevices.

        Raises ValueError if no channel with the given name is found.
        """
        for ch in self.get_all_channels():
            if ch.name == name:
                return ch
        raise ValueError(
            f"No channel named '{name}' found. "
            f"Available: {[ch.name for ch in self.get_all_channels()]}"
        )

    def _resolve_channel(self, channel):
        """Resolve a channel argument: pass through if already an object,
        look up by name if string."""
        if isinstance(channel, str):
            return self.get_channel_by_name(channel)
        return channel

    # -- Inner param/result classes --

    class StoreChannelDataParams(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        channel: Any = None
        """DataChannel instance or channel name (str)."""
        value: Any = None
        """Raw dict/scalar or Characteristic instance."""
        timestamp: Optional[dt.datetime] = None
        """Timestamp for the data point. Defaults to now(UTC)."""

    class LoadChannelDataParams(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        channel: Union[str, List[str], Any, None] = None
        """Channel name (str), list of names, DataChannel instance,
        list of DataChannel instances, or None (all channels)."""
        start: Optional[dt.datetime] = None
        end: Optional[dt.datetime] = None
        limit: Optional[int] = None
        typed: bool = True
        """If True, deserialize values using channel characteristic or
        target_schema. If False, return raw dicts (faster)."""
        target_schema: Any = None
        """Explicit class for typed deserialization (e.g. Temperature).
        Overrides channel characteristic resolution."""

    class ChannelDataPoint(BaseModel):
        """A single data point returned by load_channel_data."""

        model_config = ConfigDict(arbitrary_types_allowed=True)
        timestamp: dt.datetime
        channel: Any = None
        value: Any = None
        """Typed Characteristic instance or raw dict, depending on
        the typed parameter and channel characteristic."""

    class ChannelDataChangeNotificationParams(BaseModel):
        model_config = ConfigDict(arbitrary_types_allowed=True)
        channel: Any = None
        value: Any = None
        timestamp: Optional[dt.datetime] = None

    class AutoArchiveParams(BaseModel):
        enable: bool = True

    # -- Async methods --

    async def _handle_data_change(
        self,
        params: "DataToolMixin.ChannelDataChangeNotificationParams",
    ):
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
                    value = self._value_to_store_data(params.value, params.channel)
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

    async def configure_auto_archive(self, params: "DataToolMixin.AutoArchiveParams"):
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

    # read_archive_data, store_typed_data, read_typed_data removed.
    # Use store_channel_data / load_channel_data instead.

    def _value_to_store_data(self, value, channel):
        """Convert a value to a dict suitable for DB storage.

        Handles typed (Characteristic), dict, and raw scalar values.
        For raw scalars, uses channel's characteristic + unit to convert
        to base unit if available.
        """
        if hasattr(value, "to_json"):
            # Warn if typed value's unit differs from channel's unit
            ch_unit = getattr(channel, "unit", None)
            val_unit = getattr(value, "unit", None)
            if ch_unit is not None and val_unit is not None:
                # Resolve channel unit IRI to enum for comparison
                try:
                    resolved = type(value)(value=0, unit=ch_unit).unit
                    if str(resolved) != str(val_unit):
                        _logger.warning(
                            "Value unit %s differs from channel '%s' "
                            "unit %s. Storing in base unit.",
                            val_unit,
                            getattr(channel, "name", "?"),
                            resolved,
                        )
                except Exception:
                    pass
            if hasattr(value, "to_base"):
                try:
                    value = value.to_base()
                except Exception:
                    pass
            try:
                return value.to_json(exclude_defaults=True)
            except Exception:
                return value.to_json()
        if isinstance(value, dict):
            return json.loads(json.dumps(value, default=str))
        # Raw scalar: wrap with channel unit if available
        cls = self._resolve_characteristic_class(channel)
        ch_unit = getattr(channel, "unit", None)
        if cls is not None and ch_unit is not None:
            typed = cls(value=value, unit=ch_unit)
            if hasattr(typed, "to_base"):
                try:
                    typed = typed.to_base()
                except Exception:
                    pass
            return typed.to_json(exclude_defaults=True)
        return {"value": value}

    async def stop(self):
        _logger.warning("Stopping")
        if self.archive_database is not None:
            await self.archive_database.flush_buffer()

    # -- High-level store/load API --

    async def store_channel_data(
        self, params: "DataToolMixin.StoreChannelDataParams"
    ) -> None:
        """Store a single channel value to the archive database.

        Resolves channel by name if a string is passed.
        If value is a Characteristic instance, converts to base unit
        and serializes. Otherwise stores as raw dict/scalar.
        Auto-creates the tool table on first write (for SQLite).
        """
        if self.archive_database is None:
            raise ValueError("No archive database configured")
        channel = self._resolve_channel(params.channel)
        ts = params.timestamp or dt.datetime.now(dt.timezone.utc)
        value = params.value

        data = self._value_to_store_data(value, channel)

        ch_osw_id = channel.get_osw_id()
        if "#" in ch_osw_id:
            ch_osw_id = ch_osw_id.split("#")[-1]
        tool_osw_id = self.get_osw_id()

        await self.archive_database.write_tool_channel_raw(
            TSDCMixin.WriteToolChannelRawParams(
                tool_osw_id=tool_osw_id,
                data=[
                    {
                        "ts": ts.isoformat(),
                        "ch": ch_osw_id,
                        "data": data,
                    }
                ],
            )
        )

    async def load_channel_data(
        self,
        params: "DataToolMixin.LoadChannelDataParams",
    ) -> List["DataToolMixin.ChannelDataPoint"]:
        """Load channel data from the archive database.

        Parameters
        ----------
        params
            LoadChannelDataParams with channel (str, list, instance, or
            None for all), time range, limit, typed flag, target_schema.

        Returns
        -------
        List[ChannelDataPoint]
            Each point has timestamp, channel, and value (typed
            Characteristic if typed=True, raw dict if typed=False).
        """
        if self.archive_database is None:
            raise ValueError("No archive database configured")

        # Resolve channels
        channels = params.channel
        if channels is None:
            channels = self.get_all_channels()
        elif isinstance(channels, str):
            channels = [self._resolve_channel(channels)]
        elif isinstance(channels, list):
            channels = [
                self._resolve_channel(ch) if isinstance(ch, str) else ch
                for ch in channels
            ]
        else:
            channels = [channels]

        # Build channel ID -> channel lookup
        ch_by_id = {}
        for ch in channels:
            osw_id = ch.get_osw_id()
            short_id = osw_id.split("#")[-1] if "#" in osw_id else osw_id
            ch_by_id[short_id] = ch

        # Query: if single channel, filter by ID; otherwise get all
        ch_osw_id = None
        if len(channels) == 1:
            ch_osw_id = list(ch_by_id.keys())[0]

        raw = await self.archive_database.read_tool_channel_raw(
            TSDCMixin.ReadToolChannelRawParams(
                tool_osw_id=self.get_osw_id(),
                channel_osw_id=ch_osw_id,
                start=params.start,
                end=params.end,
                limit=params.limit,
            )
        )

        results: List["DataToolMixin.ChannelDataPoint"] = []
        for row in raw:
            ch = ch_by_id.get(row["ch"])
            if ch is None and len(ch_by_id) > 1:
                continue  # skip rows for channels not in the request

            value = row["data"]
            if params.typed:
                cls = params.target_schema
                if cls is None and ch is not None:
                    cls = self._resolve_characteristic_class(ch)
                if cls is not None:
                    value = cls.from_json(value)
                    ch_unit = getattr(ch, "unit", None) if ch else None
                    if ch_unit is not None and hasattr(value, "to_unit"):
                        try:
                            target = cls(value=0, unit=ch_unit)
                            value = value.to_unit(target.unit)
                        except Exception:
                            pass

            results.append(
                type(self).ChannelDataPoint(
                    timestamp=row["ts"],
                    channel=ch,
                    value=value,
                )
            )
        return results

    def _resolve_characteristic_class(self, channel):
        """Try to resolve the characteristic class for a channel.

        Checks __iris__ for the characteristic IRI (avoids triggering
        backend resolution), then looks up the class in the _types registry.
        Returns the class or None.
        """
        # Get IRI from __iris__ (avoids backend resolution via __getattribute__)
        iris = getattr(channel, "__iris__", {})
        char_iri = iris.get("characteristic")
        if char_iri is None:
            # Fall back to direct attribute (may trigger backend)
            try:
                char_iri = getattr(channel, "characteristic", None)
            except (ValueError, ImportError):
                return None
        if char_iri is None:
            return None
        # Handle list of IRIs (take first)
        if isinstance(char_iri, list):
            char_iri = char_iri[0] if char_iri else None
        if char_iri is None:
            return None
        # If it's already a class, return it
        if isinstance(char_iri, type) and hasattr(char_iri, "from_json"):
            return char_iri
        # Look up IRI string in the _types registry
        if isinstance(char_iri, str):
            try:
                from oold.model import _types

                return _types.get(char_iri)
            except ImportError:
                return None
        return None


class TSDCMixin(BaseController):
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


# LocalTSDCMixin and PostgrestTSDCMixin removed.
# Replaced by LocalDriver and PostgrestDriver in _drivers.py.
# Controllers use driver composition via _driver PrivateAttr.
