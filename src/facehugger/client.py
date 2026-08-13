"""Static-index client with persistent manifest and shard caching."""

import json
from bisect import bisect_left
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
from platformdirs import user_cache_dir

from facehugger.errors import IndexIntegrityError, IndexUnavailableError
from facehugger.hashes import normalize_sha256
from facehugger.models import IndexInfo, LookupResult
from facehugger.shard_format import parse_manifest, parse_shard


class FacehuggerClient:
    """Resolve exact SHA-256 matches from a static Facehugger index."""

    def __init__(
        self,
        *,
        base_url: str,
        cache_dir: Path | None = None,
        manifest_ttl_seconds: int = 3600,
        timeout_seconds: float = 10.0,
        offline: bool = False,
    ) -> None:
        """Create a client that fetches only manifests and derived static shards."""
        if manifest_ttl_seconds < 0 or timeout_seconds <= 0:
            raise ValueError("Cache TTL must be non-negative and timeout must be positive.")
        self.base_url = base_url.rstrip("/") + "/"
        self.cache_dir = cache_dir or Path(user_cache_dir("facehugger"))
        self.manifest_ttl = timedelta(seconds=manifest_ttl_seconds)
        self.timeout_seconds = timeout_seconds
        self.offline = offline

    def lookup(self, sha256: str) -> LookupResult:
        """Look up one complete SHA-256 digest in the current static index."""
        digest = normalize_sha256(sha256)
        manifest = self._manifest()
        index, base_path, prefix_length = parse_manifest(manifest)
        prefix = digest[:prefix_length]
        shard = self._shard(index.version, base_path, prefix)
        records = parse_shard(shard, prefix)
        suffixes = tuple(record[0] for record in records)
        position = bisect_left(suffixes, digest[prefix_length:])
        matches = (
            ()
            if position == len(records) or suffixes[position] != digest[prefix_length:]
            else records[position][1]
        )
        return LookupResult("sha256", digest, index, matches)

    def index_info(self) -> IndexInfo:
        """Return metadata for the current static index without fetching a shard."""
        index, _, _ = parse_manifest(self._manifest())
        return index

    def _manifest(self) -> dict[str, object]:
        path = self.cache_dir / "manifest.json"
        if path.exists() and self._fresh(path):
            return self._read_json(path)
        if self.offline:
            if path.exists():
                return self._read_json(path)
            raise IndexUnavailableError("No cached manifest is available in offline mode.")
        return self._fetch_json("api/v1/manifest.json", path)

    def _shard(self, version: str, base_path: str, prefix: str) -> dict[str, object]:
        path = self.cache_dir / "shards" / version / prefix[:2] / f"{prefix[2]}.json"
        if path.exists():
            return self._read_json(path)
        if self.offline:
            raise IndexUnavailableError("No cached shard is available in offline mode.")
        relative_path = f"{base_path}/{prefix[:2]}/{prefix[2]}.json"
        try:
            return self._fetch_json(relative_path, path)
        except IndexUnavailableError as error:
            if "404" not in str(error):
                raise
            refreshed = self._fetch_json("api/v1/manifest.json", self.cache_dir / "manifest.json")
            _, refreshed_base_path, _ = parse_manifest(refreshed)
            return self._fetch_json(f"{refreshed_base_path}/{prefix[:2]}/{prefix[2]}.json", path)

    def _fetch_json(self, relative_path: str, cache_path: Path) -> dict[str, object]:
        try:
            response = httpx.get(
                self.base_url + relative_path.lstrip("/"), timeout=self.timeout_seconds
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise IndexUnavailableError(f"Static index request failed: {error}") from error
        try:
            data: Any = response.json()
        except json.JSONDecodeError as error:
            raise IndexIntegrityError("Static index returned invalid JSON.") from error
        if not isinstance(data, dict):
            raise IndexIntegrityError("Static index returned a non-object JSON document.")
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        return cast(dict[str, object], data)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object]:
        try:
            data: Any = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise IndexIntegrityError(f"Cached index file is invalid: {path.name}") from error
        if not isinstance(data, dict):
            raise IndexIntegrityError(f"Cached index file is invalid: {path.name}")
        return cast(dict[str, object], data)

    def _fresh(self, path: Path) -> bool:
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
        return datetime.now(UTC) - modified <= self.manifest_ttl
