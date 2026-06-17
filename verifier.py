from __future__ import annotations

import hashlib
from pathlib import Path


def calculate_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(path: str | Path, expected_sha256: str | None) -> bool:
    if not expected_sha256:
        return True
    return calculate_sha256(path).lower() == expected_sha256.lower()
