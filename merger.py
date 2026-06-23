from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dedupe import build_dedupe_key, choose_winner, merge_context
from parser import iter_snapshot_records
from state import StateStore
from writer import OutputWriter


class MergeEngine:
    def __init__(self, config: dict[str, Any], state: StateStore, logger):
        self.config = config
        self.state = state
        self.logger = logger

    def merge_directory(
        self,
        input_dir: str | Path,
        output_dir: str | Path,
        config_file: str | Path,
        mode: str,
    ) -> dict[str, Any]:
        input_path = Path(input_dir)
        if not input_path.exists():
            raise FileNotFoundError(f"Input directory does not exist: {input_path}")

        run_started = datetime.now(timezone.utc)
        timestamp = run_started.strftime("%Y%m%d_%H%M%S_%f")
        snapshot_suffix = input_path.name if input_path.name.startswith("snapshot_") else ""
        run_id = f"run_{timestamp}_{snapshot_suffix}" if snapshot_suffix else f"run_{timestamp}"
        self.logger.info("Run ID: %s", run_id)
        self.state.start_run(run_id, run_started.isoformat(), mode)

        files = sorted(input_path.glob("*.json"))
        if not files:
            raise FileNotFoundError(f"No .json files found in {input_path}")

        run_output_dir = Path(output_dir) / "runs" / run_id

        manifest = {
            "run_id": run_id,
            "mode": mode,
            "fresh_run": mode == "fresh",
            "resume_run": mode == "resume",
            "started_ts": run_started.isoformat(),
            "input_dir": str(input_path),
            "input_files": [file_path.name for file_path in files],
            "output_dir": str(run_output_dir),
            "config_file": str(config_file),
            "files": [],
            "config": self.config,
        }

        total_records = 0
        total_errors = 0

        for file_path in files:
            file_result = self._process_file(file_path)
            manifest["files"].append(file_result)
            total_records += file_result["records_processed"]
            total_errors += file_result["errors"]
            self.state.commit()

        writer = OutputWriter(run_output_dir, self.config)
        merged_path = writer.write_merged(self.state.iter_winners())
        manifest["merged_output"] = str(merged_path)

        if self.config.get("output", {}).get("write_duplicates", True):
            duplicates_path = writer.write_duplicates(self.state.iter_duplicates())
            manifest["duplicates_output"] = str(duplicates_path)

        if self.config.get("output", {}).get("write_duplicate_summary", True):
            summary_path = writer.write_duplicate_summary(self.state.duplicate_summary())
            manifest["duplicate_summary_output"] = str(summary_path)

        stats = self.state.stats()
        manifest.update(stats)
        manifest["records_processed"] = total_records
        manifest["errors"] = total_errors
        completed_ts = datetime.now(timezone.utc).isoformat()
        manifest["completed_ts"] = completed_ts
        self.state.complete_run(run_id, completed_ts)
        manifest_path = writer.write_manifest(manifest)
        manifest["manifest_output"] = str(manifest_path)

        self.logger.info(
            "Merge complete: processed=%s unique=%s duplicates=%s errors=%s",
            total_records,
            stats["unique_records"],
            stats["duplicate_records"],
            total_errors,
        )
        return manifest

    def _process_file(self, file_path: Path) -> dict[str, Any]:
        self.logger.info("Processing file: %s", file_path)
        processed = 0
        errors = 0

        for record_number, record in enumerate(iter_snapshot_records(file_path), start=1):
            try:
                self._process_record(record, file_path.name, record_number)
                processed += 1
                if processed % 10000 == 0:
                    self.logger.info("Processed %s records from %s", processed, file_path.name)
                    self.state.commit()
            except Exception as exc:  # keep processing subsequent records
                errors += 1
                self.logger.error(
                    "Failed to process record file=%s record_number=%s error=%s",
                    file_path.name,
                    record_number,
                    exc,
                )

        self.state.mark_file_parsed(file_path.name, processed, errors)
        self.logger.info("Finished file: %s records=%s errors=%s", file_path.name, processed, errors)
        return {"file": file_path.name, "records_processed": processed, "errors": errors}

    def _process_record(self, record: dict[str, Any], source_file: str, record_number: int) -> None:
        if self.config.get("output", {}).get("include_provenance", True):
            record = dict(record)
            record["_snapshot_metadata"] = {
                "source_file": source_file,
                "source_record_number": record_number,
                "processed_ts": datetime.now(timezone.utc).isoformat(),
            }

        dedupe_key = build_dedupe_key(record, self.config)
        if not dedupe_key or dedupe_key.replace("|", "") == "":
            raise ValueError("Dedupe key is empty; check key_fields and record shape")

        existing = self.state.get_winner(dedupe_key)
        if existing is None:
            self.state.insert_winner(dedupe_key, record, source_file, record_number)
            return

        existing_record = json.loads(existing["winner_json"])
        decision, winner = choose_winner(existing_record, record, self.config)

        if decision == "candidate_wins":
            # Existing winner is now a duplicate of the new candidate.
            merged_candidate = merge_context(record, existing_record, self.config)
            self.state.add_duplicate(
                dedupe_key=dedupe_key,
                winner_record_id=str(record.get("id", "")),
                duplicate_record_id=str(existing_record.get("id", "")),
                reason="candidate_replaced_existing_winner",
                winner_source_file=source_file,
                duplicate_source_file=str(existing.get("source_file", "")),
                winner_record_number=record_number,
                duplicate_record_number=int(existing.get("source_record_number") or 0),
                duplicate_record=existing_record,
            )
            self.state.update_winner(dedupe_key, merged_candidate, source_file, record_number)
        else:
            merged_existing = merge_context(existing_record, record, self.config)
            self.state.add_duplicate(
                dedupe_key=dedupe_key,
                winner_record_id=str(existing_record.get("id", "")),
                duplicate_record_id=str(record.get("id", "")),
                reason="existing_winner_retained",
                winner_source_file=str(existing.get("source_file", "")),
                duplicate_source_file=source_file,
                winner_record_number=int(existing.get("source_record_number") or 0),
                duplicate_record_number=record_number,
                duplicate_record=record,
            )
            self.state.update_winner(dedupe_key, merged_existing, str(existing.get("source_file", "")), int(existing.get("source_record_number") or 0))
