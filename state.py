from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS dedupe_index (
                dedupe_key TEXT PRIMARY KEY,
                winner_record_id TEXT,
                winner_update_id INTEGER,
                winner_modified_ts TEXT,
                winner_confidence INTEGER,
                winner_json TEXT NOT NULL,
                source_file TEXT,
                source_record_number INTEGER
            );

            CREATE TABLE IF NOT EXISTS duplicate_relationships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedupe_key TEXT NOT NULL,
                winner_record_id TEXT,
                duplicate_record_id TEXT,
                reason TEXT NOT NULL,
                winner_source_file TEXT,
                duplicate_source_file TEXT,
                winner_record_number INTEGER,
                duplicate_record_number INTEGER,
                duplicate_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processing_files (
                filename TEXT PRIMARY KEY,
                parsed INTEGER DEFAULT 0,
                record_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS run_metadata (
                run_id TEXT PRIMARY KEY,
                started_ts TEXT NOT NULL,
                completed_ts TEXT,
                mode TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshot_jobs (
                snapshot_id INTEGER PRIMARY KEY,
                snapshot_ts TEXT,
                status TEXT,
                total_count INTEGER,
                max_update_id INTEGER,
                resource_uri TEXT,
                raw_json TEXT NOT NULL,
                created_or_seen_ts TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS snapshot_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_id INTEGER,
                filename TEXT,
                sha256sum TEXT,
                actual_sha256 TEXT,
                downloaded INTEGER DEFAULT 0,
                verified INTEGER DEFAULT 0,
                total_count INTEGER,
                UNIQUE(snapshot_id, filename)
            );

            CREATE TABLE IF NOT EXISTS sync_state (
                name TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_ts TEXT DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        self.conn.commit()

    def reset(self) -> None:
        self.conn.executescript(
            """
            DELETE FROM dedupe_index;
            DELETE FROM duplicate_relationships;
            DELETE FROM processing_files;
            DELETE FROM run_metadata;
            DELETE FROM snapshot_jobs;
            DELETE FROM snapshot_files;
            """
        )
        self.conn.commit()



    def upsert_snapshot_job(self, snapshot: dict[str, Any]) -> None:
        snapshot_id = snapshot.get("id")
        if snapshot_id is None:
            return
        max_update_id = _safe_int(snapshot.get("max_update_id"))
        self.conn.execute(
            """
            INSERT INTO snapshot_jobs (
                snapshot_id, snapshot_ts, status, total_count, max_update_id, resource_uri, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO UPDATE SET
                snapshot_ts = excluded.snapshot_ts,
                status = excluded.status,
                total_count = excluded.total_count,
                max_update_id = excluded.max_update_id,
                resource_uri = excluded.resource_uri,
                raw_json = excluded.raw_json
            """,
            (
                _safe_int(snapshot_id),
                str(snapshot.get("snapshot_ts", "")),
                str(snapshot.get("status", "")),
                _safe_int(snapshot.get("total_count")),
                max_update_id,
                str(snapshot.get("resource_uri", "")),
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        if max_update_id is not None and str(snapshot.get("status", "")).lower() == "completed":
            self.set_sync_value("last_successful_max_update_id", str(max_update_id))
        self.conn.commit()

    def get_snapshot_job(self, snapshot_id: int | str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM snapshot_jobs WHERE snapshot_id = ?", (_safe_int(snapshot_id),)
        ).fetchone()
        return dict(row) if row else None

    def latest_snapshot_job(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM snapshot_jobs ORDER BY created_or_seen_ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def upsert_snapshot_file(
        self,
        snapshot_id: int | str,
        filename: str,
        sha256sum: str | None,
        actual_sha256: str | None,
        downloaded: bool,
        verified: bool,
        total_count: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO snapshot_files (
                snapshot_id, filename, sha256sum, actual_sha256, downloaded, verified, total_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id, filename) DO UPDATE SET
                sha256sum = excluded.sha256sum,
                actual_sha256 = excluded.actual_sha256,
                downloaded = excluded.downloaded,
                verified = excluded.verified,
                total_count = excluded.total_count
            """,
            (
                _safe_int(snapshot_id),
                filename,
                sha256sum,
                actual_sha256,
                1 if downloaded else 0,
                1 if verified else 0,
                _safe_int(total_count),
            ),
        )
        self.conn.commit()

    def set_sync_value(self, name: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO sync_state (name, value) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_ts = CURRENT_TIMESTAMP
            """,
            (name, value),
        )

    def get_sync_value(self, name: str) -> str | None:
        row = self.conn.execute("SELECT value FROM sync_state WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def start_run(self, run_id: str, started_ts: str, mode: str) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO run_metadata (run_id, started_ts, completed_ts, mode)
            VALUES (?, ?, NULL, ?)
            """,
            (run_id, started_ts, mode),
        )
        self.conn.commit()

    def complete_run(self, run_id: str, completed_ts: str) -> None:
        self.conn.execute(
            "UPDATE run_metadata SET completed_ts = ? WHERE run_id = ?",
            (completed_ts, run_id),
        )
        self.conn.commit()

    def latest_run(self) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM run_metadata ORDER BY started_ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def get_winner(self, dedupe_key: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM dedupe_index WHERE dedupe_key = ?", (dedupe_key,)
        ).fetchone()
        return dict(row) if row else None

    def insert_winner(self, dedupe_key: str, record: dict[str, Any], source_file: str, record_number: int) -> None:
        self.conn.execute(
            """
            INSERT INTO dedupe_index (
                dedupe_key, winner_record_id, winner_update_id, winner_modified_ts,
                winner_confidence, winner_json, source_file, source_record_number
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dedupe_key,
                str(record.get("id", "")),
                _safe_int(record.get("update_id")),
                str(record.get("modified_ts", "")),
                _safe_int(record.get("confidence")),
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                source_file,
                record_number,
            ),
        )

    def update_winner(self, dedupe_key: str, record: dict[str, Any], source_file: str, record_number: int) -> None:
        self.conn.execute(
            """
            UPDATE dedupe_index
            SET winner_record_id = ?, winner_update_id = ?, winner_modified_ts = ?,
                winner_confidence = ?, winner_json = ?, source_file = ?, source_record_number = ?
            WHERE dedupe_key = ?
            """,
            (
                str(record.get("id", "")),
                _safe_int(record.get("update_id")),
                str(record.get("modified_ts", "")),
                _safe_int(record.get("confidence")),
                json.dumps(record, ensure_ascii=False, separators=(",", ":")),
                source_file,
                record_number,
                dedupe_key,
            ),
        )

    def add_duplicate(
        self,
        dedupe_key: str,
        winner_record_id: str,
        duplicate_record_id: str,
        reason: str,
        winner_source_file: str,
        duplicate_source_file: str,
        winner_record_number: int,
        duplicate_record_number: int,
        duplicate_record: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO duplicate_relationships (
                dedupe_key, winner_record_id, duplicate_record_id, reason,
                winner_source_file, duplicate_source_file, winner_record_number,
                duplicate_record_number, duplicate_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dedupe_key,
                winner_record_id,
                duplicate_record_id,
                reason,
                winner_source_file,
                duplicate_source_file,
                winner_record_number,
                duplicate_record_number,
                json.dumps(duplicate_record, ensure_ascii=False, separators=(",", ":")),
            ),
        )

    def mark_file_parsed(self, filename: str, record_count: int, error_count: int) -> None:
        self.conn.execute(
            """
            INSERT INTO processing_files (filename, parsed, record_count, error_count)
            VALUES (?, 1, ?, ?)
            ON CONFLICT(filename) DO UPDATE SET
                parsed = excluded.parsed,
                record_count = excluded.record_count,
                error_count = excluded.error_count
            """,
            (filename, record_count, error_count),
        )

    def commit(self) -> None:
        self.conn.commit()

    def iter_winners(self):
        for row in self.conn.execute("SELECT dedupe_key, winner_json FROM dedupe_index ORDER BY dedupe_key"):
            record = json.loads(row["winner_json"])
            if isinstance(record, dict):
                record.setdefault("_dedupe_key", row["dedupe_key"])
            yield record

    def iter_duplicates(self):
        query = """
        SELECT dedupe_key, winner_record_id, duplicate_record_id, reason,
               winner_source_file, duplicate_source_file,
               winner_record_number, duplicate_record_number, duplicate_json
        FROM duplicate_relationships
        ORDER BY dedupe_key, id
        """
        for row in self.conn.execute(query):
            data = dict(row)
            data["duplicate_record"] = json.loads(data.pop("duplicate_json"))
            yield data

    def duplicate_summary(self):
        query = """
        SELECT dedupe_key, COUNT(*) AS duplicate_count
        FROM duplicate_relationships
        GROUP BY dedupe_key
        ORDER BY duplicate_count DESC, dedupe_key
        """
        yield from self.conn.execute(query)

    def stats(self) -> dict[str, int]:
        winners = self.conn.execute("SELECT COUNT(*) AS c FROM dedupe_index").fetchone()["c"]
        duplicates = self.conn.execute("SELECT COUNT(*) AS c FROM duplicate_relationships").fetchone()["c"]
        return {"unique_records": winners, "duplicate_records": duplicates}


def _safe_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
