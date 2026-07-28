from __future__ import annotations

import uuid
from pathlib import Path

import orjson


def read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise FileNotFoundError(f"Required JSONL file is missing: {path}")
    rows: list[dict[str, object]] = []
    with path.open("rb") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            value = orjson.loads(line)
            if not isinstance(value, dict):
                raise ValueError(f"{path}:{line_number} must contain a JSON object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("wb") as handle:
        for row in rows:
            handle.write(orjson.dumps(row, option=orjson.OPT_APPEND_NEWLINE))
        handle.flush()


def write_json_atomic(
    path: Path,
    value: dict[str, object],
    *,
    sort_keys: bool = False,
) -> None:
    option = orjson.OPT_INDENT_2
    if sort_keys:
        option |= orjson.OPT_SORT_KEYS
    temporary = _temporary_sibling(path)
    try:
        temporary.write_bytes(orjson.dumps(value, option=option))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _temporary_sibling(path: Path) -> Path:
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
