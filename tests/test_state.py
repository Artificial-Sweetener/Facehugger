"""SQLite reverse-index replacement and duplicate tests."""

import sqlite3
from pathlib import Path
from typing import Literal, cast

import pytest

from facehugger.models import InspectedFile, InspectedRepo
from facehugger.state import IndexState

_DIGEST = bytes.fromhex("ab" * 32)


def test_state_returns_duplicate_occurrences_and_replaces_a_repo(tmp_path: Path) -> None:
    """A changed repository replaces only its own current occurrences."""
    state = IndexState(tmp_path / "state.sqlite")
    try:
        first = InspectedRepo(
            "one/model",
            "1" * 40,
            (InspectedFile("model.safetensors", 10, "git-1", _DIGEST, None, "lfs"),),
            gated=True,
        )
        second = InspectedRepo(
            "two/model",
            "2" * 40,
            (InspectedFile("copy.safetensors", 10, "git-2", _DIGEST, None, "lfs"),),
        )
        state.replace_repo(first, first.files)
        state.replace_repo(second, second.files)
        assert [item.repo_id for item in state.lookup(_DIGEST)] == ["one/model", "two/model"]
        assert state.lookup(_DIGEST)[0].gated is True

        replacement = InspectedRepo("one/model", "3" * 40, ())
        state.replace_repo(replacement, replacement.files)
        assert [item.repo_id for item in state.lookup(_DIGEST)] == ["two/model"]
        state.validate()
    finally:
        state.close()


def test_failed_repository_replacement_preserves_the_previous_verified_state(
    tmp_path: Path,
) -> None:
    """A failed inspection write cannot partially replace a repository's index rows."""
    state = IndexState(tmp_path / "state.sqlite")
    try:
        original = InspectedRepo(
            "example/model",
            "1" * 40,
            (InspectedFile("model.safetensors", 10, None, _DIGEST, None, "lfs"),),
        )
        state.replace_repo(original, original.files)
        invalid = InspectedRepo(
            "example/model",
            "2" * 40,
            (InspectedFile("replacement.safetensors", 10, None, _DIGEST, None, "lfs"),),
        )
        with pytest.raises(sqlite3.IntegrityError):
            state.replace_repo(
                invalid,
                (
                    InspectedFile(
                        "bad.safetensors",
                        10,
                        None,
                        _DIGEST,
                        None,
                        cast(Literal["lfs", "xet", "git", "unknown"], None),
                    ),
                ),
            )
        matches = state.lookup(_DIGEST)
        assert matches[0].path == "model.safetensors"
        assert matches[0].revision == "1" * 40
        state.validate()
    finally:
        state.close()
