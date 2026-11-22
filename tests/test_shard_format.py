"""Static shard determinism and integrity tests."""

import pytest

from facehugger.errors import IndexIntegrityError
from facehugger.shard_format import MAX_SHARD_BYTES, parse_manifest, parse_shard


def test_parse_shard_rejects_mismatched_prefix() -> None:
    """A client cannot accept a shard served for the wrong digest prefix."""
    with pytest.raises(IndexIntegrityError):
        parse_shard({"v": 1, "p": "def", "r": []}, "abc")


def test_parse_shard_accepts_legacy_records_as_ungated() -> None:
    """Older static index records remain readable with a conservative gate state."""
    records = parse_shard(
        {"v": 1, "p": "abc", "r": [["0" * 61, [["owner/model", "file.bin", "1" * 40, 1, "lfs"]]]]},
        "abc",
    )
    assert records[0][1][0].gated is False


def test_manifest_accepts_the_adaptive_four_character_partition() -> None:
    """Clients accept the deeper partition selected when a shallow shard would exceed its cap."""
    info, _, prefix_length = parse_manifest(
        {
            "schema_version": 3,
            "algorithm": "sha256",
            "index_version": "full",
            "generated_at": "2026-01-01T00:00:00Z",
            "catalog_cutoff": None,
            "complete": True,
            "prefix_hex_chars": 4,
            "record_format": "facehugger-json-shard-v3",
            "base_path": "api/v1/index/full/sha256",
        }
    )
    assert info.complete is True
    assert prefix_length == 4
    assert MAX_SHARD_BYTES == 128 * 1024
