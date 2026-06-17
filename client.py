from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


class AnomaliApiError(RuntimeError):
    pass


class AnomaliClient:
    """Small stdlib-only client for the Anomali Snapshot API."""

    def __init__(self, base_url: str, username: str, api_key: str, timeout: int = 60):
        if not base_url:
            raise ValueError("api.base_url is required")
        self.base_url = base_url.rstrip("/") + "/"
        self.username = username
        self.api_key = api_key
        self.timeout = timeout

    def create_snapshot(
        self,
        intel_version: int,
        filter_query: str | None = None,
        whitelist: dict[str, Any] | None = None,
        custom_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a snapshot job.

        Prefer passing custom_config when the tenant supports/uses fields such as:
        filter, whitelist, format, chunk_size, and use_org_whitelist.
        The filter_query/whitelist arguments are retained for backwards compatibility.
        """
        if custom_config is None:
            custom_config = {
                "filter": filter_query or "",
                "whitelist": whitelist or {},
            }
        else:
            # Copy so callers do not get their config mutated.
            custom_config = dict(custom_config)
            if filter_query and "filter" not in custom_config:
                custom_config["filter"] = filter_query
            if whitelist is not None and "whitelist" not in custom_config:
                custom_config["whitelist"] = whitelist

        payload = {
            "intel_version": intel_version,
            "custom_config": custom_config,
        }
        return self._json_request("POST", self.base_url, payload=payload)

    def test_auth(self) -> dict[str, Any]:
        """Perform a lightweight authenticated GET against the snapshot endpoint."""
        return self._json_request("GET", self.base_url)

    def get_snapshot(self, snapshot_id_or_resource_uri: int | str) -> dict[str, Any]:
        url = self._snapshot_url(snapshot_id_or_resource_uri)
        return self._json_request("GET", url)

    def download_file(self, url: str, destination: str | Path, retries: int = 3, backoff_seconds: int = 5) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = destination.with_suffix(destination.suffix + ".part")

        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                request = urllib.request.Request(url=url, method="GET")
                with urllib.request.urlopen(request, timeout=self.timeout) as response, tmp_path.open("wb") as handle:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        handle.write(chunk)
                tmp_path.replace(destination)
                return destination
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if tmp_path.exists():
                    tmp_path.unlink(missing_ok=True)
                if attempt < retries:
                    time.sleep(backoff_seconds * attempt)
        raise AnomaliApiError(f"Failed to download {url}: {last_error}")

    def _json_request(self, method: str, url: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"apikey {self.username}:{self.api_key}",
        }
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = urllib.request.Request(url=url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                if not raw.strip():
                    return {}
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise AnomaliApiError(f"Expected JSON object from {url}, got {type(data).__name__}")
                return data
        except urllib.error.HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise AnomaliApiError(f"HTTP {exc.code} calling {url}: {error_body}") from exc
        except urllib.error.URLError as exc:
            raise AnomaliApiError(f"URL error calling {url}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise AnomaliApiError(f"Invalid JSON response from {url}: {exc}") from exc

    def _snapshot_url(self, snapshot_id_or_resource_uri: int | str) -> str:
        value = str(snapshot_id_or_resource_uri)
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if value.startswith("/"):
            parsed = urllib.parse.urlparse(self.base_url)
            return f"{parsed.scheme}://{parsed.netloc}{value}"
        if value.endswith("/") and "/" in value:
            return urllib.parse.urljoin(self.base_url, value)
        return urllib.parse.urljoin(self.base_url, f"{value}/")
