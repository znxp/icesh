from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable


class OutputWriter:
    def __init__(self, output_dir: str | Path, config: dict[str, Any]):
        self.output_dir = Path(output_dir)
        self.config = config
        (self.output_dir / "merged").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "duplicates").mkdir(parents=True, exist_ok=True)
        (self.output_dir / "reports").mkdir(parents=True, exist_ok=True)

    def write_merged(self, records: Iterable[dict[str, Any]]) -> Path:
        output_format = self.config.get("output", {}).get("format", "jsonl")
        if output_format == "jsonl":
            return self._write_jsonl(self.output_dir / "merged" / "merged_indicators.jsonl", records)
        if output_format == "json":
            return self._write_json_array(self.output_dir / "merged" / "merged_indicators.json", records)
        if output_format == "csv":
            return self._write_csv(self.output_dir / "merged" / "merged_indicators.csv", records)
        raise ValueError(f"Unsupported output format: {output_format}")

    def write_duplicates(self, duplicates: Iterable[dict[str, Any]]) -> Path:
        path = self.output_dir / "duplicates" / "duplicate_relationships.jsonl"
        return self._write_jsonl(path, duplicates)

    def write_duplicate_summary(self, summary_rows: Iterable[Any]) -> Path:
        path = self.output_dir / "reports" / "duplicate_summary.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["dedupe_key", "duplicate_count"])
            for row in summary_rows:
                writer.writerow([row["dedupe_key"], row["duplicate_count"]])
        return path

    def write_manifest(self, manifest: dict[str, Any]) -> Path:
        path = self.output_dir / "reports" / "merge_manifest.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
        return path

    def _write_jsonl(self, path: Path, records: Iterable[dict[str, Any]]) -> Path:
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(project_fields(record, self.config), ensure_ascii=False, separators=(",", ":")))
                handle.write("\n")
        return path

    def _write_json_array(self, path: Path, records: Iterable[dict[str, Any]]) -> Path:
        with path.open("w", encoding="utf-8") as handle:
            handle.write("[\n")
            first = True
            for record in records:
                if not first:
                    handle.write(",\n")
                handle.write(json.dumps(project_fields(record, self.config), ensure_ascii=False))
                first = False
            handle.write("\n]\n")
        return path

    def _write_csv(self, path: Path, records: Iterable[dict[str, Any]]) -> Path:
        configured_fields = self.config.get("output", {}).get("fields", [])
        fields = configured_fields or [
            "id", "update_id", "type", "itype", "value", "feed_id",
            "confidence", "status", "created_ts", "modified_ts", "expiration_ts"
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            for record in records:
                writer.writerow(flatten_for_csv(project_fields(record, self.config), fields))
        return path


def project_fields(record: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    fields = config.get("output", {}).get("fields", [])
    if not fields:
        return record
    return {field: record.get(field) for field in fields}


def flatten_for_csv(record: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    row = {}
    for field in fields:
        value = record.get(field)
        if isinstance(value, (dict, list)):
            row[field] = json.dumps(value, ensure_ascii=False)
        else:
            row[field] = value
    return row
