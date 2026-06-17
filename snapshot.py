from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from client import AnomaliClient
from verifier import calculate_sha256, verify_sha256


TERMINAL_FAILURE_STATES = {"failed", "error", "cancelled", "canceled"}


class SnapshotService:
    def __init__(self, client: AnomaliClient, config: dict[str, Any], state, logger):
        self.client = client
        self.config = config
        self.state = state
        self.logger = logger

    def create_snapshot(self) -> dict[str, Any]:
        snapshot_cfg = self.config.get("snapshot", {})
        intel_version = int(snapshot_cfg.get("intel_version", 1))

        # Preferred config shape mirrors the Postman payload:
        # {"intel_version": 1, "custom_config": {...}}
        custom_config = snapshot_cfg.get("custom_config")
        if not isinstance(custom_config, dict):
            custom_config = {
                "filter": snapshot_cfg.get("filter", ""),
                "whitelist": snapshot_cfg.get("whitelist", {}),
            }
            for optional_key in ("format", "chunk_size", "use_org_whitelist"):
                if optional_key in snapshot_cfg:
                    custom_config[optional_key] = snapshot_cfg[optional_key]

        self.logger.info("Creating snapshot")
        self.logger.info("Snapshot custom_config: %s", {k: v for k, v in custom_config.items() if k != "api_key"})
        response = self.client.create_snapshot(intel_version=intel_version, custom_config=custom_config)
        self.state.upsert_snapshot_job(response)
        self.logger.info("Snapshot created: id=%s status=%s", response.get("id"), response.get("status"))
        return response

    def wait_for_completion(self, snapshot_id_or_resource_uri: int | str) -> dict[str, Any]:
        snapshot_cfg = self.config.get("snapshot", {})
        interval = int(snapshot_cfg.get("poll_interval", 15))
        timeout = int(snapshot_cfg.get("poll_timeout", 3600))
        deadline = time.time() + timeout

        last_response: dict[str, Any] = {}
        while True:
            response = self.client.get_snapshot(snapshot_id_or_resource_uri)
            last_response = response
            self.state.upsert_snapshot_job(response)
            status = str(response.get("status", "")).lower()
            self.logger.info("Snapshot status: id=%s status=%s", response.get("id", snapshot_id_or_resource_uri), status)

            if status == "completed":
                return response
            if status in TERMINAL_FAILURE_STATES:
                raise RuntimeError(f"Snapshot entered terminal failure state: {status}")
            if time.time() >= deadline:
                raise TimeoutError(f"Timed out waiting for snapshot after {timeout} seconds")
            time.sleep(interval)

    def download_snapshot_files(self, snapshot: dict[str, Any], output_dir: str | Path) -> list[dict[str, Any]]:
        snapshot_id = snapshot.get("id")
        if snapshot_id is None:
            raise ValueError("Snapshot response does not include an id")

        files = snapshot.get("files") or []
        if not isinstance(files, list) or not files:
            # Fall back to top-level download_url when files[] is absent.
            if snapshot.get("download_url"):
                files = [{
                    "download_url": snapshot["download_url"],
                    "sha256sum": snapshot.get("sha256sum") or snapshot.get("sha256"),
                    "total_count": snapshot.get("total_count"),
                }]
            else:
                raise ValueError("Snapshot response does not include files[] or download_url")

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        downloaded: list[dict[str, Any]] = []
        retries = int(self.config.get("download", {}).get("retries", 3))
        backoff = int(self.config.get("download", {}).get("backoff_seconds", 5))

        for index, file_info in enumerate(files):
            if not isinstance(file_info, dict):
                raise ValueError(f"Unexpected file entry at index {index}: {file_info!r}")
            url = file_info.get("download_url")
            if not url:
                raise ValueError(f"Missing download_url for snapshot file index {index}")

            filename = file_info.get("filename") or f"snapshot_{snapshot_id}_part_{index:06d}.json"
            destination = output_path / filename
            expected_sha = file_info.get("sha256sum") or file_info.get("sha256")

            self.logger.info("Downloading snapshot file %s/%s to %s", index + 1, len(files), destination)
            self.client.download_file(url, destination, retries=retries, backoff_seconds=backoff)

            actual_sha = calculate_sha256(destination)
            verified = verify_sha256(destination, expected_sha)
            if not verified:
                destination.unlink(missing_ok=True)
                self.state.upsert_snapshot_file(snapshot_id, filename, expected_sha, actual_sha, downloaded=False, verified=False, total_count=file_info.get("total_count"))
                raise RuntimeError(f"SHA256 mismatch for {destination}: expected={expected_sha} actual={actual_sha}")

            self.state.upsert_snapshot_file(snapshot_id, filename, expected_sha, actual_sha, downloaded=True, verified=True, total_count=file_info.get("total_count"))
            downloaded.append({
                "snapshot_id": snapshot_id,
                "filename": filename,
                "path": str(destination),
                "sha256sum": expected_sha,
                "actual_sha256": actual_sha,
                "verified": verified,
                "total_count": file_info.get("total_count"),
            })
            self.logger.info("Downloaded and verified: %s", filename)

        self.write_download_manifest(snapshot, downloaded, output_path)
        return downloaded

    def write_download_manifest(self, snapshot: dict[str, Any], downloaded: list[dict[str, Any]], output_dir: Path) -> Path:
        manifest = {
            "snapshot_id": snapshot.get("id"),
            "snapshot_ts": snapshot.get("snapshot_ts"),
            "status": snapshot.get("status"),
            "total_count": snapshot.get("total_count"),
            "max_update_id": snapshot.get("max_update_id"),
            "generated_ts": datetime.now(timezone.utc).isoformat(),
            "files": downloaded,
        }
        path = output_dir / f"snapshot_{snapshot.get('id')}_download_manifest.json"
        with path.open("w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
        self.logger.info("Download manifest written to: %s", path)
        return path
