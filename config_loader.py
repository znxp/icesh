from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "dedupe": {
        "key_fields": ["itype", "value"],
        "normalize": {"itype": "lowercase", "value": "lowercase"},
        "winner_strategy": ["highest:update_id", "newest:modified_ts", "highest:confidence"],
        "merge_tags": True,
        "merge_sources": True,
    },
    "output": {
        "format": "jsonl",
        "fields": [],
        "write_duplicates": True,
        "write_duplicate_summary": True,
        "include_provenance": True,
    },
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        user_config = json.load(handle)

    config = deep_merge(DEFAULT_CONFIG, user_config)
    validate_config(config)
    return config


def validate_config(config: dict[str, Any]) -> None:
    output_format = config.get("output", {}).get("format")
    if output_format not in {"jsonl", "json", "csv"}:
        raise ValueError("output.format must be one of: jsonl, json, csv")

    key_fields = config.get("dedupe", {}).get("key_fields")
    if not key_fields or not isinstance(key_fields, list):
        raise ValueError("dedupe.key_fields must be a non-empty list")
