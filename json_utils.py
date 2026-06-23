from __future__ import annotations

from decimal import Decimal
from typing import Any
import json


def make_json_safe(value: Any) -> Any:
    """Recursively convert values returned by streaming parsers into JSON-safe types.

    ijson may return Decimal for numeric values. The standard json module cannot
    serialize Decimal directly, so convert integral Decimals to int and fractional
    Decimals to float before records are stored or written.
    """
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        return float(value)

    if isinstance(value, dict):
        return {str(key): make_json_safe(item) for key, item in value.items()}

    if isinstance(value, list):
        return [make_json_safe(item) for item in value]

    if isinstance(value, tuple):
        return [make_json_safe(item) for item in value]

    return value


def json_dumps(value: Any, **kwargs: Any) -> str:
    """json.dumps wrapper that safely handles Decimal and nested parser values."""
    return json.dumps(make_json_safe(value), **kwargs)
