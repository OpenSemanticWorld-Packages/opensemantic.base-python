import asyncio
import os
import tempfile

import pytest

from opensemantic.base import (
    LocalTimeSeriesDatabaseController,
    TimeSeriesDatabaseController,
)
from opensemantic.base._controller_logic import (
    build_sqlite_read_query,
    check_buffer_duplicates,
    make_osw_id,
    parse_sqlite_rows,
)
from opensemantic.base.v1 import LocalTimeSeriesDatabaseController as LocalTSDC_v1
from opensemantic.base.v1 import TimeSeriesDatabaseController as TSDC_v1
from opensemantic.core._model import Label
from opensemantic.core.v1._model import Label as Label_v1

# -- Controller logic tests --


def test_make_osw_id():
    assert make_osw_id("12345678-1234-1234-1234-123456789abc") == (
        "OSW12345678123412341234123456789abc"
    )


def test_build_sqlite_read_query_basic():
    query, params = build_sqlite_read_query(tool_osw_id="OSW_test")
    assert "FROM OSW_test" in query
    assert "ORDER BY ts ASC" in query
    assert params == []


def test_build_sqlite_read_query_with_filters():
    query, params = build_sqlite_read_query(
        tool_osw_id="OSW_test",
        channel_osw_id="OSW_ch1",
        limit=10,
        filters=[{"column": "data->>'value'", "operator": "gt", "criteria": 5}],
    )
    assert "ch = ?" in query
    assert "LIMIT ?" in query
    assert "data->>'value' > ?" in query
    assert "OSW_ch1" in params
    assert 10 in params
    assert 5 in params


def test_parse_sqlite_rows():
    raw = [(1, "2024-01-01T00:00:00", "ch1", '{"value": 42}')]
    result = parse_sqlite_rows(raw)
    assert len(result) == 1
    assert result[0]["id"] == 1
    assert result[0]["data"]["value"] == 42


def test_check_buffer_duplicates_none():
    buf = {"tool1": [{"ts": "t1", "ch": "c1", "data": {"v": 1}}]}
    assert check_buffer_duplicates(buf) == {}


def test_check_buffer_duplicates_found():
    row = {"ts": "t1", "ch": "c1", "data": {"v": 1}}
    buf = {"tool1": [row, row]}
    dupes = check_buffer_duplicates(buf)
    assert "tool1" in dupes
    assert len(dupes["tool1"]) == 1


# -- LocalTimeSeriesDatabaseController tests --


@pytest.fixture
def sqlite_db():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    db = LocalTimeSeriesDatabaseController(
        name="test_db",
        label=[Label(text="Test DB", lang="en")],
        db_path=path,
    )
    yield db
    os.unlink(path)


def test_local_db_is_database_subclass(sqlite_db):
    from opensemantic.base._model import Database

    assert isinstance(sqlite_db, Database)


def test_local_db_create_and_list_tools(sqlite_db):
    async def _test():
        await sqlite_db.create_tool(
            TimeSeriesDatabaseController.CreateToolParams(tool_osw_id="OSW_t1")
        )
        tools = await sqlite_db.get_tools_list()
        assert "OSW_t1" in tools

    asyncio.run(_test())


def test_local_db_write_and_read(sqlite_db):
    async def _test():
        await sqlite_db.create_tool(
            TimeSeriesDatabaseController.CreateToolParams(tool_osw_id="OSW_rw")
        )
        await sqlite_db.write_tool_channel_raw(
            TimeSeriesDatabaseController.WriteToolChannelRawParams(
                tool_osw_id="OSW_rw",
                data=[
                    {"ts": "2024-01-01T00:00:00", "ch": "ch1", "data": {"value": 42.0}},
                    {"ts": "2024-01-01T00:01:00", "ch": "ch2", "data": {"value": 99.0}},
                ],
            )
        )
        rows = await sqlite_db.read_tool_channel_raw(
            TimeSeriesDatabaseController.ReadToolChannelRawParams(tool_osw_id="OSW_rw")
        )
        assert len(rows) == 2
        assert rows[0]["data"]["value"] == 42.0
        assert rows[1]["data"]["value"] == 99.0

    asyncio.run(_test())


def test_local_db_read_with_channel_filter(sqlite_db):
    async def _test():
        await sqlite_db.create_tool(
            TimeSeriesDatabaseController.CreateToolParams(tool_osw_id="OSW_cf")
        )
        await sqlite_db.write_tool_channel_raw(
            TimeSeriesDatabaseController.WriteToolChannelRawParams(
                tool_osw_id="OSW_cf",
                data=[
                    {"ts": "2024-01-01T00:00:00", "ch": "ch_a", "data": {"v": 1}},
                    {"ts": "2024-01-01T00:00:01", "ch": "ch_b", "data": {"v": 2}},
                ],
            )
        )
        rows = await sqlite_db.read_tool_channel_raw(
            TimeSeriesDatabaseController.ReadToolChannelRawParams(
                tool_osw_id="OSW_cf",
                channel_osw_id="ch_a",
            )
        )
        assert len(rows) == 1
        assert rows[0]["ch"] == "ch_a"

    asyncio.run(_test())


def test_local_db_delete_tool(sqlite_db):
    async def _test():
        await sqlite_db.create_tool(
            TimeSeriesDatabaseController.CreateToolParams(tool_osw_id="OSW_del")
        )
        assert "OSW_del" in await sqlite_db.get_tools_list()
        await sqlite_db.delete_tool(
            TimeSeriesDatabaseController.DeleteToolParams(tool_osw_id="OSW_del")
        )
        assert "OSW_del" not in await sqlite_db.get_tools_list()

    asyncio.run(_test())


def test_local_db_store_data(sqlite_db):
    async def _test():
        from datetime import datetime

        await sqlite_db.create_tool(
            TimeSeriesDatabaseController.CreateToolParams(tool_osw_id="OSW_sd")
        )
        await sqlite_db.store_data(
            TimeSeriesDatabaseController.StoreDataParams(
                tool_osw_id="OSW_sd",
                rows=[
                    TimeSeriesDatabaseController.DataRow(
                        ts=datetime(2024, 1, 1), ch="ch1", data={"v": 10}
                    )
                ],
            )
        )
        rows = await sqlite_db.read_tool_channel_raw(
            TimeSeriesDatabaseController.ReadToolChannelRawParams(tool_osw_id="OSW_sd")
        )
        assert len(rows) == 1

    asyncio.run(_test())


def test_local_db_get_table_size(sqlite_db):
    async def _test():
        await sqlite_db.create_tool(
            TimeSeriesDatabaseController.CreateToolParams(tool_osw_id="OSW_sz")
        )
        await sqlite_db.write_tool_channel_raw(
            TimeSeriesDatabaseController.WriteToolChannelRawParams(
                tool_osw_id="OSW_sz",
                data=[
                    {"ts": "2024-01-01T00:00:00", "ch": "c", "data": {"v": 1}},
                    {"ts": "2024-01-01T00:00:01", "ch": "c", "data": {"v": 2}},
                ],
            )
        )
        size = await sqlite_db.get_table_size("OSW_sz")
        assert size == 2

    asyncio.run(_test())


# -- v1 controller tests --


def test_v1_tsdc_subclasses_v1_database():
    from opensemantic.base.v1._model import Database as Database_v1

    assert issubclass(TSDC_v1, Database_v1)


def test_v1_tsdc_not_subclass_of_v2_database():
    from opensemantic.base._model import Database as Database_v2

    assert not issubclass(TSDC_v1, Database_v2)


def test_v2_tsdc_subclasses_v2_database():
    from opensemantic.base._model import Database as Database_v2

    assert issubclass(TimeSeriesDatabaseController, Database_v2)


def test_v2_tsdc_not_subclass_of_v1_database():
    from opensemantic.base.v1._model import Database as Database_v1

    assert not issubclass(TimeSeriesDatabaseController, Database_v1)


@pytest.fixture
def sqlite_db_v1():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = f.name
    db = LocalTSDC_v1(
        name="test_db_v1",
        label=[Label_v1(text="Test DB v1", lang="en")],
        db_path=path,
    )
    yield db
    os.unlink(path)


def test_v1_local_db_is_v1_database_subclass(sqlite_db_v1):
    from opensemantic.base.v1._model import Database as Database_v1

    assert isinstance(sqlite_db_v1, Database_v1)


def test_v1_local_db_crud(sqlite_db_v1):
    async def _test():
        await sqlite_db_v1.create_tool(TSDC_v1.CreateToolParams(tool_osw_id="OSW_v1t"))
        tools = await sqlite_db_v1.get_tools_list()
        assert "OSW_v1t" in tools

        await sqlite_db_v1.write_tool_channel_raw(
            TSDC_v1.WriteToolChannelRawParams(
                tool_osw_id="OSW_v1t",
                data=[{"ts": "2024-01-01T00:00:00", "ch": "c1", "data": {"v": 7}}],
            )
        )
        rows = await sqlite_db_v1.read_tool_channel_raw(
            TSDC_v1.ReadToolChannelRawParams(tool_osw_id="OSW_v1t")
        )
        assert len(rows) == 1
        assert rows[0]["data"]["v"] == 7

        await sqlite_db_v1.delete_tool(TSDC_v1.DeleteToolParams(tool_osw_id="OSW_v1t"))
        assert "OSW_v1t" not in await sqlite_db_v1.get_tools_list()

    asyncio.run(_test())
