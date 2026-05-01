"""Pure helper functions shared by v1 and v2 controller implementations.

No Pydantic imports here - only stdlib types.
"""

import json
from typing import Any, Dict, List, Optional, Tuple

# Mapping from filter operator names to SQL operators
FILTER_OP_MAP: Dict[str, str] = {
    "eq": "=",
    "neq": "!=",
    "lt": "<",
    "lte": "<=",
    "gt": ">",
    "gte": ">=",
}


def build_sqlite_read_query(
    tool_osw_id: str,
    channel_osw_id: Optional[str] = None,
    start: Optional[Any] = None,
    end: Optional[Any] = None,
    filters: Optional[List[Dict[str, Any]]] = None,
    limit: Optional[int] = None,
) -> Tuple[str, list]:
    """Build a SQLite SELECT query for reading time series data.

    Args:
        tool_osw_id: Table name (OSW ID of the tool).
        channel_osw_id: Optional channel filter.
        start: Start timestamp filter.
        end: End timestamp filter.
        filters: List of dicts with 'column', 'operator', 'criteria' keys.
        limit: Max rows to return.

    Returns:
        Tuple of (query_string, query_params).
    """
    query = (
        f"SELECT id, strftime('%Y-%m-%dT%H:%M:%f+00:00', ts, 'subsec'), ch, data "
        f"FROM {tool_osw_id} WHERE 1=1"
    )
    query_params: list = []

    if channel_osw_id:
        query += " AND ch = ?"
        query_params.append(channel_osw_id)
    if start:
        query += " AND ts >= datetime(?,'subsec')"
        query_params.append(start)
    if end:
        query += " AND ts <= datetime(?,'subsec')"
        query_params.append(end)
    if filters:
        for f in filters:
            column = f["column"]
            op = f["operator"]
            criteria = f["criteria"]
            if op in FILTER_OP_MAP:
                sql_operator = FILTER_OP_MAP[op]
                query += f" AND {column} {sql_operator} ?"
                query_params.append(criteria)

    query += " ORDER BY ts ASC"
    if limit:
        query += " LIMIT ?"
        query_params.append(limit)

    return query, query_params


def parse_sqlite_rows(rows: List[tuple]) -> List[Dict[str, Any]]:
    """Parse raw SQLite rows into dicts with id, ts, ch, data keys."""
    result = []
    for row in rows:
        result.append(
            {
                "id": row[0],
                "ts": row[1],
                "ch": row[2],
                "data": json.loads(row[3]),
            }
        )
    return result


def check_buffer_duplicates(
    buffer: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, List[Dict[str, Any]]]:
    """Check for duplicate entries in a buffer dict keyed by tool_osw_id.

    Returns a dict of tool_osw_id to list of duplicate rows.
    """
    duplicates: Dict[str, List[Dict[str, Any]]] = {}
    for tool_osw_id, data in buffer.items():
        seen: set = set()
        tool_dupes: List[Dict[str, Any]] = []
        for row in data:
            identifier = (row["ts"], row["ch"], json.dumps(row["data"], sort_keys=True))
            if identifier in seen:
                tool_dupes.append(row)
            else:
                seen.add(identifier)
        if tool_dupes:
            duplicates[tool_osw_id] = tool_dupes
    return duplicates


def make_osw_id(uuid_str: str) -> str:
    """Convert a UUID string to an OSW ID (no dashes, prefixed with OSW)."""
    return "OSW" + str(uuid_str).replace("-", "")
