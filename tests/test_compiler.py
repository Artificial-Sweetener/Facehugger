"""Static compilation resource and shard-size contracts."""

import json
import tracemalloc
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from facehugger.indexer.compiler import compile_site
from facehugger.models import InspectedFile, InspectedRepo, Occurrence
from facehugger.shard_format import parse_shard
from facehugger.state import IndexState


def test_compilation_memory_is_bounded_by_shards_not_the_full_index(tmp_path: Path) -> None:
    """A generated corpus larger than the memory budget compiles within that budget."""

    class GeneratedState(IndexState):
        """Generate sorted distinct hashes without storing the corpus in the fixture."""

        def iter_artifacts(self) -> Iterator[tuple[bytes, tuple[Occurrence, ...]]]:
            """Yield a spread-out corpus with independently identifiable paths."""
            for index in range(12_000):
                digest = (index * (2**256 // 12_000)).to_bytes(32, "big")
                yield (
                    digest,
                    (
                        Occurrence(
                            "owner/model", f"{index}/" + "x" * 256, "1" * 40, 1, "lfs", False
                        ),
                    ),
                )

    state = GeneratedState(tmp_path / "state.sqlite")
    tracemalloc.start()
    try:
        result = compile_site(
            state,
            tmp_path / "site",
            version="memory",
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            catalog_cutoff=None,
            complete=False,
        )
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        state.close()
    assert result["shard_count"] == 4096
    assert peak_bytes < 4 * 1024 * 1024
    assert sum(result["shard_sizes"]) > 4 * 1024 * 1024


def test_compiler_selects_deeper_shards_without_changing_records(tmp_path: Path) -> None:
    """A crowded three-character prefix splits into bounded four-character shards."""
    state = IndexState(tmp_path / "state.sqlite")
    try:
        files = tuple(
            InspectedFile(
                f"{index}/" + "x" * 512,
                1,
                None,
                bytes.fromhex(f"abc{index % 16:x}{index:060x}"),
                None,
                "lfs",
            )
            for index in range(400)
        )
        state.replace_repo(InspectedRepo("owner/model", "1" * 40, files), files)
        result = compile_site(
            state,
            tmp_path / "site",
            version="deep",
            generated_at=datetime(2026, 1, 1, tzinfo=UTC),
            catalog_cutoff=None,
            complete=False,
        )
        assert result["prefix_hex_chars"] == 4
        assert max(result["shard_sizes"]) <= 128 * 1024
    finally:
        state.close()


def test_oversized_hash_record_cannot_publish_a_manifest(tmp_path: Path) -> None:
    """An indivisible record over the shard cap fails before a manifest is written."""
    state = IndexState(tmp_path / "state.sqlite")
    try:
        files = tuple(
            InspectedFile(f"{i}/" + "x" * 1024, 1, None, bytes(32), None, "lfs") for i in range(200)
        )
        state.replace_repo(InspectedRepo("owner/model", "1" * 40, files), files)
        with pytest.raises(RuntimeError, match="maximum supported shard size"):
            compile_site(
                state,
                tmp_path / "site",
                version="large",
                generated_at=datetime(2026, 1, 1, tzinfo=UTC),
                catalog_cutoff=None,
                complete=False,
            )
        assert not (tmp_path / "site" / "api" / "v1" / "manifest.json").exists()
    finally:
        state.close()


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
