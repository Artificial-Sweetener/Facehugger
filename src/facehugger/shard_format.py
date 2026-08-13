"""Static JSON shard compilation and protocol validation."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from facehugger.errors import IndexIntegrityError
from facehugger.models import IndexInfo, Occurrence
from facehugger.state import IndexState

SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, SCHEMA_VERSION})
PREFIX_HEX_CHARS = 3
RECORD_FORMAT = "facehugger-json-shard-v2"


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
    records: dict[str, list[list[Any]]] = {
        f"{value:03x}": [] for value in range(16**PREFIX_HEX_CHARS)
    }
    for digest_bytes, occurrences in state.iter_artifacts():
        digest = digest_bytes.hex()
        prefix = digest[:PREFIX_HEX_CHARS]
        suffix = digest[PREFIX_HEX_CHARS:]
        records[prefix].append([suffix, [_occurrence_record(item) for item in occurrences]])
    shard_sizes: list[int] = []
    empty_shards = 0
    for prefix, values in records.items():
        values.sort(key=lambda item: str(item[0]))
        shard = {"v": SCHEMA_VERSION, "p": prefix, "r": values}
        encoded = _canonical_json(shard)
        shard_path = index_root / prefix[:2] / f"{prefix[2]}.json"
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
        "prefix_hex_chars": PREFIX_HEX_CHARS,
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
    }


def parse_manifest(data: object) -> tuple[IndexInfo, str, int]:
    """Validate a manifest and return client-ready index metadata."""
    manifest = _mapping(data, "Manifest must be an object.")
    _require(
        manifest.get("schema_version") in SUPPORTED_SCHEMA_VERSIONS,
        "Unsupported manifest schema version.",
    )
    _require(manifest.get("algorithm") == "sha256", "Unsupported manifest algorithm.")
    version = _string(manifest.get("index_version"), "Manifest index version is invalid.")
    base_path = _string(manifest.get("base_path"), "Manifest base path is invalid.")
    prefix_length = manifest.get("prefix_hex_chars")
    if prefix_length != PREFIX_HEX_CHARS:
        raise IndexIntegrityError("Unsupported manifest shard prefix length.")
    generated_at = _parse_datetime(
        manifest.get("generated_at"), "Manifest generated timestamp is invalid."
    )
    cutoff_value = manifest.get("catalog_cutoff")
    cutoff = (
        None
        if cutoff_value is None
        else _parse_datetime(cutoff_value, "Manifest cutoff is invalid.")
    )
    complete = manifest.get("complete")
    if not isinstance(complete, bool):
        raise IndexIntegrityError("Manifest completeness is invalid.")
    return IndexInfo(version, generated_at, cutoff, complete), base_path, PREFIX_HEX_CHARS


def parse_shard(data: object, prefix: str) -> tuple[tuple[str, tuple[Occurrence, ...]], ...]:
    """Validate one shard and return its strictly sorted lookup records."""
    shard = _mapping(data, "Shard must be an object.")
    schema_version = shard.get("v")
    _require(schema_version in SUPPORTED_SCHEMA_VERSIONS, "Unsupported shard schema version.")
    _require(shard.get("p") == prefix, "Shard prefix does not match the requested prefix.")
    raw_records = shard.get("r")
    if not isinstance(raw_records, list):
        raise IndexIntegrityError("Shard records must be a list.")
    parsed: list[tuple[str, tuple[Occurrence, ...]]] = []
    previous_suffix = ""
    for raw_record in cast(list[object], raw_records):
        if not isinstance(raw_record, list):
            raise IndexIntegrityError("Shard record is invalid.")
        record = cast(list[object], raw_record)
        if len(record) != 2:
            raise IndexIntegrityError("Shard record is invalid.")
        suffix = _string(record[0], "Shard suffix is invalid.")
        _require(
            len(suffix) == 64 - PREFIX_HEX_CHARS
            and all(char in "0123456789abcdef" for char in suffix),
            "Shard suffix is invalid.",
        )
        _require(
            not previous_suffix or suffix > previous_suffix,
            "Shard suffixes are not strictly sorted.",
        )
        previous_suffix = suffix
        raw_occurrences = record[1]
        if not isinstance(raw_occurrences, list):
            raise IndexIntegrityError("Shard occurrences must be a list.")
        occurrences = tuple(
            _parse_occurrence(item, schema_version) for item in cast(list[object], raw_occurrences)
        )
        _require(
            tuple(sorted(occurrences, key=_occurrence_key)) == occurrences,
            "Shard occurrences are not sorted.",
        )
        parsed.append((suffix, occurrences))
    return tuple(parsed)


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


def _parse_occurrence(value: object, schema_version: object) -> Occurrence:
    if not isinstance(value, list):
        raise IndexIntegrityError("Shard occurrence is invalid.")
    value = cast(list[object], value)
    expected_length = 5 if schema_version == 1 else 6
    if len(value) != expected_length:
        raise IndexIntegrityError("Shard occurrence is invalid.")
    raw_size = value[3]
    if raw_size is None:
        size = None
    elif isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0:
        size = raw_size
    else:
        raise IndexIntegrityError("Shard size is invalid.")
    gated = False if schema_version == 1 else value[5]
    if not isinstance(gated, bool):
        raise IndexIntegrityError("Shard gate state is invalid.")
    return Occurrence(
        repo_id=_string(value[0], "Shard repository identifier is invalid."),
        path=_string(value[1], "Shard path is invalid."),
        revision=_string(value[2], "Shard revision is invalid."),
        size=size,
        storage=_string(value[4], "Shard storage type is invalid."),
        gated=gated,
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _index_html() -> str:
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8"><title>Facehugger</title>'
        "<body><h1>Facehugger</h1></body></html>\n"
    )


def _mapping(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise IndexIntegrityError(message)
    mapping = cast(dict[object, object], value)
    if not all(isinstance(key, str) for key in mapping):
        raise IndexIntegrityError(message)
    return cast(dict[str, object], mapping)


def _string(value: object, message: str) -> str:
    if not isinstance(value, str) or not value:
        raise IndexIntegrityError(message)
    return value


def _parse_datetime(value: object, message: str) -> datetime:
    raw_value = _string(value, message)
    try:
        parsed = datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise IndexIntegrityError(message) from error
    _require(parsed.tzinfo is not None, message)
    return parsed


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise IndexIntegrityError(message)


def _occurrence_key(value: Occurrence) -> tuple[str, str, str]:
    return value.repo_id, value.path, value.revision
