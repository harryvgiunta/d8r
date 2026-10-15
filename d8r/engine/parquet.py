"""Explicit, read-only local Parquet sources on an in-memory DuckDB connection."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import ibis

from .datasources import DataSource


def add_parquet_source(path: str, display: str = "") -> DataSource:
    """Build a lazy single-file source; discover metadata without scanning rows.

    The fixed ``data`` relation keeps queries independent of filename punctuation.
    Only workspace metadata is persisted by callers, never the file's contents.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("An existing local .parquet or .pq file is required.")
    path = path.strip()
    drive = PureWindowsPath(path).drive
    local = path[2:] if len(drive) == 2 and drive[1] == ":" else path
    if (path.startswith(("//", "\\\\")) or ":" in local
            or any(ord(char) < 32 or char in "*?[]" for char in path)):
        raise ValueError("Use one local file path, not a URL, network path or wildcard.")
    candidate = Path(path).expanduser()
    if candidate.suffix.lower() not in {".parquet", ".pq"}:
        raise ValueError("Select a .parquet or .pq file.")
    try:
        file = candidate.resolve(strict=True)
        if (str(file).startswith(("//", "\\\\")) or not file.is_file()
                or any(ord(char) < 32 or char in "*?[]" for char in str(file))):
            raise ValueError("Select an existing local Parquet file, not a directory, network path or wildcard.")
    except OSError:
        raise ValueError("The Parquet file is missing or inaccessible. Check its local path and permissions.") from None
    key = "parquet:" + os.path.normcase(str(file))
    con = ibis.duckdb.connect()
    try:
        table = con.read_parquet(str(file), table_name="data")
        table.schema()  # Footer/schema metadata only; never execute or count rows.
        return DataSource(
            id=key, connection_key=key, display=display.strip() or f"Parquet · {file.name}",
            doc=f"Local Parquet · {file.name}", kind="parquet", dialect="duckdb", dir=file,
            con=con, datasets={"data": {"table": table, "doc": file.name, "rows": None}},
        )
    except Exception:
        con.disconnect()
        raise ValueError("Could not read Parquet metadata. Check the file format and read permissions.") from None
