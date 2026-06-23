from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from client import AnomaliClient
from config_loader import load_config
from logging_setup import setup_logging
from merger import MergeEngine
from snapshot import SnapshotService
from state import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="anomali_snapshot",
        description="Anomali Snapshot API, merge, dedupe, and reporting utility",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    merge = subparsers.add_parser("merge", help="Merge and dedupe Anomali snapshot JSON files")
    add_common_file_args(merge)
    add_state_mode_args(merge)

    test_auth = subparsers.add_parser("test-auth", help="Perform a lightweight authenticated GET against the snapshot endpoint")
    add_common_api_args(test_auth)

    create = subparsers.add_parser("snapshot-create", help="Create a Snapshot API job")
    add_common_api_args(create)

    wait = subparsers.add_parser("snapshot-wait", help="Poll a Snapshot API job until completed")
    add_common_api_args(wait)
    wait.add_argument("--snapshot-id", required=True, help="Snapshot ID or resource_uri to poll")

    download = subparsers.add_parser("snapshot-download", help="Download and verify files for a completed snapshot")
    add_common_api_args(download)
    download.add_argument("--snapshot-id", required=True, help="Snapshot ID or resource_uri to fetch before download")
    download.add_argument("--download-dir", default="downloads/raw", help="Directory for downloaded snapshot files")

    sync = subparsers.add_parser("sync", help="Create, wait, download, verify, and merge")
    add_common_file_args(sync)
    add_state_mode_args(sync)
    sync.add_argument("--download-dir", default="downloads/raw", help="Base directory for downloaded snapshot files. sync writes to a snapshot_<id> subfolder.")

    return parser


def add_common_file_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input-dir", default="downloads/raw", help="Directory containing snapshot .json files")
    parser.add_argument("--output-dir", default="output", help="Directory for merged output and reports")
    parser.add_argument("--config", default="config/settings.json", help="Path to settings JSON")
    parser.add_argument("--state-db", default="state.db", help="SQLite state database path")
    parser.add_argument("--log-dir", default="logs", help="Directory for logs")


def add_common_api_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default="config/settings.json", help="Path to settings JSON")
    parser.add_argument("--state-db", default="state.db", help="SQLite state database path")
    parser.add_argument("--log-dir", default="logs", help="Directory for logs")


def add_state_mode_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--fresh", action="store_true", help="Reset state and generated output while preserving logs and raw downloads")
    parser.add_argument("--resume", action="store_true", help="Reuse existing state and continue a previous run")
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="Deprecated alias for --fresh; kept for backwards compatibility",
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_remove_contents(path: Path, *, protected: set[Path]) -> None:
    """Remove the contents of a directory without deleting the directory itself.

    This is intentionally conservative for Windows, where folders may be
    temporarily locked by Explorer, antivirus, editors, or open file handles.
    """
    resolved = path.resolve()
    for protected_path in protected:
        protected_resolved = protected_path.resolve()
        if resolved == protected_resolved or _is_relative_to(protected_resolved, resolved):
            raise RuntimeError(f"Refusing to clean protected path: {path}")

    path.mkdir(parents=True, exist_ok=True)

    for item in path.iterdir():
        try:
            if item.is_file() or item.is_symlink():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
        except PermissionError as exc:
            raise PermissionError(
                f"Could not remove locked path during --fresh cleanup: {item}. "
                "Close Explorer/VS Code/terminals using that folder, or remove it manually."
            ) from exc


def _safe_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except PermissionError as exc:
        raise PermissionError(
            f"Could not remove locked file during --fresh cleanup: {path}. "
            "Close any process using it, then retry."
        ) from exc


def _prepare_run(args: argparse.Namespace) -> None:
    if getattr(args, "fresh", False) and getattr(args, "resume", False):
        raise RuntimeError("Use either --fresh or --resume, not both")

    if getattr(args, "reset_state", False):
        args.fresh = True

    state_path = Path(args.state_db)
    output_path = Path(getattr(args, "output_dir", "output"))
    log_path = Path(args.log_dir)
    input_path = Path(getattr(args, "input_dir", "downloads/raw"))
    download_path = Path(getattr(args, "download_dir", input_path))

    if getattr(args, "fresh", False):
        # --fresh resets processing state and generated outputs only.
        # It deliberately preserves logs/ and downloads/raw/ because those
        # directories are commonly locked on Windows and raw downloads may be
        # expensive to recreate. Sync writes new API downloads into a
        # snapshot-specific subfolder, so stale raw files are not merged.
        protected = {
            Path.cwd().resolve(),
            input_path.resolve(),
            download_path.resolve(),
            log_path.resolve(),
            (Path("downloads") / "raw").resolve(),
            Path("config").resolve(),
        }

        _safe_unlink(state_path)
        _safe_remove_contents(output_path, protected=protected)

        # Keep these directories present, but do not delete their contents.
        log_path.mkdir(parents=True, exist_ok=True)
        (Path("downloads") / "raw").mkdir(parents=True, exist_ok=True)

        # Optional generated scratch directories may be safely cleared.
        _safe_remove_contents(Path("downloads") / "temp", protected=protected)
        _safe_remove_contents(Path("downloads") / "verified", protected=protected)
        return

    if state_path.exists() and not getattr(args, "resume", False):
        raise RuntimeError(
            "Existing state detected. Use --fresh to start a clean run, "
            "or --resume to continue the previous run."
        )


def _build_snapshot_service(config: dict, state: StateStore, logger) -> SnapshotService:
    api_cfg = config.get("api", {})
    client = AnomaliClient(
        base_url=api_cfg.get("base_url") or api_cfg.get("url", ""),
        username=api_cfg.get("username") or api_cfg.get("user", ""),
        api_key=api_cfg.get("api_key") or api_cfg.get("apikey", ""),
        timeout=int(api_cfg.get("timeout", 60)),
    )
    return SnapshotService(client=client, config=config, state=state, logger=logger)


def _run_merge(args: argparse.Namespace, config: dict, state: StateStore, logger, mode: str) -> dict:
    logger.info("Mode: %s", mode.upper())
    engine = MergeEngine(config=config, state=state, logger=logger)
    manifest = engine.merge_directory(
        input_dir=Path(args.input_dir),
        output_dir=Path(args.output_dir),
        config_file=Path(args.config),
        mode=mode,
    )
    logger.info("Manifest written to: %s", manifest.get("manifest_output"))
    return manifest


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        if args.command in {"merge", "sync"}:
            _prepare_run(args)

        logger = setup_logging(args.log_dir)
        config = load_config(args.config)
        state = StateStore(args.state_db)
        try:
            if args.command == "merge":
                mode = "fresh" if args.fresh else "resume" if args.resume else "new"
                _run_merge(args, config, state, logger, mode)

            elif args.command == "test-auth":
                service = _build_snapshot_service(config, state, logger)
                response = service.client.test_auth()
                print("Authenticated GET succeeded")
                print(f"Top-level response keys: {', '.join(sorted(response.keys()))}")

            elif args.command == "snapshot-create":
                service = _build_snapshot_service(config, state, logger)
                snapshot = service.create_snapshot()
                print(f"Snapshot ID: {snapshot.get('id')}")
                print(f"Status: {snapshot.get('status')}")

            elif args.command == "snapshot-wait":
                service = _build_snapshot_service(config, state, logger)
                snapshot = service.wait_for_completion(args.snapshot_id)
                print(f"Snapshot ID: {snapshot.get('id')}")
                print(f"Status: {snapshot.get('status')}")
                print(f"Files: {len(snapshot.get('files') or [])}")

            elif args.command == "snapshot-download":
                service = _build_snapshot_service(config, state, logger)
                snapshot = service.wait_for_completion(args.snapshot_id)
                files = service.download_snapshot_files(snapshot, args.download_dir)
                print(f"Downloaded files: {len(files)}")

            elif args.command == "sync":
                mode = "fresh" if args.fresh else "resume" if args.resume else "new"
                service = _build_snapshot_service(config, state, logger)
                snapshot = service.create_snapshot()
                snapshot = service.wait_for_completion(snapshot.get("resource_uri") or snapshot.get("id"))

                snapshot_id = snapshot.get("id") or "unknown"
                effective_download_dir = Path(args.download_dir) / f"snapshot_{snapshot_id}"
                logger.info("Sync download directory: %s", effective_download_dir)

                service.download_snapshot_files(snapshot, effective_download_dir)
                args.input_dir = str(effective_download_dir)
                _run_merge(args, config, state, logger, mode)
        finally:
            state.close()
    except Exception as exc:
        try:
            logger.error("Command failed: %s", exc)  # type: ignore[name-defined]
        except Exception:
            pass
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
