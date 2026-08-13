"""Static shard determinism and integrity tests."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from facehugger.errors import IndexIntegrityError
from facehugger.models import InspectedFile, InspectedRepo
from facehugger.shard_format import compile_site, parse_shard
from facehugger.state import IndexState


def test_compiled_shards_are_deterministic_and_include_empty_shards(tmp_path: Path) -> None:
    """Identical SQLite input creates byte-identical static output."""
    state = IndexState(tmp_path / "state.sqlite")
    try:
        digest = bytes.fromhex("abc" + "0" * 61)
        repo = InspectedRepo(
            "example/model",
            "1" * 40,
            (InspectedFile("model.safetensors", 42, "git", digest, None, "lfs"),),
            gated=True,
        )
        state.replace_repo(repo, repo.files)
        timestamp = datetime(2026, 1, 1, tzinfo=UTC)
        first = tmp_path / "first"
        second = tmp_path / "second"
        compile_site(
            state,
            first,
            version="proof",
            generated_at=timestamp,
            catalog_cutoff=None,
            complete=False,
        )
        compile_site(
            state,
            second,
            version="proof",
            generated_at=timestamp,
            catalog_cutoff=None,
            complete=False,
        )
        assert (first / "api" / "v1" / "manifest.json").read_bytes() == (
            second / "api" / "v1" / "manifest.json"
        ).read_bytes()
        parsed = parse_shard(
            json.loads(
                (first / "api" / "v1" / "index" / "proof" / "sha256" / "ab" / "c.json").read_text(
                    encoding="utf-8"
                )
            ),
            "abc",
        )
        assert parsed[0][1][0].gated is True
        assert (first / "api" / "v1" / "index" / "proof" / "sha256" / "00" / "0.json").exists()
    finally:
        state.close()


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
