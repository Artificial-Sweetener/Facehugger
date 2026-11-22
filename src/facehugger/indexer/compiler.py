"""Bounded-memory compilation of verified SQLite metadata into static shards."""

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path
from typing import Any

from facehugger.models import Occurrence
from facehugger.shard_format import (
    MAX_PREFIX_HEX_CHARS,
    MAX_SHARD_BYTES,
    MIN_PREFIX_HEX_CHARS,
    RECORD_FORMAT,
    SCHEMA_VERSION,
)
from facehugger.state import IndexState


def compile_site(
    state: IndexState,
    destination: Path,
    *,
    version: str,
    generated_at: datetime,
    catalog_cutoff: datetime | None,
    complete: bool,
) -> dict[str, Any]:
    """Compile one deterministic versioned site from a verified SQLite state."""
    state.validate()
    destination.mkdir(parents=True, exist_ok=True)
    root = destination / "api" / "v1"
    index_root = root / "index" / version / "sha256"
    prefix_length = _prefix_length(state)
    shard_sizes: list[int] = []
    empty_shards = 0
    for prefix, values in _shards(state, prefix_length):
        shard = {"v": SCHEMA_VERSION, "p": prefix, "r": values}
        encoded = _canonical_json(shard)
        shard_path = index_root / prefix[:2] / f"{prefix[2:]}.json"
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard_path.write_bytes(encoded)
        shard_sizes.append(len(encoded))
        empty_shards += int(not values)
    counts = state.counts()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "sha256",
        "index_version": version,
        "generated_at": generated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "catalog_cutoff": None
        if catalog_cutoff is None
        else catalog_cutoff.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "complete": complete,
        "prefix_hex_chars": prefix_length,
        "record_format": RECORD_FORMAT,
        "base_path": f"api/v1/index/{version}/sha256",
        "counts": counts,
        "coverage": _coverage(state),
        "manifest_sha256": "",
    }
    manifest["manifest_sha256"] = hashlib.sha256(_canonical_json(manifest)).hexdigest()
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_bytes(_canonical_json(manifest))
    (destination / "index.html").write_text(_index_html(), encoding="utf-8")
    return {
        "manifest": manifest,
        "shard_count": len(shard_sizes),
        "empty_shard_count": empty_shards,
        "shard_sizes": shard_sizes,
        "prefix_hex_chars": prefix_length,
    }


def _coverage(state: IndexState) -> dict[str, float | None]:
    with_hash = state.counts()["occurrences"]
    without_hash = state.counts()["candidate_without_sha256"]
    total = with_hash + without_hash
    return {
        "candidate_files_with_sha256": None if not total else with_hash / total,
        "candidate_bytes_with_sha256": None,
    }


def _occurrence_record(occurrence: Occurrence) -> list[str | int | bool | None]:
    return [
        occurrence.repo_id,
        occurrence.path,
        occurrence.revision,
        occurrence.size,
        occurrence.storage,
        occurrence.gated,
    ]


def _canonical_json(value: object) -> bytes:
    """Encode deterministic UTF-8 protocol JSON."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _prefix_length(state: IndexState) -> int:
    """Measure bounded prefix counters without retaining the entire index."""
    empty_size = len(
        _canonical_json({"v": SCHEMA_VERSION, "p": "0" * MIN_PREFIX_HEX_CHARS, "r": []})
    )
    sizes = [empty_size] * (16**MIN_PREFIX_HEX_CHARS)
    counts = [0] * (16**MIN_PREFIX_HEX_CHARS)
    for digest, occurrences in state.iter_artifacts():
        prefix = digest.hex()[:MIN_PREFIX_HEX_CHARS]
        index = int(prefix, 16)
        record = [digest.hex()[MIN_PREFIX_HEX_CHARS:], [_occurrence_record(o) for o in occurrences]]
        sizes[index] += len(_canonical_json(record)) + int(counts[index] > 0)
        counts[index] += 1
        if sizes[index] > MAX_SHARD_BYTES:
            return MAX_PREFIX_HEX_CHARS
    return MIN_PREFIX_HEX_CHARS


def _shards(state: IndexState, prefix_length: int) -> Iterator[tuple[str, list[list[Any]]]]:
    """Yield one bounded shard at a time, including all empty prefixes."""
    groups = groupby(state.iter_artifacts(), key=lambda item: item[0].hex()[:prefix_length])
    current = next(groups, None)
    for index in range(16**prefix_length):
        prefix = f"{index:0{prefix_length}x}"
        records: list[list[Any]] = []
        size = len(_canonical_json({"v": SCHEMA_VERSION, "p": prefix, "r": []}))
        if current is not None and current[0] == prefix:
            for digest, occurrences in current[1]:
                record = [
                    digest.hex()[prefix_length:],
                    [_occurrence_record(o) for o in occurrences],
                ]
                size += len(_canonical_json(record)) + int(bool(records))
                if size > MAX_SHARD_BYTES:
                    raise RuntimeError("Static index exceeds the maximum supported shard size.")
                records.append(record)
            current = next(groups, None)
        yield prefix, records


def _index_html() -> str:
    """Return the static index landing page."""
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Facehugger</title>'
        "<body><h1>Facehugger</h1></body></html>\n"
    )
