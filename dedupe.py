from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def normalize_value(value: Any, rule: str | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if rule == "lowercase":
        return text.lower()
    if rule == "uppercase":
        return text.upper()
    return text


def build_dedupe_key(record: dict[str, Any], config: dict[str, Any]) -> str:
    """Build the configured dedupe key.

    Supported modes:
      - threatstream_id: only records with the same ThreatStream object ID match
      - operational: records match on itype + value
      - feed_aware: records match on itype + value + feed_id
      - custom: records match on configured key_fields

    The previous Phase 1 behavior was effectively operational mode.
    """
    dedupe_cfg = config.get("dedupe", {})
    mode = str(dedupe_cfg.get("mode", "custom")).strip().lower()
    normalize_rules = dedupe_cfg.get("normalize", {})

    if mode == "threatstream_id":
        identifier_field = dedupe_cfg.get("identifier_field", "id")
        return normalize_value(record.get(identifier_field), normalize_rules.get(identifier_field))

    if mode == "operational":
        key_fields = ["itype", "value"]
    elif mode == "feed_aware":
        key_fields = ["itype", "value", "feed_id"]
    elif mode == "custom":
        key_fields = dedupe_cfg.get("key_fields", ["itype", "value"])
    else:
        raise ValueError(
            f"Unsupported dedupe.mode: {mode}. "
            "Use threatstream_id, operational, feed_aware, or custom."
        )

    parts = []
    for field in key_fields:
        value = record.get(field)
        rule = normalize_rules.get(field)
        parts.append(normalize_value(value, rule))
    return "|".join(parts)


def choose_winner(
    existing_record: dict[str, Any],
    candidate_record: dict[str, Any],
    config: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """
    Returns (decision, winner). decision is one of:
    - candidate_wins
    - existing_wins
    """
    strategies = config.get("dedupe", {}).get("winner_strategy", [])

    for strategy in strategies:
        try:
            direction, field = strategy.split(":", 1)
        except ValueError:
            continue

        existing_value = existing_record.get(field)
        candidate_value = candidate_record.get(field)
        comparison = compare_values(existing_value, candidate_value, direction)

        if comparison < 0:
            return "candidate_wins", candidate_record
        if comparison > 0:
            return "existing_wins", existing_record

    return "existing_wins", existing_record


def compare_values(existing_value: Any, candidate_value: Any, direction: str) -> int:
    """
    Returns:
      1 if existing is preferred
     -1 if candidate is preferred
      0 if tied/unknown
    """
    if direction == "highest":
        existing_num = to_number(existing_value)
        candidate_num = to_number(candidate_value)
        if existing_num is None and candidate_num is None:
            return 0
        if existing_num is None:
            return -1
        if candidate_num is None:
            return 1
        return (existing_num > candidate_num) - (existing_num < candidate_num)

    if direction == "newest":
        existing_dt = to_datetime(existing_value)
        candidate_dt = to_datetime(candidate_value)
        if existing_dt is None and candidate_dt is None:
            return 0
        if existing_dt is None:
            return -1
        if candidate_dt is None:
            return 1
        return (existing_dt > candidate_dt) - (existing_dt < candidate_dt)

    return 0


def to_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def merge_context(winner: dict[str, Any], duplicate: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Attach duplicate metadata and optionally merge context fields.

    For threatstream_id mode the default is to keep the winning/latest object
    as-is and only increment _duplicate_count. This avoids reintroducing stale
    tags or fields that may have been removed from a newer version of the same
    ThreatStream object.

    For operational/feed-aware/custom modes the default is to merge tags and
    source/feed context because those modes intentionally collapse multiple
    objects into a single view.
    """
    dedupe_cfg = config.get("dedupe", {})
    mode = str(dedupe_cfg.get("mode", "custom")).strip().lower()

    merge_duplicate_context = dedupe_cfg.get("merge_duplicate_context")
    if merge_duplicate_context is None:
        merge_duplicate_context = mode != "threatstream_id"

    merged = dict(winner)

    if merge_duplicate_context:
        if dedupe_cfg.get("merge_tags", True):
            merged["tags"] = merge_list_values(winner.get("tags"), duplicate.get("tags"))

        if dedupe_cfg.get("merge_sources", True):
            sources = []
            for field in ("source", "source_name", "feed_id"):
                if winner.get(field) not in (None, ""):
                    sources.append(str(winner.get(field)))
                if duplicate.get(field) not in (None, ""):
                    sources.append(str(duplicate.get(field)))
            if sources:
                merged["_merged_sources"] = sorted(set(sources))

    merged["_duplicate_count"] = int(winner.get("_duplicate_count", 0)) + 1
    return merged


def merge_list_values(a: Any, b: Any) -> list[Any]:
    values = []
    for item in [a, b]:
        if isinstance(item, list):
            values.extend(item)
        elif item not in (None, ""):
            values.append(item)

    seen = set()
    result = []
    for value in values:
        marker = repr(value)
        if marker not in seen:
            seen.add(marker)
            result.append(value)
    return result
