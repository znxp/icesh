from __future__ import annotations

from pathlib import Path
from typing import Iterator, Any

from json_utils import make_json_safe

try:
    import ijson
except ImportError as exc:  # pragma: no cover
    ijson = None
    IJSON_IMPORT_ERROR = exc
else:
    IJSON_IMPORT_ERROR = None


def iter_snapshot_records(file_path: str | Path) -> Iterator[dict[str, Any]]:
    if ijson is None:
        raise RuntimeError(
            "ijson is required for streaming JSON array parsing. Install with: pip install ijson"
        ) from IJSON_IMPORT_ERROR

    path = Path(file_path)
    with path.open("rb") as handle:
        for item in ijson.items(handle, "item"):
            if isinstance(item, dict):
                yield make_json_safe(item)
            else:
                raise ValueError(f"Expected JSON object inside array in {path}, got {type(item).__name__}")
