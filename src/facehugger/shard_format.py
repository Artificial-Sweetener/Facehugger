"""Static lookup protocol schemas and validation."""

from datetime import datetime
from typing import cast

from facehugger.errors import IndexIntegrityError
from facehugger.models import IndexInfo, Occurrence

SCHEMA_VERSION = 3
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, SCHEMA_VERSION})
MIN_PREFIX_HEX_CHARS = 3
MAX_PREFIX_HEX_CHARS = 4
MAX_SHARD_BYTES = 128 * 1024
RECORD_FORMAT = "facehugger-json-shard-v3"


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
    if (
        not isinstance(prefix_length, int)
        or isinstance(prefix_length, bool)
        or prefix_length not in range(MIN_PREFIX_HEX_CHARS, MAX_PREFIX_HEX_CHARS + 1)
    ):
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
    return IndexInfo(version, generated_at, cutoff, complete), base_path, prefix_length


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
            len(suffix) == 64 - len(prefix) and all(char in "0123456789abcdef" for char in suffix),
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
